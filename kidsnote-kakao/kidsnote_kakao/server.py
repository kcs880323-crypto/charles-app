"""웹훅 HTTP 서버 (표준 라이브러리 ThreadingHTTPServer).

  GET  /healthz          인증 없음, {"ok": true} 만 반환
  GET  /status           토큰 필요, 대기열/발송 수/토큰 만료 시각 (알림 내용 없음)
  POST /webhook/android  토큰 필요, 안드로이드 포워더가 호출

토큰은 'Authorization: Bearer <WEBHOOK_TOKEN>' 또는 'X-Webhook-Token: <WEBHOOK_TOKEN>' 헤더로.
URL 쿼리로 토큰을 받지 않는다(접근 로그/프록시 로그에 남기 때문).
"""

from __future__ import annotations

import hmac
import json
import logging
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

from .config import ServerConfig
from .ingest.android import PayloadError, decode_body, parse_payload
from .pipeline import Pipeline

log = logging.getLogger(__name__)


def make_handler(cfg: ServerConfig, pipeline: Pipeline, status: Callable[[], dict]):
    token = cfg.webhook_token.encode()

    class Handler(BaseHTTPRequestHandler):
        server_version = "kidsnote-kakao"
        sys_version = ""
        timeout = 15  # 느린 클라이언트가 스레드를 붙잡지 않게

        def log_message(self, fmt, *args):  # 쿼리스트링/헤더를 남기지 않는다
            log.debug("%s %s %s", self.command, urlsplit(self.path).path, args[1] if len(args) > 1 else "")

        def _json(self, code: int, obj: dict) -> None:
            body = json.dumps(obj, ensure_ascii=False).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _authorized(self) -> bool:
            got = self.headers.get("X-Webhook-Token", "")
            auth = self.headers.get("Authorization", "")
            if auth.lower().startswith("bearer "):
                got = auth[7:].strip()
            return bool(token) and hmac.compare_digest(got.encode(), token)

        def do_GET(self):
            path = urlsplit(self.path).path
            if path == "/healthz":
                return self._json(200, {"ok": True})
            if path == "/status":
                if not self._authorized():
                    return self._json(401, {"error": "unauthorized"})
                return self._json(200, status())
            return self._json(404, {"error": "not found"})

        def do_POST(self):
            path = urlsplit(self.path).path
            if path not in ("/webhook/android", "/webhook/android/"):
                return self._json(404, {"error": "not found"})
            if not self._authorized():
                log.warning("인증 실패한 웹훅 요청 (%s)", self.client_address[0])
                return self._json(401, {"error": "unauthorized"})
            try:
                length = int(self.headers.get("Content-Length", ""))
            except ValueError:
                return self._json(411, {"error": "Content-Length required"})
            if length > cfg.max_body_bytes:
                return self._json(413, {"error": "too large"})
            body = self.rfile.read(length)
            try:
                data = decode_body(body, self.headers.get("Content-Type", ""))
                n = parse_payload(data)
            except PayloadError as e:
                return self._json(400, {"error": str(e)})
            try:
                res = pipeline.submit(n, raw=data)
            except Exception:
                log.exception("알림 처리 중 오류")
                return self._json(500, {"error": "internal"})
            return self._json(200, res.to_dict())

    return Handler


def make_server(cfg: ServerConfig, pipeline: Pipeline, status: Callable[[], dict]) -> ThreadingHTTPServer:
    if not cfg.webhook_token or len(cfg.webhook_token) < 16:
        raise ValueError("WEBHOOK_TOKEN 이 없거나 너무 짧습니다(16자 이상). "
                         "예: python3 -c \"import secrets;print(secrets.token_urlsafe(32))\"")
    srv = ThreadingHTTPServer((cfg.host, cfg.port), make_handler(cfg, pipeline, status))
    srv.daemon_threads = True
    return srv
