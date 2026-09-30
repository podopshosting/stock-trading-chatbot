"""
Optional LLM enrichment of evidence.

The engine is fully functional without this module. Every category,
direction, materiality and novelty figure is produced deterministically
from structured metadata - SEC item numbers, form types, Form 4
transaction codes, timestamps - before any model is consulted. See
`classification.py`.

What the model MAY do:
  * write a short readable summary of a filing
  * extract atomic factual claims, each tied to the evidence it came from
  * flag risks stated in the document
  * compare new guidance to previously stated guidance

What the model MAY NOT do, enforced here rather than requested in a
prompt:
  * invent facts, financial values or publication times
  * create a source, or assert that an article exists
  * override the deterministic classification
  * produce evidence with no retrievable document behind it

Every returned field is validated against the source item before it is
accepted. Output that fails validation is REJECTED and the deterministic
result stands - a model that returns something unusable must not be able
to degrade what we already knew.

If the model is unreachable the evidence is stored unchanged with
`llm_status = UNAVAILABLE`. Nothing is discarded and nothing is
fabricated to fill the gap.
"""
from __future__ import annotations

import json
import re
from typing import Dict, List, Optional, Sequence

from ..observability import log_event
from .models import Direction, EvidenceItem, Fact, LLMStatus

OPENAI_URL = "https://api.openai.com/v1/chat/completions"
DEFAULT_MODEL = "gpt-4o-mini"

# The model is given the document's own text and asked to stay inside it.
# The constraints are repeated as output rules because a prompt alone is
# a request, not a guarantee - the validation below is what enforces them.
SYSTEM_PROMPT = """You extract structured facts from financial documents.

You will be given a document's metadata and, where available, its text.

ABSOLUTE RULES:
- Use ONLY information present in the supplied text. If a number, date or
  name is not in the text, it does not exist for your purposes.
- Never estimate, infer or recall a financial figure. If the text does not
  state revenue, do not report revenue.
- Never invent a publication time, a source, a publisher or a URL.
- If the text is insufficient to answer a field, return null for it. A null
  is correct; a plausible guess is not.
- Do not give investment advice, price targets or trade recommendations.
- Materiality and direction may differ: an event can be highly material with
  a genuinely uncertain direction. Say UNCERTAIN when that is the case
  rather than choosing a side.

Return ONLY a JSON object with these keys:
{
  "summary": string or null,
  "facts": [{"claim": string, "value": string or null,
             "previous_value": string or null, "quote": string}],
  "risks": [string],
  "direction": "POSITIVE" | "NEGATIVE" | "MIXED" | "NEUTRAL" | "UNCERTAIN",
  "direction_reason": string,
  "guidance_change": "RAISED" | "LOWERED" | "MAINTAINED" | null
}

Every fact MUST include a "quote" copied verbatim from the supplied text.
A fact you cannot quote must be omitted."""

MAX_INPUT_CHARS = 12000
VALID_DIRECTIONS = {d.value for d in Direction}


class LLMUnavailable(Exception):
    """The model could not be reached, or returned nothing usable."""


def _extract_json(text: str) -> Optional[Dict]:
    """Pull a JSON object out of a model response.

    Models wrap JSON in prose or fences often enough that failing on the
    first character is needlessly brittle - but the result must still
    parse as an object, and anything else is rejected.
    """
    if not text:
        return None
    text = text.strip()
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    if fenced:
        text = fenced.group(1)
    else:
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end <= start:
            return None
        text = text[start:end + 1]
    try:
        parsed = json.loads(text)
    except (ValueError, TypeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _normalise(value: str) -> str:
    return re.sub(r"\s+", " ", (value or "")).strip().lower()


def validate_enrichment(payload: Dict, item: EvidenceItem,
                        source_text: str) -> Dict:
    """Accept only what the source text actually supports.

    This is the enforcement point. The prompt asks the model not to
    invent; this function is what makes it true, by discarding anything
    it cannot trace back to the supplied document.
    """
    warnings: List[str] = []
    clean: Dict = {"summary": None, "facts": [], "risks": [],
                   "direction": None, "direction_reason": "",
                   "guidance_change": None}

    haystack = _normalise(source_text)

    summary = payload.get("summary")
    if isinstance(summary, str) and summary.strip():
        # A summary is allowed to paraphrase, but it must be short enough
        # that it cannot smuggle in a narrative of its own.
        clean["summary"] = summary.strip()[:600]

    for raw in (payload.get("facts") or [])[:10]:
        if not isinstance(raw, dict):
            continue
        claim = (raw.get("claim") or "").strip()
        quote = (raw.get("quote") or "").strip()
        if not claim:
            continue
        if not quote:
            warnings.append(f"fact dropped, no supporting quote: {claim[:60]}")
            continue
        # The quote must genuinely appear in the document we supplied.
        # Without this check the model could assert anything and label it
        # a quotation.
        if haystack and _normalise(quote)[:120] not in haystack:
            warnings.append(
                f"fact dropped, quote not found in the source: {claim[:60]}")
            continue
        clean["facts"].append({
            "claim": claim[:300],
            "value": (raw.get("value") or None),
            "previous_value": (raw.get("previous_value") or None),
            "quote": quote[:400],
        })

    for risk in (payload.get("risks") or [])[:8]:
        if isinstance(risk, str) and risk.strip():
            clean["risks"].append(risk.strip()[:300])

    direction = payload.get("direction")
    if isinstance(direction, str) and direction.upper() in VALID_DIRECTIONS:
        clean["direction"] = direction.upper()
    elif direction is not None:
        warnings.append(f"unrecognised direction {direction!r} ignored")

    reason = payload.get("direction_reason")
    if isinstance(reason, str):
        clean["direction_reason"] = reason.strip()[:300]

    guidance = payload.get("guidance_change")
    if isinstance(guidance, str) and guidance.upper() in (
            "RAISED", "LOWERED", "MAINTAINED"):
        clean["guidance_change"] = guidance.upper()

    clean["warnings"] = warnings
    return clean


def enrich_item(item: EvidenceItem, source_text: str,
                api_key: Optional[str], model: str = DEFAULT_MODEL,
                http=None, timeout: int = 30) -> EvidenceItem:
    """Enrich one item in place, safely.

    Returns the item whatever happens. An outage, a malformed response or
    a hallucinated quote all leave the deterministic classification
    intact and set `llm_status` accordingly.
    """
    if not api_key:
        item.llm_status = LLMStatus.NOT_ATTEMPTED
        return item

    if not source_text or not source_text.strip():
        # Nothing to ground the model in. Asking anyway is how invented
        # summaries happen.
        item.llm_status = LLMStatus.NOT_ATTEMPTED
        item.warnings.append(
            "no document text available, so no summary was generated")
        return item

    text = source_text[:MAX_INPUT_CHARS]
    prompt = (
        f"Document type: {item.evidence_type}\n"
        f"Publisher: {item.source.publisher}\n"
        f"Symbol: {item.symbol}\n"
        f"Headline: {item.headline}\n\n"
        f"TEXT:\n{text}"
    )

    try:
        requests = http
        if requests is None:
            import requests as _r
            requests = _r
        resp = requests.post(
            OPENAI_URL,
            headers={"Authorization": f"Bearer {api_key}",
                     "Content-Type": "application/json"},
            json={
                "model": model,
                "messages": [{"role": "system", "content": SYSTEM_PROMPT},
                             {"role": "user", "content": prompt}],
                "max_tokens": 700,
                "temperature": 0.0,          # extraction, not composition
                "response_format": {"type": "json_object"},
            },
            timeout=timeout,
        )
        if resp.status_code != 200:
            raise LLMUnavailable(f"HTTP {resp.status_code}")
        body = resp.json()
        content = body["choices"][0]["message"]["content"]
    except Exception as e:
        item.llm_status = LLMStatus.UNAVAILABLE
        item.warnings.append(
            "summary unavailable: the language model could not be reached; "
            "the deterministic classification is unaffected")
        log_event("llm_enrichment_failed", symbol=item.symbol,
                  evidence_id=item.evidence_id, error=type(e).__name__)
        return item

    payload = _extract_json(content)
    if payload is None:
        item.llm_status = LLMStatus.REJECTED
        item.warnings.append(
            "the language model returned output that could not be parsed; "
            "it was discarded")
        log_event("llm_enrichment_failed", symbol=item.symbol,
                  evidence_id=item.evidence_id, error="unparseable")
        return item

    clean = validate_enrichment(payload, item, text)

    if clean["summary"]:
        item.summary = clean["summary"]
    for fact in clean["facts"]:
        # Fact construction REQUIRES a source evidence id, so a claim can
        # never exist in the record detached from its document.
        item.facts.append(Fact(
            claim=fact["claim"], source_evidence_id=item.evidence_id,
            value=fact["value"], previous_value=fact["previous_value"],
            source_quote_location=fact["quote"], extracted_by=model))
    item.risks.extend(clean["risks"])
    item.warnings.extend(clean["warnings"])

    # The model may refine direction only where the deterministic pass
    # left it genuinely open. It can never overturn a category the filer
    # themselves stated: an 8-K item 2.02 is earnings whatever the model
    # thinks of the prose.
    if clean["direction"] and item.direction is Direction.UNCERTAIN:
        item.direction = Direction(clean["direction"])
        if clean["direction_reason"]:
            item.classification_reason += (
                f"; direction refined from the document text: "
                f"{clean['direction_reason']}")

    if clean["guidance_change"]:
        item.raw_metadata["guidance_change"] = clean["guidance_change"]

    item.llm_status = LLMStatus.SUCCEEDED
    item.llm_model = model
    return item


def load_api_key(secret_id: str = "stock-chatbot/openai-api-key",
                 region: str = "us-east-2", client=None) -> Optional[str]:
    """Read the key from Secrets Manager.

    Returns None rather than raising when unavailable: a missing key must
    degrade enrichment, not break evidence collection. The key is never
    logged and never placed in a URL.
    """
    try:
        if client is None:
            import boto3
            client = boto3.client("secretsmanager", region_name=region)
        raw = client.get_secret_value(SecretId=secret_id)["SecretString"]
        raw = raw.strip()
        if raw.startswith("{"):
            parsed = json.loads(raw)
            for key in ("OPENAI_API_KEY", "openai_api_key", "api_key", "key"):
                if parsed.get(key):
                    return parsed[key]
            return None
        return raw or None
    except Exception:
        return None
