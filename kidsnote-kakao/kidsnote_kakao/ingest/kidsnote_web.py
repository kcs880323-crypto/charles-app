"""[실험적] 키즈노트 웹 내부 API 폴링.

주의
- 공개 API가 아니다. 엔드포인트/응답 형식이 예고 없이 바뀔 수 있고, 이용약관상 문제가 될 수 있다.
- 비밀번호를 저장하지 않는다. 브라우저에서 로그인한 뒤 복사한 Cookie 헤더(KIDSNOTE_SESSION_COOKIE)만 쓴다.
- 최소 5분 간격 + 지터. 세션이 만료되면 폴링을 멈추고 경고만 보낸다(재로그인 시도 없음).
- 첫 실행에는 기존 글을 '본 것'으로만 기록하고 보내지 않는다.
"""

from __future__ import annotations

import json
import logging
import random
import threading
from collections.abc import Callable

from .. import httpclient
from ..config import WebChild, WebEndpoint, WebPollConfig
from ..models import Notification
from ..pipeline import Pipeline
from ..store import Store

log = logging.getLogger(__name__)

SEEN_KEEP = 500


class SessionExpired(Exception):
    pass


def _first_str(d: dict, *keys: str) -> str:
    for k in keys:
        v = d.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
        if isinstance(v, dict):
            name = v.get("name")
            if isinstance(name, str) and name.strip():
                return name.strip()
    return ""


def extract_items(data) -> list[dict]:
    if isinstance(data, list):
        return [x for x in data if isinstance(x, dict)]
    if isinstance(data, dict):
        for key in ("results", "items", "data", "reports", "notices"):
            v = data.get(key)
            if isinstance(v, list):
                return [x for x in v if isinstance(x, dict)]
    return []


def item_to_notification(it: dict, kind: str, child: WebChild) -> Notification | None:
    item_id = it.get("id")
    if item_id is None:
        return None
    content = _first_str(it, "content", "text", "body", "description")
    title = _first_str(it, "title", "subject")
    author = _first_str(it, "author_name", "writer_name", "teacher_name", "author", "writer")
    child_name = child.name or _first_str(it, "child_name")
    head = f"[{kind}] {title}".strip() if title else f"[{kind}]"
    if author:
        head += f" - {author}"
    return Notification(
        source="web",
        title=head,
        body=content or title,
        external_id=f"{kind}:{child.id}:{item_id}",
        child_hint=child_name,
    )


class KidsnoteWebPoller:
    def __init__(self, cfg: WebPollConfig, pipeline: Pipeline, store: Store, alert: Callable[[str, str], object],
                 http: httpclient.HttpFunc | None = None):
        self.cfg = cfg
        self.pipeline = pipeline
        self.store = store
        self.alert = alert
        self.http = http or httpclient.request

    def _get_json(self, path: str):
        if not self.cfg.session_cookie:
            raise SessionExpired("KIDSNOTE_SESSION_COOKIE 가 비어 있습니다")
        url = self.cfg.base_url.rstrip("/") + path
        resp = self.http("GET", url, headers={"Cookie": self.cfg.session_cookie, "Accept": "application/json",
                                              "User-Agent": self.cfg.user_agent}, follow_redirects=False)
        if resp.status in (301, 302, 303, 307, 308, 401, 403):
            raise SessionExpired(f"HTTP {resp.status}")
        if resp.status != 200:
            raise RuntimeError(f"키즈노트 응답 {resp.status}")
        data = resp.json()
        if data is None:
            raise SessionExpired("JSON 이 아닌 응답(로그인 페이지로 보임)")
        return data

    def poll_feed(self, ep: WebEndpoint, child: WebChild) -> int:
        items = extract_items(self._get_json(ep.path.format(child_id=child.id)))
        key = f"web:seen:{ep.kind}:{child.id}"
        raw = self.store.kv_get(key)
        first = raw is None
        seen: list[str] = json.loads(raw) if raw else []
        seen_set = set(seen)
        new = [it for it in items if it.get("id") is not None and str(it["id"]) not in seen_set]
        count = 0
        if not first:
            for it in reversed(new):  # 보통 최신순 → 오래된 것부터 넣는다
                n = item_to_notification(it, ep.kind, child)
                if n:
                    self.pipeline.submit(n)
                    count += 1
        else:
            log.info("웹 폴링 기준점 설정: %s/%s %d건은 보내지 않음", ep.kind, child.name or child.id, len(new))
        seen = [str(it["id"]) for it in new] + seen
        self.store.kv_set(key, json.dumps(seen[:SEEN_KEEP]))
        return count

    def poll_once(self) -> int:
        total = 0
        for child in self.cfg.children:
            for ep in self.cfg.endpoints:
                total += self.poll_feed(ep, child)
        return total

    def discover_children(self) -> list[dict]:
        """me_path 응답에서 자녀 id/이름 후보를 찾아 돌려준다(형식이 바뀌었으면 빈 목록)."""
        data = self._get_json(self.cfg.me_path)
        found = []

        def walk(x):
            if isinstance(x, dict):
                if "children" in x and isinstance(x["children"], list):
                    for c in x["children"]:
                        if isinstance(c, dict) and "id" in c:
                            found.append({"id": c["id"], "name": c.get("name", ""),
                                          "class": _first_str(c, "class_name", "belong_to_class")})
                for v in x.values():
                    walk(v)
            elif isinstance(x, list):
                for v in x:
                    walk(v)

        walk(data)
        return found

    def run(self, stop: threading.Event) -> None:
        if not self.cfg.children:
            log.error("[web] children 이 비어 있어 웹 폴링을 시작하지 않습니다 (web-children 명령으로 확인)")
            return
        backoff = 0
        while not stop.is_set():
            wait = self.cfg.interval_seconds
            try:
                self.poll_once()
                backoff = 0
            except SessionExpired as e:
                self.alert("web:session", f"키즈노트 웹 세션 만료({e}). 브라우저에서 다시 로그인 후 쿠키를 "
                                          f"KIDSNOTE_SESSION_COOKIE 에 넣고 재시작하세요.")
                wait = 6 * 3600
            except Exception as e:
                backoff = min(backoff * 2 or 300, 3600)
                log.warning("웹 폴링 실패(%s): %s", type(e).__name__, e)
            stop.wait(wait * random.uniform(0.9, 1.15) + backoff)
