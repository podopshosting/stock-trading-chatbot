"""
Evidence provider abstraction.

Every source - SEC, a news feed, an earnings calendar, a macro calendar -
normalises into `EvidenceItem` behind one of these. Downstream logic must
never know which API produced a piece of evidence, or adding a source
means editing every consumer.

Failure is a first-class outcome. A provider that cannot be reached
returns a named failure rather than raising, because one source being
down must never destroy valid evidence from another: an SEC outage
should not erase the news feed's items.
"""
from __future__ import annotations

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from ..models import EvidenceItem, SourceClass


class EvidenceProviderError(Exception):
    """Base for provider failures. Never raised out of a collection run."""


class EvidenceUnavailable(EvidenceProviderError):
    """The source could not be reached, or returned nothing usable."""


class EvidenceRateLimited(EvidenceProviderError):
    """Throttled. Distinct from unavailable: it will work again shortly,
    and a cached-but-stale answer is preferable to nothing."""


class SymbolNotCovered(EvidenceProviderError):
    """This provider has no coverage for the symbol - not an error."""


@dataclass
class ProviderResult:
    """What one provider returned, including how it failed.

    Carries the failure rather than raising it so a collection run can
    report "SEC succeeded, news timed out" instead of losing both.
    """
    provider: str
    items: List[EvidenceItem] = field(default_factory=list)
    ok: bool = True
    error: Optional[str] = None
    error_type: Optional[str] = None
    calls_made: int = 0
    cache_hits: int = 0
    duration_seconds: float = 0.0
    warnings: List[str] = field(default_factory=list)

    @classmethod
    def failure(cls, provider: str, exc: Exception,
                calls_made: int = 0) -> "ProviderResult":
        return cls(provider=provider, items=[], ok=False,
                   error=str(exc)[:300], error_type=type(exc).__name__,
                   calls_made=calls_made)

    def as_dict(self) -> Dict:
        return {
            "provider": self.provider,
            "ok": self.ok,
            "error": self.error,
            "error_type": self.error_type,
            "item_count": len(self.items),
            "calls_made": self.calls_made,
            "cache_hits": self.cache_hits,
            "duration_seconds": round(self.duration_seconds, 3),
            "warnings": list(self.warnings),
        }


class EvidenceProvider(ABC):
    """One external source of evidence."""

    name: str = "unknown"
    source_class: SourceClass = SourceClass.UNKNOWN

    @abstractmethod
    def fetch(self, symbol: str, since: Optional[float] = None,
              limit: int = 25) -> List[EvidenceItem]:
        """Return normalised evidence, newest first.

        `since` is an epoch timestamp; implementations should filter
        server-side where the API allows it rather than fetching
        everything and discarding.
        """

    def collect(self, symbol: str, since: Optional[float] = None,
                limit: int = 25) -> ProviderResult:
        """Fetch, converting any failure into a reported one.

        This is what a collection run calls. Nothing escapes it, because
        an exception here would take down every other source in the same
        run.
        """
        started = time.time()
        self._calls = 0
        self._cache_hits = 0
        try:
            items = self.fetch(symbol, since=since, limit=limit)
            return ProviderResult(
                provider=self.name, items=items, ok=True,
                calls_made=getattr(self, "_calls", 0),
                cache_hits=getattr(self, "_cache_hits", 0),
                duration_seconds=time.time() - started,
            )
        except SymbolNotCovered as e:
            # Not a failure: this provider simply has nothing for the
            # symbol. Reporting it as an error would make a normal
            # outcome look like an outage.
            return ProviderResult(
                provider=self.name, items=[], ok=True,
                warnings=[f"no coverage: {e}"],
                calls_made=getattr(self, "_calls", 0),
                duration_seconds=time.time() - started,
            )
        except Exception as e:
            result = ProviderResult.failure(self.name, e,
                                            getattr(self, "_calls", 0))
            result.duration_seconds = time.time() - started
            return result

    def capabilities(self) -> Dict:
        return {"name": self.name, "source_class": str(self.source_class)}
