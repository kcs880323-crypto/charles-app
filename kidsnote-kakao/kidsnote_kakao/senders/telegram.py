"""텔레그램 봇 폴백 발송. 카카오 토큰이 죽어도 알림이 끊기지 않게 한다."""

from __future__ import annotations

import json

from .. import httpclient
from ..httpclient import HttpFunc, NetworkError
from .base import AuthError, QuotaExceeded, Sender, SendError


class TelegramSender(Sender):
    name = "telegram"
    label = "텔레그램"
    max_len = 4000  # API 한도 4096

    def __init__(self, bot_token: str, chat_id: str, daily_limit: int = 0, http: HttpFunc | None = None):
        super().__init__()
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.daily_limit = daily_limit
        self.http = http or httpclient.request

    def available(self) -> tuple[bool, str]:
        if not self.bot_token or not self.chat_id:
            return False, "TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID 없음"
        return super().available()

    def send_text(self, text: str) -> None:
        url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
        payload = {"chat_id": self.chat_id, "text": text[: self.max_len], "disable_web_page_preview": True}
        try:
            resp = self.http("POST", url, data=json.dumps(payload).encode(),
                             headers={"Content-Type": "application/json"})
        except NetworkError as e:
            raise SendError(str(e)) from None
        js = resp.json() or {}
        if resp.status == 200 and js.get("ok"):
            return
        desc = js.get("description", "")
        if resp.status == 429:
            raise QuotaExceeded(f"텔레그램 속도 제한 {desc}")
        if resp.status in (401, 403, 404):
            raise AuthError(f"텔레그램 봇 토큰/채팅 ID 확인 필요 ({resp.status}) {desc}")
        if resp.status >= 500:
            raise SendError(f"텔레그램 일시 오류 {resp.status}")
        raise SendError(f"텔레그램 발송 실패 {resp.status} {desc}", retryable=False)
