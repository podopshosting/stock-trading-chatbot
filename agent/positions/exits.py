"""
The exit engine.

Pure functions. Given a position, a price and a context, decide whether
to close it and why. No I/O, no clock reads beyond what is passed in, so
the same inputs always give the same answer and a replay can be trusted.

Two rules shape everything here:

1. **Missing data means exit, not hold.** If the engine cannot evaluate
   a position - no quote, a stale quote, an unreadable halt state - it
   cannot know whether the stop has been breached. Holding through that
   is taking unbounded risk in exchange for nothing. The fail-closed
   direction for an OPEN position is out, which is the opposite of the
   fail-closed direction for a new entry.

2. **Protective exits are never gated.** A posture that forbids new
   trades must not forbid closing one.
"""
from __future__ import annotations

from typing import Dict, List, Optional, Tuple

from ..observability import log_event
from .models import (
    EXIT_PRIORITY, PROTECTIVE_REASONS, ExitIntent, ExitReason,
    ManagedPosition, PositionState, StopMechanism,
)

CONFIG_VERSION = "exits-v1.0.0"

# A quote older than this cannot be trusted to evaluate a stop.
MAX_QUOTE_AGE_SECONDS = 120.0

# Trailing stops only begin once the trade has earned some room. Trailing
# from the first tick converts normal noise into an exit and guarantees
# the strategy never holds a winner.
TRAILING_ACTIVATION_PCT = 1.0


class ExitContext:
    """Everything the engine needs that is not on the position itself."""

    def __init__(self,
                 price: Optional[float] = None,
                 quote_age_seconds: Optional[float] = None,
                 minutes_to_close: Optional[float] = None,
                 market_session: str = "UNKNOWN",
                 global_halt: bool = False,
                 halt_state_readable: bool = True,
                 daily_loss_breached: bool = False,
                 thesis_invalidated: bool = False,
                 thesis_detail: str = "",
                 minutes_held: Optional[float] = None,
                 broker_divergence: bool = False,
                 now: Optional[str] = None):
        self.price = price
        self.quote_age_seconds = quote_age_seconds
        self.minutes_to_close = minutes_to_close
        self.market_session = market_session
        self.global_halt = global_halt
        self.halt_state_readable = halt_state_readable
        self.daily_loss_breached = daily_loss_breached
        self.thesis_invalidated = thesis_invalidated
        self.thesis_detail = thesis_detail
        self.minutes_held = minutes_held
        self.broker_divergence = broker_divergence
        self.now = now

    def as_dict(self) -> Dict:
        return {
            "price": self.price,
            "quote_age_seconds": self.quote_age_seconds,
            "minutes_to_close": self.minutes_to_close,
            "market_session": self.market_session,
            "global_halt": self.global_halt,
            "halt_state_readable": self.halt_state_readable,
            "daily_loss_breached": self.daily_loss_breached,
            "thesis_invalidated": self.thesis_invalidated,
            "minutes_held": self.minutes_held,
            "broker_divergence": self.broker_divergence,
        }


def update_high_water(position: ManagedPosition,
                      price: Optional[float]) -> Optional[float]:
    """Track the best price seen. Monotonic; never retreats."""
    if price is None:
        return position.high_water_price
    if position.high_water_price is None or price > position.high_water_price:
        position.high_water_price = price
    return position.high_water_price


def trailing_stop_price(position: ManagedPosition) -> Optional[float]:
    """Where the trailing stop currently sits, or None if inactive.

    Inactive until the trade is up by TRAILING_ACTIVATION_PCT, so normal
    noise around the entry does not trigger an exit.
    """
    pct = position.plan.trailing_stop_pct
    if pct is None or position.high_water_price is None:
        return None
    gain_pct = ((position.high_water_price - position.entry_price)
                / position.entry_price) * 100.0
    if gain_pct < TRAILING_ACTIVATION_PCT:
        return None
    return position.high_water_price * (1.0 - pct / 100.0)


def apply_trailing_stop(position: ManagedPosition) -> bool:
    """Ratchet the hard stop up to the trailing level.

    Uses tighten_stop, so this can only ever raise the stop. If the
    trailing level is below the current stop it is ignored rather than
    applied - the tighter of the two always wins.
    """
    level = trailing_stop_price(position)
    if level is None or level <= position.plan.stop_price:
        return False
    moved = position.tighten_stop(level, reason="TRAILING_STOP")
    if moved:
        log_event("stop_tightened", symbol=position.symbol,
                  position_id=position.position_id,
                  new_stop=round(level, 4),
                  high_water=round(position.high_water_price, 4))
    return moved


def evaluate(position: ManagedPosition,
             context: ExitContext) -> Optional[ExitIntent]:
    """Decide whether to close `position`. None means hold.

    Every reason that fires is collected; the most protective becomes
    the primary. The reasons are kept separate rather than summed into a
    score, so the record can later answer which rule was doing the work.
    """
    if position.state is PositionState.CLOSED:
        return None

    reasons: List[ExitReason] = []
    details: List[str] = []

    # --- unsafe-state exits: these come first and ignore everything ---
    if position.state is PositionState.UNKNOWN or context.broker_divergence:
        reasons.append(ExitReason.BROKER_DIVERGENCE)
        details.append("the broker's view does not match the agent's")

    if context.global_halt:
        reasons.append(ExitReason.GLOBAL_HALT)
        details.append("a global halt is in force")
    elif not context.halt_state_readable:
        # Cannot read the halt state. Assume the worst: an operator may
        # have halted trading and we simply cannot see it.
        reasons.append(ExitReason.GLOBAL_HALT)
        details.append("the halt state could not be read; assuming halted")

    if context.daily_loss_breached:
        reasons.append(ExitReason.DAILY_LOSS_LIMIT)
        details.append("the daily loss limit has been breached")

    # --- price-dependent exits ------------------------------------------
    price = context.price
    stale = (context.quote_age_seconds is not None
             and context.quote_age_seconds > MAX_QUOTE_AGE_SECONDS)

    if price is None or stale:
        # An open position whose price cannot be established is holding
        # unbounded risk. Out is the safe direction here.
        reasons.append(ExitReason.HARD_STOP)
        details.append(
            "no usable price; the stop cannot be evaluated so the "
            "position is closed rather than held blind"
            + (f" (quote {context.quote_age_seconds}s old)" if stale else ""))
    else:
        update_high_water(position, price)
        apply_trailing_stop(position)
        position.current_price = price

        if price <= position.plan.stop_price:
            # Which rule set this level matters for the record.
            trailing = trailing_stop_price(position)
            if (trailing is not None
                    and abs(trailing - position.plan.stop_price) < 1e-9):
                reasons.append(ExitReason.TRAILING_STOP)
                details.append(
                    f"price {price} hit the trailing stop "
                    f"{position.plan.stop_price}")
            else:
                reasons.append(ExitReason.HARD_STOP)
                details.append(
                    f"price {price} hit the stop {position.plan.stop_price}")

        target = position.plan.target_price
        if target is not None and price >= target:
            reasons.append(ExitReason.PROFIT_TARGET)
            details.append(f"price {price} reached the target {target}")

    # --- plan-driven exits ----------------------------------------------
    if context.thesis_invalidated:
        reasons.append(ExitReason.THESIS_INVALIDATED)
        details.append(context.thesis_detail
                       or "the reason for the trade no longer holds")

    max_hold = position.plan.max_hold_minutes
    if (max_hold is not None and context.minutes_held is not None
            and context.minutes_held >= max_hold):
        reasons.append(ExitReason.TIME_STOP)
        details.append(
            f"held {context.minutes_held:.0f}m, limit {max_hold}m")

    flatten_at = position.plan.flatten_before_close_minutes
    if (flatten_at is not None and context.minutes_to_close is not None
            and context.minutes_to_close <= flatten_at):
        reasons.append(ExitReason.END_OF_DAY)
        details.append(
            f"{context.minutes_to_close:.0f}m to the close; this system "
            "does not carry overnight gap risk")

    if not reasons:
        position.last_evaluated_at = context.now
        return None

    primary = _primary(reasons)
    intent = ExitIntent(
        intent_id=ExitIntent.make_id(),
        position_id=position.position_id,
        symbol=position.symbol,
        quantity=position.quantity,
        primary_reason=primary,
        all_reasons=_ordered(reasons),
        detail="; ".join(details),
        reference_price=price,
        expected_pnl=(None if price is None
                      else position.quantity * (price - position.entry_price)),
        protective=primary in PROTECTIVE_REASONS,
        config_version=CONFIG_VERSION,
    )
    position.exit_reasons = intent.all_reasons
    position.last_evaluated_at = context.now
    log_event("exit_intent", symbol=position.symbol,
              position_id=position.position_id,
              primary_reason=str(primary),
              all_reasons=[str(r) for r in intent.all_reasons],
              protective=intent.protective,
              reference_price=price)
    return intent


def _ordered(reasons: List[ExitReason]) -> List[ExitReason]:
    """Deduplicate, keeping priority order. Deterministic."""
    seen = set()
    out = []
    for reason in EXIT_PRIORITY:
        if reason in reasons and reason not in seen:
            seen.add(reason)
            out.append(reason)
    return out


def _primary(reasons: List[ExitReason]) -> ExitReason:
    for reason in EXIT_PRIORITY:
        if reason in reasons:
            return reason
    # Unreachable while every ExitReason appears in EXIT_PRIORITY, which
    # is asserted by a test rather than assumed here.
    return reasons[0]


def stop_gap_disclosure(position: ManagedPosition) -> Dict:
    """State plainly what the stop does and does not guarantee.

    A polled stop is not a stop order. Between evaluations the price can
    move straight through the level, and the fill comes at whatever the
    market offers when the next cycle runs. Reporting a polled stop as
    though it filled at the stop price is the most common way a paper
    record overstates a strategy.
    """
    mechanism = position.plan.stop_mechanism
    interval = position.plan.evaluation_interval_seconds
    if mechanism is StopMechanism.BROKER_RESTING:
        return {
            "mechanism": str(mechanism),
            "guaranteed_trigger": True,
            "guaranteed_fill_price": False,
            "note": ("A resting order triggers without the agent running, "
                     "but still fills at the market, not at the stop "
                     "price. A gap opens below it."),
        }
    return {
        "mechanism": str(mechanism),
        "guaranteed_trigger": False,
        "guaranteed_fill_price": False,
        "evaluation_interval_seconds": interval,
        "note": ("A polled stop is only checked when a cycle runs. If the "
                 "agent is not running, the stop does not exist. Between "
                 "cycles price can pass through it freely, so the realised "
                 "loss may exceed the planned one."),
    }
