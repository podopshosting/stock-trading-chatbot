"""
Pinned configuration versions and evidence cohorts.

Every trade records the strategy version, the risk version and the code
SHA it was made under, so a result can always be traced to the exact
behaviour that produced it.

The rule that matters: results from DIFFERENT behaviour must never be
silently pooled. If the strategy changes mid-observation, the "before"
and "after" trades are different experiments, and adding them together
produces a number describing neither.

The cohort key is derived from the behavioural versions only. The code
SHA is recorded for traceability but deliberately does NOT define the
cohort: a documentation commit changes the SHA without changing
behaviour and must not reset the evidence. A change that DOES alter
behaviour is required to bump a version constant, which is what starts
a new cohort.
"""
from __future__ import annotations

import hashlib
import os
from typing import Dict, Optional


def current_versions(limits=None, code_sha: Optional[str] = None) -> Dict:
    """The versions in force right now."""
    from ..hypothesis.engine import CONFIG_VERSION as hypothesis
    from ..journal.metrics import CONFIG_VERSION as metrics
    from ..orchestration.day import CONFIG_VERSION as orchestration
    from ..positions.exits import CONFIG_VERSION as exits
    from ..risk import RiskLimits
    limits = limits or RiskLimits()
    return {
        "strategy": hypothesis,
        "risk": limits.version,
        "exits": exits,
        "orchestration": orchestration,
        "metrics": metrics,
        "code_sha": (code_sha or os.environ.get("AGENT_CODE_SHA")
                     or "unknown"),
    }


# The keys that define behaviour. code_sha and metrics are excluded: the
# first changes without behaviour changing, the second affects reporting
# but not what the agent does.
BEHAVIOURAL_KEYS = ("strategy", "risk", "exits", "orchestration")


def cohort_key(versions: Dict) -> str:
    """A stable identifier for one behavioural configuration."""
    material = "|".join(f"{k}={versions.get(k, '?')}"
                        for k in BEHAVIOURAL_KEYS)
    return "cohort-" + hashlib.sha256(material.encode()).hexdigest()[:10]


def same_cohort(a: Dict, b: Dict) -> bool:
    return cohort_key(a) == cohort_key(b)


def split_by_cohort(trades) -> Dict[str, list]:
    """Group journalled trades by the behaviour that produced them.

    A trade with no recorded versions goes in its own "unversioned"
    group rather than being folded into a real cohort: unknown
    provenance must not borrow the credibility of a known one.
    """
    groups: Dict[str, list] = {}
    for trade in trades:
        versions = getattr(trade, "config_versions", None) or {}
        key = (cohort_key(versions)
               if any(k in versions for k in BEHAVIOURAL_KEYS)
               else "unversioned")
        groups.setdefault(key, []).append(trade)
    return groups
