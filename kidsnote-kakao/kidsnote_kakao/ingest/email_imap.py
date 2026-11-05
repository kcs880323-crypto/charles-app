"""IMAP 메일함 감시. 키즈노트가 메일 알림을 보내는 경우에만 쓸모 있다.

- 메일함을 읽기 전용으로 열고(BODY.PEEK) 읽음 표시를 바꾸지 않는다.
- 마지막으로 본 UID 를 기억한다. 처음 실행 시에는 과거 메일을 보내지 않고 기준점만 잡는다.
"""

from __future__ import annotations

import email
import email.policy
import imaplib
import logging
import threading
from collections.abc import Callable
from html.parser import HTMLParser

from ..config import EmailConfig
from ..models import Notification
from ..pipeline import Pipeline
from ..store import Store

log = logging.getLogger(__name__)


class _Text(HTMLParser):
    BLOCK = {"p", "div", "br", "tr", "li", "h1", "h2", "h3", "h4", "table"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "head"):
            self._skip += 1
        elif tag in self.BLOCK:
            self.out.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style", "head") and self._skip:
            self._skip -= 1
        elif tag in self.BLOCK:
            self.out.append("\n")

    def handle_data(self, data):
        if not self._skip:
            self.out.append(data)


def html_to_text(html: str) -> str:
    p = _Text()
    p.feed(html)
    p.close()
    return "".join(p.out)


def email_to_notification(raw: bytes) -> Notification:
    msg = email.message_from_bytes(raw, policy=email.policy.default)
    subject = str(msg.get("Subject", "") or "")
    text = ""
    part = msg.get_body(preferencelist=("plain", "html"))
    if part is not None:
        try:
            content = part.get_content()
        except (LookupError, ValueError):  # 알 수 없는 charset 등
            content = (part.get_payload(decode=True) or b"").decode("utf-8", errors="replace")
        text = html_to_text(content) if part.get_content_type() == "text/html" else content
    return Notification(
        source="email",
        title=subject[:300],
        body=text[:4000],
        app=str(msg.get("From", "") or "")[:200],
        external_id=str(msg.get("Message-ID", "") or "").strip(),
    )


def _fetched_bytes(data) -> bytes | None:
    for part in data or []:
        if isinstance(part, tuple) and len(part) >= 2 and isinstance(part[1], (bytes, bytearray)):
            return bytes(part[1])
    return None


class ImapPoller:
    def __init__(self, cfg: EmailConfig, pipeline: Pipeline, store: Store, alert: Callable[[str, str], object],
                 imap_factory: Callable[..., imaplib.IMAP4] | None = None):
        self.cfg = cfg
        self.pipeline = pipeline
        self.store = store
        self.alert = alert
        self.imap_factory = imap_factory or (lambda h, p: imaplib.IMAP4_SSL(h, p, timeout=30))

    def poll_once(self) -> int:
        c = self.cfg
        imap = self.imap_factory(c.host, c.port)
        try:
            try:
                imap.login(c.username, c.password)
            except imaplib.IMAP4.error as e:
                self.alert("imap:login", f"IMAP 로그인 실패({c.host}): {e}")
                raise
            typ, _ = imap.select(c.mailbox, readonly=True)
            if typ != "OK":
                raise RuntimeError(f"메일함 선택 실패: {c.mailbox}")
            uv = imap.response("UIDVALIDITY")[1] or [None]
            uidvalidity = uv[0].decode() if isinstance(uv[0], bytes) else str(uv[0] or "")

            key_uv, key_uid = "imap:uidvalidity", "imap:last_uid"
            last = self.store.kv_get(key_uid)
            if last is None or self.store.kv_get(key_uv) != uidvalidity:
                typ, data = imap.uid("SEARCH", "UID", "*")
                uids = [int(x) for x in (data[0] or b"").split()] if typ == "OK" else []
                self.store.kv_set(key_uv, uidvalidity)
                self.store.kv_set(key_uid, str(max(uids) if uids else 0))
                log.info("IMAP 기준점 설정(UID %s), 과거 메일은 보내지 않습니다", max(uids) if uids else 0)
                return 0

            last_uid = int(last)
            criteria = ["UID", f"{last_uid + 1}:*"]
            if c.from_filter:
                criteria += ["FROM", f'"{c.from_filter}"']
            typ, data = imap.uid("SEARCH", *criteria)
            if typ != "OK":
                return 0
            # 'N:*' 는 새 메일이 없어도 마지막 UID 를 돌려줄 수 있으므로 걸러 낸다
            uids = sorted(u for u in (int(x) for x in (data[0] or b"").split()) if u > last_uid)
            count = 0
            for uid in uids:
                typ, fetched = imap.uid("FETCH", str(uid), "(BODY.PEEK[])")
                raw = _fetched_bytes(fetched) if typ == "OK" else None
                if raw:
                    self.pipeline.submit(email_to_notification(raw))
                    count += 1
                self.store.kv_set(key_uid, str(uid))
            return count
        finally:
            try:
                imap.logout()
            except Exception:
                pass

    def run(self, stop: threading.Event) -> None:
        backoff = 0
        while not stop.is_set():
            try:
                self.poll_once()
                backoff = 0
            except Exception as e:
                backoff = min(backoff * 2 or 60, 3600)
                log.warning("IMAP 폴링 실패(%s): %s", type(e).__name__, e)
            stop.wait(max(30, self.cfg.interval_seconds) + backoff)
