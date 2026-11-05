"""안드로이드 알림 포워더(Tasker+AutoNotification, MacroDroid, 자체 NotificationListener 앱) 페이로드 파싱.

어떤 앱이든 JSON 또는 폼으로 아래 키 중 일부만 보내면 된다(별칭 허용):
  title, text, big_text, app, package, lines
치환되지 않은 변수(%antitle, [not_title], {title})는 빈 값으로 본다.
"""

from __future__ import annotations

import json
import re
from urllib.parse import parse_qs

from ..models import Notification

ALIASES: dict[str, tuple[str, ...]] = {
    "title": ("title", "antitle", "not_title", "android.title", "notification_title"),
    "text": ("text", "antext", "notification", "not_text", "message", "body", "content", "android.text"),
    "big_text": ("big_text", "bigtext", "anbigtext", "not_big_text", "android.bigText"),
    "sub_text": ("sub_text", "subtext", "ansubtext", "android.subText"),
    "app": ("app", "app_name", "appname", "anapp", "not_app_name", "application"),
    "package": ("package", "pkg", "package_name", "packagename", "anpackage", "not_app_package"),
    "lines": ("lines", "text_lines", "antextlines", "android.textLines"),
}

_UNEXPANDED = re.compile(r"^(%[A-Za-z_][\w()]*|\[[a-z_]+\]|\{[a-z_]+\})$")


class PayloadError(ValueError):
    pass


def _clean(v) -> str:
    if v is None:
        return ""
    if isinstance(v, (list, tuple)):
        return "\n".join(s for s in (_clean(x) for x in v) if s)
    s = str(v).strip()
    return "" if _UNEXPANDED.match(s) else s


def _pick(data: dict, key: str) -> str:
    lowered = {str(k).lower(): v for k, v in data.items()}
    for alias in ALIASES[key]:
        if alias.lower() in lowered:
            val = _clean(lowered[alias.lower()])
            if val:
                return val
    return ""


def decode_body(body: bytes, content_type: str) -> dict:
    ct = (content_type or "").split(";")[0].strip().lower()
    text = body.decode("utf-8", errors="replace")
    if ct == "application/x-www-form-urlencoded":
        return {k: v[-1] for k, v in parse_qs(text, keep_blank_values=True).items()}
    try:
        data = json.loads(text)
    except ValueError:
        if ct in ("text/plain", ""):
            return {"text": text}  # 본문만 통째로 보내는 단순 포워더
        raise PayloadError("JSON 형식이 아닙니다") from None
    if not isinstance(data, dict):
        raise PayloadError("JSON 객체여야 합니다")
    return data


def parse_payload(data: dict) -> Notification:
    title = _pick(data, "title")
    text = _pick(data, "text")
    big = _pick(data, "big_text")
    lines = _pick(data, "lines")
    sub = _pick(data, "sub_text")

    # 긴 본문(BigTextStyle) > 여러 줄(InboxStyle) > 일반 text
    body = big if len(big) >= len(text) else text
    if lines and len(lines) > len(body):
        body = lines
    if sub and sub not in title and sub not in body:
        title = f"{title} ({sub})" if title else sub
    if not title and not body:
        raise PayloadError("title/text 가 모두 비어 있습니다")

    return Notification(
        source="android",
        title=title[:300],
        body=body[:4000],
        app=_pick(data, "app")[:100],
        package=_pick(data, "package")[:200],
    )
