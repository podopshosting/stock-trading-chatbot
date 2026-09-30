"""
Is this item actually ABOUT the symbol we asked for?

Found by live validation on 2026-09-30. Asking the news feed for AAPL
returned, among others:

    "Micron's AI Boom Isn't Done yet, Analysts Say"
    "Make Or Break for AI Trade - Micron Earnings Ahead"

Both are tagged with AAPL because Apple is mentioned somewhere in the
body, and both were being reported as Apple's primary catalyst. They are
Micron stories. A reader told "Apple has an active EARNINGS catalyst" on
that basis has been actively misled - worse than being told nothing.

The rule applied here is simple and checkable: **does the headline name
the company we asked about?** A story whose headline names a different
company, while never naming ours, is peer or sector coverage. It stays in
the record - it is real information about the sector - but it cannot
become this symbol's catalyst.

This is a relevance DISCOUNT, never a filter. Nothing is discarded, and
the reason is written onto the item so the reduction is visible rather
than mysterious.
"""
from __future__ import annotations

import re
from typing import Dict, List, Optional, Sequence, Tuple

# Corporate suffixes and filler that carry no identifying power.
_NAME_NOISE = frozenset("""
inc incorporated corp corporation co company ltd limited plc holdings
group the and of technologies technology systems international industries
enterprises solutions services common stock class a b
""".split())

_WORD_RE = re.compile(r"[A-Za-z0-9']+")

# A ticker appearing as a standalone uppercase token.
def _ticker_re(symbol: str) -> re.Pattern:
    return re.compile(rf"\b{re.escape(symbol)}\b")


def name_tokens(company_name: str) -> List[str]:
    """Distinctive words from a registered company name.

    "NVIDIA CORP" -> ["nvidia"]. "Apple Inc." -> ["apple"]. The suffixes
    are dropped because matching on "Inc" would make every headline
    about every company.
    """
    words = _WORD_RE.findall(company_name or "")
    return [w.lower() for w in words
            if len(w) > 2 and w.lower() not in _NAME_NOISE]


def mentions_symbol(text: str, symbol: str,
                    company_name: Optional[str] = None) -> Tuple[bool, str]:
    """Does this text name the company, by ticker or by name?"""
    if not text:
        return False, "no text"
    if _ticker_re(symbol).search(text):
        return True, f"ticker {symbol} appears"
    for token in name_tokens(company_name or ""):
        if re.search(rf"\b{re.escape(token)}\b", text, re.IGNORECASE):
            return True, f"company name '{token}' appears"
    return False, "neither ticker nor company name appears"


def subject_relevance(headline: str, summary: str, symbol: str,
                      tagged_symbols: Sequence[str],
                      company_name: Optional[str] = None,
                      peer_names: Optional[Dict[str, str]] = None
                      ) -> Tuple[float, str]:
    """A multiplier for how much this item is about `symbol`.

    1.00  the headline names this company
    0.30  the headline names a DIFFERENT tagged company and not this one
    0.50  the headline names no tagged company (market or sector piece)

    Returns the multiplier and the reason, which is recorded on the item.
    """
    headline = headline or ""
    named, why = mentions_symbol(headline, symbol, company_name)
    if named:
        return 1.0, f"headline is about {symbol} ({why})"

    # Does the headline name some OTHER company it is tagged with?
    others = [s for s in (tagged_symbols or []) if s.upper() != symbol.upper()]
    for other in others:
        other_named, _ = mentions_symbol(
            headline, other, (peer_names or {}).get(other))
        if other_named:
            return 0.30, (
                f"headline is about {other}, not {symbol}; {symbol} is tagged "
                f"but not the subject")

    # Nobody named: a market-wide or thematic piece.
    if summary:
        named_in_summary, why2 = mentions_symbol(summary, symbol, company_name)
        if named_in_summary:
            return 0.70, (
                f"{symbol} appears in the summary but not the headline, so "
                f"this is likely secondary coverage")

    return 0.50, (
        f"headline names no specific company; treated as market or sector "
        f"coverage rather than news about {symbol}")


def apply_subject_relevance(items, symbol: str,
                            company_name: Optional[str] = None,
                            peer_names: Optional[Dict[str, str]] = None):
    """Discount items that are not about this symbol.

    Primary filings are exempt: an SEC filing by an issuer is definitionally
    about that issuer, whatever its synthetic headline says.
    """
    for item in items:
        if item.source.is_primary:
            continue
        multiplier, reason = subject_relevance(
            item.headline, item.summary, symbol, item.symbols,
            company_name, peer_names)
        if multiplier >= 1.0:
            continue
        item.materiality *= multiplier
        item.classification_reason = (
            (item.classification_reason + "; ") if item.classification_reason
            else "") + f"relevance x{multiplier:.2f}: {reason}"
        if multiplier <= 0.30:
            item.warnings.append(reason)
    return items
