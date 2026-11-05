"""규칙 엔진: 앱 필터 → 제외어 → 자녀 → 유형/우선순위 → 보류 시각.

우선순위
  urgent : 즉시 발송(조용한 시간 무시 가능)  예) 투약, 귀가, 긴급
  normal : 짧게 모아서 발송                 예) 알림장, 공지, 준비물
  low    : 다이제스트 시각에 모아서 발송     예) 앨범, 식단
  mute   : 버림
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime

from .config import CategoryRule, RulesConfig
from .models import PRIORITY_RANK, Notification
from .timeutil import get_tz, in_range, next_at, parse_hhmm, parse_range

# 실제 알림 문구 샘플을 모은 뒤 config.toml 에서 덮어쓰거나 추가할 것.
DEFAULT_CATEGORIES: list[CategoryRule] = [
    CategoryRule("긴급", ["긴급", "응급", "다쳤", "다쳐", "부상", "발열", "열이 나", "열이나", "병원"],
                 priority="urgent"),
    CategoryRule("투약", ["투약", "복약", "약 복용"], priority="urgent"),
    CategoryRule("귀가", ["귀가", "하원 시간", "하원시간", "하원 방법", "귀가동의"], priority="urgent"),
    CategoryRule("결석", ["결석", "조퇴", "지각"], priority="normal"),
    CategoryRule("준비물", ["준비물", "챙겨", "지참", "가져와", "보내주세요", "보내 주세요"], priority="normal"),
    CategoryRule("원복", ["원복", "체육복", "생활복", "단체복"], priority="normal"),
    CategoryRule("행사", ["행사", "견학", "현장학습", "소풍", "발표회", "참여수업", "운동회", "상담"],
                 priority="normal"),
    CategoryRule("알림장", ["알림장"], priority="normal"),
    CategoryRule("공지", ["공지", "가정통신문", "안내"], priority="normal"),
    CategoryRule("일정", ["일정"], priority="normal"),
    CategoryRule("댓글", ["댓글", "답글"], priority="normal"),
    CategoryRule("앨범", ["앨범", "사진", "동영상"], priority="low"),
    CategoryRule("식단", ["식단"], priority="low"),
]


@dataclass
class Decision:
    accepted: bool
    reason: str = ""
    category: str = ""
    priority: str = "normal"
    child: str = ""


@dataclass
class _Cat:
    name: str
    keywords: list[str]
    patterns: list[re.Pattern]
    priority: str
    order: int

    def matches(self, low_text: str, text: str) -> bool:
        return any(k in low_text for k in self.keywords) or any(p.search(text) for p in self.patterns)


def _compile(patterns: list[str], where: str) -> list[re.Pattern]:
    out = []
    for p in patterns:
        try:
            out.append(re.compile(p, re.IGNORECASE))
        except re.error as e:
            raise ValueError(f"{where}: 잘못된 정규식 {p!r}: {e}") from None
    return out


class RuleEngine:
    def __init__(self, cfg: RulesConfig):
        self.cfg = cfg
        self.tz = get_tz(cfg.timezone)
        self.quiet = parse_range(cfg.quiet_hours) if cfg.quiet_hours.strip() else None
        self.digest_times = [parse_hhmm(t) for t in cfg.digest_times]
        self.exclude_kw = [k.lower() for k in cfg.exclude_keywords if k]
        self.exclude_re = _compile(cfg.exclude_patterns, "exclude_patterns")
        self.include_kw = [k.lower() for k in cfg.include_keywords if k]
        self.packages = {p.lower() for p in cfg.allowed_packages}
        self.app_names = [a.lower() for a in cfg.allowed_app_names]

        merged: dict[str, CategoryRule] = {}
        if cfg.use_default_categories:
            merged.update({c.name: c for c in DEFAULT_CATEGORIES})
        for c in cfg.categories:  # 같은 이름이면 사용자 설정이 기본값을 대체
            merged[c.name] = c
        user_names = [c.name for c in cfg.categories]
        ordered = user_names + [n for n in merged if n not in user_names]
        self.categories = [
            _Cat(
                name=n,
                keywords=[k.lower() for k in merged[n].keywords if k],
                patterns=_compile(merged[n].patterns, f"category {n}"),
                priority=merged[n].priority,
                order=i,
            )
            for i, n in enumerate(ordered)
        ]

    # --- 분류 -------------------------------------------------------------

    def _source_allowed(self, n: Notification) -> bool:
        if n.source != "android":
            return True
        if not n.package and not n.app:
            return True  # 포워더 앱 쪽에서 이미 키즈노트만 걸렀다고 본다
        if not self.packages and not self.app_names:
            return True
        if n.package and n.package.lower() in self.packages:
            return True
        app = n.app.lower()
        return bool(app) and any(a in app for a in self.app_names)

    def _detect_child(self, n: Notification, text: str):
        if n.child_hint:
            for c in self.cfg.children:
                if n.child_hint == c.name or n.child_hint in c.aliases:
                    return c.name, c
            return n.child_hint, None
        for c in self.cfg.children:
            for name in [c.name, *c.aliases]:
                if name and name in text:
                    return c.name, c
        return "", None

    def classify(self, n: Notification) -> Decision:
        if not self._source_allowed(n):
            return Decision(False, f"다른 앱 알림({n.package or n.app})")
        text = n.text
        if not text:
            return Decision(False, "빈 알림")
        low = text.lower()
        if any(k in low for k in self.exclude_kw) or any(p.search(text) for p in self.exclude_re):
            return Decision(False, "제외어")
        if self.include_kw and not any(k in low for k in self.include_kw):
            return Decision(False, "포함어 없음")

        child, crule = self._detect_child(n, text)
        if crule is not None and not crule.enabled:
            return Decision(False, f"자녀 비활성({child})", child=child)

        matched = [c for c in self.categories if c.matches(low, text)]
        if matched:
            best = min(matched, key=lambda c: (PRIORITY_RANK.get(c.priority, 1), c.order))
            category, priority = best.name, best.priority
        else:
            category, priority = self.cfg.default_category, self.cfg.default_priority

        if crule is not None:
            if crule.categories is not None and category not in crule.categories:
                return Decision(False, f"{child}: 받지 않는 유형({category})", category, priority, child)
            if category in crule.mute_categories:
                return Decision(False, f"{child}: 음소거 유형({category})", category, priority, child)
        if priority == "mute":
            return Decision(False, f"음소거 유형({category})", category, priority, child)
        return Decision(True, "", category, priority, child)

    # --- 보류(발송 가능) 시각 --------------------------------------------

    def in_quiet(self, ts: float) -> bool:
        if not self.quiet:
            return False
        return in_range(datetime.fromtimestamp(ts, self.tz).time(), *self.quiet)

    def hold_until(self, priority: str, now: float) -> float:
        if priority == "urgent" and self.cfg.urgent_bypass_quiet:
            return now
        dt = datetime.fromtimestamp(now, self.tz)
        if priority == "low" and self.digest_times:
            target = min(next_at(dt, t) for t in self.digest_times)
        else:
            target = dt
        if self.quiet and in_range(target.time(), *self.quiet):
            target = next_at(target, self.quiet[1])
        return max(now, target.timestamp())

    def next_digest(self, now: float) -> float | None:
        if not self.digest_times:
            return None
        dt = datetime.fromtimestamp(now, self.tz)
        return min(next_at(dt, t) for t in self.digest_times).timestamp()

    def day_key(self, ts: float) -> str:
        return datetime.fromtimestamp(ts, self.tz).strftime("%Y-%m-%d")
