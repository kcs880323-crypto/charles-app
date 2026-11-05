"""명령줄 진입점.

  serve         웹훅 서버 + 발송기 + (선택) IMAP/웹 폴러 상시 구동
  kakao-auth    카카오 최초 인증(1회) → 토큰 파일 저장
  kakao-refresh 토큰 강제 갱신(점검용)
  test-send     발송 경로로 테스트 메시지 1건
  check         알림 문구가 어떻게 분류·표시되는지 미리보기(보내지 않음)
  status        로컬 상태 출력
  web-children  [실험적] 키즈노트 웹에서 자녀 ID 찾기
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import secrets
import sys
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from . import __version__
from .config import Config, ConfigError, load_config
from .formatter import build_messages
from .ingest.android import PayloadError, parse_payload
from .models import Notification, QueueItem
from .rules import RuleEngine
from .senders import AuthError, SendError, build_senders, make_kakao_client

log = logging.getLogger("kidsnote_kakao")


def _fmt_ts(ts: float | None, tz) -> str:
    if not ts:
        return "-"
    return datetime.fromtimestamp(ts, tz).strftime("%Y-%m-%d %H:%M")


# --- kakao-auth ---------------------------------------------------------------


class AuthCodeError(Exception):
    pass


def extract_code(text: str, state: str) -> str:
    text = text.strip()
    if not text:
        raise AuthCodeError("입력이 비어 있습니다")
    if "://" not in text and "code=" not in text:
        return text  # code 값만 붙여넣은 경우
    q = parse_qs(urlsplit(text).query if "://" in text else text.lstrip("?"))
    if "error" in q:
        raise AuthCodeError(f"카카오가 오류를 돌려줬습니다: {q.get('error')} {q.get('error_description', [''])[0]}")
    if state and q.get("state", [state])[0] != state:
        raise AuthCodeError("state 값이 다릅니다(다른 요청의 주소일 수 있음). 다시 시도하세요.")
    code = q.get("code", [""])[0]
    if not code:
        raise AuthCodeError("주소에 code 가 없습니다")
    return code


def _wait_for_redirect(redirect_uri: str, timeout: float) -> str | None:
    parts = urlsplit(redirect_uri)
    result: dict = {}

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            if urlsplit(self.path).path != (parts.path or "/"):
                self.send_response(404)
                self.end_headers()
                return
            result["url"] = f"{parts.scheme}://{parts.netloc}{self.path}"
            body = "<meta charset='utf-8'><h3>카카오 인증 완료 — 이 창은 닫아도 됩니다.</h3>".encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    try:
        srv = HTTPServer((parts.hostname or "127.0.0.1", parts.port or 80), H)
    except OSError as e:
        print(f"  (콜백 포트를 열 수 없습니다: {e} → 주소 붙여넣기 방식으로 진행)")
        return None
    srv.timeout = 1
    deadline = time.monotonic() + timeout
    try:
        while "url" not in result and time.monotonic() < deadline:
            srv.handle_request()
    except KeyboardInterrupt:
        print()
    finally:
        srv.server_close()
    return result.get("url")


def cmd_kakao_auth(cfg: Config, args) -> int:
    client = make_kakao_client(cfg)
    if not cfg.kakao.rest_api_key:
        print("KAKAO_REST_API_KEY 가 없습니다(.env 또는 환경변수).", file=sys.stderr)
        return 2
    state = secrets.token_urlsafe(16)
    print("1) 아래 주소를 브라우저에서 열어 카카오 로그인 후 '카카오톡 메시지 전송'에 동의하세요.\n")
    print("   " + client.authorize_url(state) + "\n")
    host = urlsplit(cfg.kakao.redirect_uri).hostname
    url = None
    if not args.manual and host in ("localhost", "127.0.0.1"):
        print(f"2) {cfg.kakao.redirect_uri} 로 돌아오기를 기다립니다(최대 5분, Ctrl+C 로 건너뛰기).")
        print("   원격 서버(SSH)에서 실행 중이면 'ssh -L 8765:localhost:8765 서버' 로 접속하거나 --manual 을 쓰세요.")
        url = _wait_for_redirect(cfg.kakao.redirect_uri, 300)
    if not url:
        url = input("2) 로그인 후 이동한 주소창의 전체 URL(또는 code 값)을 붙여넣으세요:\n> ")
    try:
        code = extract_code(url, state)
        tok = client.exchange_code(code)
    except (AuthCodeError, AuthError, SendError) as e:
        print(f"실패: {e}", file=sys.stderr)
        return 1
    tz = RuleEngine(cfg.rules).tz
    print(f"\n토큰 저장: {client.token_file}")
    print(f"  액세스 토큰 만료  : {_fmt_ts(tok.get('expires_at'), tz)} (자동 갱신)")
    print(f"  리프레시 토큰 만료: {_fmt_ts(tok.get('refresh_token_expires_at'), tz)} (매일 갱신으로 자동 연장)")
    if args.test:
        try:
            client.send_memo_text("[키즈노트 브리지] 카카오 연결 테스트입니다 ✅", cfg.kakao.link_url,
                                  cfg.kakao.button_title)
            print("테스트 메시지를 보냈습니다. 카카오톡 '나와의 채팅'을 확인하세요.")
        except SendError as e:
            print(f"테스트 발송 실패: {e}", file=sys.stderr)
            return 1
    return 0


def cmd_kakao_refresh(cfg: Config, args) -> int:
    client = make_kakao_client(cfg)
    try:
        tok = client.refresh()
    except SendError as e:
        print(f"실패: {e}", file=sys.stderr)
        return 1
    tz = RuleEngine(cfg.rules).tz
    print(f"갱신 완료. 액세스 만료 {_fmt_ts(tok.get('expires_at'), tz)}, "
          f"리프레시 만료 {_fmt_ts(tok.get('refresh_token_expires_at'), tz)}")
    return 0


# --- test-send ----------------------------------------------------------------


def cmd_test_send(cfg: Config, args) -> int:
    senders = build_senders(cfg)
    if args.sender != "all":
        senders = [s for s in senders if s.name == args.sender]
    if not senders:
        print("보낼 수 있는 경로가 없습니다(설정의 enabled 확인).", file=sys.stderr)
        return 2
    rc = 0
    for s in senders:
        ok, why = s.available()
        if not ok:
            print(f"{s.name}: 사용 불가 - {why}")
            rc = 1
            continue
        try:
            s.send_text(args.text[: s.max_len])
            print(f"{s.name}: 보냄")
        except SendError as e:
            print(f"{s.name}: 실패 - {e}")
            rc = 1
    return rc


# --- check --------------------------------------------------------------------


def _load_samples(path: Path) -> list[Notification]:
    out = []
    for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
            out.append(parse_payload(obj.get("payload", obj)))
        except (ValueError, PayloadError) as e:
            print(f"  {path}:{i} 건너뜀 ({e})", file=sys.stderr)
    return out


def cmd_check(cfg: Config, args) -> int:
    rules = RuleEngine(cfg.rules)
    if args.file:
        notes = _load_samples(Path(args.file))
    else:
        if not args.text and not args.title:
            print("문구를 주거나 --file 을 지정하세요.", file=sys.stderr)
            return 2
        notes = [Notification(source="android", title=args.title or "", body=" ".join(args.text),
                              app=args.app or "", package=args.package or "")]
    now = time.time()
    accepted: list[QueueItem] = []
    for n in notes:
        d = rules.classify(n)
        head = f"[{n.title}] " if n.title else ""
        print(f"- {head}{n.body[:60]}")
        if not d.accepted:
            print(f"    → 버림: {d.reason}")
            continue
        hold = rules.hold_until(d.priority, now)
        when = "즉시" if hold - now < 1 else f"{_fmt_ts(hold, rules.tz)} 이후"
        print(f"    → {d.category} / {d.priority}{' / ' + d.child if d.child else ''} / 발송: {when}")
        accepted.append(QueueItem(created=now, hold_until=hold, priority=d.priority, category=d.category,
                                  child=d.child, title=n.title, body=n.body, source=n.source))
    if accepted:
        print("\n카카오톡 미리보기(200자 제한):")
        for it in ([[a] for a in accepted] if args.each else [accepted]):
            for m in build_messages(it, 200, cfg.delivery.max_messages_per_flush, cfg.delivery.tag):
                print("┌" + "─" * 30)
                for ln in m.splitlines():
                    print("│ " + ln)
                print(f"└ ({len(m)}자)")
    return 0


# --- status / web-children ------------------------------------------------------


def cmd_status(cfg: Config, args) -> int:
    from .app import App

    app = App(cfg)
    try:
        print(json.dumps(app.status(), ensure_ascii=False, indent=2))
    finally:
        app.store.close()
    return 0


def cmd_web_children(cfg: Config, args) -> int:
    from .ingest.kidsnote_web import KidsnoteWebPoller, SessionExpired
    from .store import Store

    store = Store(":memory:")
    poller = KidsnoteWebPoller(cfg.web, None, store, lambda *_: None)  # type: ignore[arg-type]
    try:
        kids = poller.discover_children()
    except SessionExpired as e:
        print(f"세션 오류: {e}", file=sys.stderr)
        return 1
    except Exception as e:
        print(f"실패: {e}", file=sys.stderr)
        return 1
    finally:
        store.close()
    if not kids:
        print("자녀 정보를 찾지 못했습니다(응답 형식이 바뀌었을 수 있음). 브라우저 개발자도구 Network 탭에서 "
              "/children/<숫자>/ 형태의 요청을 찾아 ID를 확인하세요.")
        return 1
    for k in kids:
        print(f'[[web.children]]\nid = "{k["id"]}"\nname = "{k["name"]}"  # {k["class"]}\n')
    return 0


def cmd_serve(cfg: Config, args) -> int:
    from .app import App

    try:
        App(cfg).serve_forever()
    except ValueError as e:
        print(f"시작 실패: {e}", file=sys.stderr)
        return 2
    except OSError as e:  # 포트 사용 중 등
        print(f"시작 실패({cfg.server.host}:{cfg.server.port}): {e}", file=sys.stderr)
        return 2
    return 0


# --- init (설정 마법사) -----------------------------------------------------------

EXAMPLE_CONFIG = Path(__file__).resolve().parent.parent / "config.example.toml"


def _read_env(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, _, v = line.partition("=")
                out[k.strip()] = v.strip().strip("'\"")
    return out


def _write_env(path: Path, values: dict[str, str]) -> None:
    """기존 .env 의 주석/순서를 유지하며 빈 값만 채운다(이미 값이 있으면 건드리지 않음)."""
    from .util import write_private

    lines = path.read_text(encoding="utf-8").splitlines() if path.is_file() else []
    done = set()
    for i, line in enumerate(lines):
        if "=" in line and not line.lstrip().startswith("#"):
            k = line.split("=", 1)[0].strip()
            if k in values and not line.split("=", 1)[1].strip():
                lines[i] = f"{k}={values[k]}"
            done.add(k)
    lines += [f"{k}={v}" for k, v in values.items() if k not in done]
    write_private(path, "\n".join(lines) + "\n")


def cmd_init(args) -> int:
    cfg_path = Path(args.config or "config.toml").expanduser().resolve()
    base = cfg_path.parent
    if not cfg_path.exists():
        if not EXAMPLE_CONFIG.is_file():
            print(f"예시 설정을 찾을 수 없습니다: {EXAMPLE_CONFIG}", file=sys.stderr)
            return 2
        cfg_path.write_text(EXAMPLE_CONFIG.read_text(encoding="utf-8"), encoding="utf-8")
        print(f"[1/3] 설정 파일 생성: {cfg_path}")
    else:
        print(f"[1/3] 설정 파일 유지: {cfg_path}")

    env_path = base / ".env"
    if not env_path.exists():
        example = EXAMPLE_CONFIG.parent / ".env.example"
        if example.is_file():
            from .util import write_private

            write_private(env_path, example.read_text(encoding="utf-8"))
    env = _read_env(env_path)
    updates: dict[str, str] = {}
    if not env.get("WEBHOOK_TOKEN"):
        updates["WEBHOOK_TOKEN"] = secrets.token_urlsafe(32)
    key = args.kakao_key or env.get("KAKAO_REST_API_KEY", "")
    if not key and not args.yes:
        key = input("카카오 REST API 키를 붙여넣으세요(나중에 하려면 그냥 Enter): ").strip()
    if key and not env.get("KAKAO_REST_API_KEY"):
        updates["KAKAO_REST_API_KEY"] = key
    secret = args.kakao_secret or ""
    if key and not secret and not env.get("KAKAO_CLIENT_SECRET") and not args.yes:
        secret = input("Client Secret 을 켰다면 붙여넣으세요(안 켰으면 Enter): ").strip()
    if secret and not env.get("KAKAO_CLIENT_SECRET"):
        updates["KAKAO_CLIENT_SECRET"] = secret
    _write_env(env_path, updates)
    env = _read_env(env_path)
    print(f"[2/3] 비밀값 파일: {env_path} (WEBHOOK_TOKEN {'생성됨' if 'WEBHOOK_TOKEN' in updates else '유지'})")
    print(f"      폰 포워더 앱에 넣을 헤더 → Authorization: Bearer {env.get('WEBHOOK_TOKEN', '')}")

    if not env.get("KAKAO_REST_API_KEY"):
        print("[3/3] 카카오 키가 없어 인증은 건너뜁니다. 키를 받은 뒤 다시 'init' 을 실행하세요.")
        return 0
    if args.no_auth:
        print("[3/3] 다음: python -m kidsnote_kakao kakao-auth --test")
        return 0
    print("[3/3] 카카오 인증을 시작합니다.\n")
    for k, v in env.items():
        if v:
            os.environ[k] = v
    try:
        cfg = load_config(cfg_path)
    except ConfigError as e:
        print(f"설정 오류: {e}", file=sys.stderr)
        return 2
    args.test = True
    return cmd_kakao_auth(cfg, args)


# --- main ---------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="kidsnote_kakao", description="키즈노트 알림 → 카카오톡 나에게 보내기 브리지")
    p.add_argument("-c", "--config", help="설정 파일(기본: ./config.toml 또는 $KIDSNOTE_KAKAO_CONFIG)")
    p.add_argument("-v", "--verbose", action="store_true")
    p.add_argument("--version", action="version", version=__version__)
    sub = p.add_subparsers(dest="cmd", required=True)

    i = sub.add_parser("init", help="처음 설정: config.toml/.env 만들기 + 카카오 인증까지 한 번에")
    i.add_argument("--kakao-key", default="", help="카카오 REST API 키(안 주면 물어봄)")
    i.add_argument("--kakao-secret", default="", help="클라이언트 시크릿(켠 경우)")
    i.add_argument("--no-auth", action="store_true", help="파일만 만들고 카카오 인증은 나중에")
    i.add_argument("--manual", action="store_true", help="카카오 인증 시 주소 붙여넣기 방식")
    i.add_argument("-y", "--yes", action="store_true", help="묻지 않기")
    i.set_defaults(func=None)

    sub.add_parser("serve", help="상시 구동").set_defaults(func=cmd_serve)

    a = sub.add_parser("kakao-auth", help="카카오 최초 인증(토큰 발급)")
    a.add_argument("--manual", action="store_true", help="로컬 콜백 서버 없이 주소 붙여넣기")
    a.add_argument("--test", action="store_true", help="발급 후 테스트 메시지 발송")
    a.set_defaults(func=cmd_kakao_auth)

    sub.add_parser("kakao-refresh", help="카카오 토큰 강제 갱신").set_defaults(func=cmd_kakao_refresh)

    t = sub.add_parser("test-send", help="테스트 메시지 발송")
    t.add_argument("--sender", choices=["all", "kakao", "telegram"], default="all")
    t.add_argument("--text", default="[키즈노트 브리지] 테스트 메시지입니다 ✅")
    t.set_defaults(func=cmd_test_send)

    c = sub.add_parser("check", help="분류/표시 미리보기(발송 안 함)")
    c.add_argument("text", nargs="*", help="알림 본문")
    c.add_argument("--title", default="")
    c.add_argument("--app", default="")
    c.add_argument("--package", default="")
    c.add_argument("--file", help="samples.jsonl (capture_samples 로 모은 파일)")
    c.add_argument("--each", action="store_true", help="묶지 않고 한 건씩 미리보기")
    c.set_defaults(func=cmd_check)

    sub.add_parser("status", help="상태 출력").set_defaults(func=cmd_status)
    sub.add_parser("web-children", help="[실험적] 키즈노트 웹에서 자녀 ID 찾기").set_defaults(func=cmd_web_children)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.cmd == "init":
        logging.basicConfig(level=logging.INFO, format="%(message)s")
        return cmd_init(args)
    try:
        cfg = load_config(args.config)
    except ConfigError as e:
        print(f"설정 오류: {e}", file=sys.stderr)
        return 2
    level = logging.DEBUG if args.verbose else getattr(logging, cfg.logging.level.upper(), logging.INFO)
    logging.basicConfig(level=level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    return args.func(cfg, args)
