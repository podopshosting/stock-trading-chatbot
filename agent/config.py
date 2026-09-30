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


# --- scanner universe ----------------------------------------------------

@dataclass(frozen=True)
class UniverseConfig:
    """Which securities the scanner is even allowed to look at.

    Conservative by construction. Every exclusion here is a decision to
    research less rather than risk researching something untradeable, and
    a symbol the provider merely *returns* is not a symbol this system
    will consider.
    """

    # Price band. The floor keeps out sub-$5 names where a one-cent tick is
    # a large percentage move; the ceiling is generous and exists mainly to
    # stop a data error from dominating a ranking.
    min_price: float = 5.00
    max_price: float = 2000.00

    # Liquidity. Dollar volume rather than share count, because 1M shares
    # of a $6 stock and 1M shares of a $600 stock are not comparable.
    min_avg_daily_volume: float = 500_000
    min_dollar_volume: float = 20_000_000

    # A wide spread is a cost paid on entry and again on exit.
    max_spread_pct: float = 0.50

    # Beyond this a quote cannot support an intraday decision.
    max_data_age_seconds: float = 1800.0

    # Caps on the expensive stages. The cheap stages see everything; only
    # this many symbols reach per-symbol feature work.
    max_universe_size: int = 300
    max_candidates: int = 25

    allow_equities: bool = True
    allow_etfs: bool = True
    allow_otc: bool = False
    allow_penny_stocks: bool = False
    allow_leveraged_etfs: bool = False

    # Exchanges considered ordinary US venues. ARCA, BATS and AMEX list
    # most ETFs; OTC is excluded by allow_otc, not by this list.
    allowed_exchanges: tuple = ("NASDAQ", "NYSE", "ARCA", "BATS", "AMEX")

    # Only us_equity is supported. Options, crypto and futures are out of
    # scope for this system entirely.
    allowed_asset_classes: tuple = ("us_equity",)

    # A symbol must be fractionable to be usable at a $50/day allocation:
    # one whole share of a $600 stock does not fit in the budget.
    require_fractionable: bool = False

    def validate(self) -> None:
        if self.min_price <= 0:
            raise ValueError("min_price must be positive")
        if self.max_price <= self.min_price:
            raise ValueError("max_price must exceed min_price")
        if self.max_spread_pct <= 0:
            raise ValueError("max_spread_pct must be positive")
        if self.max_universe_size < 1 or self.max_candidates < 1:
            raise ValueError("universe and candidate caps must be at least 1")
        if self.max_candidates > self.max_universe_size:
            raise ValueError("max_candidates cannot exceed max_universe_size")
        if not (self.allow_equities or self.allow_etfs):
            raise ValueError("the universe would be empty: no asset type allowed")


@dataclass(frozen=True)
class ScannerWeights:
    """Ranking weights. Must sum to 1.0.

    These rank "worth a closer look", not expected return. Nothing here
    forecasts a price.
    """
    liquidity: float = 0.30
    relative_volume: float = 0.20
    short_term_momentum: float = 0.20
    market_relative_strength: float = 0.15
    vwap_and_range: float = 0.10
    data_quality: float = 0.05

    def total(self) -> float:
        return (self.liquidity + self.relative_volume
                + self.short_term_momentum + self.market_relative_strength
                + self.vwap_and_range + self.data_quality)

    def validate(self) -> None:
        total = self.total()
        if abs(total - 1.0) > 1e-9:
            raise ValueError(f"scanner weights must sum to 1.0, got {total:.6f}")
        for name, value in self.as_dict().items():
            if value < 0:
                raise ValueError(f"weight {name} must not be negative: {value}")

    def as_dict(self) -> Dict[str, float]:
        return {
            "liquidity": self.liquidity,
            "relative_volume": self.relative_volume,
            "short_term_momentum": self.short_term_momentum,
            "market_relative_strength": self.market_relative_strength,
            "vwap_and_range": self.vwap_and_range,
            "data_quality": self.data_quality,
        }


@dataclass(frozen=True)
class RegimeGate:
    """How one market regime changes what the scanner will surface.

    `spread_multiplier` below 1.0 tightens the spread limit; a
    `min_score` above 0 raises the bar a candidate must clear. Long-only
    throughout: a hostile regime makes the scanner pickier, it never
    produces short ideas.
    """
    scan_enabled: bool = True
    min_score: float = 0.0
    spread_multiplier: float = 1.0
    dollar_volume_multiplier: float = 1.0
    momentum_weight_multiplier: float = 1.0
    warning: str = ""


@dataclass(frozen=True)
class ScannerConfig:
    weights: ScannerWeights = field(default_factory=ScannerWeights)
    universe: UniverseConfig = field(default_factory=UniverseConfig)

    intraday_timeframe: str = "5Min"
    intraday_lookback_minutes: int = 180
    adv_lookback_days: int = 20
    snapshot_batch_size: int = 1000     # provider accepts 2000; 1000 leaves headroom
    bars_batch_size: int = 150

    benchmark_symbols: tuple = ("SPY", "QQQ")

    # Never rank a stale quote beside a fresh one without saying so.
    allow_stale_candidates: bool = False
    stale_score_penalty: float = 0.5

    # Per-regime gating. Long-only research in every case.
    regime_gates: Dict[str, RegimeGate] = field(default_factory=lambda: {
        "STRONG_BULLISH": RegimeGate(min_score=0.0, momentum_weight_multiplier=1.15),
        "BULLISH":        RegimeGate(min_score=0.0, momentum_weight_multiplier=1.05),
        "NEUTRAL":        RegimeGate(min_score=0.0, momentum_weight_multiplier=0.90),
        "MIXED":          RegimeGate(min_score=45.0, spread_multiplier=0.80,
                                     momentum_weight_multiplier=0.80,
                                     warning="regime MIXED: stricter threshold applied"),
        "VOLATILE":       RegimeGate(min_score=55.0, spread_multiplier=0.60,
                                     dollar_volume_multiplier=1.50,
                                     momentum_weight_multiplier=0.70,
                                     warning="regime VOLATILE: liquidity and spread "
                                             "limits tightened"),
        "BEARISH":        RegimeGate(min_score=55.0, spread_multiplier=0.80,
                                     momentum_weight_multiplier=0.70,
                                     warning="regime BEARISH: long-side candidates "
                                             "require a stronger showing"),
        "STRONG_BEARISH": RegimeGate(min_score=70.0, spread_multiplier=0.60,
                                     dollar_volume_multiplier=2.00,
                                     momentum_weight_multiplier=0.50,
                                     warning="regime STRONG_BEARISH: long-side "
                                             "research heavily restricted"),
        # Not knowing the regime is not the same as a calm one. Candidates
        # are still produced so the funnel is inspectable, but every one
        # carries the warning and the bar is raised.
        "UNKNOWN":        RegimeGate(min_score=60.0, spread_multiplier=0.70,
                                     momentum_weight_multiplier=0.50,
                                     warning="market regime UNKNOWN: ranking is "
                                             "not regime-informed"),
    })

    def gate_for(self, regime: str) -> RegimeGate:
        return self.regime_gates.get(regime, self.regime_gates["UNKNOWN"])

    def validate(self) -> None:
        self.weights.validate()
        self.universe.validate()
        if self.adv_lookback_days < 2:
            raise ValueError("adv_lookback_days must be at least 2")
        if self.snapshot_batch_size < 1 or self.bars_batch_size < 1:
            raise ValueError("batch sizes must be positive")


# --- persistence ---------------------------------------------------------

@dataclass(frozen=True)
class StorageConfig:
    state_table: str = "stock-agent-dev-state"
    scanner_table: str = "stock-agent-dev-scanner"
    signal_table: str = "stock-agent-dev-signals"
    evidence_table: str = "stock-agent-dev-evidence"
    region: str = "us-east-2"
    alpaca_secret_id: str = "stock-agent/alpaca-paper"


# --- top level -----------------------------------------------------------

@dataclass(frozen=True)
class AgentConfig:
    capital: CapitalConfig = field(default_factory=CapitalConfig)
    regime: RegimeConfig = field(default_factory=RegimeConfig)
    scanner: ScannerConfig = field(default_factory=ScannerConfig)
    storage: StorageConfig = field(default_factory=StorageConfig)

    # Trading is off unless something explicitly turns it on. Phase 1 has
    # no execution path at all, and this default must not be flipped as a
    # convenience.
    trading_enabled: bool = False
    market_timezone: str = "America/New_York"

    def validate(self) -> None:
        self.regime.validate()
        self.scanner.validate()
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
