"""
Evidence collection orchestration.

Runs the providers, normalises, deduplicates, aggregates, persists.

Bounded by design. Evidence analysis costs several provider calls per
symbol, so it runs on the scanner's top N - the scanner exists to decide
what is worth that. `EVIDENCE_TOP_N` is configurable and defaults low.

One property governs the whole module: **partial evidence is better than
none**. A provider that times out is recorded as failed and the run
continues with what the others returned. A collection that raised on the
first failure would let an SEC outage erase the news feed's items, and a
result that silently omitted them would be worse still - so the failure
is carried into the result and surfaced as a warning on the output.
"""
from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Dict, List, Optional, Sequence

from ..observability import log_event
from .engine import build_catalyst_result
from .relevance import apply_subject_relevance
from .models import (
    CatalystResult, EvidenceItem, EvidenceRun, SourceClass, utcnow,
)
from .providers.base import EvidenceProvider, ProviderResult

# How far back to look. Older evidence is still stored and still
# retrievable, it simply stops being a candidate for "today's catalyst".
DEFAULT_LOOKBACK_HOURS = 72.0

# How many scanner candidates get evidence analysis.
DEFAULT_TOP_N = 5

# Per-symbol cap per provider.
DEFAULT_ITEM_LIMIT = 20


class EvidenceService:
    """Collects and aggregates evidence for symbols."""

    def __init__(self, providers: Sequence[EvidenceProvider],
                 store=None, top_n: int = DEFAULT_TOP_N,
                 lookback_hours: float = DEFAULT_LOOKBACK_HOURS,
                 item_limit: int = DEFAULT_ITEM_LIMIT,
                 llm_key: Optional[str] = None,
                 name_resolver=None,
                 clock=None, wall_clock=time.time):
        self.providers = list(providers)
        # Resolves a ticker to its registered company name, used to tell
        # "a story about this company" from "a story that mentions it".
        # Optional: without it the relevance check falls back to the
        # ticker alone, which is weaker but still correct.
        self.name_resolver = name_resolver
        self.store = store
        self.top_n = top_n
        self.lookback_hours = lookback_hours
        self.item_limit = item_limit
        self.llm_key = llm_key
        self._wall_clock = wall_clock
        self._clock = clock or (
            lambda: datetime.now(timezone.utc).date().isoformat())

    # --- one symbol ------------------------------------------------------

    def collect(self, symbol: str,
                prior_items: Optional[Sequence[EvidenceItem]] = None
                ) -> CatalystResult:
        """Gather evidence for one symbol from every provider.

        Never raises. Every provider failure becomes a named entry in
        `providers_failed` and a warning on the result, so a consumer can
        tell "no catalyst" from "we could not look".
        """
        symbol = (symbol or "").upper()
        since = self._wall_clock() - self.lookback_hours * 3600.0

        items: List[EvidenceItem] = []
        attempted: List[str] = []
        failed: List[str] = []
        warnings: List[str] = []
        self._last_calls = 0
        self._last_cache_hits = 0

        for provider in self.providers:
            attempted.append(provider.name)
            result: ProviderResult = provider.collect(
                symbol, since=since, limit=self.item_limit)
            self._last_calls += result.calls_made
            self._last_cache_hits += result.cache_hits

            if not result.ok:
                failed.append(provider.name)
                warnings.append(
                    f"{provider.name}: {result.error_type or 'failed'}")
                log_event("evidence_provider_failed", provider=provider.name,
                          symbol=symbol, error=result.error_type)
                continue

            items.extend(result.items)
            warnings.extend(f"{provider.name}: {w}" for w in result.warnings)

        # Discount items that are not actually about this symbol. A news
        # feed tags an article with every ticker it mentions, so a peer's
        # earnings story arrives tagged with ours; without this it would
        # be reported as our catalyst.
        company_name = self._company_name(symbol)
        peer_names = self._peer_names(items, symbol)
        apply_subject_relevance(items, symbol, company_name, peer_names)

        return build_catalyst_result(
            symbol, items, providers_attempted=attempted,
            providers_failed=failed, prior_items=prior_items,
            warnings=warnings)

    # --- a batch ---------------------------------------------------------

    def run(self, symbols: Sequence[str],
            scanner_run_id: Optional[str] = None,
            signal_run_id: Optional[str] = None,
            session_date: Optional[str] = None) -> EvidenceRun:
        """Collect for a batch, persist, and return the run."""
        started = time.time()
        session_date = session_date or self._clock()
        started_at = utcnow()

        requested = [s.upper() for s in symbols][:self.top_n]
        run = EvidenceRun(
            evidence_run_id=EvidenceRun.make_id(session_date, started_at),
            session_date=session_date, started_at=started_at,
            scanner_run_id=scanner_run_id, signal_run_id=signal_run_id,
            requested_count=len(requested),
        )

        log_event("evidence_collection_started",
                  evidence_run_id=run.evidence_run_id,
                  scanner_run_id=scanner_run_id, symbols=len(requested))

        try:
            for symbol in requested:
                prior = self._prior_items(symbol)
                result = self.collect(symbol, prior_items=prior)
                run.results.append(result)
                run.provider_calls += getattr(self, "_last_calls", 0)
                run.cache_hits += getattr(self, "_last_cache_hits", 0)

                run.items_retrieved += result.total_evidence_count
                run.items_normalized += result.total_evidence_count
                run.duplicates_removed += result.duplicates_collapsed
                run.primary_source_items += result.primary_source_count
                if result.has_active_catalyst:
                    run.catalysts_created += 1
                if result.providers_failed:
                    run.error_count += 1
                    for name in result.providers_failed:
                        run.provider_failures[name] = \
                            run.provider_failures.get(name, 0) + 1
                run.evaluated_count += 1

                for item in result.items:
                    if str(item.llm_status) == "SUCCEEDED":
                        run.llm_enrichments += 1
                    elif str(item.llm_status) in ("UNAVAILABLE", "REJECTED"):
                        run.llm_failures += 1

            run.completed_at = utcnow()
            run.duration_seconds = round(time.time() - started, 3)

            if self.store is not None:
                self.store.save_run(run)
                log_event("evidence_result_persisted",
                          evidence_run_id=run.evidence_run_id,
                          results=len(run.results))

            log_event("evidence_collection_completed",
                      evidence_run_id=run.evidence_run_id,
                      requested=run.requested_count,
                      evaluated=run.evaluated_count,
                      items=run.items_normalized,
                      duplicates_removed=run.duplicates_removed,
                      catalysts=run.catalysts_created,
                      primary_share=(round(run.primary_source_share, 3)
                                     if run.primary_source_share is not None
                                     else None),
                      provider_calls=run.provider_calls,
                      cache_hits=run.cache_hits,
                      provider_failures=run.provider_failures,
                      llm_enrichments=run.llm_enrichments,
                      llm_failures=run.llm_failures,
                      duration_seconds=run.duration_seconds)
        except Exception as e:
            run.error = f"{type(e).__name__}: {e}"
            run.completed_at = utcnow()
            log_event("evidence_collection_failed",
                      evidence_run_id=run.evidence_run_id,
                      error=type(e).__name__)
            raise

        return run

    def _company_name(self, symbol: str) -> Optional[str]:
        """Registered name for a ticker, best-effort.

        A failure here only weakens the relevance check; it must never
        stop evidence collection.
        """
        if self.name_resolver is None:
            return None
        try:
            return self.name_resolver(symbol)
        except Exception:
            return None

    def _peer_names(self, items: Sequence[EvidenceItem],
                    symbol: str) -> Dict[str, str]:
        """Names for the other tickers an article is tagged with.

        Needed to say "this headline is about Micron" rather than only
        "this headline is not about Apple". Resolution is best-effort and
        capped, because an article tagged with nine tickers must not cost
        nine lookups.
        """
        if self.name_resolver is None:
            return {}
        peers: Dict[str, str] = {}
        seen = 0
        for item in items:
            for other in item.symbols:
                if other.upper() == symbol.upper() or other in peers:
                    continue
                if seen >= 12:
                    return peers
                seen += 1
                try:
                    peers[other] = self.name_resolver(other)
                except Exception:
                    peers[other] = ""
        return peers

    def _prior_items(self, symbol: str) -> List[EvidenceItem]:
        """Previously stored evidence, used only to assess novelty.

        Read-only and best-effort: a store failure must not stop
        collection, it only means novelty is assessed without history.
        """
        if self.store is None:
            return []
        try:
            rows = self.store.evidence_for_symbol(symbol, limit=40)
        except Exception:
            return []
        out: List[EvidenceItem] = []
        for row in rows:
            try:
                out.append(_item_from_dict(row))
            except Exception:
                continue
        return out

    def run_for_scanner(self, scanner_run, top_n: Optional[int] = None,
                        signal_run_id: Optional[str] = None) -> EvidenceRun:
        """Enrich a scanner run's top candidates, in rank order."""
        limit = top_n if top_n is not None else self.top_n
        candidates = sorted(getattr(scanner_run, "candidates", []),
                            key=lambda c: c.rank)[:limit]
        return self.run(
            [c.symbol for c in candidates],
            scanner_run_id=getattr(scanner_run, "scanner_run_id", None),
            signal_run_id=signal_run_id,
            session_date=getattr(scanner_run, "session_date", None),
        )


def _item_from_dict(row: Dict) -> EvidenceItem:
    """Rebuild a stored item well enough for novelty comparison."""
    from .models import Direction, EvidenceSource, EvidenceType

    source_row = row.get("source") or {}
    item = EvidenceItem(
        evidence_id=row.get("evidence_id", ""),
        symbol=row.get("symbol", ""),
        source=EvidenceSource(
            provider=source_row.get("provider", ""),
            publisher=source_row.get("publisher", ""),
            source_class=SourceClass(source_row.get("source_class", "UNKNOWN")),
            url=source_row.get("url", ""),
            document_id=source_row.get("document_id", "")),
        headline=row.get("headline", ""),
        summary=row.get("summary", ""),
    )
    try:
        item.evidence_type = EvidenceType(row.get("type", "OTHER"))
    except ValueError:
        pass
    try:
        item.direction = Direction(row.get("direction", "NEUTRAL"))
    except ValueError:
        pass
    item.published_at = row.get("published_at")
    item.age_hours = row.get("age_hours")
    item.materiality = row.get("materiality") or 0.0
    item.novelty = row.get("novelty") if row.get("novelty") is not None else 1.0
    item.duplicate_group_id = row.get("duplicate_group_id")
    return item
