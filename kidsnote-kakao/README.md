# kidsnote-kakao — 키즈노트 알림 → 카카오톡 '나에게 보내기'

키즈노트 알림(알림장·공지·투약·귀가·준비물·앨범 등)을 받아 **규칙으로 거르고, 중복을 없애고, 묶어서**
내 카카오톡 '나와의 채팅'으로 보내는 개인용 브리지입니다.
**Python 3.11 이상, 표준 라이브러리만** 사용하므로 `pip install` 없이 라즈베리파이·NAS·미니PC·VM 어디서든 돌아갑니다.

```
 [안드로이드 폰]                          [내 서버: kidsnote_kakao serve]
 키즈노트 앱 알림                          ┌───────────────────────────────────────────┐
   └ MacroDroid / Tasker ── HTTPS ──────▶ │ POST /webhook/android  (토큰 인증)         │
       (알림 가로채기)     (Cloudflare     │   ↓                                       │
                            Tunnel /       │ 규칙: 앱 필터 → 제외어 → 자녀 → 유형/우선순위 │
 [메일함] ── IMAP (선택) ──  Tailscale)    │   ↓                                       │
 [키즈노트 웹] ── 폴링 (선택·실험적) ────▶ │ 중복 제거(해시) → 대기열(SQLite)            │
                                           │   ↓                                       │
                                           │ 디스패처: 즉시 / 묶음 / 조용한 시간 / 다이제스트│
                                           │   ↓                                       │
                                           │ 카카오 나에게 보내기 ──(실패·토큰만료)──▶ 텔레그램│
                                           └───────────────────────────────────────────┘
```

## 왜 이 구조인가

| 제약 | 대응 |
|---|---|
| 키즈노트는 학부모용 공개 API가 없다 | **안드로이드 알림을 가로채 웹훅으로** 보내는 방식이 기본(지연 1~3초). 메일·웹 폴링은 보조 |
| 아이폰은 다른 앱의 알림을 읽을 수 없다 | iOS만 있다면 IMAP(메일 알림이 올 때) 또는 웹 폴링(실험적)만 가능 |
| 개인이 카카오톡으로 자동 발송할 현실적 경로는 '나에게 보내기'뿐 | `talk/memo/default/send` 사용. 알림톡은 사업자·채널·템플릿 검수가 필요해 제외 |
| 액세스 토큰은 약 6시간, 리프레시 토큰은 약 2개월 | 만료 5분 전 자동 갱신 + 401 시 갱신 후 재시도 + **하루 1번 정기 갱신**(리프레시 토큰은 남은 기간이 1개월 미만일 때 갱신 응답에 새로 실려 오므로, 매일 갱신하면 계속 연장됨) + 만료 7일 전 경고 |
| 카카오 텍스트 템플릿은 **최대 200자**, `link` 필수 | 여러 건은 한 줄 요약으로 묶고, 넘치면 메시지를 나누거나 `…외 N건` |
| 카카오가 죽으면 알림이 조용히 끊긴다 | 발송부를 인터페이스로 분리, **텔레그램 폴백** + 장애 경고 |

## 빠른 시작

### 0. 카카오 디벨로퍼스 앱 준비 (수동 1회, 약 30분)

[developers.kakao.com](https://developers.kakao.com) → 내 애플리케이션 → 애플리케이션 추가 후
(콘솔 메뉴 이름은 개편에 따라 조금 다를 수 있습니다)

1. **앱 키**에서 **REST API 키** 복사
2. **카카오 로그인** 활성화
3. **Redirect URI** 에 `http://localhost:8765/kakao/callback` 등록 (config 의 `kakao.redirect_uri` 와 똑같이)
4. **동의항목**에서 **카카오톡 메시지 전송(talk_message)** 을 '선택 동의'로 설정
5. **플랫폼 > Web > 사이트 도메인**에 `https://www.kidsnote.com` 등록 (메시지 안 링크/버튼이 열리려면 필요)
6. (선택) **보안 > Client Secret** 을 켰다면 그 값도 복사 → `.env` 의 `KAKAO_CLIENT_SECRET`

### 1. 설치·설정 — 명령 하나로

```bash
git clone <이 저장소> && cd charles-app/kidsnote-kakao
python3 -m kidsnote_kakao init
```

`init` 이 하는 일: `config.toml` 생성 → `.env` 생성(권한 0600) + `WEBHOOK_TOKEN` 자동 생성 →
카카오 REST API 키(와 Client Secret) 입력받기 → 바로 2단계 카카오 인증 + 테스트 메시지까지.
다시 실행해도 이미 있는 값은 덮어쓰지 않습니다. 폰 포워더에 넣을 `Authorization` 헤더 값도 출력해 줍니다.

**Windows PC** 라면 `deploy\windows-setup.bat` 을 더블클릭하세요(Python 3.11+ 확인 → 시간대 데이터 설치 → `init`).

수동으로 하려면: `cp config.example.toml config.toml`, `cp .env.example .env && chmod 600 .env` 후
`.env` 에 `WEBHOOK_TOKEN`(16자 이상), `KAKAO_REST_API_KEY`(+ `KAKAO_CLIENT_SECRET`) 입력.

비밀값(토큰·비밀번호·쿠키)은 **`.env` 또는 환경변수에만** 둡니다. `config.toml`, `.env`, `data/` 는 `.gitignore` 에 들어 있습니다.

### 2. 카카오 토큰 발급 + 카톡 1건 수신 확인 (`init` 에서 이미 했다면 건너뛰기)

```bash
python3 -m kidsnote_kakao kakao-auth --test
```

출력된 주소를 브라우저에서 열고 로그인·동의하면 `data/kakao_token.json`(권한 0600)이 생기고,
'나와의 채팅'에 테스트 메시지가 옵니다.
서버에 SSH로 접속해 실행 중이라면 `ssh -L 8765:localhost:8765 서버` 로 접속하거나 `--manual`(주소창 URL 붙여넣기)을 쓰세요.

### 3. 서버 실행 + 웹훅 확인

```bash
python3 -m kidsnote_kakao serve
# 다른 터미널에서
curl -s -X POST http://127.0.0.1:8787/webhook/android \
  -H "Authorization: Bearer $(grep ^WEBHOOK_TOKEN .env | cut -d= -f2)" \
  -H "Content-Type: application/json" \
  -d '{"title":"해바라기반","text":"지우 오후 투약 완료했습니다","app":"키즈노트"}'
# → {"status":"queued","category":"투약","priority":"urgent",...} 그리고 카톡 수신
```

### 4. 폰에서 서버로 오는 길 열기 (HTTPS)

서버는 기본적으로 `127.0.0.1` 에만 열립니다. 폰이 접근하려면 터널을 씁니다.

- **Cloudflare Tunnel**: `cloudflared tunnel --url http://localhost:8787` (빠른 테스트용 임시 주소).
  상시용은 이름 있는 터널 + 내 도메인(`https://kn.example.com`)으로 설정. 필요하면 Cloudflare Access 로 한 번 더 막기.
- **Tailscale**: 폰과 서버를 같은 tailnet 에 넣고 `tailscale serve` 로 `https://<서버>.<tailnet>.ts.net` → `localhost:8787` 연결.
  인터넷에 노출되지 않아 가장 안전합니다.

어느 쪽이든 웹훅은 `WEBHOOK_TOKEN` 헤더 없이는 401 입니다. 토큰을 URL 쿼리로 받지 않습니다(로그에 남기 때문).

### 5. 안드로이드 연결 → [아래 '안드로이드 포워더 설정'](#안드로이드-포워더-설정)

### 6. 상시 구동

리눅스(라즈베리파이·NAS·VM):

```bash
sudo cp deploy/kidsnote-kakao.service /etc/systemd/system/   # User/WorkingDirectory 수정
sudo systemctl daemon-reload && sudo systemctl enable --now kidsnote-kakao
journalctl -u kidsnote-kakao -f
```

Windows: `deploy\windows-autostart.bat` 실행 → 작업 스케줄러에 '로그인 시 실행'으로 등록하고 바로 시작합니다.
로그는 `data\bridge.log`, 해제는 `schtasks /Delete /TN kidsnote-kakao /F`.
PC 가 절전/종료되면 알림도 멈추므로, 상시 켜 둘 기기(미니PC·NAS 등)가 더 적합합니다.

## 안드로이드 포워더 설정

공통 준비: 키즈노트 앱 알림 켜기 · 포워더 앱에 **알림 접근 권한** 허용 · 포워더 앱 **배터리 최적화 제외**
(안 하면 Doze 모드에서 몇 시간 뒤 조용히 멈춥니다) · 폰은 충전기에 꽂아 상시 켜 두기.

### 웹훅 형식

`POST /webhook/android` · 헤더 `Authorization: Bearer <WEBHOOK_TOKEN>` (또는 `X-Webhook-Token`)

| 본문 형식 | 예 |
|---|---|
| JSON | `{"title":"..","text":"..","big_text":"..","app":"키즈노트","package":"com.vaultmicro.kidsnote"}` |
| 폼 | `title=..&text=..` (`application/x-www-form-urlencoded`) |
| 텍스트 | 본문 전체를 알림 내용으로 (`text/plain`) — JSON 이스케이프가 어려운 앱에서 가장 안전 |

키 이름은 별칭도 받습니다(`antitle`/`antext`/`anbigtext`/`anapp`/`anpackage`, `not_title`/`notification`/`not_app_name` 등).
치환되지 않은 변수(`%antitle`, `[not_title]`)는 빈 값으로 처리합니다. `big_text`(펼친 전문)가 있으면 그걸 본문으로 씁니다.

### MacroDroid (무료 매크로 5개로 충분)

1. 트리거: **알림 수신(Notification Received)** → 앱 선택: 키즈노트 → 내용: 아무거나
2. 액션: **HTTP 요청(HTTP Request)**
   - 방식 POST, URL `https://<터널 주소>/webhook/android`
   - 헤더 `Authorization: Bearer <WEBHOOK_TOKEN>`
   - 본문: Content-Type `text/plain`, 내용에 매직 텍스트 `[notification]`
     (제목도 받고 싶으면 JSON 으로 `{"title":"[not_title]","text":"[notification]","app":"[not_app_name]"}` — 단, 알림 문구에 큰따옴표가 섞이면 JSON 이 깨질 수 있음)

매직 텍스트 이름은 앱 버전마다 다를 수 있으니 편집기의 매직 텍스트 목록에서 고르세요.

### Tasker + AutoNotification

1. 프로필: 이벤트 → 플러그인 → AutoNotification → **Intercept** (Apps: 키즈노트, Action Type: Created)
2. 태스크
   - **JavaScriptlet** (따옴표·줄바꿈이 있어도 안전한 JSON 만들기)
     ```js
     var payload = JSON.stringify({title: antitle, text: antext, big_text: anbigtext, app: anapp, package: anpackage});
     ```
   - **HTTP Request**: POST, URL `https://<터널 주소>/webhook/android`,
     Headers `Authorization:Bearer <WEBHOOK_TOKEN>` / `Content-Type:application/json`, Body `%payload`

### 자체 앱(NotificationListenerService)

`onNotificationPosted` 에서 `EXTRA_TITLE`, `EXTRA_TEXT`, `EXTRA_BIG_TEXT`, `packageName` 을 위 JSON 으로 POST 하면 됩니다.
실패 시 로컬 큐에 쌓았다가 재전송하도록 만들면 가장 견고합니다.

## 규칙 튜닝 (실제 알림 문구로)

기본 유형 키워드는 '그럴듯한 추정'입니다. **실제 알림 원문 5~10개를 모아서** 맞추세요.

```bash
# 1) 며칠 동안 원문 수집 (data/samples.jsonl, 0600) — 끝나면 다시 false 로!
#    config.toml: [logging] capture_samples = true
# 2) 수집한 원문이 어떻게 분류·표시되는지 미리보기 (발송 안 함)
python3 -m kidsnote_kakao check --file data/samples.jsonl
python3 -m kidsnote_kakao check --title "해바라기반" "내일 현장학습, 우비 챙겨주세요"
```

```
- [해바라기반] 내일 현장학습, 우비 챙겨주세요
    → 준비물 / normal / 발송: 즉시
카카오톡 미리보기(200자 제한):
┌──────────────────────────────
│ [키즈노트·준비물]
│ 해바라기반
│ 내일 현장학습, 우비 챙겨주세요
└ (34자)
```

`config.toml` 의 `[rules]` 에서 조정합니다.

- `[[rules.categories]]`: `name`, `keywords`(부분 문자열), `patterns`(정규식), `priority` — 같은 이름이면 기본값 대체.
  여러 유형에 걸리면 **우선순위가 높은 쪽**(urgent > normal > low)이 이깁니다.
- `exclude_keywords` / `exclude_patterns`: 광고·그룹 요약 알림 등 버리기. `include_keywords`: 화이트리스트.
- `[[rules.children]]`: 자녀 이름/별칭으로 구분, 자녀별 `enabled`, `categories`(이것만 받기), `mute_categories`.
- `allowed_packages` / `allowed_app_names`: 키즈노트 앱 알림만 통과(패키지명은 Play 스토어 URL 의 `id=` 값으로 확인).

## 발송 정책

| 우선순위 | 기본 유형 | 언제 보내나 |
|---|---|---|
| `urgent` | 긴급·투약·귀가 | **즉시**(2초 이내). 조용한 시간도 무시(`urgent_bypass_quiet`) |
| `normal` | 결석·준비물·원복·행사·알림장·공지·일정·댓글·기타 | 마지막 알림 후 `batch_window_seconds`(60초) 조용하면 묶어서. 계속 와도 최대 `batch_max_wait_seconds`(300초) |
| `low` | 앨범·식단 | `digest_times`(12:30, 19:00)에 모아서 |
| `mute` | (설정 예: 식단) | 버림 |

- **조용한 시간**(`quiet_hours = "22:00-07:00"`): urgent 외에는 끝나는 시각에 모아서 발송.
- **중복 제거**: 제목+본문 해시(웹/메일은 글 ID)로 `dedup_hours`(72시간) 안의 같은 알림은 무시. 저장되는 건 해시뿐입니다.
- **발송량**: `kakao.daily_limit`(기본 100, 자체 안전 상한). 남은 양이 `low_quota_reserve` 이하이면 1시간 단위 묶음(절약 모드),
  0 이면 다음 경로(텔레그램)로. ※ '나에게 보내기'의 실제 쿼터는 콘솔의 쿼터 메뉴에서 확인하세요.
- **폴백**: `delivery.mode = "fallback"` 이면 카카오 실패 시 텔레그램, `"broadcast"` 면 둘 다.
- **재시도**: 전부 실패하면 30초부터 지수 백오프(최대 30분). 72시간 넘게 못 보낸 항목은 버리고 경고.
- 대기열은 SQLite 에 있으므로 재시작해도 잃지 않습니다.

### 텔레그램 폴백 켜기 (10분)

@BotFather 로 봇 생성 → 봇과 대화 시작 → `https://api.telegram.org/bot<TOKEN>/getUpdates` 에서 `chat.id` 확인 →
`.env` 에 `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` → `config.toml` 에서 `[telegram] enabled = true`
→ `python3 -m kidsnote_kakao test-send --sender telegram`

## 모니터링 · 경고

- `GET /healthz` (인증 없음) → `{"ok": true}`
- `GET /status` (토큰 필요) 또는 `python3 -m kidsnote_kakao status` → 대기열 수, 경로별 오늘 발송 수, 카카오 토큰 만료 시각, 마지막 수집 시각
- 자동 경고(같은 종류는 6시간에 1번, 살아 있는 경로로 발송)
  - 카카오 토큰 없음/갱신 실패/동의 누락 → 카카오 발송 중단 후 텔레그램으로 경고
  - 리프레시 토큰 만료 7일 전
  - 72시간 넘게 못 보내 버린 알림
  - `silence_alert_hours`(예: 72) 동안 수집이 없을 때 — 폰 꺼짐·권한 해제 감지
  - IMAP 로그인 실패, 키즈노트 웹 세션 만료
- `monitor.heartbeat_url`: [healthchecks.io](https://healthchecks.io) 같은 데드맨 스위치. 브리지 자체가 죽으면 그쪽이 알려 줍니다.

## 보조 수집 경로

### IMAP 메일 감시 (`[email]`)

키즈노트가 메일 알림을 보내는 경우에만 쓸모 있습니다. 메일함을 **읽기 전용**으로 열고(읽음 표시 안 바꿈)
`from_filter` 가 들어간 발신자의 새 메일만 봅니다. 첫 실행은 과거 메일을 보내지 않고 기준점만 잡습니다.
Gmail 은 2단계 인증 + 앱 비밀번호를 `IMAP_PASSWORD` 에.

### 키즈노트 웹 폴링 (`[web]`, 실험적)

- **비공식 내부 API**(`/api/v1_2/children/{id}/reports/` 등)라 언제든 깨질 수 있고, **이용약관상 문제가 될 수 있습니다.** 판단은 본인 몫입니다.
- 비밀번호를 저장하지 않습니다. 브라우저에서 키즈노트에 로그인 → 개발자도구 Network 탭 → 아무 API 요청의 `Cookie` 헤더 값을 `KIDSNOTE_SESSION_COOKIE` 에.
- 자녀 ID: `python3 -m kidsnote_kakao web-children` (실패하면 Network 탭에서 `/children/<숫자>/` 확인).
- 최소 5분 간격(기본 15분) + 지터. 세션이 만료되면 재로그인을 시도하지 않고 6시간 멈추며 경고만 보냅니다.
- 첫 실행은 기존 글을 '본 것'으로만 기록합니다.

## 보안 · 개인정보

- 웹훅·상태 API 는 16자 이상 토큰 필수(상수 시간 비교), 본문 64KB 제한, 기본 `127.0.0.1` 바인딩 → 터널로만 노출
- 로그에는 알림 내용 대신 `<길이 #해시>` 만 남김(`log_content = false`). 텔레그램 봇 토큰이 든 URL 은 오류 메시지에도 남기지 않음
- 토큰 파일·샘플 파일은 0600, systemd 유닛은 `UMask=0077` + `ProtectSystem=strict`
- 알림 본문은 발송 대기 중에만 SQLite 에 있고 발송 후 삭제. 중복 판정용으로는 해시만 보관

## 문제 해결

| 증상 | 원인/해결 |
|---|---|
| `KOE010` | Client Secret 을 켰는데 `KAKAO_CLIENT_SECRET` 이 없음 |
| `KOE006` / redirect 오류 | 콘솔의 Redirect URI 와 `kakao.redirect_uri` 가 글자 하나까지 같아야 함 |
| `KOE320`/`KOE322`, 갱신 실패 | 인가 코드/리프레시 토큰 만료 → `kakao-auth` 다시 실행(서버 재시작 없이 자동 복구) |
| `-402` | talk_message 동의 누락 → 동의항목 확인 후 `kakao-auth` |
| 메시지는 오는데 버튼/링크가 안 열림 | 플랫폼 > Web 사이트 도메인에 `link_url` 도메인 등록 |
| 웹훅이 401 | 헤더 `Authorization: Bearer <WEBHOOK_TOKEN>` 확인(앞뒤 공백 주의) |
| 몇 시간 뒤 알림이 끊김 | 포워더 앱 배터리 최적화 제외, 알림 접근 권한 재확인. `silence_alert_hours` 켜기 |
| 제목에 `%antitle` 이 보임 | 서버가 걸러 주지만, Tasker 쪽 변수명을 확인 |
| 다른 앱 알림도 옴/키즈노트가 버려짐 | `check --package ... --app ...` 로 확인 후 `allowed_packages`/`allowed_app_names` 조정 |

## 명령 모음

```bash
python3 -m kidsnote_kakao init                  # 처음 설정(파일 생성 + 카카오 인증)
python3 -m kidsnote_kakao serve                 # 상시 구동
python3 -m kidsnote_kakao kakao-auth [--manual] [--test]
python3 -m kidsnote_kakao kakao-refresh         # 토큰 강제 갱신
python3 -m kidsnote_kakao test-send [--sender kakao|telegram|all] [--text ...]
python3 -m kidsnote_kakao check [문구...] [--title ..] [--file samples.jsonl] [--each]
python3 -m kidsnote_kakao status
python3 -m kidsnote_kakao web-children          # 실험적
# 공통: -c config.toml, -v(디버그 로그)
```

## 개발

```bash
python3 -m unittest discover -s tests     # 또는 pytest
```

외부 호출(카카오/텔레그램/키즈노트/IMAP)은 전부 가짜 객체로 대체해 테스트합니다.

| 모듈 | 역할 |
|---|---|
| `ingest/android.py` · `email_imap.py` · `kidsnote_web.py` | 수집 경로 → 공통 `Notification` |
| `pipeline.py` | 규칙 → 중복 → 대기열 (모든 경로가 지나는 한 곳) |
| `rules.py` | 유형/우선순위/자녀/조용한 시간/다이제스트 |
| `dispatcher.py` | 묶음·즉시·재시도·폴백·발송량·경고·정기 점검 |
| `formatter.py` | 200자 제한 안에서 메시지 만들기 |
| `senders/kakao.py` · `telegram.py` | 발송 경로(토큰 갱신 포함) |
| `server.py` · `cli.py` · `app.py` | 웹훅 서버, 명령줄, 조립 |

### 다음 단계 아이디어

- 키즈노트 알림 원문을 모은 뒤 유형 키워드·제외어 확정
- 가족 공유: 가족 각자 `kakao-auth` 로 토큰을 받아 여러 수신자에게 보내기(각자 OAuth 필요)
- '내일 준비물'만 모아 전날 저녁에 한 번 더 리마인드
- 자체 안드로이드 앱(NotificationListenerService + 오프라인 큐)으로 Tasker/MacroDroid 대체
