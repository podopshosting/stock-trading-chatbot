"""Peer comparison and price performance. Relative facts only - there is
no ranking, no 'best', no composite."""
from __future__ import annotations

from datetime import date, timedelta
from typing import Dict, List, Optional, Sequence

from .fundamentals import ALIGN_DAYS, PERIOD_MISMATCH, UNKNOWN, _d

HORIZONS = {"1D": 1, "1M": 30, "3M": 91, "YTD": None, "1Y": 365,
            "3Y": 1095, "5Y": 1826}


def compare_metric(name: str, subject: Optional[Dict],
                   peers: Dict[str, Optional[Dict]]) -> Dict:
    """Each side is {'value': number, 'period_end': 'YYYY-MM-DD'} or None.
    Peers whose period is not within ALIGN_DAYS of the subject's are set
    aside as PERIOD_MISMATCH, not blended in."""
    if not subject or subject.get("value") in (None, UNKNOWN):
        return {"metric": name, "status": UNKNOWN,
                "reason": "subject value unavailable"}
    ok, excluded = {}, []
    for sym, m in peers.items():
        if not m or m.get("value") in (None, UNKNOWN):
            excluded.append({"symbol": sym, "reason": "UNKNOWN"})
        elif abs((_d(m["period_end"]) - _d(subject["period_end"])).days) \
                > ALIGN_DAYS:
            excluded.append({"symbol": sym, "reason": PERIOD_MISMATCH,
                             "peer_period_end": m["period_end"],
                             "subject_period_end": subject["period_end"]})
        else:
            ok[sym] = m["value"]
    if not ok:
        return {"metric": name, "status": UNKNOWN,
                "subject": subject, "excluded": excluded,
                "reason": "no aligned peer values"}
    vals = sorted(ok.values())
    n = len(vals)
    median = (vals[n // 2] if n % 2 else (vals[n // 2 - 1] + vals[n // 2]) / 2)
    below = sum(v < subject["value"] for v in vals)
    return {"metric": name, "status": "OK", "subject": subject,
            "peer_median": median, "peer_min": vals[0], "peer_max": vals[-1],
            "peer_count": n, "peers": ok,
            "subject_percentile": round(100 * below / n, 1),
            "excluded": excluded,
            "note": "relative fact, not a ranking"}


def split_adjust(series: Sequence[Dict], splits: Sequence) -> List[Dict]:
    """For RAW (unadjusted) closes: divide each close by the cumulative
    ratio of splits that happened after it, so a split does not read as a
    price collapse or surge."""
    out = []
    for row in series:
        factor = 1.0
        for s in splits:
            if _d(s.ex_date) > _d(row["date"]):
                factor *= s.ratio
        out.append({"date": row["date"], "close": row["close"] / factor})
    return out


def performance(series: Sequence[Dict], today: date,
                splits: Sequence = (), adjustment: str = "split") -> Dict:
    """Total price change per horizon from [{'date','close'}] oldest-first.

    adjustment='split' means the series is already split-adjusted and is
    used as-is; 'raw' means it is not, and the splits are applied here.
    Dividends are NOT reinvested: this is price change, and says so.
    """
    if adjustment == "raw":
        series = split_adjust(series, splits)
    elif adjustment != "split":
        raise ValueError("adjustment must be 'split' or 'raw'")
    if len(series) < 2:
        return {h: UNKNOWN for h in HORIZONS}
    end = series[-1]
    out: Dict = {"basis": "price change, split-adjusted, dividends excluded",
                 "as_of": end["date"]}
    for h, days in HORIZONS.items():
        target = (date(today.year, 1, 1) - timedelta(days=1)) if h == "YTD" \
            else _d(end["date"]) - timedelta(days=days)
        prior = [r for r in series if _d(r["date"]) <= target]
        if not prior or (_d(end["date"]) - _d(series[0]["date"])).days < \
                (days or 0) - 5:
            out[h] = UNKNOWN
            continue
        base = prior[-1]
        if base["close"] <= 0:
            out[h] = UNKNOWN
            continue
        out[h] = round(end["close"] / base["close"] - 1, 6)
    return out
