"""시간대/조용한 시간/다이제스트 시각 계산."""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone, tzinfo
from datetime import time as dtime

_HHMM = re.compile(r"^\s*(\d{1,2}):(\d{2})\s*$")


def get_tz(name: str) -> tzinfo:
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo(name)
    except Exception:  # tzdata 없는 환경(Windows 등)
        if name in ("Asia/Seoul", "KST"):
            return timezone(timedelta(hours=9), "KST")
        raise ValueError(f"알 수 없는 시간대: {name} (Windows라면 'pip install tzdata')") from None


def parse_hhmm(s: str) -> dtime:
    m = _HHMM.match(s or "")
    if not m:
        raise ValueError(f"시각은 HH:MM 형식이어야 합니다: {s!r}")
    h, mi = int(m.group(1)), int(m.group(2))
    if h > 23 or mi > 59:
        raise ValueError(f"잘못된 시각: {s!r}")
    return dtime(h, mi)


def parse_range(s: str) -> tuple[dtime, dtime]:
    """'22:00-07:00' → (22:00, 07:00)."""
    a, sep, b = (s or "").partition("-")
    if not sep:
        raise ValueError(f"시간 범위는 'HH:MM-HH:MM' 형식이어야 합니다: {s!r}")
    return parse_hhmm(a), parse_hhmm(b)


def in_range(t: dtime, start: dtime, end: dtime) -> bool:
    """[start, end) 안인지. 자정을 넘는 범위(22:00-07:00)도 처리."""
    t = dtime(t.hour, t.minute)
    if start == end:
        return False
    if start < end:
        return start <= t < end
    return t >= start or t < end


def next_at(now: datetime, t: dtime) -> datetime:
    """now 이후(초과) 처음 오는 t 시각."""
    cand = now.replace(hour=t.hour, minute=t.minute, second=0, microsecond=0)
    if cand <= now:
        cand += timedelta(days=1)
    return cand
