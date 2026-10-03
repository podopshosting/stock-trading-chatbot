"""
The replay loop.

Drives the real pipeline over historical bars: signals, hypothesis,
risk, execution, position management, journal. Every one of those is
the same module that runs live. The replay supplies a clock and a
point-in-time data view; it does not supply a second implementation of
any decision.

That constraint is what makes the output worth anything. A backtest
written as its own simplified model of the strategy measures the model,
not the strategy, and the two diverge in exactly the places that matter.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional, Sequence

from ..broker.paper import PaperBrokerConfig
from ..hypothesis import generate as generate_hypothesis
from ..journal import InMemoryJournal, describe, record_closed_position
from ..observability import log_event
from ..positions import (
    ExitContext, ExitPlan, PositionManager, StopMechanism,
)
from ..risk import RiskContext, RiskLimits, evaluate as evaluate_risk
from ..signals.engine import evaluate as evaluate_signals
from ..signals.models import DataFreshness
from .broker import ReplayBroker
from .clock import LookaheadError, ReplayClock
from .data import Bar, PointInTimeEvidence, PointInTimeSeries

CONFIG_VERSION = "replay-v1.0.0"

# Indicators need history before they mean anything. Deciding on bar 3
# of a 200-bar moving average is not a strategy decision, it is a
# division by a number that happens to exist.
DEFAULT_WARMUP_BARS = 200


@dataclass
class ReplayConfig:
    """How a run is set up. Recorded with the result."""
    session_date: str = ""
    warmup_bars: int = DEFAULT_WARMUP_BARS
    starting_cash: float = 100.0
    slippage_bps: float = 5.0
    partial_fill_probability: float = 0.0
    seed: Optional[int] = 42
    risk_limits: RiskLimits = field(default_factory=RiskLimits)
    # Where the exit distances come from.
    #
    # "DECISION" is the FAITHFUL mode: the live cycle reads
    # `decision.stop_distance_pct` - derived from realised volatility,
    # 1% to 8% - and sets target = 2x stop and trailing = stop from it.
    # A replay that hardcodes 3/6/3 is simulating a DIFFERENT STRATEGY,
    # and every R-multiple it produces describes that other strategy.
    #
    # "FIXED" is nonetheless the DEFAULT, and deliberately so.
    #
    # Making DECISION the default was tried and reverted. It changed
    # which bars hit which level, which changed trade outcomes, which
    # broke three scenarios calibrated against the 3% stop -
    # DAILY_LOSS_LOCK_TEST, GAP_RISK_TEST, and the gap-through-the-stop
    # case. Those scenarios are not wrong; they encode expectations
    # about a known configuration. Changing the default would have
    # silently reinterpreted every committed expectation and every
    # previously reported figure, which is precisely what freezing the
    # strategy for external validation is meant to prevent.
    #
    # So the faithful mode must be ASKED FOR, the two are comparable on
    # identical data, and the divergence is a measurement rather than a
    # migration. A future switch of the default belongs with a
    # recalibration of the scenarios and a new cohort, not here.
    stop_source: str = "FIXED"
    # Used when stop_source is FIXED, and as the fallback when DECISION
    # is asked for and the decision carries no distance. The fallback is
    # counted, never silent: a run that fell back for most of its trades
    # is a FIXED run wearing a DECISION label.
    stop_distance_pct: float = 3.0
    target_distance_pct: float = 6.0
    trailing_stop_pct: Optional[float] = 3.0
    # Live couples the target to the stop at this multiple.
    target_stop_multiple: float = 2.0
    max_hold_bars: Optional[int] = None
    evaluation_interval_seconds: int = 60
    # The bar interval, used as the fallback data age when a gap cannot
    # be measured. Not inferred from the bars, because a series with a
    # halt in it would infer the wrong interval from the hole.
    bar_interval_seconds: float = 60.0
    # How competing candidates are ordered when they contend for one
    # day's capital.
    #
    # ALPHABETICAL is deterministic and declared. It is NOT clever: with
    # three symbols and room for two, the first two alphabetically win.
    # Ranking by signal strength would be better allocation and it would
    # CHANGE WHICH TRADES HAPPEN, which is a strategy change - and the
    # strategy is frozen for external paper validation. So ranking is a
    # future experiment with its own cohort, not a quiet improvement
    # smuggled in through the backtester.
    #
    # Before this, order came from the bars dict, so the same dataset
    # passed in a different order was a different experiment.
    candidate_order: str = "ALPHABETICAL"
    # Minutes to the close, as reported to the risk gate. A replay has
    # no session calendar, so this is declared rather than derived -
    # and it is a CONFIG field rather than a literal so that a scenario
    # can make TOO_LATE_IN_SESSION fire, which it previously could not.
    minutes_to_close: float = 120.0

    def as_dict(self) -> Dict:
        return {
            "config_version": CONFIG_VERSION,
            "session_date": self.session_date,
            "warmup_bars": self.warmup_bars,
            "starting_cash": self.starting_cash,
            "slippage_bps": self.slippage_bps,
            "partial_fill_probability": self.partial_fill_probability,
            "seed": self.seed,
            "stop_source": self.stop_source,
            "stop_distance_pct": self.stop_distance_pct,
            "target_stop_multiple": self.target_stop_multiple,
            "target_distance_pct": self.target_distance_pct,
            "trailing_stop_pct": self.trailing_stop_pct,
            "max_hold_bars": self.max_hold_bars,
            "bar_interval_seconds": self.bar_interval_seconds,
            "minutes_to_close": self.minutes_to_close,
            "candidate_order": self.candidate_order,
            "risk_limits_version": self.risk_limits.version,
        }


@dataclass
class ReplayResult:
    """What a run produced, and what it is not entitled to claim."""
    config: Dict
    bars_processed: int
    decisions_evaluated: int
    entries_attempted: int
    entries_filled: int
    exits_filled: int
    unfillable_orders: int
    positions_open_at_end: int
    rejections: Dict[str, int]
    performance: Dict
    lookahead_detected: bool = False
    lookahead_detail: str = ""
    warnings: List[str] = field(default_factory=list)
    # What this ran against. A result that cannot name its dataset is
    # not an experiment, because "the last 250 days" means something
    # different every day.
    dataset_id: Optional[str] = None
    dataset_checksum: Optional[str] = None
    config_name: str = "ad-hoc"
    config_deployable: bool = True
    # Bars where candidates COMPETED for one day's capital, and what
    # each was told. "Why A and not B" is otherwise unanswerable from
    # aggregate refusal counts.
    allocations: List[Dict] = field(default_factory=list)
    # WHERE THE MARKET REGIME CAME FROM.
    #
    # The strategy is long-only and regime-gated, so the regime decides
    # whether a candidate is ever considered at all. A replay that
    # injects a permanently BULLISH, permanently OPEN regime has
    # REMOVED that gate, and its numbers describe the strategy with its
    # market filter disabled - a different and far more permissive
    # system than the deployed one.
    #
    # This field exists because it did not. Historical replays were run
    # with an injected permissive regime and the result carried no
    # trace of it, so a breach rate could be quoted as measured "on
    # real market data" while being conditioned on permission that was
    # manufactured. The bars were real; the licence to trade them was
    # not.
    #
    # NONE means no regime callable was supplied. Under NONE the
    # strategy refuses every candidate, so such a run is a control -
    # evidence that the gate works - and never a performance result.
    regime_source: str = "NONE"
    regime_detail: Optional[str] = None

    @property
    def regime_is_synthetic(self) -> bool:
        """True unless the regime was genuinely observed.

        Derived rather than stored, and derived to be TRUE by default:
        anything that is not explicitly OBSERVED is synthetic. A new
        regime source added later is therefore treated as synthetic
        until someone states otherwise, which is the direction of error
        that cannot overstate a result.
        """
        return self.regime_source != "OBSERVED"

    @property
    def valid(self) -> bool:
        """Derived. A run that peeked is void, not merely suspect.

        There is no setter, so a run cannot be marked valid after the
        fact by code that would prefer a result.
        """
        return not self.lookahead_detected

    def as_dict(self) -> Dict:
        return {
            "valid": self.valid,
            "lookahead_detected": self.lookahead_detected,
            "lookahead_detail": self.lookahead_detail,
            "dataset_id": self.dataset_id,
            "dataset_checksum": self.dataset_checksum,
            "config_name": self.config_name,
            "regime_source": self.regime_source,
            "regime_detail": self.regime_detail,
            "regime_is_synthetic": self.regime_is_synthetic,
            "config_deployable": self.config_deployable,
            "contested_bars": len(self.allocations),
            "allocations": self.allocations[:50],
            "config": self.config,
            "bars_processed": self.bars_processed,
            "decisions_evaluated": self.decisions_evaluated,
            "entries_attempted": self.entries_attempted,
            "entries_filled": self.entries_filled,
            "exits_filled": self.exits_filled,
            "unfillable_orders": self.unfillable_orders,
            "positions_open_at_end": self.positions_open_at_end,
            "rejections": self.rejections,
            "performance": self.performance,
            "warnings": self.warnings,
        }


def run(bars: Dict[str, Sequence[Bar]],
        config: Optional[ReplayConfig] = None,
        evidence: Optional[Sequence[Dict]] = None,
        catalyst_for=None,
        regime_for=None,
        spread_pct: float = 0.05,
        dataset_id: Optional[str] = None,
        dataset_checksum: Optional[str] = None,
        config_name: str = "ad-hoc",
        config_deployable: bool = True,
        regime_source: Optional[str] = None,
        regime_detail: Optional[str] = None) -> ReplayResult:
    """Replay the pipeline over `bars`.

    `bars` maps symbol to a chronological bar series. `catalyst_for` and
    `regime_for` are optional callables invoked with (symbol, clock) so a
    caller can supply point-in-time evidence and regime without this
    module reaching for a live service.
    """
    config = config or ReplayConfig()
    # A caller that supplies a regime but does not say where it came
    # from gets SYNTHETIC_UNDECLARED, not OBSERVED. An unnamed regime is
    # precisely the case most likely to be quoted as if it had been
    # measured, so the default names the doubt.
    if regime_source is None:
        regime_source = ("SYNTHETIC_UNDECLARED" if regime_for is not None
                         else "NONE")
    clock = ReplayClock()
    series = {symbol: PointInTimeSeries(symbol, list(sym_bars), clock)
              for symbol, sym_bars in bars.items()}
    if not series:
        raise ValueError("a replay needs at least one symbol")

    feed = PointInTimeEvidence(evidence or [], clock)
    broker = ReplayBroker(clock, series, PaperBrokerConfig(
        starting_cash=config.starting_cash,
        slippage_bps=config.slippage_bps,
        partial_fill_probability=config.partial_fill_probability,
        seed=config.seed), spread_pct=spread_pct)
    manager = PositionManager(broker=broker, execution_available=True)
    journal = InMemoryJournal()

    timeline = _unified_timeline(series)
    total_bars = len(timeline)
    warnings: List[str] = []
    if total_bars <= config.warmup_bars:
        warnings.append(
            f"only {total_bars} bars supplied against a warmup of "
            f"{config.warmup_bars}; no decisions will be taken")
    if regime_for is None:
        # The risk posture fails closed to NO_NEW_TRADES without a
        # regime, which is correct - but a reader seeing zero trades
        # would otherwise conclude the strategy found no setups, when in
        # fact it was never allowed to look. Supplying a permissive
        # default here would be the replay relaxing a safety gate to
        # manufacture trades, which is exactly how a backtest starts
        # lying.
        warnings.append(
            "no regime_for callable was supplied, so the risk posture "
            "failed closed to NO_NEW_TRADES on every bar; this run "
            "cannot produce entries and its emptiness says nothing "
            "about the strategy")
    if feed.undated_count:
        warnings.append(
            f"{feed.undated_count} evidence item(s) had no publication "
            "time and were never shown to the strategy")

    stats = {"decisions": 0, "entries_attempted": 0, "entries_filled": 0,
             "exits_filled": 0}
    rejections: Dict[str, int] = {}
    entry_bar: Dict[str, int] = {}
    entry_orders: Dict[str, Dict] = {}
    # Per-DAY counters, reset when the calendar date changes.
    #
    # These used to accumulate across the whole run while the risk
    # context derived session_date from each bar - so the governor saw
    # the day change and the counters never did. A 250-bar daily replay
    # was therefore one session with one $50 ceiling spread over a year,
    # which is why 250 days of AAPL produced two trades. That read as
    # strategy selectivity and was arithmetic.
    daily_capital_used = 0.0
    positions_opened = 0
    realized_pnl = 0.0
    realized_at_day_start = 0.0
    current_day: Optional[str] = None
    days_seen = 0
    previous_timestamp: Optional[str] = None
    allocations: List[Dict] = []

    try:
        for index, timestamp in enumerate(timeline):
            clock.advance(index, timestamp)
            broker.sync_quotes()

            # A new calendar day resets the day's budget, exactly as the
            # live agent's counters reset at the session boundary.
            bar_day = str(timestamp)[:10]
            if bar_day != current_day:
                current_day = bar_day
                days_seen += 1
                daily_capital_used = 0.0
                positions_opened = 0
                realized_at_day_start = sum(
                    (t.net_pnl or 0.0) for t in journal.list_trades()
                    if t.net_pnl is not None)

            # Realised P&L comes from the JOURNAL rather than a running
            # tally, so it cannot drift from the record the metrics are
            # computed from. Measured from the start of THIS day, since
            # the daily loss limit is a daily limit.
            realized_pnl = sum(
                (t.net_pnl or 0.0) for t in journal.list_trades()
                if t.net_pnl is not None) - realized_at_day_start

            # --- manage what is already open, before opening more ---
            _manage_open_positions(manager, series, clock, config,
                                   journal, entry_bar, entry_orders, stats)

            if index < config.warmup_bars:
                continue
            # An order decided on the final bar cannot fill, so there is
            # no point deciding.
            if index >= total_bars - 1:
                continue

            # Deterministic order, so the same dataset is the same
            # experiment however the caller built the dict.
            contended = []
            for symbol in _candidate_sequence(series, config):
                sym_series = series[symbol]
                if clock.index >= len(sym_series):
                    continue
                if manager.get(symbol) is not None:
                    continue

                stats["decisions"] += 1
                decision, hypothesis = _decide(
                    symbol, sym_series, clock, config, feed,
                    catalyst_for, regime_for, manager, daily_capital_used,
                    spread_pct,
                    data_age_seconds=_bar_gap_seconds(
                        previous_timestamp, timestamp,
                        config.bar_interval_seconds),
                    positions_opened_today=positions_opened,
                    realized_pnl_today=realized_pnl)

                if decision is None:
                    continue
                if not decision.approved:
                    for code in decision.reason_codes:
                        key = str(code)
                        rejections[key] = rejections.get(key, 0) + 1
                    contended.append({
                        "symbol": symbol, "outcome": "REFUSED",
                        "reasons": [str(c) for c in decision.reason_codes],
                        "capital_used_before": round(daily_capital_used, 2)})
                    continue

                stats["entries_attempted"] += 1
                order = broker.submit_entry(
                    symbol, decision.capital_required,
                    hypothesis_id=hypothesis.hypothesis_id,
                    risk_decision_id=decision.decision_id,
                    intent="ENTRY")
                if (order.get("filled_quantity") or 0) <= 0:
                    continue

                stats["entries_filled"] += 1
                positions_opened += 1
                daily_capital_used += decision.capital_required
                fill = order["average_fill_price"]
                # Mirror the live cycle exactly: one distance drives
                # the stop, the target and the trail. See stop_source.
                stop_pct = config.stop_distance_pct
                target_pct = config.target_distance_pct
                trail_pct = config.trailing_stop_pct
                if config.stop_source == "DECISION":
                    from_decision = getattr(
                        decision, "stop_distance_pct", None)
                    if from_decision is None:
                        stats["stop_fallbacks"] = (
                            stats.get("stop_fallbacks", 0) + 1)
                    else:
                        stop_pct = float(from_decision)
                        target_pct = stop_pct * config.target_stop_multiple
                        trail_pct = stop_pct
                        stats["stops_from_decision"] = (
                            stats.get("stops_from_decision", 0) + 1)
                plan = ExitPlan(
                    stop_price=fill * (1 - stop_pct / 100.0),
                    target_price=fill * (1 + target_pct / 100.0),
                    trailing_stop_pct=trail_pct,
                    stop_mechanism=StopMechanism.ENGINE_POLLED,
                    evaluation_interval_seconds=(
                        config.evaluation_interval_seconds))
                manager.open_from_order(
                    order, plan, hypothesis_id=hypothesis.hypothesis_id,
                    risk_decision_id=decision.decision_id,
                    config_version=CONFIG_VERSION)
                entry_bar[symbol] = index
                entry_orders[symbol] = order
                contended.append({
                    "symbol": symbol, "outcome": "ENTERED",
                    "reasons": [],
                    "capital_used_before": round(
                        daily_capital_used - decision.capital_required, 2)})

            # Keep the allocation record only where candidates actually
            # COMPETED - one qualifying candidate is not a portfolio
            # decision, and recording every bar would bury the bars
            # where the envelope was the binding constraint.
            if len(contended) > 1 and any(
                    c["outcome"] == "ENTERED" for c in contended):
                allocations.append({"bar": index, "timestamp": timestamp,
                                    "candidates": contended})

            # The age of the data at the NEXT bar is measured from this
            # one. Set at the end of the iteration so a halt - a hole in
            # the timeline - is seen as the gap it is.
            previous_timestamp = timestamp

        # --- end of data: close what is still open -------------------
        _flatten_at_end(manager, series, journal, entry_orders, config, stats)

    except LookaheadError as exc:
        log_event("replay_lookahead_detected", detail=str(exc))
        return ReplayResult(
            config=config.as_dict(), bars_processed=max(0, clock.index),
            decisions_evaluated=stats["decisions"],
            entries_attempted=stats["entries_attempted"],
            entries_filled=stats["entries_filled"],
            exits_filled=stats["exits_filled"],
            unfillable_orders=broker.unfillable_orders,
            positions_open_at_end=manager.open_count,
            rejections=rejections,
            performance={"verdict": "VOID_LOOKAHEAD"},
            dataset_id=dataset_id,
            dataset_checksum=dataset_checksum,
            config_name=config_name,
            config_deployable=config_deployable,
        regime_source=regime_source,
        regime_detail=regime_detail,
            lookahead_detected=True, lookahead_detail=str(exc),
            warnings=warnings + [
                "this run reached for data it should not have seen; the "
                "result is void, not merely optimistic"])

    trades = journal.list_trades()
    performance = describe(trades)
    if manager.open_count:
        warnings.append(
            f"{manager.open_count} position(s) were still open when the "
            "run ended and could not be closed; their profit or loss is "
            "NOT in these results, so the reported performance is "
            "incomplete")
    if broker.unfillable_orders:
        warnings.append(
            f"{broker.unfillable_orders} order(s) could not fill because "
            "they were decided on the final bar; they were dropped rather "
            "than filled at an invented price")

    log_event("replay_complete", bars=total_bars,
              decisions=stats["decisions"],
              entries=stats["entries_filled"],
              trades=len(trades),
              verdict=performance.get("verdict"))

    return ReplayResult(
        config=config.as_dict(), bars_processed=total_bars,
        decisions_evaluated=stats["decisions"],
        entries_attempted=stats["entries_attempted"],
        entries_filled=stats["entries_filled"],
        exits_filled=stats["exits_filled"],
        unfillable_orders=broker.unfillable_orders,
        positions_open_at_end=manager.open_count,
        rejections=rejections, performance=performance,
        allocations=allocations,
        dataset_id=dataset_id, dataset_checksum=dataset_checksum,
        config_name=config_name, config_deployable=config_deployable,
        regime_source=regime_source,
        regime_detail=regime_detail,
        warnings=warnings)


def _candidate_sequence(series: Dict, config: ReplayConfig) -> List[str]:
    """The order candidates are offered the day's remaining capital.

    Deterministic by construction. The previous behaviour iterated the
    bars dict, so `{"AAA":..., "BBB":..., "CCC":...}` and the same three
    symbols in reverse produced DIFFERENT trades - the first two to be
    offered capital took it. Same dataset, different experiment.
    """
    order = (config.candidate_order or "ALPHABETICAL").upper()
    if order == "ALPHABETICAL":
        return sorted(series)
    if order == "AS_SUPPLIED":
        # Available deliberately, for a caller testing the ordering
        # effect itself. Not the default, because it is only
        # reproducible if the caller's dict order is.
        return list(series)
    raise ValueError(
        f"unknown candidate_order {config.candidate_order!r}; a replay "
        f"whose candidate order is undefined is not reproducible")


def _bar_gap_seconds(previous: Optional[str], current: Optional[str],
                     expected: float) -> float:
    """How old the prevailing data is at `current`.

    A halt is an ABSENCE of prints, and the honest measure of that is
    the distance to the previous bar. This used to be the literal 0.0,
    which meant replay data was permanently and perfectly fresh - so
    STALE_MARKET_DATA could never fire and a scenario claiming to test
    the freshness gate was testing nothing.

    Falls back to the expected bar interval when either timestamp is
    unparseable, because an unknown gap must not read as a zero one.
    """
    if not previous or not current:
        return expected
    try:
        a = datetime.fromisoformat(str(previous).replace("Z", "+00:00"))
        b = datetime.fromisoformat(str(current).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return expected
    gap = (b - a).total_seconds()
    if gap <= 0:
        return expected
    # The EXCESS over the expected interval, not the raw gap.
    #
    # At a bar's close its own price is current, so a bar arriving on
    # schedule is not stale however long the interval is. Staleness is
    # data MISSING: a 45-minute hole in a one-minute series is 44
    # minutes of absence, while a daily bar arriving a day later is
    # perfectly fresh.
    #
    # Returning the raw gap made every DAILY replay refuse everything
    # with STALE_MARKET_DATA - 86,400 seconds against a 120-second
    # limit - which silently turned a year of AAPL into zero trades.
    return max(0.0, gap - expected)


def _unified_timeline(series: Dict[str, PointInTimeSeries]) -> List[str]:
    """One ordered list of timestamps across every symbol.

    Sorted and deduplicated so the clock advances monotonically no
    matter how the inputs were assembled.
    """
    stamps = set()
    for sym_series in series.values():
        stamps.update(sym_series.all_timestamps)
    return sorted(stamps)


def _decide(symbol, sym_series, clock, config, feed, catalyst_for,
            regime_for, manager, daily_capital_used, spread_pct=0.05,
            data_age_seconds=0.0, positions_opened_today=0,
            realized_pnl_today=0.0):
    """Run the live decision pipeline at the current bar."""
    closes = sym_series.closes_through_now()
    if len(closes) < 2:
        return None, None

    catalyst = (catalyst_for(symbol, clock) if catalyst_for is not None
                else None)
    regime = regime_for(symbol, clock) if regime_for is not None else None

    bar = sym_series.current()

    # Use the same top-level entry point the live service uses, so the
    # hypothesis engine receives exactly the payload shape it would
    # receive in production - including data_quality, which it reads to
    # decide whether the reading is fresh enough to act on. Assembling a
    # narrower dict here by hand would silently change the inputs to a
    # safety gate.
    #
    # Freshness is FRESH because at a bar's close that bar IS the
    # current observation: the data is exactly as current as it would
    # have been live. This is the only place replay may assert that.
    signal_result = evaluate_signals(
        symbol, closes, freshness=DataFreshness.FRESH,
        market_regime=str((regime or {}).get("regime", "UNKNOWN")),
        regime_confidence=float((regime or {}).get("regime_confidence") or 0.0),
        data_quality={"freshness": str(DataFreshness.FRESH),
                      "price_source": "replay_bars",
                      "price_as_of": bar.timestamp,
                      "age_seconds": 0.0,
                      # In a replay the bar's timestamp IS the clock, so
                      # the data is current by construction.
                      "source_age_seconds": 0.0,
                      "feed_quality": "REPLAY",
                      "history_bars": len(closes)},
        price=bar.close)
    signal_summary = signal_result.as_dict()

    hypothesis = generate_hypothesis(symbol, signal_summary, catalyst, regime)

    context = RiskContext(
        session_date=config.session_date or bar.timestamp[:10],
        market_session="OPEN",
        minutes_to_close=config.minutes_to_close,
        trading_enabled=True, execution_available=True,
        price=bar.close,
        # The spread the broker is actually quoting. This used to be the
        # literal 0.05, so a replay could not test the spread gate at
        # all: a scenario could widen the broker's spread and the risk
        # context would still report five basis points. A gate that the
        # harness can never make fire is not a tested gate.
        spread_pct=spread_pct,
        # The bar's ACTUAL dollar volume. This used to be floored at
        # min_dollar_volume, which meant the liquidity gate could never
        # refuse anything in a replay - the floor guaranteed the gate
        # passed. Synthetic bars must therefore carry realistic volume,
        # which is the scenario's job, not the engine's.
        dollar_volume=bar.volume * bar.close,
        # Derived from the gap to the previous bar, not a literal. A
        # halt is a hole in the timeline and must read as stale data.
        quote_age_seconds=data_age_seconds,
        source_age_seconds=data_age_seconds,
        open_positions=manager.open_count,
        capital_deployed_today=daily_capital_used,
        # Both of these were literals, so MAX_NEW_POSITIONS_REACHED and
        # DAILY_RISK_LOCK could never fire in a replay - the daily loss
        # limit, the single most important risk control in the system,
        # was silently absent from every backtest.
        positions_opened_today=positions_opened_today,
        realized_pnl_today=realized_pnl_today,
        held_symbols=[p.symbol for p in manager.open_positions()])
    decision = evaluate_risk(hypothesis, context, limits=config.risk_limits)
    return decision, hypothesis


def _manage_open_positions(manager, series, clock, config, journal,
                           entry_bar, entry_orders, stats):
    """Evaluate exits and resolve them on the next bar."""
    contexts = {}
    for position in manager.open_positions():
        sym_series = series.get(position.symbol)
        if sym_series is None or clock.index >= len(sym_series):
            contexts[position.symbol] = ExitContext()
            continue
        bar = sym_series.current()
        held_bars = clock.index - entry_bar.get(position.symbol, clock.index)
        contexts[position.symbol] = ExitContext(
            price=bar.close, quote_age_seconds=0.0,
            market_session="OPEN", minutes_to_close=120.0,
            minutes_held=(held_bars if config.max_hold_bars is None
                          else held_bars),
            now=bar.timestamp)

    for intent in manager.evaluate_exits(contexts):
        position = manager.get(intent.symbol)
        if position is None:
            continue
        order = manager.broker.resolve_exit(
            intent.symbol, position.plan.stop_price,
            intent=str(intent.primary_reason))
        if (order.get("filled_quantity") or 0) > 0:
            record_closed_position(
                journal, position, order,
                entry_order=entry_orders.get(intent.symbol),
                intent=intent, session_date=config.session_date,
                config_versions={"replay": CONFIG_VERSION},
                is_paper=True)
            manager._close(position, intent)
            stats["exits_filled"] += 1
            entry_bar.pop(intent.symbol, None)
            entry_orders.pop(intent.symbol, None)


def _flatten_at_end(manager, series, journal, entry_orders, config, stats):
    """Close remaining positions at the last available price.

    Marked as an end-of-data exit rather than a strategy decision, so a
    run that ends mid-trade cannot report that exit as a profit target
    the strategy earned.
    """
    for intent in manager.flatten_all(detail="end of replay data"):
        position = manager.get(intent.symbol)
        if position is None:
            continue
        order = manager.broker.resolve_exit(intent.symbol, None,
                                            intent="END_OF_DATA")
        if (order.get("filled_quantity") or 0) > 0:
            record_closed_position(
                journal, position, order,
                entry_order=entry_orders.get(intent.symbol),
                intent=intent, session_date=config.session_date,
                config_versions={"replay": CONFIG_VERSION}, is_paper=True)
            manager._close(position, intent)
            stats["exits_filled"] += 1
