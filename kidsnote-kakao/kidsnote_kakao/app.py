"""구성요소 조립 + 상시 구동."""

from __future__ import annotations

import logging
import signal
import threading
import time
from collections.abc import Callable

from . import __version__
from .config import Config
from .dispatcher import Dispatcher
from .httpclient import HttpFunc
from .pipeline import Pipeline
from .rules import RuleEngine
from .senders import build_senders
from .store import Store

log = logging.getLogger(__name__)


class App:
    def __init__(self, cfg: Config, clock: Callable[[], float] = time.time, http: HttpFunc | None = None,
                 store: Store | None = None):
        self.cfg = cfg
        self.clock = clock
        self.store = store or Store(cfg.path(cfg.storage.path))
        self.rules = RuleEngine(cfg.rules)
        self.senders = build_senders(cfg, http)
        self.dispatcher = Dispatcher(cfg, self.store, self.senders, self.rules, clock=clock, http=http)
        self.pipeline = Pipeline(cfg, self.store, self.rules, clock=clock)
        self.http = http

    def status(self) -> dict:
        last = float(self.store.kv_get("last_ingest", "0") or 0)
        return {
            "version": __version__,
            "last_ingest_seconds_ago": int(self.clock() - last) if last else None,
            **self.dispatcher.status(),
        }

    def serve_forever(self) -> None:
        from .server import make_server

        srv = make_server(self.cfg.server, self.pipeline, self.status)  # 토큰 검증 실패 시 스레드 시작 전에 중단
        stop = threading.Event()
        threads: list[threading.Thread] = []

        def start(name: str, target, *args) -> None:
            t = threading.Thread(target=target, args=args, name=name, daemon=True)
            t.start()
            threads.append(t)

        if not self.senders:
            log.error("활성화된 발송 경로가 없습니다([kakao] 또는 [telegram] enabled 확인)")
        for s in self.senders:
            ok, why = s.available()
            log.info("발송 경로 %s: %s", s.name, "사용 가능" if ok else f"사용 불가 - {why}")
        if self.cfg.logging.capture_samples:
            log.warning("capture_samples=true: 원본 알림을 %s 에 저장합니다. 규칙 설계가 끝나면 끄세요.",
                        self.cfg.logging.samples_file)

        start("dispatcher", self.dispatcher.run, stop)
        if self.cfg.email.enabled:
            from .ingest.email_imap import ImapPoller

            start("imap", ImapPoller(self.cfg.email, self.pipeline, self.store, self.dispatcher.alert).run, stop)
        if self.cfg.web.enabled:
            from .ingest.kidsnote_web import KidsnoteWebPoller

            log.warning("[web] 실험적 키즈노트 웹 폴링 사용 중 (%d초 간격)", self.cfg.web.interval_seconds)
            start("web", KidsnoteWebPoller(self.cfg.web, self.pipeline, self.store, self.dispatcher.alert,
                                           http=self.http).run, stop)

        start("http", srv.serve_forever)
        log.info("웹훅 대기: http://%s:%d/webhook/android", self.cfg.server.host, self.cfg.server.port)

        def _stop(signum, _frame):
            log.info("종료 신호(%s) 수신", signum)
            stop.set()

        signal.signal(signal.SIGINT, _stop)
        signal.signal(signal.SIGTERM, _stop)
        try:
            while not stop.is_set():
                stop.wait(1)
        finally:
            srv.shutdown()
            srv.server_close()
            for t in threads:
                t.join(timeout=5)
            self.store.close()
            log.info("종료")
