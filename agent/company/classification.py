"""Fixed SIC -> (sector, industry) mapping. Division ranges follow the SEC's
published SIC division structure; the industry is the 3-digit SIC group
description when known, else the SIC code itself. No model is involved."""
from __future__ import annotations

from typing import Optional, Tuple

_DIVISIONS = [
    (100, 999, "Agriculture, Forestry, Fishing"),
    (1000, 1499, "Mining"),
    (1500, 1799, "Construction"),
    (2000, 3999, "Manufacturing"),
    (4000, 4999, "Transportation, Communications, Utilities"),
    (5000, 5199, "Wholesale Trade"),
    (5200, 5999, "Retail Trade"),
    (6000, 6799, "Finance, Insurance, Real Estate"),
    (7000, 8999, "Services"),
    (9100, 9999, "Public Administration"),
]


def sector_for_sic(sic: Optional[str]) -> Optional[str]:
    try:
        n = int(str(sic))
    except (TypeError, ValueError):
        return None
    for lo, hi, name in _DIVISIONS:
        if lo <= n <= hi:
            return name
    return None


def sic_group(sic: Optional[str], width: int = 3) -> Optional[str]:
    """Leading SIC digits: width 3 = industry group (2040 -> '204'),
    width 2 = major group (2040 -> '20', the SEC's food-products group)."""
    s = str(sic or "").strip()
    return s[:width] if len(s) >= width and s.isdigit() else None
