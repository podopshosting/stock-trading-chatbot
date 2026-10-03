"""
Canonical replay datasets: immutable, identified by content.

A replay result is only meaningful if you can say what it ran against.
"AAPL, last 250 days" is not an experiment identifier - it means
something different every day, and two runs a week apart are different
experiments reported under the same name.

So a dataset here is content-addressed. The manifest records what was
asked for, what arrived, and what was wrong with it; the checksum is
taken over the normalised bars. Refetching the same window after a
provider revision produces a DIFFERENT dataset id, which is the point:
the old result still names the old data.

What this deliberately does NOT do
----------------------------------
It does not fill gaps. A missing interval is counted and reported, never
synthesised, because a fabricated bar is indistinguishable from a real
one once it is in the series and every result downstream inherits it.

It does not adjust prices itself. Whether the provider's bars are
split-adjusted is recorded as a CLAIM about the provider, not as a
verified property, and `agent/replay/data.py` already refuses a series
with an unadjusted split discontinuity in it.
"""
from __future__ import annotations

import hashlib
import json
import os
import pathlib
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Sequence, Tuple

from .data import Bar

MANIFEST_VERSION = "dataset-v1.0.0"

# Where datasets live. Local, because a replay dataset is an input to an
# experiment rather than production state.
DEFAULT_ROOT = pathlib.Path(
    os.environ.get("AGENT_DATASET_ROOT",
                   pathlib.Path(__file__).resolve().parents[2] / "datasets"))

# What the provider is believed to do about splits. A claim, not a check.
ADJUSTMENT_PROVIDER_SPLIT = "PROVIDER_SPLIT_ADJUSTED"
ADJUSTMENT_RAW = "PROVIDER_RAW"
ADJUSTMENT_UNKNOWN = "UNKNOWN"

SESSION_ALL = "ALL_HOURS"
SESSION_REGULAR = "REGULAR_ONLY"

_INTERVAL_SECONDS = {
    "1min": 60, "5min": 300, "15min": 900, "30min": 1800,
    "1hour": 3600, "1day": 86400,
}


class DatasetError(Exception):
    """The dataset could not be built or read. Never returned as empty."""


@dataclass
class DatasetManifest:
    """Everything needed to say what a replay ran against."""
    dataset_id: str
    manifest_version: str
    provider: str
    feed: Optional[str]
    symbols: List[str]
    timeframe: str
    requested_start: Optional[str]
    requested_end: Optional[str]
    actual_start: Optional[str]
    actual_end: Optional[str]
    retrieved_at: str
    adjustment: str
    session_policy: str
    timezone: str
    bar_counts: Dict[str, int]
    total_bars: int
    duplicates_dropped: Dict[str, int]
    missing_intervals: Dict[str, int]
    missing_detail: Dict[str, List[str]]
    out_of_order_dropped: Dict[str, int]
    checksum: str
    provider_metadata: Dict = field(default_factory=dict)
    notes: List[str] = field(default_factory=list)

    def as_dict(self) -> Dict:
        return asdict(self)

    @property
    def is_complete(self) -> bool:
        """No missing intervals anywhere.

        A dataset with holes is still usable - the replay treats a hole
        as stale data, which is correct - but a result from it should
        not be compared with one from a complete series without saying
        so.
        """
        return not any(self.missing_intervals.values())


def _to_utc(stamp: str) -> Optional[datetime]:
    try:
        parsed = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        # A naive timestamp is an ambiguity, not a UTC timestamp. The
        # provider documents UTC, so it is read as UTC and the
        # assumption is recorded on the manifest rather than hidden.
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def normalise_bars(rows: Sequence, timeframe: str,
                   session_policy: str = SESSION_ALL) -> Tuple[
                       List[Bar], Dict]:
    """One canonical series from whatever the provider returned.

    Returns (bars, report). The report is not decoration: duplicates and
    holes change what a result means, and a caller that cannot see them
    cannot tell a clean run from a patchy one.
    """
    interval = _INTERVAL_SECONDS.get(timeframe.lower())
    report = {"duplicates_dropped": 0, "out_of_order_dropped": 0,
              "unparseable_dropped": 0, "missing_intervals": 0,
              "missing_detail": [], "filtered_out_of_session": 0}

    seen: Dict[str, Bar] = {}
    for row in rows or []:
        stamp = _to_utc(getattr(row, "timestamp", None)
                        or (row.get("timestamp") if isinstance(row, dict)
                            else None))
        if stamp is None:
            report["unparseable_dropped"] += 1
            continue
        key = stamp.isoformat(timespec="seconds")
        try:
            bar = Bar(timestamp=key,
                      open=float(_attr(row, "open")),
                      high=float(_attr(row, "high")),
                      low=float(_attr(row, "low")),
                      close=float(_attr(row, "close")),
                      volume=float(_attr(row, "volume") or 0.0))
        except (TypeError, ValueError, KeyError):
            report["unparseable_dropped"] += 1
            continue
        if session_policy == SESSION_REGULAR and not _in_regular_session(
                stamp, timeframe):
            report["filtered_out_of_session"] += 1
            continue
        if key in seen:
            # Last write wins, and the drop is counted. A duplicate
            # timestamp is a provider artefact, and silently keeping
            # both would put two bars at one instant into a series the
            # replay reads as strictly increasing.
            report["duplicates_dropped"] += 1
        seen[key] = bar

    bars = [seen[k] for k in sorted(seen)]

    # Holes. Counted, never filled.
    #
    # Counted in TRADING terms, not calendar terms. Measuring daily
    # bars against a 24-hour interval counted every weekend as missing
    # data: 111 "holes" in 250 trading days, which made is_complete
    # False for every daily dataset ever built and therefore useless.
    # A closed market is not absent data.
    if interval and len(bars) > 1:
        daily = timeframe.lower() == "1day"
        expected = timedelta(seconds=interval)
        for previous, nxt in zip(bars, bars[1:]):
            a, b = _to_utc(previous.timestamp), _to_utc(nxt.timestamp)
            if a is None or b is None:
                continue
            missed = (_missing_weekdays(a, b) if daily
                      else max(0, int((b - a).total_seconds() // interval) - 1))
            if missed > 0:
                report["missing_intervals"] += missed
                if len(report["missing_detail"]) < 50:
                    report["missing_detail"].append(
                        f"{previous.timestamp} -> {nxt.timestamp} "
                        f"({missed} missing)")
    return bars, report


def _missing_weekdays(a: datetime, b: datetime) -> int:
    """Weekdays strictly between two daily bars.

    Weekends are not missing data. Market holidays still count here,
    because without a market calendar they are indistinguishable from a
    genuinely absent bar - and over-reporting a hole is the safer
    direction than hiding one. Roughly nine or ten a year is expected
    and is recorded as a note rather than silently zeroed.
    """
    missing, cursor = 0, a.date() + timedelta(days=1)
    end = b.date()
    while cursor < end:
        if cursor.weekday() < 5:
            missing += 1
        cursor += timedelta(days=1)
    return missing


def _attr(row, name):
    if isinstance(row, dict):
        return row.get(name)
    return getattr(row, name, None)


def _in_regular_session(stamp: datetime, timeframe: str) -> bool:
    """09:30-16:00 US/Eastern, approximated without a tz database.

    Daily bars are always in session. For intraday bars this uses a
    fixed -5/-4 offset by month, which is WRONG for the two weeks a
    year when the US and the approximation disagree about daylight
    saving. Recorded rather than hidden: a dataset built with
    REGULAR_ONLY carries a note saying so.
    """
    if timeframe.lower() == "1day":
        return True
    offset = -4 if 3 <= stamp.month <= 10 else -5
    local = stamp + timedelta(hours=offset)
    if local.weekday() >= 5:
        return False
    minutes = local.hour * 60 + local.minute
    return 9 * 60 + 30 <= minutes < 16 * 60


def _checksum(bars_by_symbol: Dict[str, List[Bar]]) -> str:
    """Content address over the NORMALISED bars.

    Taken over the data, not over the request, so refetching a revised
    window yields a different id and cannot be mistaken for the same
    experiment.
    """
    digest = hashlib.sha256()
    for symbol in sorted(bars_by_symbol):
        digest.update(symbol.encode())
        for bar in bars_by_symbol[symbol]:
            digest.update(
                f"{bar.timestamp}|{bar.open:.6f}|{bar.high:.6f}|"
                f"{bar.low:.6f}|{bar.close:.6f}|{bar.volume:.2f}".encode())
    return digest.hexdigest()


def build_manifest(bars_by_symbol: Dict[str, List[Bar]], *, provider: str,
                   timeframe: str, feed: Optional[str] = None,
                   requested_start: Optional[str] = None,
                   requested_end: Optional[str] = None,
                   adjustment: str = ADJUSTMENT_UNKNOWN,
                   session_policy: str = SESSION_ALL,
                   reports: Optional[Dict[str, Dict]] = None,
                   provider_metadata: Optional[Dict] = None,
                   retrieved_at: Optional[str] = None,
                   notes: Optional[Sequence[str]] = None
                   ) -> DatasetManifest:
    if not bars_by_symbol or not any(bars_by_symbol.values()):
        raise DatasetError(
            "a dataset needs at least one bar; an empty dataset would be "
            "indistinguishable from a failed fetch")
    reports = reports or {}
    checksum = _checksum(bars_by_symbol)
    starts = [b[0].timestamp for b in bars_by_symbol.values() if b]
    ends = [b[-1].timestamp for b in bars_by_symbol.values() if b]
    note_list = list(notes or [])
    if session_policy == SESSION_REGULAR and timeframe.lower() != "1day":
        note_list.append(
            "REGULAR_ONLY used a fixed daylight-saving approximation; "
            "the session boundary is wrong for the weeks when the "
            "approximation and the US transition disagree")
    if timeframe.lower() == "1day" and any(
            (reports.get(s) or {}).get("missing_intervals")
            for s in bars_by_symbol):
        note_list.append(
            "missing daily intervals exclude weekends but still count "
            "market holidays, of which roughly nine or ten a year are "
            "expected; this is not evidence of a data fault")
    if adjustment == ADJUSTMENT_UNKNOWN:
        note_list.append(
            "the provider's split-adjustment behaviour was not declared, "
            "so it is recorded as UNKNOWN rather than assumed")
    return DatasetManifest(
        dataset_id=f"ds_{checksum[:16]}",
        manifest_version=MANIFEST_VERSION,
        provider=provider, feed=feed,
        symbols=sorted(bars_by_symbol),
        timeframe=timeframe,
        requested_start=requested_start, requested_end=requested_end,
        actual_start=min(starts) if starts else None,
        actual_end=max(ends) if ends else None,
        retrieved_at=(retrieved_at
                      or datetime.now(timezone.utc).isoformat(
                          timespec="seconds")),
        adjustment=adjustment, session_policy=session_policy,
        timezone="UTC",
        bar_counts={s: len(b) for s, b in sorted(bars_by_symbol.items())},
        total_bars=sum(len(b) for b in bars_by_symbol.values()),
        duplicates_dropped={s: (reports.get(s) or {}).get(
            "duplicates_dropped", 0) for s in sorted(bars_by_symbol)},
        missing_intervals={s: (reports.get(s) or {}).get(
            "missing_intervals", 0) for s in sorted(bars_by_symbol)},
        missing_detail={s: (reports.get(s) or {}).get("missing_detail", [])
                        for s in sorted(bars_by_symbol)},
        out_of_order_dropped={s: (reports.get(s) or {}).get(
            "out_of_order_dropped", 0) for s in sorted(bars_by_symbol)},
        checksum=checksum,
        provider_metadata=dict(provider_metadata or {}),
        notes=note_list)


def save(manifest: DatasetManifest, bars_by_symbol: Dict[str, List[Bar]],
         root: Optional[pathlib.Path] = None) -> pathlib.Path:
    """Write the dataset, refusing to overwrite a different one.

    Same id means same content by construction, so a re-save is a
    no-op. A DIFFERENT checksum under the same id would mean the
    content address is broken, and that is worth failing over.
    """
    root = pathlib.Path(root or DEFAULT_ROOT)
    target = root / manifest.dataset_id
    target.mkdir(parents=True, exist_ok=True)
    manifest_path = target / "manifest.json"
    if manifest_path.exists():
        existing = json.loads(manifest_path.read_text())
        if existing.get("checksum") != manifest.checksum:
            raise DatasetError(
                f"{manifest.dataset_id} already exists with a different "
                f"checksum; a content-addressed dataset cannot change")
        return target
    manifest_path.write_text(json.dumps(manifest.as_dict(), indent=2))
    (target / "bars.json").write_text(json.dumps(
        {s: [b.as_dict() for b in bars]
         for s, bars in sorted(bars_by_symbol.items())}))
    return target


def load(dataset_id: str, root: Optional[pathlib.Path] = None) -> Tuple[
        DatasetManifest, Dict[str, List[Bar]]]:
    """Read a dataset back and VERIFY its checksum.

    An unverified content address is just a filename.
    """
    root = pathlib.Path(root or DEFAULT_ROOT)
    target = root / dataset_id
    try:
        manifest_raw = json.loads((target / "manifest.json").read_text())
        bars_raw = json.loads((target / "bars.json").read_text())
    except Exception as exc:                              # noqa: BLE001
        raise DatasetError(
            f"{dataset_id} could not be read: {exc}") from None
    bars = {s: [Bar(**row) for row in rows]
            for s, rows in bars_raw.items()}
    actual = _checksum(bars)
    if actual != manifest_raw.get("checksum"):
        raise DatasetError(
            f"{dataset_id} does not match its checksum; the stored bars "
            f"have changed since the manifest was written")
    return DatasetManifest(**manifest_raw), bars


def list_datasets(root: Optional[pathlib.Path] = None) -> List[Dict]:
    root = pathlib.Path(root or DEFAULT_ROOT)
    if not root.exists():
        return []
    out = []
    for child in sorted(root.iterdir()):
        path = child / "manifest.json"
        if path.exists():
            try:
                out.append(json.loads(path.read_text()))
            except Exception:                             # noqa: BLE001
                out.append({"dataset_id": child.name,
                            "error": "manifest unreadable"})
    return out
