"""
Peer selection. Structured first; a language model can never add a peer.

A peer is accepted only with at least one STRUCTURED reason of industry
strength (same SIC code, same 3-digit SIC group, or same named industry)
and without being a market-cap outlier. Same sector alone is not enough,
and market-cap similarity alone is never a reason to be a peer.

Rejections are explicit and recorded, so "why is X not a peer" has an
answer.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence

from .classification import sic_group
from .models import CompanyProfile

METHOD_VERSION = "peers-v1.0.0"

STRUCTURED_REASONS = {"SAME_SIC", "SAME_SIC_GROUP", "SAME_SIC_MAJOR",
                      "SAME_INDUSTRY",
                      "SAME_SECTOR", "SIMILAR_MARKET_CAP"}
INDUSTRY_REASONS = {"SAME_SIC", "SAME_SIC_GROUP", "SAME_SIC_MAJOR",
                    "SAME_INDUSTRY"}
CAP_BAND = (0.2, 5.0)        # SIMILAR_MARKET_CAP

# SIC major groups (2 digits) that are genuinely ONE industry, so a
# 2-digit match is real evidence. Everywhere else a 2-digit match is not:
# verified live 2026-10-01, major group 35 put Apple (3571, electronic
# computers) in with Caterpillar and Deere (3531/3523, construction and
# farm machinery), and major group 36 made NVIDIA (3674, semiconductors)
# a peer of General Electric (3600, electrical equipment). Both were
# wrong, and both looked plausible in a list.
#
# This list is deliberately short. Adding to it is a judgement that a
# whole major group competes with itself, which is usually false.
COHESIVE_MAJOR_GROUPS = {
    "20",   # Food and Kindred Products - grain mills, canned goods,
            # dairy, confectionery and beverages genuinely compete
    "60",   # Depository Institutions
    "63",   # Insurance Carriers
}
CAP_OUTLIER = 20.0           # beyond this ratio either way: not a peer


class UnstructuredPeer(ValueError):
    """A peer without a structured reason was offered for persistence."""


@dataclass
class PeerSet:
    subject: str
    peers: List[Dict] = field(default_factory=list)       # symbol, reasons
    rejected: List[Dict] = field(default_factory=list)    # symbol, reasons
    llm_rejected: List[Dict] = field(default_factory=list)
    llm_notes: Dict[str, str] = field(default_factory=dict)
    method: str = METHOD_VERSION
    warnings: List[str] = field(default_factory=list)

    def as_dict(self) -> Dict:
        return dict(self.__dict__)


def validate(ps: PeerSet) -> PeerSet:
    """Run before any persistence. Every persisted peer must carry at least
    one industry-strength structured reason."""
    for p in ps.peers:
        reasons = set(p.get("reasons") or [])
        if not reasons <= STRUCTURED_REASONS or not (reasons & INDUSTRY_REASONS):
            raise UnstructuredPeer(
                f"{p.get('symbol')}: reasons {sorted(reasons)} are not "
                "structured industry evidence")
    return ps


def _evaluate(subject: CompanyProfile, c: CompanyProfile):
    if c.active is False:
        return None, ["INACTIVE_SECURITY"]
    if not (c.sic or c.industry):
        return None, ["INSUFFICIENT_DATA"]
    reasons: List[str] = []
    if subject.sic and c.sic and subject.sic == c.sic:
        reasons.append("SAME_SIC")
    elif sic_group(subject.sic) and sic_group(subject.sic) == sic_group(c.sic):
        reasons.append("SAME_SIC_GROUP")
    elif (sic_group(subject.sic, 2) in COHESIVE_MAJOR_GROUPS
          and sic_group(subject.sic, 2) == sic_group(c.sic, 2)):
        reasons.append("SAME_SIC_MAJOR")
    if (subject.industry and c.industry
            and subject.industry.lower() == c.industry.lower()
            and "SAME_SIC" not in reasons):
        reasons.append("SAME_INDUSTRY")
    if subject.sector and subject.sector == c.sector:
        reasons.append("SAME_SECTOR")
    ratio = None
    if subject.market_cap and c.market_cap:
        ratio = c.market_cap / subject.market_cap
        if CAP_BAND[0] <= ratio <= CAP_BAND[1]:
            reasons.append("SIMILAR_MARKET_CAP")
    if not (set(reasons) & INDUSTRY_REASONS):
        if subject.sector and c.sector and subject.sector != c.sector:
            return None, ["UNRELATED_BUSINESS", "DIFFERENT_INDUSTRY"]
        return None, ["DIFFERENT_INDUSTRY"]
    if ratio is not None and (ratio > CAP_OUTLIER or ratio < 1 / CAP_OUTLIER):
        return None, ["MARKET_CAP_OUTLIER"]
    return reasons, None


def select(subject: CompanyProfile, candidates: Sequence[CompanyProfile],
           max_peers: int = 8) -> PeerSet:
    ps = PeerSet(subject=subject.symbol)
    if not (subject.sic or subject.industry):
        ps.warnings.append("INSUFFICIENT_SUBJECT_CLASSIFICATION")
        return validate(ps)
    scored = []
    for c in candidates:
        if c.symbol == subject.symbol:
            continue
        reasons, why = _evaluate(subject, c)
        if why:
            ps.rejected.append({"symbol": c.symbol, "reasons": why})
            continue
        closeness = (abs(_log_ratio(c.market_cap, subject.market_cap))
                     if c.market_cap and subject.market_cap else 99.0)
        scored.append((-len(reasons), closeness, c.symbol, reasons))
    scored.sort()
    for _n, _cl, sym, reasons in scored[:max_peers]:
        ps.peers.append({"symbol": sym, "reasons": reasons})
    for _n, _cl, sym, reasons in scored[max_peers:]:
        ps.rejected.append({"symbol": sym, "reasons": ["BEYOND_PEER_LIMIT"]})
    return validate(ps)


def _log_ratio(a, b):
    import math
    return math.log(a / b)


def refine_with_llm(ps: PeerSet, suggestions: Sequence[Dict]) -> PeerSet:
    """A language model may explain, annotate and nominate; it may not
    create. Suggestions naming a symbol that is not already a structured
    peer are recorded in `llm_rejected` and never enter `peers`."""
    structured = {p["symbol"] for p in ps.peers}
    for s in suggestions or []:
        sym = str(s.get("symbol", "")).upper()
        if sym in structured:
            if s.get("note"):
                ps.llm_notes[sym] = str(s["note"])[:300]
        else:
            ps.llm_rejected.append({
                "symbol": sym, "reason": "LLM_INVENTED_PEER_REJECTED",
                "note": str(s.get("note", ""))[:300]})
    return validate(ps)
