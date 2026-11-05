"""수집된 알림과 발송 대기열 항목."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from .util import normalize_ws, one_line

PRIORITY_RANK = {"urgent": 0, "normal": 1, "low": 2, "mute": 3}
RANK_PRIORITY = {v: k for k, v in PRIORITY_RANK.items()}


@dataclass
class Notification:
    """수집 경로와 무관한 공통 알림 형태."""

    source: str  # android | email | web | test
    title: str = ""
    body: str = ""
    app: str = ""
    package: str = ""
    external_id: str = ""  # 경로별 고유 ID(웹 글 ID, Message-ID 등). 안드로이드 알림 ID는 재사용되므로 넣지 않는다.
    child_hint: str = ""  # 수집 단계에서 이미 아는 자녀 이름(웹 폴링 등)
    received_at: float = 0.0

    def __post_init__(self) -> None:
        self.title = normalize_ws(self.title)
        self.body = normalize_ws(self.body)

    @property
    def text(self) -> str:
        return f"{self.title}\n{self.body}".strip()

    def fingerprint(self) -> str:
        if self.external_id:
            key = f"id|{self.source}|{self.external_id}"
        else:
            key = f"txt|{one_line(self.title)}|{one_line(self.body)}"
        return hashlib.sha256(key.encode("utf-8")).hexdigest()


@dataclass
class QueueItem:
    created: float
    hold_until: float
    priority: str
    category: str
    child: str
    title: str
    body: str
    source: str
    id: int | None = None
    attempts: int = 0

    @property
    def rank(self) -> int:
        return PRIORITY_RANK.get(self.priority, 1)
