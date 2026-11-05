"""수집 → 규칙 → 중복 제거 → 대기열. 모든 수집 경로가 이 한 곳을 지난다."""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass

from .config import Config
from .models import Notification, QueueItem
from .rules import RuleEngine
from .store import Store
from .util import append_private, redact

log = logging.getLogger(__name__)


@dataclass
class SubmitResult:
    status: str  # queued | duplicate | dropped
    reason: str = ""
    category: str = ""
    priority: str = ""
    child: str = ""
    hold_until: float = 0.0

    def to_dict(self) -> dict:
        return asdict(self)


class Pipeline:
    def __init__(self, cfg: Config, store: Store, rules: RuleEngine, clock: Callable[[], float] = time.time):
        self.cfg = cfg
        self.store = store
        self.rules = rules
        self.clock = clock

    def submit(self, n: Notification, raw: dict | None = None) -> SubmitResult:
        now = self.clock()
        n.received_at = n.received_at or now
        self.store.kv_set("last_ingest", str(now))
        if raw is not None and self.cfg.logging.capture_samples:
            self._capture(raw, now)

        show = self.cfg.logging.log_content
        d = self.rules.classify(n)
        if not d.accepted:
            log.info("버림 [%s] %s: %s", n.source, d.reason, redact(n.text, show))
            return SubmitResult("dropped", d.reason, d.category, d.priority, d.child)

        if self.store.check_and_mark(n.fingerprint(), now, self.cfg.storage.dedup_hours * 3600):
            log.info("중복 [%s] %s/%s: %s", n.source, d.category, d.priority, redact(n.text, show))
            return SubmitResult("duplicate", "이미 받은 알림", d.category, d.priority, d.child)

        hold = self.rules.hold_until(d.priority, now)
        self.store.enqueue(
            QueueItem(created=now, hold_until=hold, priority=d.priority, category=d.category, child=d.child,
                      title=n.title, body=n.body, source=n.source)
        )
        log.info("대기열 [%s] %s/%s%s 보류 %ds: %s", n.source, d.category, d.priority,
                 f"/{d.child}" if d.child else "", int(hold - now), redact(n.text, show))
        return SubmitResult("queued", "", d.category, d.priority, d.child, hold)

    def _capture(self, raw: dict, now: float) -> None:
        try:
            append_private(self.cfg.path(self.cfg.logging.samples_file),
                           json.dumps({"ts": now, "payload": raw}, ensure_ascii=False))
        except OSError as e:
            log.warning("샘플 저장 실패: %s", e)
