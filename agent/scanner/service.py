"""
MarketScannerService: one scan, start to finish.

The staged funnel exists to keep provider cost bounded. Measured against
the live API on 2026-09-30:

    /v2/assets                      1 request     14,388 assets
    static filters                  0 requests    reference data only
    batched snapshots (1000/req)   ~13 requests   whole universe priced
    dynamic filters                 0 requests    snapshot already has
                                                  price, volume, bid/ask
                                                  and the session VWAP
    shortlist cap                   0 requests    top N by turnover
    daily bars (ADV) + intraday    ~4 requests    shortlist only

A whole-market scan therefore costs roughly 18 requests against a
200/minute allowance. The expensive per-symbol work only ever sees
`max_universe_size` symbols, so widening the universe cannot make the
scan explode.

This module owns all provider interaction. Scoring, eligibility and
feature code stay pure and provider-agnostic.

**There is no execution path here.** Nothing imports a broker adapter, and
a `Candidate` carries no side, entry, target or stop.
"""
from __future__ import annotations

import time
from collections import Counter
from typing import Dict, List, Optional, Tuple

from ..config import DEFAULT_CONFIG, AgentConfig
from ..observability import log_event
from ..providers.base import ProviderError
from .eligibility import classify_freshness, dynamic_eligibility, static_eligibility
from .features import average_daily_volume, compute_features
from .models import (
    Candidate, DataFreshness, RejectionReason, Rejection, ScanStatus,
    ScannerRun, ScannerSnapshot, utcnow,
)
from .scoring import ScannerScorer, rank_candidates

MAX_REJECTION_SAMPLES = 25


class ScanContext:
    """What the scanner knew about the market when it ran."""

    def __init__(self, market_session: str = "UNKNOWN",
                 regime: str = "UNKNOWN", regime_confidence: float = 0.0,
                 risk_posture: str = "NO_NEW_TRADES",
                 minutes_elapsed: Optional[float] = None,
                 is_open: bool = False):
        self.market_session = market_session
        self.regime = regime
        self.regime_confidence = regime_confidence
        self.risk_posture = risk_posture
        self.minutes_elapsed = minutes_elapsed
        self.is_open = is_open


class MarketScannerService:

    def __init__(self, provider, universe_provider, store,
                 config: AgentConfig = DEFAULT_CONFIG,
                 clock=time.time):
        self.provider = provider
        self.universe_provider = universe_provider
        self.store = store
        self.config = config
        self._clock = clock
        # Counted from the provider's own request counter where it has
        # one. Incrementing once per call site undercounted badly: a
        # paginating batch is many HTTP requests, and the first live scan
        # reported 18 calls against 161 actual requests. A wrong
        # efficiency number is worse than none, because it is the evidence
        # that the scan does not explode.
        self._baseline_requests = self._provider_request_count()
        self._provider_calls = 0
        self._provider_errors = 0
        self._cache_hits = 0
        self._cache_misses = 0

    def _provider_request_count(self) -> Optional[int]:
        """Read the underlying provider's HTTP request counter, through a
        cache wrapper if there is one."""
        for candidate in (self.provider, getattr(self.provider, "inner", None)):
            count = getattr(candidate, "request_count", None)
            if isinstance(count, int):
                return count
        return None

    def _requests_used(self) -> int:
        current = self._provider_request_count()
        if current is None or self._baseline_requests is None:
            return self._provider_calls
        return current - self._baseline_requests

    # -- helpers ----------------------------------------------------------

    def _new_run(self, session_date: str, context: ScanContext) -> ScannerRun:
        started = utcnow()
        gate = self.config.scanner.gate_for(context.regime)
        return ScannerRun(
            scanner_run_id=ScannerRun.make_id(session_date, started),
            session_date=session_date,
            started_at=started,
            market_session=context.market_session,
            market_regime=context.regime,
            regime_confidence=context.regime_confidence,
            risk_posture=context.risk_posture,
            regime_gate={
                "scan_enabled": gate.scan_enabled,
                "min_score": gate.min_score,
                "spread_multiplier": gate.spread_multiplier,
                "dollar_volume_multiplier": gate.dollar_volume_multiplier,
                "momentum_weight_multiplier": gate.momentum_weight_multiplier,
                "warning": gate.warning,
            },
        )

    def _finish(self, run: ScannerRun, status: ScanStatus,
                started_monotonic: float, warning: Optional[str] = None,
                error: Optional[str] = None) -> ScannerRun:
        run.status = status
        run.completed_at = utcnow()
        run.duration_seconds = self._clock() - started_monotonic
        run.provider_calls = self._requests_used()
        run.provider_errors = self._provider_errors
        run.cache_hits = self._cache_hits
        run.cache_misses = self._cache_misses
        run.candidate_count = len(run.candidates)
        if warning:
            run.warnings.append(warning)
        if error:
            run.error = error

        try:
            self.store.save_run(run)
        except Exception as e:
            # A persistence failure must not discard the scan result: the
            # caller still gets the run, with the failure recorded.
            run.warnings.append(f"persistence failed: {str(e)[:160]}")
            log_event("scanner_failed", scanner_run_id=run.scanner_run_id,
                      stage="persist", error=str(e)[:200])

        if status is ScanStatus.COMPLETE:
            log_event("scanner_completed",
                      scanner_run_id=run.scanner_run_id,
                      status=str(status),
                      universe_count=run.universe_count,
                      static_eligible=run.static_eligible_count,
                      shortlist=run.shortlist_count,
                      dynamic_eligible=run.dynamic_eligible_count,
                      candidates=run.candidate_count,
                      rejected=run.rejected_count,
                      stale=run.stale_count,
                      provider_calls=run.provider_calls,
                      duration_seconds=round(run.duration_seconds or 0, 3),
                      regime=run.market_regime)
        else:
            log_event("scanner_failed", scanner_run_id=run.scanner_run_id,
                      status=str(status), error=error or warning or "")
        return run

    # -- provider stages --------------------------------------------------

    def _fetch_snapshots(self, symbols: List[str]
                         ) -> Tuple[Dict[str, ScannerSnapshot], int]:
        """Batched snapshots for the whole static-eligible universe.

        A failed batch degrades only its own symbols: the rest of the
        universe still scans. Dropping every symbol because one batch
        failed would turn a partial outage into a total one.
        """
        cfg = self.config.scanner
        policy = cfg.universe
        out: Dict[str, ScannerSnapshot] = {}
        failed_batches = 0
        now = self._clock()

        for i in range(0, len(symbols), cfg.snapshot_batch_size):
            batch = symbols[i:i + cfg.snapshot_batch_size]
            try:
                quotes = self.provider.get_snapshot(batch)
                self._provider_calls += 1
            except ProviderError as e:
                failed_batches += 1
                self._provider_errors += 1
                log_event("provider_error", operation="get_snapshot",
                          batch_size=len(batch), error=str(e)[:200])
                continue
            except Exception as e:
                failed_batches += 1
                self._provider_errors += 1
                log_event("provider_error", operation="get_snapshot",
                          batch_size=len(batch), error=str(e)[:200])
                continue

            log_event("provider_batch_completed", operation="get_snapshot",
                      requested=len(batch), returned=len(quotes))

            # Cache accounting is per REQUEST. Counting it per symbol made
            # a 13-request scan report 12,427 "misses", which reads as a
            # broken cache rather than one batched fetch.
            batch_from_cache = all(
                (q.provenance is not None and q.provenance.cache_hit)
                for q in quotes.values()
            ) if quotes else False
            if batch_from_cache:
                self._cache_hits += 1
            else:
                self._cache_misses += 1

            for symbol in batch:
                quote = quotes.get(symbol)
                if quote is None:
                    continue
                prov = quote.provenance
                age = None if prov is None else max(0.0, now - prov.retrieved_at)
                out[symbol] = ScannerSnapshot(
                    symbol=symbol,
                    as_of=(prov.as_of if prov else None),
                    retrieved_at=(prov.retrieved_at if prov else None),
                    age_seconds=age,
                    price=quote.price,
                    bid=quote.bid,
                    ask=quote.ask,
                    volume=quote.volume,
                    session_open=quote.open,
                    day_high=quote.high,
                    day_low=quote.low,
                    previous_close=quote.previous_close,
                    vwap=getattr(quote, "vwap", None),
                    provider=(prov.provider if prov else ""),
                    freshness=classify_freshness(age, policy.max_data_age_seconds),
                )
        return out, failed_batches

    def _fetch_shortlist_bars(self, symbols: List[str]
                              ) -> Tuple[Dict[str, List], Dict[str, List]]:
        """Daily bars (for ADV) and intraday bars (for short-window
        returns), for the shortlist only."""
        cfg = self.config.scanner
        daily: Dict[str, List] = {}
        intraday: Dict[str, List] = {}

        for i in range(0, len(symbols), cfg.bars_batch_size):
            batch = symbols[i:i + cfg.bars_batch_size]
            for kind, target, timeframe, limit, lookback in (
                ("daily", daily, "1day", cfg.adv_lookback_days + 2, None),
                # An explicit intraday window: deriving it from limit x
                # slack asked for days of 5-minute bars across the whole
                # shortlist and blew past the page cap, so most symbols
                # came back with no recent bars and their short-window
                # returns were silently missing.
                ("intraday", intraday, cfg.intraday_timeframe.lower(),
                 max(8, cfg.intraday_lookback_minutes // 5),
                 cfg.intraday_lookback_minutes * 60.0),
            ):
                try:
                    kwargs = {"limit": limit}
                    if lookback is not None:
                        kwargs["lookback_seconds"] = lookback
                    bars = self.provider.get_bars_multi(batch, timeframe,
                                                        **kwargs)
                    self._provider_calls += 1
                    target.update(bars)
                except TypeError:
                    # A provider without the lookback parameter.
                    bars = self.provider.get_bars_multi(batch, timeframe,
                                                        limit=limit)
                    self._provider_calls += 1
                    target.update(bars)
                except AttributeError:
                    # Provider has no batch bars: fall back per symbol.
                    for symbol in batch:
                        try:
                            bs = self.provider.get_bars(symbol, timeframe,
                                                        limit=limit)
                            self._provider_calls += 1
                            target[symbol] = list(bs.bars)
                        except Exception:
                            self._provider_errors += 1
                except Exception as e:
                    self._provider_errors += 1
                    log_event("provider_error", operation=f"get_bars_multi:{kind}",
                              batch_size=len(batch), error=str(e)[:200])
        return daily, intraday

    def _benchmark_change(self, snapshots: Dict[str, ScannerSnapshot]
                          ) -> Tuple[Optional[float], str]:
        """Session change for the first available benchmark."""
        for symbol in self.config.scanner.benchmark_symbols:
            snap = snapshots.get(symbol)
            if snap and snap.price and snap.session_open:
                change = (snap.price - snap.session_open) / snap.session_open * 100
                return change, symbol
        return None, ""

    # -- the scan ---------------------------------------------------------

    def run_scan(self, context: ScanContext,
                 session_date: Optional[str] = None) -> ScannerRun:
        from ..state.store import today_market_date
        session_date = session_date or today_market_date(
            self.config.market_timezone)
        started_monotonic = self._clock()
        cfg = self.config.scanner
        policy = cfg.universe
        gate = cfg.gate_for(context.regime)

        run = self._new_run(session_date, context)
        log_event("scanner_started", scanner_run_id=run.scanner_run_id,
                  session_date=session_date, market_session=context.market_session,
                  regime=context.regime)

        if gate.warning:
            run.warnings.append(gate.warning)
            log_event("regime_gate_applied", scanner_run_id=run.scanner_run_id,
                      regime=context.regime, min_score=gate.min_score,
                      spread_multiplier=gate.spread_multiplier)

        if not gate.scan_enabled:
            return self._finish(run, ScanStatus.SCAN_DISABLED, started_monotonic,
                                warning=f"scanning disabled for regime "
                                        f"{context.regime}")

        # The market being shut is a normal outcome, not a failure.
        if not context.is_open:
            return self._finish(
                run, ScanStatus.MARKET_CLOSED, started_monotonic,
                warning=f"market session {context.market_session}: no scan")

        # Stage 1: universe
        try:
            universe = self.universe_provider.list_symbols()
            self._provider_calls += self.universe_provider.request_count()
        except Exception as e:
            return self._finish(run, ScanStatus.PROVIDER_ERROR, started_monotonic,
                                error=f"universe unavailable: {str(e)[:200]}")

        run.universe_count = len(universe)
        if not universe:
            return self._finish(run, ScanStatus.EMPTY_UNIVERSE, started_monotonic,
                                warning="universe provider returned nothing")

        # Stage 2: static filters (no provider calls)
        rejection_counts: Counter = Counter()
        rejection_samples: List[Rejection] = []
        eligible_securities = []

        for security in universe:
            ok, reasons = static_eligibility(security, policy)
            if ok:
                eligible_securities.append(security)
                continue
            rejection_counts.update(reasons)
            if len(rejection_samples) < MAX_REJECTION_SAMPLES:
                rejection_samples.append(
                    Rejection(security.symbol, reasons, "static"))

        run.static_eligible_count = len(eligible_securities)
        # Aggregate counts only: one log line per rejected symbol would be
        # ~13,000 CloudWatch entries per scan.
        log_event("symbol_rejected", scanner_run_id=run.scanner_run_id,
                  stage="static", rejected=len(universe) - len(eligible_securities),
                  reason_counts=dict(rejection_counts))

        if not eligible_securities:
            run.rejected_count = sum(rejection_counts.values())
            run.rejection_reason_counts = dict(rejection_counts)
            return self._finish(run, ScanStatus.EMPTY_UNIVERSE, started_monotonic,
                                warning="no symbol passed static eligibility")

        # Stage 3: batched snapshots for everything that survived
        symbols = [s.symbol for s in eligible_securities]
        for benchmark in cfg.benchmark_symbols:
            if benchmark not in symbols:
                symbols.append(benchmark)

        snapshots, failed_batches = self._fetch_snapshots(symbols)
        if not snapshots:
            run.rejected_count = sum(rejection_counts.values())
            run.rejection_reason_counts = dict(rejection_counts)
            return self._finish(run, ScanStatus.PROVIDER_ERROR, started_monotonic,
                                error="no snapshot data returned")
        if failed_batches:
            run.warnings.append(
                f"{failed_batches} snapshot batch(es) failed; scanned the rest")

        # Stage 4: dynamic filters, then cap the shortlist by turnover
        passing: List[ScannerSnapshot] = []
        for security in eligible_securities:
            snap = snapshots.get(security.symbol)
            if snap is None:
                rejection_counts[RejectionReason.MISSING_QUOTE.value] += 1
                if len(rejection_samples) < MAX_REJECTION_SAMPLES:
                    rejection_samples.append(Rejection(
                        security.symbol,
                        [RejectionReason.MISSING_QUOTE.value], "dynamic"))
                continue

            if snap.freshness is DataFreshness.STALE:
                run.stale_count += 1
                log_event("snapshot_stale", symbol=snap.symbol,
                          age_seconds=snap.age_seconds)
            elif snap.freshness is DataFreshness.MISSING:
                run.missing_count += 1

            ok, reasons = dynamic_eligibility(
                snap, policy,
                spread_multiplier=gate.spread_multiplier,
                dollar_volume_multiplier=gate.dollar_volume_multiplier,
                allow_stale=cfg.allow_stale_candidates,
            )
            if ok:
                passing.append(snap)
            else:
                rejection_counts.update(reasons)
                if len(rejection_samples) < MAX_REJECTION_SAMPLES:
                    rejection_samples.append(
                        Rejection(snap.symbol, reasons, "dynamic"))

        run.dynamic_eligible_count = len(passing)

        # Most liquid first, so the cap keeps the names most worth the
        # expensive per-symbol work.
        passing.sort(key=lambda s: -(s.dollar_volume or 0))
        over_cap = max(0, len(passing) - policy.max_universe_size)
        if over_cap:
            rejection_counts[RejectionReason.UNIVERSE_CAP_REACHED.value] += over_cap
        shortlist = passing[:policy.max_universe_size]
        run.shortlist_count = len(shortlist)

        # Stage 5: per-symbol bars for the shortlist only
        shortlist_symbols = [s.symbol for s in shortlist]
        daily_bars, intraday_bars = self._fetch_shortlist_bars(shortlist_symbols)

        for snap in shortlist:
            bars = daily_bars.get(snap.symbol) or []
            snap.avg_daily_volume = average_daily_volume(
                bars, lookback=cfg.adv_lookback_days)
            if bars:
                snap.previous_volume = bars[-1].volume

        benchmark_change, benchmark_symbol = self._benchmark_change(snapshots)

        # Stage 6: features, scoring, ranking
        scorer = ScannerScorer(
            weights=cfg.weights,
            momentum_weight_multiplier=gate.momentum_weight_multiplier,
            stale_score_penalty=cfg.stale_score_penalty,
        )
        timeframe_minutes = 5 if "5" in cfg.intraday_timeframe else 1

        candidates: List[Candidate] = []
        for snap in shortlist:
            features = compute_features(
                snap,
                intraday_bars=intraday_bars.get(snap.symbol),
                timeframe_minutes=timeframe_minutes,
                benchmark_change_pct=benchmark_change,
                benchmark_symbol=benchmark_symbol,
                minutes_elapsed=context.minutes_elapsed,
            )
            score, detail = scorer.score(snap, features)

            warnings: List[str] = []
            if snap.freshness is DataFreshness.STALE:
                warnings.append(
                    f"quote is stale ({snap.age_seconds:.0f}s old); score "
                    f"penalised by {cfg.stale_score_penalty:g}x"
                )
            if gate.warning:
                warnings.append(gate.warning)
            if features.relative_volume is None:
                warnings.append("relative volume unavailable (no ADV or "
                                "unknown elapsed session)")

            if score < gate.min_score:
                rejection_counts[RejectionReason.BELOW_REGIME_THRESHOLD.value] += 1
                if len(rejection_samples) < MAX_REJECTION_SAMPLES:
                    rejection_samples.append(Rejection(
                        snap.symbol,
                        [RejectionReason.BELOW_REGIME_THRESHOLD.value],
                        "regime_gate"))
                continue

            candidate = Candidate(
                candidate_id=Candidate.make_id(run.scanner_run_id, snap.symbol),
                scanner_run_id=run.scanner_run_id,
                symbol=snap.symbol,
                timestamp=utcnow(),
                price=snap.price,
                spread_pct=snap.spread_pct,
                dollar_volume=snap.dollar_volume,
                freshness=snap.freshness,
                features=features,
                component_scores=detail,
                scanner_score=score,
                market_regime=context.regime,
                regime_confidence=context.regime_confidence,
                risk_posture=context.risk_posture,
                reasons=self._describe(snap, features),
                warnings=warnings,
                provider=snap.provider,
            )
            candidates.append(candidate)

        ranked = rank_candidates(candidates)[:policy.max_candidates]
        run.candidates = ranked
        run.rejected_count = sum(rejection_counts.values())
        run.rejection_reason_counts = dict(rejection_counts)
        run.rejection_samples = [r.as_dict() for r in rejection_samples]

        if ranked:
            scores = [c.scanner_score for c in ranked]
            run.score_distribution = {
                "count": len(scores),
                "max": max(scores),
                "min": min(scores),
                "mean": round(sum(scores) / len(scores), 2),
            }
            log_event("candidate_ranked", scanner_run_id=run.scanner_run_id,
                      count=len(ranked), top_symbol=ranked[0].symbol,
                      top_score=ranked[0].scanner_score)

        return self._finish(run, ScanStatus.COMPLETE, started_monotonic)

    # -- explanation ------------------------------------------------------

    @staticmethod
    def _describe(snapshot: ScannerSnapshot, features) -> List[str]:
        """Human-readable reasons. Descriptive only - nothing here is an
        instruction."""
        reasons: List[str] = []
        if snapshot.dollar_volume and snapshot.dollar_volume >= 100_000_000:
            reasons.append("HIGH_TURNOVER")
        elif snapshot.dollar_volume and snapshot.dollar_volume >= 20_000_000:
            reasons.append("GOOD_LIQUIDITY")
        if snapshot.spread_pct is not None and snapshot.spread_pct <= 0.05:
            reasons.append("TIGHT_SPREAD")
        if features.above_vwap is True:
            reasons.append("ABOVE_VWAP")
        elif features.above_vwap is False:
            reasons.append("BELOW_VWAP")
        if features.relative_volume and features.relative_volume >= 1.5:
            reasons.append("ELEVATED_RELATIVE_VOLUME")
        if features.return_15m is not None and features.return_15m >= 0.5:
            reasons.append("STRONG_15M_MOMENTUM")
        elif features.return_15m is not None and features.return_15m <= -0.5:
            reasons.append("WEAK_15M_MOMENTUM")
        if features.market_relative_strength is not None and \
                features.market_relative_strength >= 0.5:
            reasons.append("OUTPERFORMING_BENCHMARK")
        if features.range_position is not None and features.range_position >= 0.8:
            reasons.append("NEAR_HIGH_OF_DAY")
        elif features.range_position is not None and features.range_position <= 0.2:
            reasons.append("NEAR_LOW_OF_DAY")
        return reasons
