"""Evidence source adapters. Each normalises into EvidenceItem."""
from .base import (
    EvidenceProvider, EvidenceProviderError, EvidenceRateLimited,
    EvidenceUnavailable, ProviderResult, SymbolNotCovered,
)
from .news import AlpacaNewsProvider
from .sec import SECProvider

__all__ = [
    "AlpacaNewsProvider", "EvidenceProvider", "EvidenceProviderError",
    "EvidenceRateLimited", "EvidenceUnavailable", "ProviderResult",
    "SECProvider", "SymbolNotCovered",
]
