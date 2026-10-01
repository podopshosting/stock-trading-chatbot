"""
What a body of paper-trading evidence is allowed to prove.

Two things can quietly turn a clean-looking record into a misleading one,
and neither is visible in a P&L number:

  1. The data it traded on. Alpaca's free plan serves the consolidated
     tape 15 minutes late (`delayed_sip`). A fill computed against a
     quote from fifteen minutes ago is a real exercise of the machinery
     and NOT a measurement of what the strategy would have got in the
     market. Counting it as strategy performance would overstate what
     has been demonstrated.

  2. The runtime that produced it. A session that spans a redeploy
     contains results from two different programs. Its totals describe
     neither, and the stored `versions.code_sha` names only the first
     one - so the record would actively misattribute the work.

Both are therefore recorded per session and carried into the readiness
report as an explicit class, rather than left as prose in a document
that the gate never reads.

    REAL_TIME_STRATEGY_EVIDENCE  may support a performance gate
    OPERATIONAL_VALIDATION_ONLY  proves the machinery runs, nothing more
    VOID                         cannot be attributed to any one runtime

Absence of a label is UNKNOWN, which is treated as not-real-time. A
session recorded before the agent knew how to report its feed must not
be promoted to real-time evidence by the fact that nobody wrote it down.
"""
from __future__ import annotations

import enum
from typing import Dict, Iterable, List, Optional, Sequence


class FeedQuality(str, enum.Enum):
    """What one quote actually was, per provider feed and data age.

    The distinction REALTIME_SIP vs REALTIME_IEX matters: IEX is
    real-time but about 2.5% of US volume, so its best bid and offer is
    not the national one. It is usable for diagnostics and must not
    silently count as a consolidated-tape measurement.
    """
    REALTIME_SIP = "REALTIME_SIP"
    REALTIME_IEX = "REALTIME_IEX"
    DELAYED_SIP = "DELAYED_SIP"
    STALE = "STALE"
    UNKNOWN = "UNKNOWN"

    def __str__(self) -> str:
        return self.value


# The only feed quality that may support a claim about the strategy.
STRATEGY_GRADE_FEED = FeedQuality.REALTIME_SIP

# Feeds that are real-time but not the consolidated tape, and the delayed
# tape: both prove the machinery runs and neither measures the strategy.
OPERATIONAL_FEEDS = {FeedQuality.REALTIME_IEX, FeedQuality.DELAYED_SIP}


def classify_feed(feed: Optional[str], source_age_seconds: Optional[float],
                  max_age_seconds: float = 120.0) -> FeedQuality:
    """Classify one quote from its feed and the age of the DATA.

    `source_age_seconds` is the age of the provider's own timestamp, not
    of our fetch. None means the provider gave no timestamp, which is
    UNKNOWN - never real-time, because a feed name alone does not prove
    the data behind it is current.
    """
    name = (feed or "").strip().lower()
    if source_age_seconds is None:
        return FeedQuality.UNKNOWN
    if name == "delayed_sip":
        return FeedQuality.DELAYED_SIP
    if source_age_seconds > max_age_seconds:
        # A real-time feed serving old data is stale, whatever it is
        # called. A halted or thinly traded symbol does this.
        return FeedQuality.STALE
    if name == "sip":
        return FeedQuality.REALTIME_SIP
    if name == "iex":
        return FeedQuality.REALTIME_IEX
    return FeedQuality.UNKNOWN


class DataQuality(str, enum.Enum):
    REAL_TIME = "REAL_TIME"
    DELAYED = "DELAYED"
    MIXED = "MIXED"
    UNKNOWN = "UNKNOWN"

    def __str__(self) -> str:
        return self.value


class EvidenceClass(str, enum.Enum):
    REAL_TIME_STRATEGY_EVIDENCE = "REAL_TIME_STRATEGY_EVIDENCE"
    OPERATIONAL_VALIDATION_ONLY = "OPERATIONAL_VALIDATION_ONLY"
    VOID = "VOID"

    def __str__(self) -> str:
        return self.value


# Only this class may be cited by a gate that claims the strategy works.
STRATEGY_GRADE = EvidenceClass.REAL_TIME_STRATEGY_EVIDENCE


def data_quality_from_feeds(counts: Dict[str, int]) -> DataQuality:
    """Session data quality from per-cycle FeedQuality counts.

    Only REALTIME_SIP counts as REAL_TIME. REALTIME_IEX is real-time but
    not the consolidated tape, so it is reported as DELAYED here rather
    than being promoted: the session-level question is "may this support
    a strategy claim", and the answer for IEX is no. The specific feed
    counts are kept alongside so the reason is never lost.
    """
    counts = {k: v for k, v in (counts or {}).items() if v}
    if not counts:
        return DataQuality.UNKNOWN
    kinds = set(counts)
    if kinds == {str(FeedQuality.REALTIME_SIP)}:
        return DataQuality.REAL_TIME
    if str(FeedQuality.UNKNOWN) in kinds and len(kinds) == 1:
        return DataQuality.UNKNOWN
    if str(FeedQuality.REALTIME_SIP) in kinds or len(kinds) > 1:
        return DataQuality.MIXED
    return DataQuality.DELAYED


def data_quality(realtime_cycles: int = 0, delayed_cycles: int = 0,
                 unknown_cycles: int = 0) -> DataQuality:
    """Derived from per-cycle counts. No setter, no override.

    Any unknown cycle contaminates the session: we cannot say a session
    traded on real-time data if we do not know what some of it traded
    on.
    """
    if unknown_cycles:
        return DataQuality.MIXED if (realtime_cycles or delayed_cycles) \
            else DataQuality.UNKNOWN
    if realtime_cycles and delayed_cycles:
        return DataQuality.MIXED
    if delayed_cycles:
        return DataQuality.DELAYED
    if realtime_cycles:
        return DataQuality.REAL_TIME
    return DataQuality.UNKNOWN


def classify_session(tally) -> Dict:
    """Classify one session's tally. Accepts a SessionTally or a dict.

    Returns the class, the data quality, the code SHAs the session
    actually ran under, and the reasons - so a demotion is always
    explainable rather than mysterious.
    """
    get = (tally.get if isinstance(tally, dict)
           else lambda k, d=None: getattr(tally, k, d))
    shas = list(get("code_shas", None) or [])
    if not shas:
        # Older records predate per-cycle SHA recording. Fall back to the
        # single SHA the tally was opened with, which is the best that
        # record can support - and note that it is a fallback.
        legacy = ((get("versions", None) or {}) or {}).get("code_sha")
        shas = [legacy] if legacy else []
        sha_source = "versions.code_sha (single value; per-cycle SHAs not recorded)"
    else:
        sha_source = "per-cycle record"

    feed_counts = get("feed_quality_counts", None) or {}
    quality = (data_quality_from_feeds(feed_counts) if feed_counts else
               data_quality(get("realtime_data_cycles", 0) or 0,
                            get("delayed_data_cycles", 0) or 0,
                            get("unknown_data_quality_cycles", 0) or 0))
    reasons: List[str] = []

    if len(shas) > 1:
        reasons.append(
            "session spans more than one code SHA (" + ", ".join(shas) +
            "); its totals describe no single runtime")
        return {"evidence_class": str(EvidenceClass.VOID),
                "data_quality": str(quality), "code_shas": shas,
                "sha_source": sha_source, "reasons": reasons,
                "counts_toward_strategy_gates": False}

    if quality is DataQuality.REAL_TIME:
        return {"evidence_class": str(STRATEGY_GRADE),
                "data_quality": str(quality), "code_shas": shas,
                "sha_source": sha_source,
                "reasons": ["every cycle ran on real-time data under one "
                            "runtime"],
                "counts_toward_strategy_gates": True}

    if feed_counts:
        detail = ", ".join(f"{k} x{v}" for k, v in sorted(feed_counts.items())
                           if v)
        reasons.append(f"feeds used: {detail}")
    if str(FeedQuality.REALTIME_IEX) in feed_counts:
        reasons.append("IEX is real-time but about 2.5% of volume, so its "
                       "quotes are not the consolidated tape")
    if quality is DataQuality.DELAYED:
        reasons.append("traded on delayed market data; fills do not "
                       "represent real-time execution")
    elif quality is DataQuality.MIXED:
        reasons.append("some cycles ran on delayed or unrecorded data")
    else:
        reasons.append("the data feed was not recorded, so real-time "
                       "quality cannot be claimed")
    return {"evidence_class": str(EvidenceClass.OPERATIONAL_VALIDATION_ONLY),
            "data_quality": str(quality), "code_shas": shas,
            "sha_source": sha_source, "reasons": reasons,
            "counts_toward_strategy_gates": False}


def classify_evidence(tallies: Sequence,
                      in_progress: int = 0,
                      excluded_void: int = 0) -> Dict:
    """Classify a whole body of evidence.

    The class of the whole is the weakest class in it: one delayed-data
    session in a pool means the pool cannot support a real-time claim.
    An empty body is UNKNOWN - it proves nothing, which is not the same
    as proving the machinery works.
    """
    per = [classify_session(t) for t in tallies]
    void = [p for p in per if p["evidence_class"] == str(EvidenceClass.VOID)]
    strategy_grade = [p for p in per
                      if p["evidence_class"] == str(STRATEGY_GRADE)]
    if not per:
        # "Nothing recorded" and "nothing finalised yet" are different
        # facts, and reporting the second as the first reads as a broken
        # pipeline on a day that is simply still running. A session
        # becomes evidence at its close, not while it is open.
        if excluded_void:
            why = (f"{excluded_void} session(s) recorded but all VOID "
                   f"(each spanned a redeploy, so no result can be "
                   f"attributed to one runtime)")
        elif in_progress:
            why = (f"{in_progress} session(s) in progress and none "
                   f"finalised; a session becomes evidence at its close")
        else:
            why = "no sessions recorded"
        return {"evidence_class": str(EvidenceClass.OPERATIONAL_VALIDATION_ONLY),
                "sessions": 0, "strategy_grade_sessions": 0,
                "void_sessions": excluded_void,
                "data_quality": str(DataQuality.UNKNOWN),
                "reasons": [why],
                "counts_toward_strategy_gates": False}
    qualities = {p["data_quality"] for p in per}
    overall_quality = (DataQuality.REAL_TIME
                       if qualities == {str(DataQuality.REAL_TIME)}
                       else DataQuality.MIXED if len(qualities) > 1
                       else DataQuality(next(iter(qualities))))
    all_strategy_grade = len(strategy_grade) == len(per)
    reasons: List[str] = []
    if void:
        reasons.append(f"{len(void)} session(s) VOID (spanned a redeploy)")
    if not all_strategy_grade:
        notes = sorted({r for p in per if p["evidence_class"] !=
                        str(STRATEGY_GRADE) for r in p["reasons"]})
        reasons.extend(notes)
    else:
        reasons.append("every session is real-time, single-runtime evidence")
    return {
        "evidence_class": (str(STRATEGY_GRADE) if all_strategy_grade
                           else str(EvidenceClass.OPERATIONAL_VALIDATION_ONLY)),
        "sessions": len(per),
        "strategy_grade_sessions": len(strategy_grade),
        "void_sessions": len(void),
        "data_quality": str(overall_quality),
        "reasons": reasons,
        "counts_toward_strategy_gates": all_strategy_grade,
    }


def quality_from_provenance(is_delayed: Optional[bool]) -> DataQuality:
    """One quote's provenance to a data quality.

    `None` is UNKNOWN, never real-time: the provider base class
    documents `is_delayed=None` as "unknown, never assume real-time".
    """
    if is_delayed is True:
        return DataQuality.DELAYED
    if is_delayed is False:
        return DataQuality.REAL_TIME
    return DataQuality.UNKNOWN
