"""발송 경로 공통 인터페이스. 카카오가 죽어도 텔레그램으로 넘어가도록 분리한다."""

from __future__ import annotations

from collections.abc import Callable


class SendError(Exception):
    def __init__(self, msg: str, retryable: bool = True):
        super().__init__(msg)
        self.retryable = retryable


class QuotaExceeded(SendError):
    pass


class AuthError(SendError):
    """사람이 손봐야 하는 오류(토큰 만료, 동의 누락, 잘못된 봇 토큰 등)."""

    def __init__(self, msg: str):
        super().__init__(msg, retryable=False)


AlertFn = Callable[[str, str], object]


class Sender:
    name: str = "base"
    label: str = "발송"
    max_len: int = 1000
    daily_limit: int = 0  # 0 = 자체 상한 없음

    def __init__(self) -> None:
        self._dead_reason = ""

    def send_text(self, text: str) -> None:  # pragma: no cover - 인터페이스
        raise NotImplementedError

    def available(self) -> tuple[bool, str]:
        if self._dead_reason:
            return False, self._dead_reason
        return True, ""

    def mark_dead(self, reason: str) -> None:
        self._dead_reason = reason

    def maintenance(self, now: float, alert: AlertFn) -> None:
        """주기 점검(토큰 갱신 등). 기본은 아무것도 안 함."""

    def status(self) -> dict:
        ok, why = self.available()
        return {"available": ok, "reason": why}
