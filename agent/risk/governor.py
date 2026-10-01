
"""
The Risk Governor.

Absolute veto authority. Every check below can only ADD a rejection code;
nothing removes one, and `RiskDecision.approved` is derived from whether
any code is present. There is no override parameter, no force flag and no
path by which a language model, a prompt or a caller can convert a
rejection into an approval.

The governor is a pure function of (hypothesis, context, limits). It
performs no I/O, reads no clock and fetches no market data, so a decision
can be replayed exactly from its stored snapshots — which is what makes
"why was this trade allowed" answerable months later.

FAIL CLOSED. Missing data is a rejection, never a pass. An unknown spread
is not a tight one; an absent quote age is not a fresh quote. Every
`None` below routes to a reject, because the alternative is that a
provider outage quietly becomes permission to trade.
"""
from __future__ import annotations

from typing import Dict, List, Optional, Sequence, Tuple

from ..observability import log_event
from .models import (
    GlobalHaltState, RejectionCode, RiskContext, RiskDecision, RiskLimits,
    utcnow,
)


class _Rejections:
    """Accumulator. Codes go in; nothing ever comes out."""

    def __init__(self):
        self._codes: List[RejectionCode] = []
        self._reasons: List[str] = []

    def add(self, code: RejectionCode, reason: str) -> None:
        if code not in self._codes:
            self._codes.append(code)
            self._reasons.append(f"{code}: {reason}")

    @property
    def codes(self) -> List[RejectionCode]:
        return list(self._codes)

    @property
    def reasons(self) -> List[str]:
        return list(self._reasons)


def position_size(hypothesis, context: RiskContext,
                  limits: RiskLimits) -> Tuple[float, float, float]:
    """How much capital this idea would take, and what it could lose.

    Sized by RISK, not by conviction: the position is whatever amount
    puts `max_trade_risk` at stake given the stop distance, capped by
    the concentration limit and by what remains of the day's ceiling.

    Sizing on strength instead would mean the most confident-looking
    setups carry the largest losses, which is precisely backwards — the
    setups that feel strongest are the ones that hurt most when wrong.

    Returns (position_value, max_loss, capital_remaining).
    """
    remaining = max(0.0, limits.daily_capital_limit
                    - context.capital_deployed_today)

    stop_pct = getattr(hypothesis, "suggested_stop_distance_pct", None)
    if not stop_pct or stop_pct <= 0:
        return 0.0, 0.0, remaining

    # Capital such that a move of stop_pct against us loses max_trade_risk.
    risk_sized = limits.max_trade_risk / (stop_pct / 100.0)
    value = min(risk_sized, limits.max_position_value, remaining)
    value = max(0.0, value)
    max_loss = value * (stop_pct / 100.0)
    return value, max_loss, remaining


def _check_global_halts(rej: _Rejections, context: RiskContext,
                        halt: Optional[GlobalHaltState]) -> None:
    """Halts come first: nothing else matters if the system is stopped."""
    if halt is not None and halt.halted:
        rej.add(RejectionCode.EMERGENCY_STOP,
                f"a global halt is in force: {halt.reason or 'no reason '
                                              'recorded'}")
    if context.emergency_stop:
        rej.add(RejectionCode.EMERGENCY_STOP,
                context.emergency_stop_reason or "emergency stop engaged")
    if context.daily_risk_lock:
        rej.add(RejectionCode.DAILY_RISK_LOCK,
                "the daily loss limit has been reached; no new exposure")
    if not context.trading_enabled:
        rej.add(RejectionCode.TRADING_DISABLED,
                "trading_enabled is false")
    if not context.execution_available:
        rej.add(RejectionCode.EXECUTION_UNAVAILABLE,
                "no execution path exists")


def _check_session(rej: _Rejections, context: RiskContext,
                   limits: RiskLimits) -> None:
    if context.market_session not in ("OPEN",):
        rej.add(RejectionCode.MARKET_CLOSED,
                f"market session is {context.market_session}")
        return
    minutes = context.minutes_to_close
    if minutes is None:
        # Fail closed: not knowing how long is left is not the same as
        # having plenty of time.
        rej.add(RejectionCode.TOO_LATE_IN_SESSION,
                "time remaining in the session is unknown")
    elif minutes <= limits.no_new_entries_within_minutes_of_close:
        rej.add(RejectionCode.TOO_LATE_IN_SESSION,
                f"only {minutes:.0f} minutes remain; a position opened now "
                f"must be flattened before the close, which is a round trip "
                f"paid for with two spreads")


def _check_hypothesis(rej: _Rejections, hypothesis,
                      limits: RiskLimits) -> None:
    if hypothesis is None:
        rej.add(RejectionCode.HYPOTHESIS_NOT_ACTIONABLE,
                "no hypothesis supplied")
        return
    if not getattr(hypothesis, "is_actionable", False):
        rej.add(RejectionCode.HYPOTHESIS_NOT_ACTIONABLE,
                f"hypothesis is {getattr(hypothesis, 'strategy', 'unknown')}")
    if getattr(hypothesis, "blocking_contradictions", None):
        codes = ", ".join(c.code for c in hypothesis.blocking_contradictions)
        rej.add(RejectionCode.BLOCKING_CONTRADICTION, codes)
    strength = getattr(hypothesis, "hypothesis_strength", 0.0) or 0.0
    if strength < limits.min_hypothesis_strength:
        rej.add(RejectionCode.HYPOTHESIS_TOO_WEAK,
                f"strength {strength:.2f} is below the minimum "
                f"{limits.min_hypothesis_strength:.2f}")
    if str(getattr(hypothesis, "direction", "NONE")) != "LONG":
        rej.add(RejectionCode.NOT_LONG_ONLY,
                f"direction is {getattr(hypothesis, 'direction', 'NONE')}; "
                f"this system is long-only")


def _check_instrument(rej: _Rejections, context: RiskContext,
                      limits: RiskLimits) -> None:
    if context.is_otc and not limits.allow_otc:
        rej.add(RejectionCode.INSTRUMENT_NOT_PERMITTED, "OTC security")
    if context.is_leveraged and not limits.allow_leverage:
        rej.add(RejectionCode.INSTRUMENT_NOT_PERMITTED,
                "leveraged or inverse product")
    if context.security_type not in ("equity", "etf"):
        rej.add(RejectionCode.INSTRUMENT_NOT_PERMITTED,
                f"security type {context.security_type} is not permitted")


def _check_market_quality(rej: _Rejections, context: RiskContext,
                          limits: RiskLimits) -> None:
    # Every one of these fails closed on None. An unknown spread is not
    # a tight spread, and an absent quote age is not a fresh quote.
    if context.price is None:
        rej.add(RejectionCode.STALE_MARKET_DATA, "no price available")
    elif not (limits.min_price <= context.price <= limits.max_price):
        rej.add(RejectionCode.PRICE_OUT_OF_RANGE,
                f"price {context.price:.2f} is outside "
                f"{limits.min_price:.2f}-{limits.max_price:.2f}")

    # Freshness is judged on how old the DATA is, never on how long ago
    # we fetched it. Those differ by the length of the feed delay, and
    # using the fetch age let a quote describing the market fifteen
    # minutes earlier pass a 120-second check.
    if context.source_age_seconds is None:
        rej.add(RejectionCode.STALE_MARKET_DATA,
                "market-data timestamp is unknown, which is not the same "
                "as current"
                + (f" (fetched {context.quote_age_seconds:.0f}s ago)"
                   if context.quote_age_seconds is not None else ""))
    elif context.source_age_seconds > limits.max_quote_age_seconds:
        rej.add(RejectionCode.STALE_MARKET_DATA,
                f"market data is {context.source_age_seconds:.0f}s old, "
                f"limit {limits.max_quote_age_seconds:.0f}s"
                + (f" (feed {context.feed_quality})"
                   if context.feed_quality else ""))

    if context.spread_pct is None:
        rej.add(RejectionCode.SPREAD_TOO_WIDE,
                "spread is unknown, which is not the same as tight")
    elif context.spread_pct > limits.max_spread_pct:
        rej.add(RejectionCode.SPREAD_TOO_WIDE,
                f"spread {context.spread_pct:.2f}% exceeds "
                f"{limits.max_spread_pct:.2f}%")

    if context.dollar_volume is None:
        rej.add(RejectionCode.INSUFFICIENT_LIQUIDITY,
                "dollar volume is unknown")
    elif context.dollar_volume < limits.min_dollar_volume:
        rej.add(RejectionCode.INSUFFICIENT_LIQUIDITY,
                f"dollar volume {context.dollar_volume:,.0f} is below "
                f"{limits.min_dollar_volume:,.0f}")


def _check_exposure(rej: _Rejections, symbol: str, context: RiskContext,
                    limits: RiskLimits) -> None:
    if context.open_positions >= limits.max_concurrent_positions:
        rej.add(RejectionCode.MAX_POSITIONS_REACHED,
                f"{context.open_positions} of "
                f"{limits.max_concurrent_positions} positions already open")
    if context.positions_opened_today >= limits.max_new_positions_per_day:
        rej.add(RejectionCode.MAX_NEW_POSITIONS_REACHED,
                f"{context.positions_opened_today} entries already today, "
                f"limit {limits.max_new_positions_per_day}")
    held = {s.upper() for s in (context.held_symbols or [])}
    if symbol.upper() in held:
        # Adding to a losing position is the single most reliable way to
        # turn a small loss into an account-sized one, and adding to a
        # winner is a different decision that this build does not make.
        rej.add(RejectionCode.ALREADY_HOLDING,
                f"already holding {symbol}")
        if not limits.allow_averaging_down:
            rej.add(RejectionCode.AVERAGING_DOWN_PROHIBITED,
                    "adding to an existing position is prohibited")


def _check_capital(rej: _Rejections, value: float, max_loss: float,
                   remaining: float, context: RiskContext,
                   limits: RiskLimits) -> None:
    if value <= 0:
        rej.add(RejectionCode.INSUFFICIENT_CAPITAL,
                "no capital remains within today's limit, or the idea could "
                "not be sized")
        return
    if value < limits.min_position_value:
        rej.add(RejectionCode.INSUFFICIENT_CAPITAL,
                f"position value {value:.2f} is below the practical minimum "
                f"{limits.min_position_value:.2f}; costs would dominate")
    # Defence in depth: `position_size` already caps at `remaining`, so
    # this is unreachable today. It stays because the cap lives in a
    # different function, and a future change to sizing must not be able
    # to breach the day's ceiling silently. It is exercised directly by
    # tests rather than through `evaluate`, since nothing can currently
    # reach it that way.
    if value > remaining + 1e-9:
        rej.add(RejectionCode.DAILY_CAPITAL_EXCEEDED,
                f"{value:.2f} required, {remaining:.2f} remains of today's "
                f"{limits.daily_capital_limit:.2f}")
    if value > limits.max_position_value + 1e-9:
        rej.add(RejectionCode.POSITION_SIZE_EXCEEDED,
                f"{value:.2f} exceeds the single-name cap "
                f"{limits.max_position_value:.2f}")
    if max_loss > limits.max_trade_risk + 1e-9:
        rej.add(RejectionCode.PER_TRADE_RISK_EXCEEDED,
                f"potential loss {max_loss:.2f} exceeds the per-trade limit "
                f"{limits.max_trade_risk:.2f}")

    # Would this trade, if fully wrong, breach the day's loss limit?
    already_lost = max(0.0, -context.realized_pnl_today)
    if already_lost + max_loss > limits.daily_loss_limit + 1e-9:
        rej.add(RejectionCode.DAILY_RISK_LOCK,
                f"{already_lost:.2f} already lost today; a further "
                f"{max_loss:.2f} would breach the {limits.daily_loss_limit:.2f} "
                f"daily limit")


def _check_events(rej: _Rejections, context: RiskContext) -> None:
    if context.imminent_binary_event:
        rej.add(RejectionCode.IMMINENT_BINARY_EVENT,
                context.imminent_event_detail
                or "a binary event is imminent; the outcome is not a "
                   "function of the setup")


def evaluate(hypothesis, context: RiskContext,
             limits: Optional[RiskLimits] = None,
             halt: Optional[GlobalHaltState] = None) -> RiskDecision:
    """The single entry point. Approve or reject, with reasons.

    Pure: no I/O, no clock, no provider. Every input is supplied and
    snapshotted onto the decision, so the verdict can be replayed
    exactly.
    """
    limits = limits or RiskLimits()
    limits.validate()

    symbol = (getattr(hypothesis, "symbol", "") or "").upper()
    rej = _Rejections()

    _check_global_halts(rej, context, halt)
    _check_session(rej, context, limits)
    _check_hypothesis(rej, hypothesis, limits)
    _check_instrument(rej, context, limits)
    _check_market_quality(rej, context, limits)
    _check_exposure(rej, symbol, context, limits)
    _check_events(rej, context)

    value, max_loss, remaining = position_size(hypothesis, context, limits)
    _check_capital(rej, value, max_loss, remaining, context, limits)

    decided_at = utcnow()
    decision = RiskDecision(
        decision_id=RiskDecision.make_id(symbol, decided_at),
        symbol=symbol,
        hypothesis_id=getattr(hypothesis, "hypothesis_id", None),
        decided_at=decided_at,
        reason_codes=rej.codes,
        reasons=rej.reasons,
        capital_required=value,
        capital_available=remaining,
        max_loss=max_loss,
        position_value=value,
        stop_distance_pct=getattr(hypothesis, "suggested_stop_distance_pct",
                                  None),
        limits_version=limits.version,
        limits_snapshot=limits.as_dict(),
        context_snapshot=context.as_dict(),
    )

    log_event("risk_decision",
              symbol=symbol, approved=decision.approved,
              hypothesis_id=decision.hypothesis_id,
              reason_codes=[str(c) for c in decision.reason_codes],
              capital_required=round(value, 2), max_loss=round(max_loss, 2),
              limits_version=limits.version)
    if decision.requires_halt:
        log_event("risk_halt_observed", symbol=symbol,
                  codes=[str(c) for c in decision.reason_codes])
    return decision


def should_engage_risk_lock(realized_pnl_today: float,
                            unrealized_pnl: float,
                            limits: RiskLimits) -> Tuple[bool, str]:
    """Has the day's loss limit been reached?

    Unrealised losses count. Waiting for a loss to be realised before
    locking would mean the limit is only enforced after the damage is
    already taken, which is the opposite of a limit.
    """
    total = realized_pnl_today + unrealized_pnl
    if total <= -abs(limits.daily_loss_limit):
        return True, (f"total P&L {total:.2f} has reached the daily loss "
                      f"limit of {limits.daily_loss_limit:.2f} "
                      f"(realised {realized_pnl_today:.2f}, unrealised "
                      f"{unrealized_pnl:.2f})")
    return False, ""
