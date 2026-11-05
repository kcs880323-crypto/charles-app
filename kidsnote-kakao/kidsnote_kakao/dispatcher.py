"""대기열을 보고 언제·어디로·어떻게 묶어 보낼지 결정한다.

- urgent 가 하나라도 준비되면 즉시 (준비된 나머지도 함께)
- 그 외에는 디바운스: 마지막 알림 후 batch_window 동안 조용하거나, 첫 알림 후 batch_max_wait 가 지나면
- 1순위 발송 경로의 남은 발송량이 적으면 절약 모드(묶음 간격을 크게)
- 실패하면 지수 백오프로 재시도, 오래된 항목은 버리고 경고
- 시스템 경고(토큰 만료, 세션 만료 등)는 쿨다운을 두고 살아 있는 경로로 직접 보낸다
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable

from . import httpclient
from .config import Config
from .formatter import build_messages
from .models import QueueItem
from .rules import RuleEngine
from .senders.base import AuthError, QuotaExceeded, Sender, SendError
from .store import Store
from .util import truncate

log = logging.getLogger(__name__)


class Dispatcher:
    def __init__(self, cfg: Config, store: Store, senders: list[Sender], rules: RuleEngine,
                 clock: Callable[[], float] = time.time, http: httpclient.HttpFunc | None = None):
        self.cfg = cfg
        self.store = store
        self.senders = senders
        self.rules = rules
        self.clock = clock
        self.http = http or httpclient.request
        self._fail_count = 0
        self._next_attempt = 0.0
        self._last_heartbeat = 0.0
        self._started = clock()
        self._lock = threading.Lock()  # tick 과 alert 가 다른 스레드에서 동시에 보내지 않도록

    # --- 발송량 ------------------------------------------------------------

    def remaining(self, s: Sender, now: float) -> int | None:
        if s.daily_limit <= 0:
            return None
        return max(0, s.daily_limit - self.store.sends(self.rules.day_key(now), s.name))

    def _primary(self) -> Sender | None:
        for s in self.senders:
            if s.available()[0]:
                return s
        return None

    def low_quota(self, now: float) -> bool:
        p = self._primary()
        if p is None:
            return False
        rem = self.remaining(p, now)
        return rem is not None and rem <= self.cfg.delivery.low_quota_reserve

    # --- 메인 루프 ---------------------------------------------------------

    def tick(self) -> int:
        """준비된 항목을 (조건이 맞으면) 보낸다. 보낸 항목 수를 반환."""
        now = self.clock()
        if now < self._next_attempt:
            return 0
        ready = self.store.ready(now)
        if not ready:
            return 0
        d = self.cfg.delivery
        if not any(i.priority == "urgent" for i in ready):
            window, max_wait = d.batch_window_seconds, d.batch_max_wait_seconds
            if self.low_quota(now):
                window = max_wait = max(window, d.low_quota_batch_seconds)
            # 조용한 시간/다이제스트로 보류됐던 항목은 풀린 시각부터 센다
            newest = max(max(i.created, i.hold_until) for i in ready)
            oldest = min(max(i.created, i.hold_until) for i in ready)
            if now - newest < window and now - oldest < max_wait:
                return 0

        with self._lock:
            ok = self._deliver(ready, now)
        ids = [i.id for i in ready if i.id is not None]
        if ok:
            self.store.delete(ids)
            self._fail_count = 0
            self._next_attempt = 0.0
            return len(ready)
        self.store.bump_attempts(ids)
        self._fail_count += 1
        delay = min(d.retry_base_seconds * 2 ** (self._fail_count - 1), d.retry_max_seconds)
        self._next_attempt = now + delay
        log.warning("발송 실패 %d회째, %d초 후 재시도 (대기 %d건)", self._fail_count, delay, len(ready))
        return 0

    def _deliver(self, items: list[QueueItem], now: float) -> bool:
        d = self.cfg.delivery
        day = self.rules.day_key(now)
        delivered = False
        for s in self.senders:
            ok, why = s.available()
            if not ok:
                log.debug("%s 건너뜀: %s", s.name, why)
                continue
            max_msgs = d.max_messages_per_flush
            rem = self.remaining(s, now)
            if rem is not None:
                if rem <= 0:
                    log.warning("%s 오늘 발송 상한(%d) 도달 → 다음 경로", s.name, s.daily_limit)
                    continue
                max_msgs = min(max_msgs, rem)
            msgs = build_messages(items, s.max_len, max_msgs, d.tag)
            try:
                for m in msgs:
                    s.send_text(m)
                    self.store.incr_sends(day, s.name)
            except AuthError as e:
                s.mark_dead(str(e))
                log.error("%s 인증 오류: %s", s.name, e)
                self._alert_locked(f"auth:{s.name}", f"{s.label} 발송 중단: {e}", now)
                continue
            except QuotaExceeded as e:
                log.warning("%s 쿼터/속도 제한: %s", s.name, e)
                continue
            except SendError as e:
                log.warning("%s 발송 실패(%s): %s", s.name, "재시도 가능" if e.retryable else "재시도 불가", e)
                continue
            except Exception:  # 발송 경로 버그가 전체를 멈추지 않게
                log.exception("%s 발송 중 예기치 못한 오류", s.name)
                continue
            delivered = True
            log.info("%s 로 %d건을 메시지 %d개로 발송", s.name, len(items), len(msgs))
            if d.mode == "fallback":
                break
        return delivered

    # --- 시스템 경고 -------------------------------------------------------

    def alert(self, key: str, text: str) -> bool:
        with self._lock:
            return self._alert_locked(key, text, self.clock())

    def _alert_locked(self, key: str, text: str, now: float) -> bool:
        last = float(self.store.kv_get(f"alert:{key}", "0") or 0)
        if now - last < self.cfg.monitor.alert_cooldown_seconds:
            return False
        self.store.kv_set(f"alert:{key}", str(now))
        log.warning("경고[%s] %s", key, text)
        day = self.rules.day_key(now)
        for s in self.senders:
            if not s.available()[0]:
                continue
            rem = self.remaining(s, now)
            if rem is not None and rem <= 0:
                continue
            try:
                s.send_text(truncate(f"⚠️[{self.cfg.delivery.tag} 브리지] {text}", s.max_len))
                self.store.incr_sends(day, s.name)
                return True
            except SendError as e:
                if isinstance(e, AuthError):
                    s.mark_dead(str(e))
                log.warning("경고 발송 실패(%s): %s", s.name, e)
        return False

    # --- 주기 점검 ---------------------------------------------------------

    def maintenance(self) -> None:
        now = self.clock()
        self.store.purge_seen(now - self.cfg.storage.dedup_hours * 3600)
        dropped = self.store.drop_stale(now - self.cfg.delivery.max_queue_age_hours * 3600)
        if dropped:
            hours = self.cfg.delivery.max_queue_age_hours
            self.alert("queue:dropped",
                       f"{hours}시간 넘게 못 보낸 알림 {dropped}건을 버렸습니다. 키즈노트 앱을 확인하세요.")
        for s in self.senders:
            try:
                s.maintenance(now, self.alert)
            except Exception:
                log.exception("%s 점검 중 오류", s.name)
        if not any(s.available()[0] for s in self.senders):
            log.error("사용 가능한 발송 경로가 없습니다 (대기 %d건)", self.store.queue_size())

        m = self.cfg.monitor
        if m.silence_alert_hours > 0:
            last = float(self.store.kv_get("last_ingest", "0") or 0) or self._started
            if now - last > m.silence_alert_hours * 3600:
                self.alert("silence", f"{m.silence_alert_hours}시간 넘게 들어온 알림이 없습니다. "
                                      "폰/포워더 앱이 살아 있는지 확인하세요.")
        if m.heartbeat_url and now - self._last_heartbeat >= m.heartbeat_interval_seconds:
            self._last_heartbeat = now
            if any(s.available()[0] for s in self.senders):
                try:
                    self.http("GET", m.heartbeat_url, timeout=10)
                except httpclient.NetworkError as e:
                    log.warning("하트비트 실패: %s", e)

    def run(self, stop: threading.Event, tick_seconds: float = 2.0, maint_seconds: float = 60.0) -> None:
        last_maint = 0.0
        while not stop.is_set():
            try:
                if time.monotonic() - last_maint >= maint_seconds:
                    last_maint = time.monotonic()
                    self.maintenance()
                self.tick()
            except Exception:
                log.exception("디스패처 오류")
            stop.wait(tick_seconds)

    def status(self) -> dict:
        now = self.clock()
        return {
            "queue": self.store.queue_size(),
            "low_quota_mode": self.low_quota(now),
            "next_retry_in": max(0, int(self._next_attempt - now)) if self._next_attempt else 0,
            "senders": {
                s.name: {**s.status(), "sent_today": self.store.sends(self.rules.day_key(now), s.name),
                         "daily_limit": s.daily_limit}
                for s in self.senders
            },
        }
