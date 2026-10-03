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

    def as_dict(self) -> Dict:
        series = next(iter(self.bars.values()), [])
        return {
            "name": self.name,
            "question": self.question,
            "expectation": self.expectation,
            "symbols": sorted(self.bars),
            "bars": len(series),
            "spread_pct": self.spread_pct,
            "regime": self.regime,
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
        question="does the agent trade at all under ordinary conditions?",
        expectation="an entry is taken and managed; this is the control "
                    "that proves the harness can produce a trade",
        bars={symbol: _calm(WARMUP + 120)},
    )


def chop(symbol: str = "XYZ") -> Scenario:
    return Scenario(
        name="chop",
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
        question="when price declines smoothly through the stop, is the "
                 "loss close to the planned amount?",
        expectation="the loss should be CLOSE to max_trade_risk, within "
                    "slippage. If it is not, the risk model is wrong "
                    "even without a gap to blame",
        bars={symbol: _bleed_series()},
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
