"""
Deduplication and novelty.

The single most important correctness property in this layer.

One press release commonly appears as: the company's own release, a
Reuters story, a Yahoo syndication, two aggregator copies, and an SEC
8-K exhibit. That is ONE event with a few independent provenances. A
system that counted it as six pieces of corroborating evidence would
manufacture conviction out of republication - and the more heavily a
story is syndicated, the more conviction it would manufacture, which is
exactly backwards.

So: deduplicate BEFORE aggregating, and count corroboration separately
from event count.

Nothing is deleted. Duplicates keep their own records and their own
provenance; they are marked non-canonical and excluded from the counts
that imply independence.
"""
from __future__ import annotations

import hashlib
import re
from typing import Dict, Iterable, List, Optional, Sequence, Tuple
from urllib.parse import urlparse, urlunparse

from ..observability import log_event
from .models import EvidenceItem, SourceClass

# Two stories about the same event, published this far apart, are
# treated as candidates for the same group. Syndication is usually
# minutes to hours behind the original.
SYNDICATION_WINDOW_HOURS = 36.0

# Jaccard similarity over significant headline tokens above which two
# headlines are treated as the same story.
HEADLINE_SIMILARITY = 0.60

# How close in time a primary filing and news coverage of it must be to
# be treated as one event. A company files an 8-K and the wires carry it
# within the hour; a day later is a different story about the same topic.
CROSS_SOURCE_WINDOW_HOURS = 6.0

# Words that carry no distinguishing information in a financial headline.
_STOPWORDS = frozenset("""
a an the and or but of for to in on at by with from as is are was were be
been being it its this that these those has have had will would could
should may might can new says say said reports report reported announces
announce announced after before amid over under up down more most
inc corp corporation co ltd plc sa nv group holdings company
""".split())

_TOKEN_RE = re.compile(r"[a-z0-9]+")

# Tracking parameters that differ between syndications of one URL.
_TRACKING_PARAMS = ("utm_", "fbclid", "gclid", "ref", "src", "cmpid",
                    "partner", "yptr", "guccounter", "mc_cid", "mc_eid")


def canonical_url(url: str) -> str:
    """Strip the parts of a URL that differ between syndications.

    Scheme, host case, tracking parameters and trailing slashes vary
    between copies of the same article; the path does not.
    """
    if not url:
        return ""
    try:
        parsed = urlparse(url.strip())
    except ValueError:
        return url.strip().lower()

    host = (parsed.netloc or "").lower()
    if host.startswith("www."):
        host = host[4:]
    if host.startswith("amp."):
        host = host[4:]

    path = (parsed.path or "").rstrip("/").lower()
    if path.endswith("/amp"):
        path = path[:-4]

    query = ""
    if parsed.query:
        kept = [p for p in parsed.query.split("&")
                if p and not any(p.lower().startswith(t)
                                 for t in _TRACKING_PARAMS)]
        query = "&".join(sorted(kept))

    return urlunparse(("", host, path, "", query, ""))


def headline_tokens(headline: str) -> frozenset:
    """Significant lowercase tokens from a headline."""
    tokens = _TOKEN_RE.findall((headline or "").lower())
    return frozenset(t for t in tokens
                     if len(t) > 2 and t not in _STOPWORDS)


def jaccard(a: Iterable, b: Iterable) -> float:
    sa, sb = set(a), set(b)
    if not sa or not sb:
        return 0.0
    union = sa | sb
    return len(sa & sb) / len(union) if union else 0.0


def _hours_apart(a: EvidenceItem, b: EvidenceItem) -> Optional[float]:
    if a.age_hours is None or b.age_hours is None:
        return None
    return abs(a.age_hours - b.age_hours)


def same_story(a: EvidenceItem, b: EvidenceItem) -> Tuple[bool, str]:
    """Do these two items describe the same event?

    Ordered from strongest evidence to weakest, so a definite match is
    never reached by a fuzzy route.
    """
    # 1. The same document from the same provider.
    if (a.source.provider == b.source.provider
            and a.source.document_id and b.source.document_id
            and a.source.document_id == b.source.document_id):
        return True, "same provider document id"

    # 2. The same canonical URL.
    ca, cb = canonical_url(a.source.url), canonical_url(b.source.url)
    if ca and cb and ca == cb:
        return True, "same canonical URL"

    # 3. Near-identical headlines close together in time.
    gap = _hours_apart(a, b)
    if gap is not None and gap > SYNDICATION_WINDOW_HOURS:
        return False, "published too far apart"

    similarity = jaccard(headline_tokens(a.headline), headline_tokens(b.headline))
    if similarity >= HEADLINE_SIMILARITY:
        # Require the entities to overlap too. Two companies can announce
        # structurally identical news on the same morning - "Q3 revenue
        # up 12%" - and those are different events.
        if a.symbol == b.symbol or (set(a.symbols) & set(b.symbols)):
            return True, f"headline similarity {similarity:.2f}"

    # 4. A filing and a story ABOUT that filing.
    #
    # Headline similarity cannot link these: an SEC item reads "NVIDIA
    # CORP: 8-K - Results of Operations" while the story reads "Nvidia
    # raises full-year guidance". They share almost no tokens, so
    # without this rule the filing and the coverage of it are counted as
    # two separate events - which overstates how much independent
    # evidence exists.
    #
    # Deliberately narrow: same symbol, same event type, close in time,
    # and exactly one of the two is a primary source. Two news stories
    # of the same type do NOT merge by this route, so ordinary
    # same-category coverage is still kept apart.
    if (a.symbol == b.symbol
            and a.evidence_type is b.evidence_type
            and a.source.is_primary != b.source.is_primary
            and gap is not None
            and gap <= CROSS_SOURCE_WINDOW_HOURS):
        return True, (f"a primary filing and coverage of the same "
                      f"{a.evidence_type} event {gap:.1f}h apart")

    return False, f"headline similarity {similarity:.2f} below threshold"


def _group_id(members: Sequence[EvidenceItem]) -> str:
    seed = "|".join(sorted(m.evidence_id for m in members))
    return "dup_" + hashlib.sha256(seed.encode()).hexdigest()[:12]


# Provenance ranking for choosing the canonical member. A filing or an
# issuer release outranks a story about it.
_CLASS_RANK = {
    SourceClass.PRIMARY: 3,
    SourceClass.STRUCTURED_NEWS: 2,
    SourceClass.AGGREGATED: 1,
    SourceClass.UNKNOWN: 0,
}


def _canonical_member(members: Sequence[EvidenceItem]) -> EvidenceItem:
    """Best provenance wins; earliest publication breaks the tie.

    Choosing by provenance rather than by recency matters: the
    syndicated copy is often the one that arrives last, and picking it
    would discard the primary source's URL and reliability.
    """
    def key(item: EvidenceItem):
        rank = _CLASS_RANK.get(item.source.source_class, 0)
        age = item.age_hours if item.age_hours is not None else -1.0
        return (-rank, -age if age >= 0 else 0.0)

    return sorted(members, key=key)[0]


def deduplicate(items: Sequence[EvidenceItem]) -> Dict:
    """Group retellings of one event.

    Returns the grouped items plus the counts that must be reported
    separately: how many distinct EVENTS there are, and how many
    INDEPENDENT provenances corroborate each.
    """
    remaining = list(items)
    groups: List[List[EvidenceItem]] = []

    for item in remaining:
        placed = False
        for group in groups:
            matched, reason = same_story(item, group[0])
            if matched:
                group.append(item)
                placed = True
                log_event("evidence_duplicate_detected",
                          symbol=item.symbol,
                          evidence_id=item.evidence_id,
                          matched=group[0].evidence_id,
                          reason=reason)
                break
        if not placed:
            groups.append([item])

    collapsed = 0
    for group in groups:
        gid = _group_id(group)
        canonical = _canonical_member(group)
        for member in group:
            member.duplicate_group_id = gid
            member.canonical_evidence_id = canonical.evidence_id
            member.is_canonical = member.evidence_id == canonical.evidence_id
            if not member.is_canonical:
                collapsed += 1

    canonical_items = [g and _canonical_member(g) for g in groups]
    canonical_items = [c for c in canonical_items if c]

    return {
        "items": list(items),
        "groups": groups,
        "canonical_items": canonical_items,
        "group_count": len(groups),
        "duplicates_collapsed": collapsed,
    }


def independent_source_count(group: Sequence[EvidenceItem]) -> int:
    """How many genuinely distinct provenances back one event.

    Counted by (source_class, provider), NOT by publisher.

    That choice is deliberately conservative. When one press release
    reaches us as four articles from Benzinga, Reuters, Yahoo and
    MarketWatch through a single news feed, we cannot tell which of them
    did independent reporting and which merely republished the wire. The
    feed does not say. Counting four publishers would let syndication
    manufacture corroboration, and the more widely a story is carried the
    more corroborated it would appear - exactly backwards.

    So all copies arriving through one provider count once, and what
    genuinely adds corroboration is a DIFFERENT KIND of source: an SEC
    filing alongside the coverage, or a second provider entirely.

    A company press release plus four syndications plus an 8-K is
    therefore 2 independent sources, not 5.
    """
    seen = set()
    for item in group:
        seen.add((str(item.source.source_class), item.source.provider))
    return len(seen)


# --- novelty -------------------------------------------------------------

def novelty_for(item: EvidenceItem, group: Sequence[EvidenceItem],
                prior_items: Optional[Sequence[EvidenceItem]] = None) -> float:
    """How new this development actually is.

    1.0 is a genuinely new development; 0.0 is a restatement of
    something already known.

    Three things reduce it, and none of them is "how many articles
    mentioned it" - ten republications of an old story must not make it
    look important:

      * an earlier item describing the same event (the group's own age)
      * a similar item seen previously for this symbol
      * age on its own, since yesterday's news is less new than this
        hour's regardless of how it was covered
    """
    novelty = 1.0
    reasons: List[str] = []

    # If the group contains something older than this item, the event was
    # already public before this retelling.
    ages = [m.age_hours for m in group if m.age_hours is not None]
    if ages and item.age_hours is not None:
        oldest = max(ages)
        lead = oldest - item.age_hours
        if lead > 0.5:
            # This item is a follow-up, not the break.
            novelty *= max(0.25, 1.0 - min(1.0, lead / 24.0))
            reasons.append(
                f"the same event was already public {lead:.1f}h earlier")

    # A near-identical headline seen before for this symbol.
    if prior_items:
        tokens = headline_tokens(item.headline)
        best = 0.0
        for prior in prior_items:
            if prior.evidence_id == item.evidence_id:
                continue
            best = max(best, jaccard(tokens, headline_tokens(prior.headline)))
        if best >= HEADLINE_SIMILARITY:
            novelty *= max(0.1, 1.0 - best)
            reasons.append(
                f"a similar story was already recorded (similarity "
                f"{best:.2f})")

    # Age alone.
    if item.age_hours is not None:
        if item.age_hours > 720:          # 30 days
            novelty *= 0.1
        elif item.age_hours > 168:        # 1 week
            novelty *= 0.3
        elif item.age_hours > 24:
            novelty *= 0.6
        elif item.age_hours > 8:
            novelty *= 0.85
    else:
        # No timestamp: novelty cannot be assessed, and assuming it is
        # new would let undated items outrank dated ones.
        novelty *= 0.5
        reasons.append("no publication timestamp, so novelty is uncertain")

    novelty = max(0.0, min(1.0, novelty))
    if reasons:
        item.classification_reason = (
            (item.classification_reason + "; ") if item.classification_reason
            else "") + "novelty reduced: " + "; ".join(reasons)
    return novelty
