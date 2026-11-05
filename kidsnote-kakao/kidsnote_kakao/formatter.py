"""대기열 항목 → 메시지 문자열(발송 경로별 길이 제한 반영).

카카오 텍스트 템플릿의 text 는 최대 200자이므로, 여러 건은 한 줄 요약으로 묶고
넘치면 다음 메시지로 나누며, 최대 메시지 수를 넘으면 '…외 N건' 으로 줄인다.
"""

from __future__ import annotations

from .models import QueueItem
from .util import normalize_ws, one_line, truncate

URGENT_MARK = "❗"


def _label(it: QueueItem) -> str:
    return f"{it.category}·{it.child}" if it.child else it.category


def _summary(it: QueueItem) -> str:
    title, body = one_line(it.title), one_line(it.body)
    if not body:
        return title
    if not title or body.startswith(title) or title in ("키즈노트", "Kidsnote", "kidsnote"):
        return body
    return f"{title}: {body}"


def single_message(it: QueueItem, tag: str) -> str:
    mark = URGENT_MARK if it.priority == "urgent" else ""
    head = f"{mark}[{tag}·{it.category}]" + (f" {it.child}" if it.child else "")
    parts = [head]
    title, body = normalize_ws(it.title), normalize_ws(it.body)
    if title and title not in ("키즈노트", "Kidsnote", "kidsnote") and not body.startswith(title):
        parts.append(title)
    if body:
        parts.append(body)
    return "\n".join(parts)


def bullet(it: QueueItem) -> str:
    mark = URGENT_MARK if it.priority == "urgent" else "•"
    return f"{mark} [{_label(it)}] {_summary(it)}"


def build_messages(items: list[QueueItem], max_len: int, max_messages: int, tag: str) -> list[str]:
    if not items or max_messages <= 0:
        return []
    items = sorted(items, key=lambda i: (i.rank, i.created))
    if len(items) == 1:
        return [truncate(single_message(items[0], tag), max_len)]

    head = f"[{tag}] 새 소식 {len(items)}건"
    cont = f"[{tag}] (계속)"
    line_limit = max_len - max(len(head), len(cont)) - 1
    lines = [truncate(bullet(it), line_limit) for it in items]

    chunks: list[list[str]] = []
    cur, cur_len = [head], len(head)
    for ln in lines:
        if cur_len + 1 + len(ln) > max_len and len(cur) > 1:
            chunks.append(cur)
            cur, cur_len = [cont], len(cont)
        cur.append(ln)
        cur_len += 1 + len(ln)
    chunks.append(cur)

    if len(chunks) > max_messages:
        chunks = chunks[:max_messages]
        last = chunks[-1]
        shown = sum(len(c) - 1 for c in chunks)
        hidden = len(lines) - shown
        while True:
            suffix = f"…외 {hidden}건 (키즈노트 앱에서 확인)"
            if len("\n".join(last)) + 1 + len(suffix) <= max_len or len(last) <= 1:
                break
            last.pop()
            hidden += 1
        last.append(truncate(suffix, max_len - len("\n".join(last)) - 1))

    return ["\n".join(c) for c in chunks]
