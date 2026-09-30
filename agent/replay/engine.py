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
    stop_distance_pct: float = 3.0
    target_distance_pct: float = 6.0
    trailing_stop_pct: Optional[float] = 3.0
    max_hold_bars: Optional[int] = None
    evaluation_interval_seconds: int = 60

    def as_dict(self) -> Dict:
        return {
            "config_version": CONFIG_VERSION,
            "session_date": self.session_date,
            "warmup_bars": self.warmup_bars,
            "starting_cash": self.starting_cash,
            "slippage_bps": self.slippage_bps,
            "partial_fill_probability": self.partial_fill_probability,
            "seed": self.seed,
            "stop_distance_pct": self.stop_distance_pct,
            "target_distance_pct": self.target_distance_pct,
            "trailing_stop_pct": self.trailing_stop_pct,
            "max_hold_bars": self.max_hold_bars,
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
        regime_for=None) -> ReplayResult:
    """Replay the pipeline over `bars`.

    `bars` maps symbol to a chronological bar series. `catalyst_for` and
    `regime_for` are optional callables invoked with (symbol, clock) so a
    caller can supply point-in-time evidence and regime without this
    module reaching for a live service.
    """
    config = config or ReplayConfig()
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
        seed=config.seed))
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
    daily_capital_used = 0.0

    try:
        for index, timestamp in enumerate(timeline):
            clock.advance(index, timestamp)
            broker.sync_quotes()

            # --- manage what is already open, before opening more ---
            _manage_open_positions(manager, series, clock, config,
                                   journal, entry_bar, entry_orders, stats)

            if index < config.warmup_bars:
                continue
            # An order decided on the final bar cannot fill, so there is
            # no point deciding.
            if index >= total_bars - 1:
                continue

            for symbol, sym_series in series.items():
                if clock.index >= len(sym_series):
                    continue
                if manager.get(symbol) is not None:
                    continue

                stats["decisions"] += 1
                decision, hypothesis = _decide(
                    symbol, sym_series, clock, config, feed,
                    catalyst_for, regime_for, manager, daily_capital_used)

                if decision is None:
                    continue
                if not decision.approved:
                    for code in decision.reason_codes:
                        key = str(code)
                        rejections[key] = rejections.get(key, 0) + 1
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
                daily_capital_used += decision.capital_required
                fill = order["average_fill_price"]
                plan = ExitPlan(
                    stop_price=fill * (1 - config.stop_distance_pct / 100.0),
                    target_price=fill * (
                        1 + config.target_distance_pct / 100.0),
                    trailing_stop_pct=config.trailing_stop_pct,
                    stop_mechanism=StopMechanism.ENGINE_POLLED,
                    evaluation_interval_seconds=(
                        config.evaluation_interval_seconds))
                manager.open_from_order(
                    order, plan, hypothesis_id=hypothesis.hypothesis_id,
                    risk_decision_id=decision.decision_id,
                    config_version=CONFIG_VERSION)
                entry_bar[symbol] = index
                entry_orders[symbol] = order

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
        warnings=warnings)


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
            regime_for, manager, daily_capital_used):
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
                      "history_bars": len(closes)},
        price=bar.close)
    signal_summary = signal_result.as_dict()

    hypothesis = generate_hypothesis(symbol, signal_summary, catalyst, regime)

    context = RiskContext(
        session_date=config.session_date or bar.timestamp[:10],
        market_session="OPEN", minutes_to_close=120.0,
        trading_enabled=True, execution_available=True,
        price=bar.close,
        spread_pct=0.05,
        dollar_volume=max(bar.volume * bar.close,
                          config.risk_limits.min_dollar_volume),
        quote_age_seconds=0.0,
        open_positions=manager.open_count,
        capital_deployed_today=daily_capital_used,
        positions_opened_today=0,
        realized_pnl_today=0.0,
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
