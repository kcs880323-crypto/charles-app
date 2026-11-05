"""문자열/로그 관련 작은 도우미."""

from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path

_WS = re.compile(r"[ \t\r\f\v ]+")
_MANY_NL = re.compile(r"\n{3,}")


def normalize_ws(text: str) -> str:
    """줄 안의 공백은 하나로, 빈 줄은 최대 1개로 줄인다."""
    if not text:
        return ""
    lines = [_WS.sub(" ", ln).strip() for ln in text.replace("\r\n", "\n").split("\n")]
    out = "\n".join(lines).strip()
    return _MANY_NL.sub("\n\n", out)


def one_line(text: str) -> str:
    """모든 공백(줄바꿈 포함)을 공백 하나로."""
    return " ".join((text or "").split())


def truncate(text: str, limit: int, ellipsis: str = "…") -> str:
    if limit <= 0:
        return ""
    if len(text) <= limit:
        return text
    if limit <= len(ellipsis):
        return text[:limit]
    return text[: limit - len(ellipsis)].rstrip() + ellipsis


def short_hash(text: str, n: int = 8) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()[:n]


def redact(text: str, show: bool) -> str:
    """로그용. 기본은 내용 대신 길이+해시만 남긴다(아이 정보 최소화)."""
    if show:
        return repr(truncate(one_line(text), 80))
    return f"<{len(text or '')}자 #{short_hash(text)}>"


def write_private(path: Path, data: str) -> None:
    """0600 권한으로 원자적 저장 (토큰/샘플 파일용)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    try:
        os.chmod(path, 0o600)
    except OSError:  # Windows 등
        pass


def append_private(path: Path, line: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "a", encoding="utf-8") as f:
        f.write(line.rstrip("\n") + "\n")
