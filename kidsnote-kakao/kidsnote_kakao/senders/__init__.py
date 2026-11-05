from __future__ import annotations

from ..config import Config
from ..httpclient import HttpFunc
from .base import AuthError, QuotaExceeded, Sender, SendError
from .kakao import KakaoClient, KakaoSender
from .telegram import TelegramSender

__all__ = ["AuthError", "QuotaExceeded", "Sender", "SendError", "KakaoClient", "KakaoSender", "TelegramSender",
           "make_kakao_client", "build_senders"]


def make_kakao_client(cfg: Config, http: HttpFunc | None = None) -> KakaoClient:
    k = cfg.kakao
    return KakaoClient(k.rest_api_key, k.redirect_uri, cfg.path(k.token_file), k.client_secret, http=http)


def build_senders(cfg: Config, http: HttpFunc | None = None) -> list[Sender]:
    """delivery.order 순서대로, enabled 인 것만."""
    made: dict[str, Sender] = {}
    if cfg.kakao.enabled:
        k = cfg.kakao
        made["kakao"] = KakaoSender(make_kakao_client(cfg, http), k.link_url, k.button_title, k.daily_limit,
                                    k.daily_token_refresh)
    if cfg.telegram.enabled:
        t = cfg.telegram
        made["telegram"] = TelegramSender(t.bot_token, t.chat_id, t.daily_limit, http=http)
    return [made[n] for n in cfg.delivery.order if n in made]
