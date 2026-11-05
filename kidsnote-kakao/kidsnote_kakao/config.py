"""설정 로더: config.toml + .env + 환경변수(비밀값은 환경변수 우선).

비밀값(토큰·비밀번호·쿠키)은 config.toml 대신 .env 또는 환경변수에 둔다.
"""

from __future__ import annotations

import logging
import os
import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


class ConfigError(Exception):
    pass


# --- 섹션별 설정 ---------------------------------------------------------


@dataclass
class ServerConfig:
    host: str = "127.0.0.1"  # 외부 노출은 Cloudflare Tunnel / Tailscale 로. 0.0.0.0 비권장.
    port: int = 8787
    webhook_token: str = ""  # 환경변수 WEBHOOK_TOKEN
    max_body_bytes: int = 64 * 1024


@dataclass
class KakaoConfig:
    enabled: bool = True
    rest_api_key: str = ""  # 환경변수 KAKAO_REST_API_KEY
    client_secret: str = ""  # 환경변수 KAKAO_CLIENT_SECRET (앱에서 클라이언트 시크릿을 켰다면 필수)
    redirect_uri: str = "http://localhost:8765/kakao/callback"
    token_file: str = "data/kakao_token.json"
    link_url: str = "https://www.kidsnote.com"  # [플랫폼 > Web] 사이트 도메인에 등록해야 링크가 동작
    button_title: str = "키즈노트 열기"
    daily_limit: int = 100  # 자체 안전 상한(0=무제한)
    daily_token_refresh: bool = True  # 하루 1번 갱신 → 리프레시 토큰이 자동 연장되도록


@dataclass
class TelegramConfig:
    enabled: bool = False
    bot_token: str = ""  # 환경변수 TELEGRAM_BOT_TOKEN
    chat_id: str = ""  # 환경변수 TELEGRAM_CHAT_ID
    daily_limit: int = 0


@dataclass
class DeliveryConfig:
    mode: str = "fallback"  # fallback: 순서대로 하나만 성공하면 끝 / broadcast: 모두에게
    order: list[str] = field(default_factory=lambda: ["kakao", "telegram"])
    tag: str = "키즈노트"  # 메시지 머리말 [키즈노트]
    batch_window_seconds: int = 60  # 마지막 알림 후 이만큼 조용하면 묶어서 발송
    batch_max_wait_seconds: int = 300  # 계속 들어와도 최대 이만큼 기다리면 발송
    max_messages_per_flush: int = 3  # 한 번에 보낼 최대 메시지 수(넘치면 '…외 N건')
    low_quota_reserve: int = 10  # 남은 발송량이 이 이하이면 절약 모드
    low_quota_batch_seconds: int = 3600  # 절약 모드에서 묶음 간격
    retry_base_seconds: int = 30
    retry_max_seconds: int = 1800
    max_queue_age_hours: int = 72  # 이보다 오래 못 보낸 항목은 버리고 경고


@dataclass
class CategoryRule:
    name: str
    keywords: list[str] = field(default_factory=list)  # 부분 문자열(대소문자 무시)
    patterns: list[str] = field(default_factory=list)  # 정규식
    priority: str = "normal"  # urgent | normal | low | mute


@dataclass
class ChildRule:
    name: str
    aliases: list[str] = field(default_factory=list)
    enabled: bool = True
    categories: list[str] | None = None  # 지정하면 이 유형만 받음
    mute_categories: list[str] = field(default_factory=list)


@dataclass
class RulesConfig:
    timezone: str = "Asia/Seoul"
    quiet_hours: str = ""  # 예: "22:00-07:00" (빈 값이면 사용 안 함)
    urgent_bypass_quiet: bool = True
    digest_times: list[str] = field(default_factory=lambda: ["12:30", "19:00"])  # low 우선순위 발송 시각
    exclude_keywords: list[str] = field(default_factory=list)
    exclude_patterns: list[str] = field(default_factory=list)
    include_keywords: list[str] = field(default_factory=list)  # 비어 있지 않으면 하나라도 있어야 통과
    default_category: str = "기타"
    default_priority: str = "normal"
    use_default_categories: bool = True
    categories: list[CategoryRule] = field(default_factory=list)
    children: list[ChildRule] = field(default_factory=list)
    # 안드로이드: 키즈노트 앱 알림만 통과 (Play 스토어 URL의 id= 값으로 확인)
    allowed_packages: list[str] = field(default_factory=lambda: ["com.vaultmicro.kidsnote"])
    allowed_app_names: list[str] = field(default_factory=lambda: ["키즈노트", "kidsnote", "kids note"])


@dataclass
class EmailConfig:
    enabled: bool = False
    host: str = ""
    port: int = 993
    username: str = ""
    password: str = ""  # 환경변수 IMAP_PASSWORD (앱 비밀번호 권장)
    mailbox: str = "INBOX"
    from_filter: str = "kidsnote"
    interval_seconds: int = 120


@dataclass
class WebChild:
    id: str
    name: str = ""


@dataclass
class WebEndpoint:
    kind: str
    path: str  # {child_id} 치환


@dataclass
class WebPollConfig:
    """실험적: 키즈노트 웹 내부 API 폴링. 비공식이라 언제든 깨질 수 있고 약관 리스크가 있다."""

    enabled: bool = False
    base_url: str = "https://www.kidsnote.com"
    session_cookie: str = ""  # 환경변수 KIDSNOTE_SESSION_COOKIE (브라우저에서 복사한 Cookie 헤더 값)
    children: list[WebChild] = field(default_factory=list)
    endpoints: list[WebEndpoint] = field(
        default_factory=lambda: [WebEndpoint("알림장", "/api/v1_2/children/{child_id}/reports/?page_size=10")]
    )
    me_path: str = "/api/v1/me/info"
    interval_seconds: int = 900  # 최소 300초로 강제
    user_agent: str = "Mozilla/5.0 (personal notification bridge)"


@dataclass
class StorageConfig:
    path: str = "data/state.db"
    dedup_hours: int = 72


@dataclass
class LoggingConfig:
    level: str = "INFO"
    log_content: bool = False  # True면 알림 내용 일부를 로그에 남김(디버그용)
    capture_samples: bool = False  # True면 원본 웹훅 페이로드를 samples_file에 저장(규칙 설계용, 민감정보 주의)
    samples_file: str = "data/samples.jsonl"


@dataclass
class MonitorConfig:
    heartbeat_url: str = ""  # healthchecks.io 같은 데드맨 스위치 URL (선택)
    heartbeat_interval_seconds: int = 300
    silence_alert_hours: int = 0  # N시간 동안 수집이 없으면 경고(폰 꺼짐 감지). 0=끔
    alert_cooldown_seconds: int = 6 * 3600


@dataclass
class Config:
    server: ServerConfig = field(default_factory=ServerConfig)
    kakao: KakaoConfig = field(default_factory=KakaoConfig)
    telegram: TelegramConfig = field(default_factory=TelegramConfig)
    delivery: DeliveryConfig = field(default_factory=DeliveryConfig)
    rules: RulesConfig = field(default_factory=RulesConfig)
    email: EmailConfig = field(default_factory=EmailConfig)
    web: WebPollConfig = field(default_factory=WebPollConfig)
    storage: StorageConfig = field(default_factory=StorageConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)
    monitor: MonitorConfig = field(default_factory=MonitorConfig)
    base_dir: Path = field(default_factory=Path.cwd)

    def path(self, p: str) -> Path:
        pp = Path(p).expanduser()
        return pp if pp.is_absolute() else (self.base_dir / pp)


# --- 로딩 ----------------------------------------------------------------

ENV_OVERRIDES: dict[str, tuple[str, str]] = {
    "WEBHOOK_TOKEN": ("server", "webhook_token"),
    "KAKAO_REST_API_KEY": ("kakao", "rest_api_key"),
    "KAKAO_CLIENT_SECRET": ("kakao", "client_secret"),
    "TELEGRAM_BOT_TOKEN": ("telegram", "bot_token"),
    "TELEGRAM_CHAT_ID": ("telegram", "chat_id"),
    "IMAP_PASSWORD": ("email", "password"),
    "KIDSNOTE_SESSION_COOKIE": ("web", "session_cookie"),
}

_SIMPLE_SECTIONS = {
    "server": ServerConfig,
    "kakao": KakaoConfig,
    "telegram": TelegramConfig,
    "delivery": DeliveryConfig,
    "email": EmailConfig,
    "storage": StorageConfig,
    "logging": LoggingConfig,
    "monitor": MonitorConfig,
}


def load_dotenv(path: Path) -> None:
    """KEY=VALUE 형식의 .env를 읽어 아직 없는 환경변수만 채운다."""
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[7:]
        key, _, val = line.partition("=")
        key, val = key.strip(), val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in "'\"":
            val = val[1:-1]
        os.environ.setdefault(key, val)


def _build(cls: type, data: Any, section: str):
    if not isinstance(data, dict):
        raise ConfigError(f"[{section}] 는 테이블이어야 합니다")
    names = {f.name for f in fields(cls)}
    unknown = sorted(set(data) - names)
    if unknown:
        raise ConfigError(f"[{section}] 알 수 없는 항목: {', '.join(unknown)}")
    try:
        return cls(**data)
    except TypeError as e:
        raise ConfigError(f"[{section}] {e}") from None


def _build_rules(data: dict) -> RulesConfig:
    data = dict(data)
    cats = data.pop("categories", [])
    kids = data.pop("children", [])
    rc = _build(RulesConfig, data, "rules")
    rc.categories = [_build(CategoryRule, c, "rules.categories") for c in cats]
    rc.children = [_build(ChildRule, c, "rules.children") for c in kids]
    return rc


def _build_web(data: dict) -> WebPollConfig:
    data = dict(data)
    kids = data.pop("children", [])
    eps = data.pop("endpoints", None)
    wc = _build(WebPollConfig, data, "web")
    wc.children = [
        WebChild(id=str(c["id"]), name=str(c.get("name", ""))) if isinstance(c, dict) else WebChild(id=str(c))
        for c in kids
    ]
    if eps is not None:
        wc.endpoints = [_build(WebEndpoint, e, "web.endpoints") for e in eps]
    return wc


def load_config(path: str | os.PathLike | None = None) -> Config:
    p = Path(path or os.environ.get("KIDSNOTE_KAKAO_CONFIG", "config.toml")).expanduser()
    base = p.resolve().parent
    data: dict = {}
    if p.is_file():
        with p.open("rb") as f:
            try:
                data = tomllib.load(f)
            except tomllib.TOMLDecodeError as e:
                raise ConfigError(f"{p}: TOML 문법 오류: {e}") from None
    elif path is not None:
        raise ConfigError(f"설정 파일이 없습니다: {p}  (config.example.toml 을 복사해서 만드세요)")
    else:
        log.warning("config.toml 이 없어 기본값으로 실행합니다 (%s)", p)

    load_dotenv(base / ".env")

    cfg = Config(base_dir=base)
    known = set(_SIMPLE_SECTIONS) | {"rules", "web"}
    unknown = sorted(set(data) - known)
    if unknown:
        raise ConfigError(f"알 수 없는 섹션: {', '.join(unknown)}")
    for name, cls in _SIMPLE_SECTIONS.items():
        if name in data:
            setattr(cfg, name, _build(cls, data[name], name))
    if "rules" in data:
        cfg.rules = _build_rules(data["rules"])
    if "web" in data:
        cfg.web = _build_web(data["web"])

    for env, (section, attr) in ENV_OVERRIDES.items():
        val = os.environ.get(env)
        if val:
            setattr(getattr(cfg, section), attr, val)

    _validate(cfg)
    return cfg


def _validate(cfg: Config) -> None:
    if cfg.delivery.mode not in ("fallback", "broadcast"):
        raise ConfigError("[delivery] mode 는 fallback 또는 broadcast")
    for name in cfg.delivery.order:
        if name not in ("kakao", "telegram"):
            raise ConfigError(f"[delivery] order 에 알 수 없는 발송 경로: {name}")
    prios = {"urgent", "normal", "low", "mute"}
    if cfg.rules.default_priority not in prios:
        raise ConfigError("[rules] default_priority 는 urgent/normal/low/mute 중 하나")
    for c in cfg.rules.categories:
        if c.priority not in prios:
            raise ConfigError(f"[rules.categories] {c.name}: priority 는 urgent/normal/low/mute 중 하나")
    if cfg.web.interval_seconds < 300:
        log.warning("[web] interval_seconds 가 너무 짧아 300초로 올립니다(차단 위험)")
        cfg.web.interval_seconds = 300
    if cfg.server.host not in ("127.0.0.1", "localhost", "::1"):
        log.warning("[server] host=%s: 외부에 직접 노출됩니다. 터널(Cloudflare/Tailscale) 사용을 권장합니다.",
                    cfg.server.host)
