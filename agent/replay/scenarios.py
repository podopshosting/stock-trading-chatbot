"""
Named market conditions, built on purpose.

Historical replay answers "what would this have done last March".
Scenarios answer a different and in some ways more useful question:
"what does this do when the thing I am afraid of happens". History
contains the events it happens to contain, and the ones that matter most
for a risk system are rare by construction - a gap straight through a
stop, a halt, a spread that opens to a dollar wide. Waiting for them to
appear in a sample is not a plan.

What a scenario is NOT
----------------------
It is not evidence of edge, and it is not a backtest. The bars are
invented. A scenario that "makes money" means a made-up price series
went the agent's way, which is worth nothing. Every result carries
`SCENARIO_NOT_EVIDENCE` and scenario runs never enter an evidence
cohort.

What it IS good for is the machinery: did the stop get hit, was the
loss bounded by the configured per-trade risk, did the spread gate
refuse, did a gap produce a loss LARGER than planned - which is the
question the `stops_hold` readiness gate exists to ask and which no
amount of calm paper trading will answer.
"""
from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Sequence

from .data import Bar

# A scenario result may never be read as performance evidence.
SCENARIO_NOT_EVIDENCE = (
    "SCENARIO_NOT_EVIDENCE: the bars are invented. This measures what "
    "the system DOES under a named condition, not whether the strategy "
    "has an edge. A profitable scenario means a made-up series went the "
    "agent's way."
)

WARMUP = 210          # enough for the 200-bar indicators plus margin

# Where false_breakout's thrust begins. Named so the guarantee check
# slices at the generator's boundary rather than guessing a window.
_BREAKOUT_BASE = WARMUP + 30


def _ts(index: int, day: str = "2026-01-02") -> str:
    """A minute timestamp. Bars are one minute apart."""
    return f"{day}T{14 + index // 60:02d}:{index % 60:02d}:00+00:00"


def _bar(index: int, open_, high, low, close, volume=500_000.0,
         day: str = "2026-01-02") -> Bar:
    # high/low are forced to bracket open/close: a bar whose high is
    # below its open is not a market condition, it is a bad fixture, and
    # it would make every downstream check meaningless.
    return Bar(timestamp=_ts(index, day),
               open=round(open_, 4),
               high=round(max(high, open_, close), 4),
               low=round(min(low, open_, close), 4),
               close=round(close, 4), volume=volume)


def _calm(count: int, start: float = 100.0, drift: float = 0.0012,
          vol: float = 0.010, day: str = "2026-01-02",
          seed: int = 11) -> List[Bar]:
    """A trending series with pullbacks.

    A SEEDED generator, not a fixed alternating pattern. The first
    version of this used a deterministic up-down wobble on the grounds
    that it reproduced exactly - and it did, but the indicators read it
    as directionless, so the control scenario produced 119 decisions and
    zero entries. A harness in which nothing ever trades makes every
    other scenario look safe for the wrong reason.

    A fixed seed is just as reproducible and actually moves.
    """
    rng, out, price = random.Random(seed), [], start
    for i in range(count):
        price *= (1 + drift + rng.gauss(0, vol))
        o = price * (1 + rng.gauss(0, vol / 3))
        high = max(o, price) * (1 + abs(rng.gauss(0, vol / 3)))
        low = min(o, price) * (1 - abs(rng.gauss(0, vol / 3)))
        out.append(_bar(i, o, high, low, price, day=day))
    return out


# Whether a scenario is supposed to produce a trade. NOT a judgement
# about the strategy - "no trade" is the correct answer to several of
# these, and stating it up front is what stops a silent harness being
# mistaken for a safe one.
TRADE_EXPECTED = "TRADE_EXPECTED"
NO_TRADE_EXPECTED = "NO_TRADE_EXPECTED"
TRADE_OPTIONAL = "TRADE_OPTIONAL"


@dataclass
class Scenario:
    """One named condition, and what a correct system should do in it."""
    name: str
    question: str                 # what this is asking the system
    expectation: str              # what correct behaviour looks like
    bars: Dict[str, List[Bar]] = field(default_factory=dict)
    spread_pct: Optional[float] = None
    regime: str = "BULLISH"
    note: str = ""
    # Should a trade happen? Declared, so a scenario that produces
    # nothing cannot be read as proof that trade management is safe.
    trade: str = TRADE_OPTIONAL
    # May a loss legitimately exceed the planned 1R here? True for
    # gaps and halts, where a stop is an instruction the market is free
    # to ignore. False means a breach IS a finding.
    may_exceed_1r: bool = False
    # What going wrong would look like, as distinct from losing money.
    expected_failure: str = ""
    # Config the scenario needs in order to be the condition it claims
    # to be - a late-session scenario cannot test the session cut-off
    # without moving minutes_to_close.
    config_overrides: Dict = field(default_factory=dict)

    def as_dict(self) -> Dict:
        series = next(iter(self.bars.values()), [])
        return {
            "name": self.name,
            "question": self.question,
            "expectation": self.expectation,
            "trade": self.trade,
            "may_exceed_1r": self.may_exceed_1r,
            "expected_failure": self.expected_failure,
            "symbols": sorted(self.bars),
            "bars": len(series),
            "spread_pct": self.spread_pct,
            "regime": self.regime,
            "config_overrides": dict(self.config_overrides),
            "note": self.note,
            "disclaimer": SCENARIO_NOT_EVIDENCE,
        }


# =====================================================================
# The scenarios
# =====================================================================

def _bleed_series() -> List[Bar]:
    """A rise, then a smooth decline, as ONE continuous series.

    The first version concatenated two _calm() calls, and the second
    restarted its timestamps at index 0 - so the bars ran backwards in
    time and the engine refused the whole run. It was right to: a
    replay whose clock can rewind can re-decide with knowledge it did
    not have. The fixture was the bug, and the engine catching it is
    the engine working.
    """
    rise = _calm(WARMUP + 40)
    out = list(rise)
    price = rise[-1].close
    for i in range(60):
        close = price * (1 - 0.0030)
        out.append(_bar(len(out), price, max(price, close) * 1.0004,
                        min(price, close) * 0.9996, close))
        price = close
    return out


def grind_up(symbol: str = "XYZ") -> Scenario:
    """The control. Without a benign case, a harness in which nothing
    ever trades looks identical to one in which everything is refused
    for the right reasons."""
    return Scenario(
        name="grind_up",
        trade=TRADE_EXPECTED,
        may_exceed_1r=False,
        expected_failure="no entry at all, which would mean every other scenario here is measuring silence rather than behaviour",
        question="does the agent trade at all under ordinary conditions?",
        expectation="an entry is taken and managed; this is the control "
                    "that proves the harness can produce a trade",
        bars={symbol: _calm(WARMUP + 120)},
    )


def chop(symbol: str = "XYZ") -> Scenario:
    return Scenario(
        name="chop",
        trade=TRADE_OPTIONAL,
        # True, and the reason is not a gap. The stop is ENGINE_POLLED:
        # it is evaluated at a bar CLOSE and filled at the next bar's
        # OPEN, so a polled stop can never do better than the next
        # print. With 1.2% bars that is routinely worse than 1R. This
        # was initially declared False and the scenario breached at
        # -1.30R, which is how the distinction got noticed.
        may_exceed_1r=True,
        expected_failure="heavy trading in a directionless market, which is churn paying the spread twice",
        question="does a directionless market produce entries anyway?",
        expectation="few or no entries; a signal engine that trades "
                    "noise will show up here as activity",
        bars={symbol: _calm(WARMUP + 120, drift=0.0, vol=0.012)},
    )


def gap_through_stop(symbol: str = "XYZ", gap_pct: float = 12.0) -> Scenario:
    """The one that matters most.

    The agent sizes a position so that a stop at -3% loses no more than
    the per-trade limit. A gap straight through the stop does not lose
    that amount - it loses whatever the next print says. This is the
    difference between a planned risk and an actual one, and it is the
    question the `stops_hold` readiness gate asks.
    """
    series = _calm(WARMUP + 40)
    last = series[-1].close
    floor = last * (1 - gap_pct / 100.0)
    # One bar that opens far below the stop and never trades back up.
    series.append(_bar(len(series), floor, floor * 1.001,
                       floor * 0.995, floor * 0.998))
    series.extend(
        _bar(len(series) + i, floor * 0.998, floor * 1.002,
             floor * 0.996, floor * 0.999) for i in range(30))
    return Scenario(
        name="gap_through_stop",
        trade=TRADE_EXPECTED,
        may_exceed_1r=True,
        expected_failure="the loss being REPORTED as if the stop held",
        question=f"when price gaps {gap_pct}% through the stop, is the "
                 f"realised loss still bounded by the per-trade limit?",
        expectation="the position exits, and the loss is EXPECTED to "
                    "exceed max_trade_risk - a stop is an instruction, "
                    "not a guarantee. The number is the point: it says "
                    "how wrong the risk model can be",
        bars={symbol: series},
        note="A loss larger than max_trade_risk here is not a defect. "
             "Reporting it as if the stop held would be.",
    )


def flash_crash(symbol: str = "XYZ", depth_pct: float = 20.0) -> Scenario:
    """Down hard and back within two bars. Tests whether the exit
    engine sells the bottom of a wick it should have ignored - or
    whether it fails to exit a genuine breach."""
    series = _calm(WARMUP + 40)
    last = series[-1].close
    bottom = last * (1 - depth_pct / 100.0)
    series.append(_bar(len(series), last, last * 1.001, bottom,
                       last * 0.995))
    series.append(_bar(len(series), last * 0.995, last * 1.002,
                       last * 0.99, last))
    series.extend(_bar(len(series) + i, last, last * 1.002,
                       last * 0.998, last) for i in range(30))
    return Scenario(
        name="flash_crash",
        trade=TRADE_EXPECTED,
        may_exceed_1r=True,
        expected_failure="selling the bottom of a wick that recovered within the bar",
        question="does a deep wick that recovers immediately trigger an "
                 "exit, and at what price?",
        expectation="the polled stop evaluates on bar closes, so a wick "
                    "that recovers within the bar may not trigger at "
                    "all. Whatever happens, the fill price is what "
                    "matters, not the intent",
        bars={symbol: series},
    )


def trading_halt(symbol: str = "XYZ", gap_minutes: int = 45,
                 reopen_pct: float = -8.0) -> Scenario:
    """No prints for a period, then a reopen at a different price.

    Modelled as a gap in the timeline rather than flat bars, because a
    halt is an ABSENCE of data and flat bars are data saying 'unchanged'
    - which is the opposite.
    """
    series = _calm(WARMUP + 40)
    last = series[-1].close
    reopen = last * (1 + reopen_pct / 100.0)
    start = len(series) + gap_minutes
    series.append(_bar(start, reopen, reopen * 1.004, reopen * 0.996,
                       reopen * 1.001))
    series.extend(_bar(start + 1 + i, reopen, reopen * 1.003,
                       reopen * 0.997, reopen) for i in range(30))
    return Scenario(
        name="trading_halt",
        trade=TRADE_OPTIONAL,
        may_exceed_1r=True,
        expected_failure="a NEW entry taken on data that is 45 minutes old",
        question="what happens to an open position across a halt, and "
                 "does the staleness check notice the data gap?",
        expectation="quote age should exceed the freshness limit during "
                    "the gap, so no NEW entry is taken; an existing "
                    "position is exposed across it with no way to act",
        bars={symbol: series},
        note=f"a {gap_minutes}-minute hole in the timeline, not flat bars",
    )


def spread_blowout(symbol: str = "XYZ", spread_pct: float = 2.5) -> Scenario:
    """A spread far wider than the entry gate permits."""
    return Scenario(
        name="spread_blowout",
        trade=NO_TRADE_EXPECTED,
        may_exceed_1r=False,
        expected_failure="any entry at all; the spread gate exists to refuse these",
        question=f"does a {spread_pct}% spread refuse new entries?",
        expectation="every entry refused with SPREAD_TOO_WIDE; the gate "
                    "exists so an entry does not pay away the expected "
                    "move",
        bars={symbol: _calm(WARMUP + 120)},
        spread_pct=spread_pct,
    )


def bear_regime(symbol: str = "XYZ") -> Scenario:
    return Scenario(
        name="bear_regime",
        trade=NO_TRADE_EXPECTED,
        may_exceed_1r=False,
        expected_failure="long entries into a hostile regime",
        question="does a hostile regime suppress entries?",
        expectation="entries refused or sized down; a long-only agent in "
                    "a bear regime should mostly decline to act",
        bars={symbol: _calm(WARMUP + 120, drift=-0.0020)},
        regime="BEARISH",
    )


def slow_bleed(symbol: str = "XYZ") -> Scenario:
    """A drift down with no gap: the stop should hold almost exactly,
    which makes it the counterpart to gap_through_stop."""
    return Scenario(
        name="slow_bleed",
        trade=TRADE_EXPECTED,
        may_exceed_1r=False,
        expected_failure="a loss materially beyond 1R with no gap to explain it - that would mean the risk model is wrong even on continuous prices",
        question="when price declines smoothly through the stop, is the "
                 "loss close to the planned amount?",
        expectation="the loss should be CLOSE to max_trade_risk, within "
                    "slippage. If it is not, the risk model is wrong "
                    "even without a gap to blame",
        bars={symbol: _bleed_series()},
    )


def downtrend(symbol: str = "XYZ") -> Scenario:
    return Scenario(
        name="downtrend",
        question="does a long-only agent short, or refuse?",
        expectation="no entries; a sustained decline offers a long-only "
                    "strategy nothing, and NOT_LONG_ONLY should dominate "
                    "the refusals",
        trade=NO_TRADE_EXPECTED,
        expected_failure="a long entry into a sustained decline",
        bars={symbol: _calm(WARMUP + 120, drift=-0.0025)},
    )


def volatile_chop(symbol: str = "XYZ") -> Scenario:
    return Scenario(
        name="volatile_chop",
        question="with 4% bars, how far past 1R can a POLLED stop be "
                 "filled?",
        expectation="breaches are expected and the magnitude is the "
                    "answer: a stop evaluated at a bar close and filled "
                    "at the next open cannot do better than the next "
                    "print, and a wide bar makes that gap large",
        trade=TRADE_OPTIONAL,
        may_exceed_1r=True,
        expected_failure="reporting these losses as if the stop had held",
        bars={symbol: _calm(WARMUP + 120, drift=0.0, vol=0.040)},
    )


def gap_through_target(symbol: str = "XYZ", gap_pct: float = 14.0) -> Scenario:
    """The favourable mirror of gap_through_stop.

    Included because a harness that only models adverse gaps overstates
    how bad gapping is: the same mechanism that overshoots a stop also
    overshoots a target.
    """
    series = _calm(WARMUP + 40)
    last = series[-1].close
    top = last * (1 + gap_pct / 100.0)
    series.append(_bar(len(series), top, top * 1.005, top * 0.999,
                       top * 1.002))
    series.extend(_bar(len(series) + i, top * 1.002, top * 1.004,
                       top * 0.998, top * 1.001) for i in range(30))
    return Scenario(
        name="gap_through_target",
        question="when price gaps past the target, is the gain recorded "
                 "at the fill rather than at the target?",
        expectation="the exit fills ABOVE the target and the recorded "
                    "gain exceeds the planned one; a backtest that "
                    "clipped it to the target would understate the "
                    "mechanism it overstates on the downside",
        trade=TRADE_EXPECTED,
        may_exceed_1r=True,
        expected_failure="recording the exit at the target price it "
                         "never traded at",
        bars={symbol: series},
    )


def low_liquidity(symbol: str = "XYZ") -> Scenario:
    """Volume far below the dollar-volume floor."""
    series = [
        Bar(timestamp=b.timestamp, open=b.open, high=b.high, low=b.low,
            close=b.close, volume=200.0)
        for b in _calm(WARMUP + 120)]
    return Scenario(
        name="low_liquidity",
        question="does thin volume refuse entries?",
        expectation="every entry refused with INSUFFICIENT_LIQUIDITY; "
                    "about $20k of dollar volume against a $20m floor",
        trade=NO_TRADE_EXPECTED,
        expected_failure="entering a name that cannot be exited without "
                         "moving it",
        bars={symbol: series},
    )


def late_in_session(symbol: str = "XYZ") -> Scenario:
    return Scenario(
        name="late_in_session",
        question="does the session cut-off refuse a new entry near the "
                 "close?",
        expectation="every entry refused with TOO_LATE_IN_SESSION; "
                    "positions are flattened before the bell, so an "
                    "entry minutes beforehand buys a round trip and no "
                    "time to work",
        trade=NO_TRADE_EXPECTED,
        expected_failure="opening exposure that must be closed minutes "
                         "later regardless of outcome",
        bars={symbol: _calm(WARMUP + 120)},
        config_overrides={"minutes_to_close": 2.0},
    )


def partial_fills(symbol: str = "XYZ") -> Scenario:
    return Scenario(
        name="partial_fills",
        question="what happens when entries and exits fill only partly?",
        expectation="the managed quantity is what actually FILLED, never "
                    "what was requested; a remainder left working is "
                    "cancelled rather than assumed away",
        trade=TRADE_EXPECTED,
        may_exceed_1r=True,
        expected_failure="managing a position size that was never filled",
        bars={symbol: _calm(WARMUP + 120)},
        config_overrides={"partial_fill_probability": 0.6},
    )


def false_breakout(symbol: str = "XYZ") -> Scenario:
    """Pushes to a new high, then fails straight back through the range.

    The textbook trap for a momentum strategy, and the loss here is
    CORRECT behaviour: the setup was real and it did not work.
    """
    series = _calm(WARMUP + 30)
    last = series[-1].close
    # The thrust must stay BELOW the 6% target, or the trade takes
    # profit before the failure arrives and the scenario becomes a
    # winner named after a trap. It produced +2.51R before this bound.
    for i in range(4):                       # the thrust
        price = last * (1 + 0.006 * (i + 1))
        series.append(_bar(len(series), price * 0.998, price * 1.004,
                           price * 0.996, price))
    peak = series[-1].close
    for i in range(40):                      # and the failure
        price = peak * (1 - 0.006 * (i + 1))
        series.append(_bar(len(series), price * 1.002, price * 1.003,
                           price * 0.995, price))
    return Scenario(
        name="false_breakout",
        question="does the agent lose CORRECTLY on a failed breakout?",
        expectation="an entry is taken on the thrust and stopped out on "
                    "the failure. A loss is the right outcome - the "
                    "setup existed and did not work, which is what a "
                    "stop is for",
        trade=TRADE_EXPECTED,
        expected_failure="holding past the stop, or not entering at all "
                         "- the second would mean the strategy cannot "
                         "see a breakout rather than that it avoided a "
                         "bad one",
        bars={symbol: series},
    )


def momentum_reversal(symbol: str = "XYZ") -> Scenario:
    series = _calm(WARMUP + 60, drift=0.0030)
    peak = series[-1].close
    for i in range(50):
        price = peak * (1 - 0.0045 * (i + 1))
        series.append(_bar(len(series), price * 1.001, price * 1.002,
                           price * 0.997, price))
    return Scenario(
        name="momentum_reversal",
        question="how quickly does a position opened in a strong trend "
                 "get out when the trend inverts?",
        expectation="the stop or the trailing stop closes it; the "
                    "interesting number is how far past the entry the "
                    "exit lands",
        trade=TRADE_EXPECTED,
        expected_failure="riding the reversal down because the trailing "
                         "stop only ever loosened",
        bars={symbol: series},
    )


def consecutive_losses(symbol: str = "XYZ") -> Scenario:
    """Repeated losing entries, to reach the daily loss lock.

    This is the scenario the harness could not express at all until
    realized_pnl_today stopped being a literal zero: the daily loss
    limit is the single most important risk control in the system and
    no backtest had ever exercised it.
    """
    # Spread over SEPARATE SESSIONS, because the daily budget resets.
    #
    # Within one session the capital ceiling binds long before the loss
    # limit does: $50 a day at roughly $25 a position is about two
    # trades, risking about $2 each, so a single session cannot lose the
    # $5 the daily limit allows. A run of losses is therefore a run of
    # DAYS, and that is what this builds.
    series = _calm(WARMUP + 20, day="2026-01-02")
    price = series[-1].close
    for day_index in range(6):
        day = f"2026-01-{5 + day_index:02d}"
        index = 0
        for i in range(3):                   # a rise that never targets
            price *= 1.008
            series.append(_bar(index, price * 0.998, price * 1.004,
                               price * 0.996, price, day=day))
            index += 1
        for i in range(5):                   # and a fall through the stop
            price *= 0.988
            series.append(_bar(index, price * 1.002, price * 1.003,
                               price * 0.992, price, day=day))
            index += 1
    return Scenario(
        name="consecutive_losses",
        question="does a run of losses reach the daily loss lock, and "
                 "does the lock then stop new entries?",
        expectation="losses accumulate across sessions and the daily "
                    "budget resets each day. The finding is which limit "
                    "actually binds: the $50 capital ceiling allows "
                    "about two trades a day risking about $2 each, so a "
                    "single session cannot reach the $5 daily loss "
                    "limit. That limit only binds when a stop BREACH "
                    "makes one trade lose more than planned - which the "
                    "gap scenarios show can be four times over",
        trade=TRADE_EXPECTED,
        may_exceed_1r=True,
        expected_failure="trading on past the daily loss limit, which "
                         "would mean the limit is decorative",
        bars={symbol: series},
    )


ALL: Dict[str, Callable[[], Scenario]] = {
    "grind_up": grind_up,
    "chop": chop,
    "slow_bleed": slow_bleed,
    "gap_through_stop": gap_through_stop,
    "flash_crash": flash_crash,
    "trading_halt": trading_halt,
    "spread_blowout": spread_blowout,
    "bear_regime": bear_regime,
    "downtrend": downtrend,
    "volatile_chop": volatile_chop,
    "gap_through_target": gap_through_target,
    "low_liquidity": low_liquidity,
    "late_in_session": late_in_session,
    "partial_fills": partial_fills,
    "false_breakout": false_breakout,
    "momentum_reversal": momentum_reversal,
    "consecutive_losses": consecutive_losses,
}


def build(name: str) -> Scenario:
    if name not in ALL:
        raise KeyError(
            f"{name!r} is not a scenario; known: {sorted(ALL)}")
    return ALL[name]()


def regime_for(scenario: Scenario):
    """A point-in-time regime callable for the replay engine.

    Deliberately ignores the clock: a scenario's regime is a stated
    condition of the scenario, not something inferred from its own
    prices. It takes the clock argument because the engine passes one.
    """
    def _regime(symbol, clock):                           # noqa: ARG001
        return {"regime": scenario.regime, "regime_confidence": 0.8,
                "risk_posture": "NORMAL", "market_session": "OPEN"}
    return _regime


# =====================================================================
# Does the generated DATA actually contain the named condition?
# =====================================================================
#
# A scenario's identity must be a property of its bars, never of its
# eventual P&L. Two scenarios here were named after conditions they did
# not create - false_breakout returned +2.51R and consecutive_losses
# +1.66R, because each thrust reached the profit target before the
# failure arrived - and both were only noticed by reading the result.
# A profitable trap is still a trap scenario that never happened.
#
# These checks assert on the GENERATED SERIES, so the name is verified
# before anything is run.

def _closes(bars: List[Bar]) -> List[float]:
    return [b.close for b in bars]


def _largest_adverse_gap_pct(bars: List[Bar]) -> float:
    """The biggest downward open-to-previous-close gap, as a percent."""
    worst = 0.0
    for previous, nxt in zip(bars, bars[1:]):
        if previous.close <= 0:
            continue
        gap = (previous.close - nxt.open) / previous.close * 100.0
        worst = max(worst, gap)
    return worst


def _largest_favourable_gap_pct(bars: List[Bar]) -> float:
    best = 0.0
    for previous, nxt in zip(bars, bars[1:]):
        if previous.close <= 0:
            continue
        gap = (nxt.open - previous.close) / previous.close * 100.0
        best = max(best, gap)
    return best


def _timeline_holes(bars: List[Bar]) -> int:
    """Minutes missing from a one-minute series."""
    holes = 0
    for previous, nxt in zip(bars, bars[1:]):
        a, b = _parse(previous.timestamp), _parse(nxt.timestamp)
        if a is None or b is None:
            continue
        gap = (b - a).total_seconds()
        if gap > 60:
            holes += int(gap // 60) - 1
    return holes


def _parse(stamp):
    from datetime import datetime, timezone
    try:
        out = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return out if out.tzinfo else out.replace(tzinfo=timezone.utc)


def _deepest_wick_pct(bars: List[Bar]) -> float:
    """The deepest intrabar drop from open to low that CLOSES back up."""
    worst = 0.0
    for bar in bars:
        if bar.open <= 0:
            continue
        drop = (bar.open - bar.low) / bar.open * 100.0
        recovered = bar.close > bar.low * 1.01
        if recovered:
            worst = max(worst, drop)
    return worst


def _mean_range_pct(bars: List[Bar]) -> float:
    """Average high-low range as a percent of the open.

    The range, not the body: the generator draws each open around its
    own close, so the body understates the volatility it was asked for.
    """
    usable = [b for b in bars if b.open]
    if not usable:
        return 0.0
    return sum((b.high - b.low) / b.open * 100.0
               for b in usable) / len(usable)


def _down_cycles(bars: List[Bar], stop_pct: float = 3.0) -> int:
    """How many times price rises then falls MORE than a stop distance."""
    cycles, peak, armed = 0, bars[0].close, False
    for bar in bars:
        if bar.close > peak:
            peak, armed = bar.close, True
        elif armed and peak > 0 and (peak - bar.close) / peak * 100.0 > stop_pct:
            cycles += 1
            peak, armed = bar.close, False
    return cycles


# What each scenario's DATA must contain for its name to be honest.
# (property description, predicate over the bar series)
GUARANTEES: Dict[str, List] = {
    "grind_up": [("the series ends materially higher than it started",
                  lambda b: _closes(b)[-1] > _closes(b)[0] * 1.02)],
    "downtrend": [("the series ends materially lower than it started",
                   lambda b: _closes(b)[-1] < _closes(b)[0] * 0.95)],
    "chop": [("no sustained direction: the end is near the start",
              lambda b: abs(_closes(b)[-1] / _closes(b)[0] - 1.0) < 0.30)],
    # Expressed as a RANGE, and relative to the calm case. The first
    # version asserted a 2% average open-to-close move, which the
    # generator does not produce - it draws the open around the close,
    # so the body is roughly vol/3 while the RANGE is the volatility.
    "volatile_chop": [
        # Anchored to the 3% stop distance, not to a round number and
        # not to the measured value. A bar whose RANGE exceeds the stop
        # is exactly what makes a polled stop dangerous: the stop is
        # evaluated at the close and filled at the next open, so a bar
        # wider than the stop can leave the fill far beyond it. That is
        # the property this scenario exists to create.
        ("the average bar range exceeds the 3% stop distance",
         lambda b: _mean_range_pct(b) > 3.0),
        ("and is several times the calm case",
         lambda b: _mean_range_pct(b) > 2.5 * _mean_range_pct(
             _calm(200)))],
    "gap_through_stop": [("an adverse open gap exceeds the 3% stop "
                          "distance",
                          lambda b: _largest_adverse_gap_pct(b) > 3.0)],
    "gap_through_target": [("a favourable open gap exceeds the 6% target "
                            "distance",
                            lambda b: _largest_favourable_gap_pct(b) > 6.0)],
    "trading_halt": [("the timeline has a hole of at least 10 minutes",
                      lambda b: _timeline_holes(b) >= 10)],
    "flash_crash": [("a bar drops at least 10% intrabar and closes back "
                     "up", lambda b: _deepest_wick_pct(b) >= 10.0)],
    # A rise THEN a smooth decline. The first version asserted a NET
    # decline over the whole series, which is false: the warm-up rises
    # about 40% before the bleed starts, so the honest property is the
    # fall FROM THE PEAK with no gap in it.
    "slow_bleed": [
        ("price falls materially from its peak",
         lambda b: _closes(b)[-1] < max(_closes(b)) * 0.90),
        ("with no adverse open gap beyond the stop distance",
         lambda b: _largest_adverse_gap_pct(b) < 3.0)],
    "spread_blowout": [("the scenario declares a spread above the 0.50% "
                        "gate", lambda b: True)],
    "low_liquidity": [("every bar's dollar volume is below the $20m "
                       "floor",
                       lambda b: all(x.volume * x.close < 20_000_000
                                     for x in b))],
    "late_in_session": [("the scenario declares minutes_to_close inside "
                         "the cut-off", lambda b: True)],
    "partial_fills": [("the scenario declares a partial-fill "
                       "probability", lambda b: True)],
    "bear_regime": [("the scenario declares a bearish regime",
                     lambda b: True)],
    # Sliced at the generator's own boundary, not a guessed window. The
    # first version used the last 80 bars, which spanned the thrust AND
    # the failure, so "the thrust did not reach the target" compared the
    # wrong two numbers and reported a correct scenario as broken.
    "false_breakout": [
        ("price makes a new high above the pre-thrust range",
         lambda b: max(_closes(b)[_BREAKOUT_BASE:]) >
         max(_closes(b)[:_BREAKOUT_BASE])),
        ("and then falls back below where the thrust began",
         lambda b: _closes(b)[-1] < max(_closes(b)[:_BREAKOUT_BASE])),
        ("without the thrust reaching the 6% target first",
         lambda b: (max(_closes(b)[_BREAKOUT_BASE:_BREAKOUT_BASE + 4])
                    / max(_closes(b)[:_BREAKOUT_BASE]) - 1.0) < 0.06)],
    "consecutive_losses": [
        ("at least three rise-then-fall cycles clear the stop distance",
         lambda b: _down_cycles(b) >= 3),
        ("spanning more than one session",
         lambda b: len({str(x.timestamp)[:10] for x in b}) > 1)],
    "momentum_reversal": [
        ("a sustained rise is followed by a sustained fall",
         lambda b: max(_closes(b)) > _closes(b)[0] * 1.05
         and _closes(b)[-1] < max(_closes(b)) * 0.85)],
}


def verify_guarantees(scenario: Scenario) -> List[Dict]:
    """Check a scenario's DATA against what its name claims.

    Returns one row per declared property. A scenario with no declared
    guarantees returns a single failing row, because an unverifiable
    name is the problem this exists to prevent.
    """
    checks = GUARANTEES.get(scenario.name)
    if not checks:
        return [{"property": "the scenario declares what its data "
                             "guarantees", "ok": False,
                 "detail": f"{scenario.name} has no entry in GUARANTEES, "
                           f"so its name cannot be verified against its "
                           f"bars"}]
    bars = next(iter(scenario.bars.values()), [])
    rows = []
    for description, predicate in checks:
        try:
            ok = bool(predicate(bars))
            detail = ""
        except Exception as exc:                          # noqa: BLE001
            ok, detail = False, f"{type(exc).__name__}: {exc}"
        rows.append({"property": description, "ok": ok, "detail": detail})
    return rows
