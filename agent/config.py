"""
Tunable parameters for the trading agent, in one place.

Every number here is a choice, not a discovered truth. They are starting
defaults chosen for explainability, and none has been validated against
out-of-sample results. Treat them as hypotheses to be revised once the
paper record exists, not as settings that are known to work.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field, replace
from typing import Dict


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ[name])
    except (KeyError, TypeError, ValueError):
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


# --- capital and risk (placeholders; the Risk Governor owns enforcement) --

@dataclass(frozen=True)
class CapitalConfig:
    daily_capital_limit: float = 50.00      # configurable up to 100
    max_daily_capital: float = 100.00
    max_trade_risk: float = 2.00
    daily_loss_limit: float = 5.00
    max_concurrent_positions: int = 2
    max_new_positions_per_day: int = 3


# --- market regime -------------------------------------------------------

@dataclass(frozen=True)
class RegimeWeights:
    """Directional weights. These must sum to 1.0.

    Volatility is deliberately absent. Volatility is not directional - a
    violent selloff and a violent rally both raise it - so folding it into
    a directional score would be a category error. It is applied as a
    separate confidence penalty and label override instead.
    """
    spy_trend: float = 0.30
    qqq_trend: float = 0.25
    iwm_trend: float = 0.20
    cross_index_confirmation: float = 0.15
    vwap_alignment: float = 0.10

    def total(self) -> float:
        return (self.spy_trend + self.qqq_trend + self.iwm_trend
                + self.cross_index_confirmation + self.vwap_alignment)

    def validate(self) -> None:
        total = self.total()
        if abs(total - 1.0) > 1e-9:
            raise ValueError(
                f"regime weights must sum to 1.0, got {total:.6f}"
            )
        for name, value in self.as_dict().items():
            if value < 0:
                raise ValueError(f"weight {name} must not be negative: {value}")

    def as_dict(self) -> Dict[str, float]:
        return {
            "spy_trend": self.spy_trend,
            "qqq_trend": self.qqq_trend,
            "iwm_trend": self.iwm_trend,
            "cross_index_confirmation": self.cross_index_confirmation,
            "vwap_alignment": self.vwap_alignment,
        }


@dataclass(frozen=True)
class RegimeThresholds:
    """Score cutoffs on a raw_score in [-1, +1]."""
    strong_bullish: float = 0.60
    bullish: float = 0.20
    bearish: float = -0.20          # scores above this are NEUTRAL
    strong_bearish: float = -0.60

    # Volatility, as annualised realised vol from daily returns.
    vol_elevated: float = 0.22
    vol_extreme: float = 0.40

    # A MIXED label needs genuine disagreement, not mild unevenness:
    # the spread between the strongest and weakest index trend score.
    mixed_dispersion: float = 0.90

    # Data freshness, in seconds, measured from when we fetched it.
    fresh_max_age: float = 300.0
    stale_max_age: float = 1800.0   # beyond this an input is unusable

    # Confidence floors/penalties
    stale_confidence_multiplier: float = 0.5
    min_indices_for_regime: int = 2  # fewer than this -> UNKNOWN

    # Transition hysteresis: suppress noise at a boundary.
    min_score_delta_for_transition: float = 0.10


@dataclass(frozen=True)
class RegimeConfig:
    instruments: tuple = ("SPY", "QQQ", "IWM")
    weights: RegimeWeights = field(default_factory=RegimeWeights)
    thresholds: RegimeThresholds = field(default_factory=RegimeThresholds)
    daily_bars: int = 60            # enough for SMA50 with headroom
    intraday_timeframe: str = "5min"
    intraday_bars: int = 78         # one 6.5h session of 5-minute bars
    sma_short: int = 20
    sma_long: int = 50

    def validate(self) -> None:
        self.weights.validate()
        if self.sma_short >= self.sma_long:
            raise ValueError("sma_short must be shorter than sma_long")
        if self.daily_bars < self.sma_long:
            raise ValueError(
                f"daily_bars ({self.daily_bars}) must cover sma_long "
                f"({self.sma_long})"
            )


# --- persistence ---------------------------------------------------------

@dataclass(frozen=True)
class StorageConfig:
    state_table: str = "stock-agent-dev-state"
    region: str = "us-east-2"
    alpaca_secret_id: str = "stock-agent/alpaca-paper"


# --- top level -----------------------------------------------------------

@dataclass(frozen=True)
class AgentConfig:
    capital: CapitalConfig = field(default_factory=CapitalConfig)
    regime: RegimeConfig = field(default_factory=RegimeConfig)
    storage: StorageConfig = field(default_factory=StorageConfig)

    # Trading is off unless something explicitly turns it on. Phase 1 has
    # no execution path at all, and this default must not be flipped as a
    # convenience.
    trading_enabled: bool = False
    market_timezone: str = "America/New_York"

    def validate(self) -> None:
        self.regime.validate()
        if self.capital.daily_capital_limit > self.capital.max_daily_capital:
            raise ValueError("daily_capital_limit exceeds max_daily_capital")

    @classmethod
    def from_env(cls) -> "AgentConfig":
        cfg = cls(
            capital=CapitalConfig(
                daily_capital_limit=_env_float("AGENT_DAILY_CAPITAL", 50.00),
            ),
            trading_enabled=_env_bool("TRADING_ENABLED", False),
        )
        cfg.validate()
        return cfg

    def with_weights(self, **kw) -> "AgentConfig":
        """Override weights, e.g. for experiments. Validated immediately."""
        weights = replace(self.regime.weights, **kw)
        cfg = replace(self, regime=replace(self.regime, weights=weights))
        cfg.validate()
        return cfg


DEFAULT_CONFIG = AgentConfig()
DEFAULT_CONFIG.validate()
