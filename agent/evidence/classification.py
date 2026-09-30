"""
Deterministic classification of evidence.

Almost everything that matters here can be decided from structured
metadata, without a language model:

  * An 8-K carries ITEM NUMBERS. Item 2.02 is "Results of Operations and
    Financial Condition" - that is earnings, stated by the filer, not
    inferred from prose.
  * A 424B5 is a priced offering. An S-3 is a shelf registration. Those
    are different events and the form type says which.
  * A Form 4 carries transaction CODES. "P" is an open-market purchase;
    "S" is a sale; "F" is shares withheld for tax. Calling them all
    "insider selling" would be wrong on the majority of filings.

The LLM, where used at all, refines a summary or extracts facts on top
of this. It never decides the category, because a deterministic mapping
that can be audited beats a probabilistic one that cannot.

MATERIALITY AND DIRECTION ARE SEPARATE. "CEO departs unexpectedly" is
high materiality and UNCERTAIN direction, and forcing a sign onto it
would be inventing information.
"""
from __future__ import annotations

import enum
import re
from typing import Dict, List, Optional, Tuple

from .models import Direction, EvidenceType


class FinancingStage(str, enum.Enum):
    """The distinction that matters most for short-duration trading.

    A shelf registration is permission to sell securities later. A priced
    prospectus supplement is a sale happening now. Treating the first as
    the second would report dilution on a company that has merely kept
    its options open - and most large issuers keep a shelf on file
    permanently.
    """
    ABILITY_TO_ISSUE = "ABILITY_TO_ISSUE"    # S-3, S-3ASR: capacity only
    ACTUAL_OFFERING = "ACTUAL_OFFERING"      # 424B5: priced and being sold
    AUTHORIZED_PROGRAM = "AUTHORIZED_PROGRAM"  # ATM authorised, sales unknown
    NOT_FINANCING = "NOT_FINANCING"

    def __str__(self) -> str:
        return self.value


# --- 8-K item numbers ----------------------------------------------------
# (type, direction, materiality, description)
#
# Materiality is "how much could this matter", never "is this good".
# Direction is UNCERTAIN wherever the item genuinely does not say.
EIGHT_K_ITEMS: Dict[str, Tuple[EvidenceType, Direction, float, str]] = {
    "1.01": (EvidenceType.MATERIAL_AGREEMENT, Direction.UNCERTAIN, 0.55,
             "entry into a material definitive agreement"),
    "1.02": (EvidenceType.MATERIAL_AGREEMENT, Direction.UNCERTAIN, 0.55,
             "termination of a material definitive agreement"),
    "1.03": (EvidenceType.BANKRUPTCY, Direction.NEGATIVE, 1.00,
             "bankruptcy or receivership"),
    "1.05": (EvidenceType.OTHER, Direction.NEGATIVE, 0.70,
             "material cybersecurity incident"),
    "2.01": (EvidenceType.ACQUISITION, Direction.UNCERTAIN, 0.75,
             "completion of an acquisition or disposition of assets"),
    "2.02": (EvidenceType.EARNINGS, Direction.UNCERTAIN, 0.90,
             "results of operations and financial condition"),
    "2.03": (EvidenceType.DEBT_OFFERING, Direction.UNCERTAIN, 0.60,
             "creation of a direct financial obligation"),
    "2.04": (EvidenceType.DEBT_OFFERING, Direction.NEGATIVE, 0.75,
             "triggering event accelerating a financial obligation"),
    "2.05": (EvidenceType.RESTRUCTURING, Direction.UNCERTAIN, 0.60,
             "costs associated with exit or disposal activities"),
    "2.06": (EvidenceType.OTHER, Direction.NEGATIVE, 0.70,
             "material impairment"),
    "3.01": (EvidenceType.OTHER, Direction.NEGATIVE, 0.85,
             "notice of delisting or failure to satisfy a listing rule"),
    "3.02": (EvidenceType.DILUTION, Direction.NEGATIVE, 0.70,
             "unregistered sale of equity securities"),
    "3.03": (EvidenceType.OTHER, Direction.UNCERTAIN, 0.50,
             "material modification to the rights of security holders"),
    "4.01": (EvidenceType.OTHER, Direction.UNCERTAIN, 0.60,
             "change in the registrant's certifying accountant"),
    "4.02": (EvidenceType.OTHER, Direction.NEGATIVE, 0.90,
             "non-reliance on previously issued financial statements"),
    "5.01": (EvidenceType.OTHER, Direction.UNCERTAIN, 0.75,
             "change in control of the registrant"),
    "5.02": (EvidenceType.MANAGEMENT_CHANGE, Direction.UNCERTAIN, 0.65,
             "departure or election of directors or officers"),
    "5.03": (EvidenceType.OTHER, Direction.NEUTRAL, 0.25,
             "amendment to articles or bylaws"),
    "5.07": (EvidenceType.OTHER, Direction.NEUTRAL, 0.20,
             "submission of matters to a vote of security holders"),
    "7.01": (EvidenceType.OTHER, Direction.UNCERTAIN, 0.40,
             "Regulation FD disclosure"),
    "8.01": (EvidenceType.OTHER, Direction.UNCERTAIN, 0.40,
             "other events"),
    # 9.01 accompanies another item and is not itself an event.
    "9.01": (EvidenceType.OTHER, Direction.NEUTRAL, 0.05,
             "financial statements and exhibits"),
}

# Items that only ever appear alongside a substantive one.
ANCILLARY_ITEMS = {"9.01"}


# --- form types ----------------------------------------------------------
# (type, direction, materiality, financing_stage, description)
FORM_TYPES: Dict[str, Tuple[EvidenceType, Direction, float, FinancingStage, str]] = {
    "10-K": (EvidenceType.PERIODIC_REPORT, Direction.NEUTRAL, 0.35,
             FinancingStage.NOT_FINANCING, "annual report"),
    "10-Q": (EvidenceType.PERIODIC_REPORT, Direction.NEUTRAL, 0.40,
             FinancingStage.NOT_FINANCING, "quarterly report"),
    "S-3": (EvidenceType.SHELF_REGISTRATION, Direction.UNCERTAIN, 0.35,
            FinancingStage.ABILITY_TO_ISSUE,
            "shelf registration: capacity to issue securities later, not a "
            "sale"),
    "S-3ASR": (EvidenceType.SHELF_REGISTRATION, Direction.UNCERTAIN, 0.30,
               FinancingStage.ABILITY_TO_ISSUE,
               "automatic shelf registration by a well-known seasoned "
               "issuer: capacity only"),
    "S-1": (EvidenceType.SHELF_REGISTRATION, Direction.UNCERTAIN, 0.45,
            FinancingStage.ABILITY_TO_ISSUE, "registration statement"),
    "424B5": (EvidenceType.SHARE_OFFERING, Direction.NEGATIVE, 0.75,
              FinancingStage.ACTUAL_OFFERING,
              "priced prospectus supplement: an offering being sold"),
    "424B2": (EvidenceType.SHARE_OFFERING, Direction.NEGATIVE, 0.65,
              FinancingStage.ACTUAL_OFFERING,
              "prospectus supplement: securities being offered"),
    "424B3": (EvidenceType.SHARE_OFFERING, Direction.UNCERTAIN, 0.50,
              FinancingStage.ACTUAL_OFFERING, "prospectus supplement"),
    "424B4": (EvidenceType.SHARE_OFFERING, Direction.NEGATIVE, 0.70,
              FinancingStage.ACTUAL_OFFERING, "prospectus: offering priced"),
    "SC 13D": (EvidenceType.ACTIVIST_POSITION, Direction.UNCERTAIN, 0.70,
               FinancingStage.NOT_FINANCING,
               "beneficial ownership over 5% with intent to influence"),
    "SC 13G": (EvidenceType.INSTITUTIONAL_CHANGE, Direction.NEUTRAL, 0.30,
               FinancingStage.NOT_FINANCING,
               "passive beneficial ownership over 5%"),
    "144": (EvidenceType.OTHER, Direction.NEUTRAL, 0.15,
            FinancingStage.NOT_FINANCING,
            "notice of proposed sale of restricted securities: an "
            "intention, not a completed sale"),
    "3": (EvidenceType.OTHER, Direction.NEUTRAL, 0.10,
          FinancingStage.NOT_FINANCING, "initial statement of beneficial "
          "ownership"),
    "5": (EvidenceType.OTHER, Direction.NEUTRAL, 0.15,
          FinancingStage.NOT_FINANCING, "annual statement of changes in "
          "beneficial ownership"),
}


# --- Form 4 transaction codes -------------------------------------------
# The reason "every insider sale is bearish" is wrong: most Form 4 sales
# are not discretionary. Code F is shares withheld to pay tax on a vesting
# grant - the insider never chose to sell - and code A is the grant
# itself. Only open-market P and S reflect a decision to buy or sell.
FORM4_CODES: Dict[str, Tuple[EvidenceType, Direction, float, str]] = {
    "P": (EvidenceType.INSIDER_BUY, Direction.POSITIVE, 0.60,
          "open-market purchase"),
    "S": (EvidenceType.INSIDER_SELL, Direction.NEGATIVE, 0.45,
          "open-market sale"),
    "A": (EvidenceType.OTHER, Direction.NEUTRAL, 0.10,
          "grant, award or other acquisition from the issuer"),
    "M": (EvidenceType.OTHER, Direction.NEUTRAL, 0.15,
          "exercise or conversion of a derivative security"),
    "F": (EvidenceType.OTHER, Direction.NEUTRAL, 0.05,
          "shares withheld by the issuer to satisfy tax on vesting; not a "
          "discretionary sale"),
    "G": (EvidenceType.OTHER, Direction.NEUTRAL, 0.10, "bona fide gift"),
    "C": (EvidenceType.OTHER, Direction.NEUTRAL, 0.15,
          "conversion of a derivative security"),
    "D": (EvidenceType.OTHER, Direction.NEUTRAL, 0.15,
          "disposition to the issuer"),
    "X": (EvidenceType.OTHER, Direction.NEUTRAL, 0.15,
          "exercise of an in-the-money or at-the-money derivative"),
}

# A sale executed under a pre-arranged plan was scheduled in advance and
# carries much less signal about the insider's current view.
RULE_10B5_1_HINT = re.compile(
    r"10b5[-\s]?1|pursuant to a (?:pre-?arranged |trading )?plan",
    re.IGNORECASE)


def classify_eight_k(items_raw: str) -> Tuple[EvidenceType, Direction, float, str]:
    """Classify an 8-K from its item numbers.

    An 8-K commonly carries several items. The most material substantive
    one wins; ancillary items like 9.01 never decide the classification
    on their own, because "financial statements and exhibits" describes
    an attachment rather than an event.
    """
    codes = [c.strip() for c in (items_raw or "").split(",") if c.strip()]
    known = [(c, EIGHT_K_ITEMS[c]) for c in codes if c in EIGHT_K_ITEMS]
    substantive = [(c, v) for c, v in known if c not in ANCILLARY_ITEMS]
    pool = substantive or known

    if not pool:
        unknown = ", ".join(codes) if codes else "none listed"
        return (EvidenceType.OTHER, Direction.UNCERTAIN, 0.40,
                f"8-K with unrecognised item(s): {unknown}")

    code, (etype, direction, materiality, description) = max(
        pool, key=lambda kv: kv[1][2])
    others = [c for c, _ in known if c != code]
    reason = f"8-K item {code}: {description}"
    if others:
        reason += f" (also filed: {', '.join(others)})"
    return etype, direction, materiality, reason


def classify_form(form: str, items_raw: str = ""
                  ) -> Tuple[EvidenceType, Direction, float, FinancingStage, str]:
    """Classify any SEC filing from its form type.

    Amendments (`/A`) fall back to the base form: a 10-K/A is still a
    periodic report.
    """
    form = (form or "").strip().upper()
    base = form.split("/")[0].strip()

    if base == "8-K":
        etype, direction, materiality, reason = classify_eight_k(items_raw)
        stage = (FinancingStage.ACTUAL_OFFERING
                 if etype in (EvidenceType.SHARE_OFFERING,
                              EvidenceType.DILUTION)
                 else FinancingStage.NOT_FINANCING)
        if form.endswith("/A"):
            reason += " (amended filing)"
        return etype, direction, materiality, stage, reason

    if base == "4":
        return (EvidenceType.OTHER, Direction.NEUTRAL, 0.30,
                FinancingStage.NOT_FINANCING,
                "insider transaction report; direction depends on the "
                "transaction code")

    for key in (form, base):
        if key in FORM_TYPES:
            etype, direction, materiality, stage, description = FORM_TYPES[key]
            reason = f"form {form}: {description}"
            return etype, direction, materiality, stage, reason

    return (EvidenceType.OTHER, Direction.NEUTRAL, 0.15,
            FinancingStage.NOT_FINANCING,
            f"form {form}: not individually classified")


def classify_form4_transaction(code: str, footnotes: str = ""
                               ) -> Tuple[EvidenceType, Direction, float, str]:
    """Classify one Form 4 transaction from its code.

    A sale under a Rule 10b5-1 plan was scheduled in advance, so it says
    much less about the insider's current view than a discretionary one.
    """
    code = (code or "").strip().upper()
    if code not in FORM4_CODES:
        return (EvidenceType.OTHER, Direction.NEUTRAL, 0.10,
                f"transaction code {code or '(none)'}: not classified")

    etype, direction, materiality, description = FORM4_CODES[code]
    reason = f"Form 4 code {code}: {description}"

    if code in ("P", "S") and RULE_10B5_1_HINT.search(footnotes or ""):
        materiality *= 0.4
        reason += "; executed under a pre-arranged Rule 10b5-1 plan, so it "
        reason += "reflects a decision made earlier"
        if code == "S":
            direction = Direction.NEUTRAL
    return etype, direction, materiality, reason


# --- headline classification (news) --------------------------------------
# Keyword matching is a HEURISTIC and is treated as one: it assigns a
# category and a reason, never a confident direction on its own. Where a
# headline is ambiguous the result is UNCERTAIN rather than a guess.
_HEADLINE_RULES: List[Tuple[EvidenceType, Direction, float, str]] = [
    (EvidenceType.EARNINGS, Direction.UNCERTAIN, 0.80,
     r"\b(?:q[1-4]|first|second|third|fourth)[\s-]?quarter\b|\bearnings\b"
     r"|\breports?\s+(?:q[1-4]|results)\b"),
    (EvidenceType.GUIDANCE, Direction.UNCERTAIN, 0.80,
     r"\bguidance\b|\boutlook\b|\bforecast\b"),
    # "to buy" alone is ambiguous: "Analyst Upgrades Acme to Buy" is a
    # rating change, not a takeover, and the bare pattern classified it
    # as an ACQUISITION with 0.75 materiality. An acquisition headline
    # names the act or an agreement to perform it.
    (EvidenceType.ACQUISITION, Direction.UNCERTAIN, 0.75,
     r"\bacquir\w+\b|\bacquisition\b|\btakeover\b"
     r"|\bagree[sd]?\s+to\s+(?:buy|acquire)\b"
     r"|\bto\s+(?:buy|acquire)\s+\w+\s+(?:corp|inc|ltd|plc|group|"
     r"holdings|for\s+\$)"),
    (EvidenceType.MERGER, Direction.UNCERTAIN, 0.75, r"\bmerger\b|\bmerges?\b"),
    (EvidenceType.SHARE_OFFERING, Direction.NEGATIVE, 0.70,
     r"\b(?:public|secondary|common stock)\s+offering\b|\bprices?\s+"
     r"\$?[\d.]+\s*(?:million|billion)?\s+offering\b"),
    (EvidenceType.CONVERTIBLE_DEBT, Direction.UNCERTAIN, 0.65,
     r"\bconvertible\s+(?:notes?|bonds?|senior)\b"),
    (EvidenceType.BANKRUPTCY, Direction.NEGATIVE, 1.00,
     r"\bchapter\s+(?:7|11)\b|\bbankrupt\w*\b"),
    (EvidenceType.FDA, Direction.UNCERTAIN, 0.85,
     r"\bfda\b|\bphase\s+[123]\b|\bclinical\s+trial\b"),
    (EvidenceType.REGULATORY_APPROVAL, Direction.POSITIVE, 0.80,
     r"\bapprov(?:es|ed|al)\b.*\b(?:fda|regulator|ema)\b"
     r"|\b(?:fda|regulator|ema)\b.*\bapprov(?:es|ed|al)\b"),
    (EvidenceType.LAWSUIT, Direction.NEGATIVE, 0.55,
     r"\blawsuit\b|\bsues?\b|\bsued\b|\blitigation\b|\bclass action\b"),
    (EvidenceType.SETTLEMENT, Direction.UNCERTAIN, 0.55, r"\bsettle(?:s|d|ment)\b"),
    (EvidenceType.ANALYST_UPGRADE, Direction.POSITIVE, 0.40,
     r"\bupgrade[sd]?\b|\braises?\s+(?:to\s+)?(?:buy|outperform|overweight)\b"),
    (EvidenceType.ANALYST_DOWNGRADE, Direction.NEGATIVE, 0.40,
     r"\bdowngrade[sd]?\b|\bcuts?\s+(?:to\s+)?(?:sell|underperform|underweight)\b"),
    (EvidenceType.PRICE_TARGET_CHANGE, Direction.UNCERTAIN, 0.30,
     r"\bprice target\b"),
    (EvidenceType.MANAGEMENT_CHANGE, Direction.UNCERTAIN, 0.60,
     r"\b(?:ceo|cfo|coo|president|chairman)\b.*\b(?:steps? down|resign\w*"
     r"|depart\w*|appoint\w*|names?|hires?)\b"
     r"|\b(?:names?|appoints?)\b.*\b(?:ceo|cfo|coo)\b"),
    (EvidenceType.LAYOFF, Direction.UNCERTAIN, 0.55,
     r"\blay[\s-]?offs?\b|\bjob cuts?\b|\bworkforce reduction\b"),
    (EvidenceType.DIVIDEND, Direction.POSITIVE, 0.35,
     r"\bdividend\b(?!.*\bcuts?\b)"),
    (EvidenceType.BUYBACK, Direction.POSITIVE, 0.45,
     r"\bbuyback\b|\bshare repurchase\b|\brepurchase program\b"),
    (EvidenceType.STOCK_SPLIT, Direction.NEUTRAL, 0.35, r"\bstock split\b"),
    (EvidenceType.CONTRACT, Direction.POSITIVE, 0.55,
     r"\bawarded?\s+(?:a\s+)?contract\b|\bwins?\s+(?:a\s+)?(?:contract|deal)\b"),
    (EvidenceType.PARTNERSHIP, Direction.POSITIVE, 0.45,
     r"\bpartnership\b|\bpartners? with\b|\bcollaborat\w+ with\b"),
    (EvidenceType.PRODUCT_LAUNCH, Direction.POSITIVE, 0.45,
     r"\blaunch\w*\b|\bunveil\w*\b|\bintroduces?\b|\bannounces? (?:the )?new\b"),
]

_HEADLINE_COMPILED = [(t, d, m, re.compile(p, re.IGNORECASE))
                      for t, d, m, p in _HEADLINE_RULES]


# Price-action language in a headline. These describe what the stock did,
# not what the company announced - but they are a reliable check on a
# direction inferred from keywords elsewhere in the text.
#
# Found in live validation: "Why Is Intel Stock Falling on Monday?" was
# classified POSITIVE, because a keyword in the article summary matched a
# positive pattern while the headline said the opposite. Reporting a
# POSITIVE catalyst on that headline is precisely the kind of misleading
# output this layer exists to prevent.
_FALLING_RE = re.compile(
    r"\b(?:falling|falls|fell|drops?|dropping|slides?|sliding|plunges?|"
    r"plunging|tumbles?|tumbling|sinks?|sinking|slumps?|slumping|"
    r"declines?|declining|craters?|lower|down)\b", re.IGNORECASE)
_RISING_RE = re.compile(
    r"\b(?:rising|rises|rose|surges?|surging|jumps?|jumping|soars?|"
    r"soaring|climbs?|climbing|rallies|rallying|gains?|gaining|"
    r"higher|up)\b", re.IGNORECASE)


def _price_action_sense(headline: str) -> Optional[Direction]:
    """What the HEADLINE says the price did, if anything."""
    falling = bool(_FALLING_RE.search(headline or ""))
    rising = bool(_RISING_RE.search(headline or ""))
    if falling and not rising:
        return Direction.NEGATIVE
    if rising and not falling:
        return Direction.POSITIVE
    return None


def classify_headline(headline: str, summary: str = ""
                      ) -> Tuple[EvidenceType, Direction, float, str]:
    """Best-effort category for a news headline.

    Deliberately conservative. This is pattern matching over a sentence
    fragment, so it produces a category and a stated reason, and leaves
    direction UNCERTAIN wherever the wording does not settle it. An
    unmatched headline is OTHER with low materiality and a reason saying
    so - never a silent default that looks like a considered judgement.
    """
    text = f"{headline or ''} {summary or ''}".strip()
    if not text:
        return (EvidenceType.OTHER, Direction.NEUTRAL, 0.0,
                "no headline text to classify")

    matches = [(t, d, m, rx.pattern)
               for t, d, m, rx in _HEADLINE_COMPILED if rx.search(text)]
    if not matches:
        return (EvidenceType.OTHER, Direction.NEUTRAL, 0.20,
                "headline did not match a known event pattern")

    etype, direction, materiality, _pattern = max(matches, key=lambda x: x[2])
    reason = f"headline matched the {etype} pattern"

    # A direction inferred from keywords must not contradict what the
    # headline plainly says the price did. When it does, the honest
    # answer is UNCERTAIN: we know the two disagree, not which is right.
    sense = _price_action_sense(headline)
    if sense is not None and direction in (Direction.POSITIVE,
                                           Direction.NEGATIVE) \
            and sense is not direction:
        reason += (f"; the headline describes the price as "
                   f"{'falling' if sense is Direction.NEGATIVE else 'rising'}, "
                   f"which contradicts the keyword reading, so direction is "
                   f"reported as uncertain")
        direction = Direction.UNCERTAIN
    if len(matches) > 1:
        others = ", ".join(str(t) for t, _, _, _ in matches if t is not etype)
        reason += f" (also matched: {others})"
        # Several distinct event types in one headline is a sign the
        # story is compound; direction is not safely inferable.
        if direction is not Direction.UNCERTAIN:
            distinct = {d for _, d, _, _ in matches
                        if d in (Direction.POSITIVE, Direction.NEGATIVE)}
            if len(distinct) > 1:
                direction = Direction.MIXED
                reason += "; the matched patterns point in different directions"
    return etype, direction, materiality, reason


# --- earnings ------------------------------------------------------------

def classify_earnings_outcome(eps_surprise: Optional[float],
                              revenue_surprise: Optional[float],
                              guidance_change: Optional[str]
                              ) -> Tuple[Direction, float, str]:
    """Direction for an earnings result.

    A beat with cut guidance is MIXED, not positive. Simplistic
    beat/miss sentiment is wrong in exactly the cases that move a stock
    most, because the market trades the forward number.
    """
    parts: List[str] = []
    positives = 0
    negatives = 0

    for label, value in (("EPS", eps_surprise), ("revenue", revenue_surprise)):
        if value is None:
            continue
        if value > 0:
            positives += 1
            parts.append(f"{label} beat")
        elif value < 0:
            negatives += 1
            parts.append(f"{label} missed")
        else:
            parts.append(f"{label} in line")

    # Guidance is weighted more heavily than the reported quarter.
    if guidance_change == "RAISED":
        positives += 2
        parts.append("guidance raised")
    elif guidance_change == "LOWERED":
        negatives += 2
        parts.append("guidance lowered")
    elif guidance_change == "MAINTAINED":
        parts.append("guidance maintained")

    reason = "; ".join(parts) if parts else "no comparable figures available"

    if positives and negatives:
        return Direction.MIXED, 0.90, reason
    if positives:
        return Direction.POSITIVE, 0.85, reason
    if negatives:
        return Direction.NEGATIVE, 0.85, reason
    return Direction.UNCERTAIN, 0.70, reason
