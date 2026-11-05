"""카카오톡 '나에게 보내기' (POST /v2/api/talk/memo/default/send).

- 액세스 토큰은 약 6시간, 리프레시 토큰은 약 2개월 유효.
- 갱신 응답에는 리프레시 토큰의 남은 기간이 1개월 미만일 때만 새 refresh_token 이 들어온다.
  → 응답에 있으면 반드시 교체 저장, 없으면 기존 값 유지.
- 하루 1번 갱신해 두면 리프레시 토큰이 계속 연장되어 '며칠/몇 달 뒤 조용히 죽는' 일을 막는다.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections.abc import Callable
from pathlib import Path
from urllib.parse import urlencode

from .. import httpclient
from ..httpclient import HttpFunc, NetworkError
from ..util import write_private
from .base import AlertFn, AuthError, QuotaExceeded, Sender, SendError

log = logging.getLogger(__name__)

AUTH_URL = "https://kauth.kakao.com/oauth/authorize"
TOKEN_URL = "https://kauth.kakao.com/oauth/token"
MEMO_URL = "https://kapi.kakao.com/v2/api/talk/memo/default/send"
FORM = "application/x-www-form-urlencoded;charset=utf-8"
TEXT_LIMIT = 200  # 텍스트 템플릿 text 최대 길이

REFRESH_MARGIN = 300  # 만료 5분 전이면 미리 갱신
REFRESH_WARN_DAYS = 7


class KakaoAuthError(AuthError):
    pass


class KakaoClient:
    def __init__(self, rest_api_key: str, redirect_uri: str, token_file: str | Path, client_secret: str = "",
                 http: HttpFunc | None = None, clock: Callable[[], float] = time.time):
        self.rest_api_key = rest_api_key
        self.redirect_uri = redirect_uri
        self.client_secret = client_secret
        self.token_file = Path(token_file)
        self.http = http or httpclient.request
        self.clock = clock
        self._lock = threading.RLock()

    # --- 토큰 저장 -------------------------------------------------------

    def load(self) -> dict | None:
        try:
            return json.loads(self.token_file.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, ValueError) as e:
            raise KakaoAuthError(f"토큰 파일을 읽을 수 없습니다: {e}") from None

    def _save(self, js: dict, prev: dict | None) -> dict:
        now = self.clock()
        tok = dict(prev or {})
        tok["access_token"] = js["access_token"]
        tok["expires_at"] = now + int(js.get("expires_in", 21599))
        if js.get("refresh_token"):
            tok["refresh_token"] = js["refresh_token"]
            tok["refresh_token_expires_at"] = now + int(js.get("refresh_token_expires_in", 60 * 86400))
        if js.get("scope"):
            tok["scope"] = js["scope"]
        tok["updated_at"] = now
        write_private(self.token_file, json.dumps(tok, ensure_ascii=False, indent=2))
        return tok

    # --- OAuth ------------------------------------------------------------

    def authorize_url(self, state: str = "") -> str:
        q = {"client_id": self.rest_api_key, "redirect_uri": self.redirect_uri, "response_type": "code",
             "scope": "talk_message"}
        if state:
            q["state"] = state
        return f"{AUTH_URL}?{urlencode(q)}"

    def _token_request(self, data: dict) -> dict:
        if not self.rest_api_key:
            raise KakaoAuthError("KAKAO_REST_API_KEY 가 비어 있습니다")
        if self.client_secret:
            data["client_secret"] = self.client_secret
        try:
            resp = self.http("POST", TOKEN_URL, data=urlencode(data).encode(), headers={"Content-Type": FORM})
        except NetworkError as e:
            raise SendError(str(e)) from None
        js = resp.json() or {}
        if resp.status == 200 and js.get("access_token"):
            return js
        code = js.get("error_code") or js.get("error") or resp.status
        desc = js.get("error_description", "")
        if resp.status >= 500:
            raise SendError(f"카카오 토큰 서버 오류 {resp.status}")
        hint = ""
        if code == "KOE010":
            hint = " (클라이언트 시크릿을 켰다면 KAKAO_CLIENT_SECRET 필요)"
        elif code in ("KOE320", "KOE322", "invalid_grant"):
            hint = " → 'python -m kidsnote_kakao kakao-auth' 로 다시 인증하세요"
        raise KakaoAuthError(f"카카오 토큰 요청 실패 [{code}] {desc}{hint}".strip())

    def exchange_code(self, code: str) -> dict:
        with self._lock:
            js = self._token_request({"grant_type": "authorization_code", "client_id": self.rest_api_key,
                                      "redirect_uri": self.redirect_uri, "code": code})
            if "talk_message" not in js.get("scope", "talk_message"):
                log.warning("발급된 토큰 scope 에 talk_message 가 없습니다: %s", js.get("scope"))
            return self._save(js, None)

    def refresh(self) -> dict:
        with self._lock:
            cur = self.load()
            if not cur or not cur.get("refresh_token"):
                raise KakaoAuthError("카카오 토큰이 없습니다 → 'python -m kidsnote_kakao kakao-auth' 실행")
            js = self._token_request({"grant_type": "refresh_token", "client_id": self.rest_api_key,
                                      "refresh_token": cur["refresh_token"]})
            tok = self._save(js, cur)
            log.info("카카오 액세스 토큰 갱신%s", " (리프레시 토큰도 교체)" if js.get("refresh_token") else "")
            return tok

    def access_token(self) -> str:
        with self._lock:
            tok = self.load()
            if not tok:
                raise KakaoAuthError("카카오 토큰이 없습니다 → 'python -m kidsnote_kakao kakao-auth' 실행")
            if tok.get("expires_at", 0) - self.clock() < REFRESH_MARGIN:
                tok = self.refresh()
            return tok["access_token"]

    # --- 발송 --------------------------------------------------------------

    def send_memo_text(self, text: str, link_url: str, button_title: str = "") -> None:
        template = {"object_type": "text", "text": text[:TEXT_LIMIT],
                    "link": {"web_url": link_url, "mobile_web_url": link_url}}
        if button_title:
            template["button_title"] = button_title
        body = urlencode({"template_object": json.dumps(template, ensure_ascii=False)}).encode()

        for attempt in range(2):
            token = self.access_token()
            try:
                resp = self.http("POST", MEMO_URL, data=body,
                                 headers={"Authorization": f"Bearer {token}", "Content-Type": FORM})
            except NetworkError as e:
                raise SendError(str(e)) from None
            js = resp.json() or {}
            if resp.status == 200 and js.get("result_code", 0) == 0:
                return
            code = js.get("code")
            msg = js.get("msg", "")
            if (resp.status == 401 or code == -401) and attempt == 0:
                log.info("카카오 401 → 토큰 갱신 후 재시도")
                self.refresh()
                continue
            if code == -401 or resp.status == 401:
                raise KakaoAuthError(f"카카오 토큰 거부(-401) {msg}")
            if code == -402:
                raise KakaoAuthError("talk_message(카카오톡 메시지 전송) 동의가 없습니다 "
                                     "→ 동의항목 확인 후 kakao-auth 재실행")
            if code == -10 or resp.status == 429:
                raise QuotaExceeded(f"카카오 쿼터 초과 {msg}")
            if resp.status >= 500 or code in (-1, -603, -9798):
                raise SendError(f"카카오 일시 오류 {resp.status}/{code} {msg}")
            raise SendError(f"카카오 발송 실패 {resp.status}/{code} {msg}", retryable=False)


class KakaoSender(Sender):
    name = "kakao"
    label = "카카오톡"
    max_len = TEXT_LIMIT

    def __init__(self, client: KakaoClient, link_url: str, button_title: str = "", daily_limit: int = 100,
                 daily_refresh: bool = True):
        super().__init__()
        self.client = client
        self.link_url = link_url
        self.button_title = button_title
        self.daily_limit = daily_limit
        self.daily_refresh = daily_refresh
        self._dead_mtime: float | None = None

    def _mtime(self) -> float | None:
        try:
            return os.path.getmtime(self.client.token_file)
        except OSError:
            return None

    def send_text(self, text: str) -> None:
        self.client.send_memo_text(text, self.link_url, self.button_title)

    def mark_dead(self, reason: str) -> None:
        super().mark_dead(reason)
        self._dead_mtime = self._mtime()

    def available(self) -> tuple[bool, str]:
        if self._dead_reason and self._mtime() != self._dead_mtime:
            log.info("카카오 토큰 파일이 바뀌어 발송을 다시 시도합니다")
            self._dead_reason = ""
        if self._dead_reason:
            return False, self._dead_reason
        if self._mtime() is None:
            return False, "토큰 없음(kakao-auth 필요)"
        return True, ""

    def maintenance(self, now: float, alert: AlertFn) -> None:
        try:
            tok = self.client.load()
        except KakaoAuthError as e:
            alert("kakao:token", str(e))
            return
        if not tok:
            alert("kakao:token", "카카오 토큰이 없습니다 → 'python -m kidsnote_kakao kakao-auth' 실행")
            return
        if self._dead_reason:
            return
        if self.daily_refresh and now - tok.get("updated_at", 0) > 20 * 3600:
            try:
                tok = self.client.refresh()
            except AuthError as e:
                self.mark_dead(str(e))
                alert("kakao:auth", f"카카오 토큰 갱신 실패: {e}")
                return
            except SendError as e:
                log.warning("카카오 정기 갱신 실패(다음에 재시도): %s", e)
        rexp = tok.get("refresh_token_expires_at")
        if rexp and rexp - now < REFRESH_WARN_DAYS * 86400:
            days = max(0, int((rexp - now) // 86400))
            alert("kakao:refresh-expiring",
                  f"카카오 리프레시 토큰이 {days}일 후 만료됩니다 → 'python -m kidsnote_kakao kakao-auth' 로 재인증")

    def status(self) -> dict:
        st = super().status()
        try:
            tok = self.client.load() or {}
        except KakaoAuthError:
            tok = {}
        st["access_expires_at"] = tok.get("expires_at")
        st["refresh_expires_at"] = tok.get("refresh_token_expires_at")
        st["token_updated_at"] = tok.get("updated_at")
        return st
