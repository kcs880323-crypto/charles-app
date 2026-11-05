from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from kidsnote_kakao.config import Config
from kidsnote_kakao.httpclient import Response
from kidsnote_kakao.senders.base import Sender, SendError

KST = timezone(timedelta(hours=9))


def kst(y, mo, d, h, mi=0, s=0) -> float:
    return datetime(y, mo, d, h, mi, s, tzinfo=KST).timestamp()


class FakeClock:
    def __init__(self, t: float):
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, sec: float) -> None:
        self.t += sec


class FakeHttp:
    """(method, url 접두어) → 응답 목록을 차례로 돌려준다."""

    def __init__(self):
        self.routes: dict[tuple[str, str], list[Response]] = {}
        self.calls: list[dict] = []

    def add(self, method: str, url_prefix: str, status: int, body=None, headers=None):
        raw = body if isinstance(body, bytes) else json.dumps(body or {}).encode()
        self.routes.setdefault((method, url_prefix), []).append(Response(status, raw, headers or {}))

    def __call__(self, method, url, data=None, headers=None, timeout=15.0, follow_redirects=True):
        self.calls.append({"method": method, "url": url, "data": data, "headers": headers or {}})
        for (m, prefix), queue in self.routes.items():
            if m == method and url.startswith(prefix) and queue:
                return queue.pop(0) if len(queue) > 1 else queue[0]
        raise AssertionError(f"예상치 못한 요청 {method} {url}")


class FakeSender(Sender):
    def __init__(self, name="fake", max_len=200, daily_limit=0, fail: Exception | None = None):
        super().__init__()
        self.name = name
        self.label = name
        self.max_len = max_len
        self.daily_limit = daily_limit
        self.fail = fail
        self.sent: list[str] = []

    def send_text(self, text: str) -> None:
        if self.fail:
            raise self.fail
        assert len(text) <= self.max_len, f"{len(text)} > {self.max_len}"
        self.sent.append(text)


def make_cfg(tmp_path=None, **rules) -> Config:
    cfg = Config()
    if tmp_path is not None:
        cfg.base_dir = tmp_path
    for k, v in rules.items():
        setattr(cfg.rules, k, v)
    cfg.monitor.alert_cooldown_seconds = 3600
    return cfg


__all__ = ["KST", "kst", "FakeClock", "FakeHttp", "FakeSender", "make_cfg", "SendError"]
