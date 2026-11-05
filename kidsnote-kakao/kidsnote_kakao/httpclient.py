"""urllib 기반 최소 HTTP 클라이언트. 테스트에서 함수 하나로 바꿔 끼울 수 있게 한다.

오류 메시지에 URL을 넣지 않는다(텔레그램 봇 토큰이 URL에 들어가므로).
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field


class NetworkError(Exception):
    pass


@dataclass
class Response:
    status: int
    body: bytes = b""
    headers: dict = field(default_factory=dict)

    def json(self):
        try:
            return json.loads(self.body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return None

    @property
    def text(self) -> str:
        return self.body.decode("utf-8", errors="replace")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):  # noqa: D401 - 리다이렉트 따라가지 않음
        return None


HttpFunc = Callable[..., Response]


def request(method: str, url: str, data: bytes | None = None, headers: dict | None = None,
            timeout: float = 15.0, follow_redirects: bool = True) -> Response:
    req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    opener = urllib.request.build_opener() if follow_redirects else urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(req, timeout=timeout) as resp:
            return Response(resp.status, resp.read(), dict(resp.headers.items()))
    except urllib.error.HTTPError as e:
        try:
            body = e.read()
        except Exception:
            body = b""
        return Response(e.code, body, dict(e.headers.items()) if e.headers else {})
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        reason = getattr(e, "reason", e)
        raise NetworkError(f"네트워크 오류: {type(e).__name__}: {reason}") from None
