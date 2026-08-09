# kr-stock-watch

DART 주요공시 1분 감시 + 마감 후 데일리 스캔 → 텔레그램 알림.
공개 repo라서 GitHub Actions 사용량은 **무제한 무료**.

## 봇 구성

| 봇 | 파일 | 워크플로 | 실행 |
|---|---|---|---|
| DART 공시 감시 | `dart_watch.py` | dart-watch.yml | cron-job.org가 15분마다 깨움 → 잡 안에서 60초 간격 폴링 (평일 07:20~19:50) |
| 마감 스캔 | `daily_scan.py` | daily-scan.yml | 평일 KST 19:05 (+다음날 08:10 백업, 기준일 중복실행 방지) |

### DART 공시 감시가 잡는 것
1. **주요공시** — 관심종목의 유상증자·CB 발행·자사주·합병·공급계약·소송·거래정지 등
2. **오버행 경고** — 관심종목의 전환가액 조정(리픽싱)·전환청구·신주인수권 행사
3. **5% 대량보유** — 국민연금 등 주요 보고자의 대량보유상황보고서 (전 종목)

### 마감 스캔이 잡는 것
4. **수급** — 외국인 5일↑ 연속 순매수 + 당일 기관 동반 (거래대금 상위 350종목, 네이버 매매동향)
5. **공매도 급증** — 휴면 중 (2026-08 KRX가 로그인에 nProtect 암호화 도입, 봇 로그인 차단 — KRX_ID/KRX_PW 미설정 시 자동 휴면)
6. **신용융자 잔고** — 시장 전체 추이·급증·1년 신고점 (금융투자협회)
7. **52주 신고가/신저가** — 거래량 동반 돌파 (KRX OpenAPI + 다음증권)

## 텔레그램 명령 (봇 대화방에서)

| 명령 | 동작 |
|---|---|
| `/추가 005930 삼성전자` | 관심종목 추가 |
| `/삭제 005930` | 관심종목 제외 (watchlist.csv에 있는 종목도 제외됨) |
| `/목록` | 현재 관심종목·설정 확인 |
| `/유형` | 감시 중인 공시 서식 목록 보기 — **DART 정식 서식명 기준 46종** (유상증자결정, 전환가액의조정, 단일판매ㆍ공급계약체결 등) |
| `/유형 7 끄기` | 7번 서식 알림 중단 (여러 개: `/유형 7 8 9 끄기`, 이름도 가능, `켜기`로 복원) |
| `/전체 31 켜기` | 해당 서식을 관심종목 무관 **전 종목** 구독 (알림에 🌐·시장구분 표시, `/전체`로 목록) |
| `/보고자 추가 이름` | 5% 대량보유 감시 보고자 확대 (기본 국민연금 · 관심종목은 보고자 무관 전부 알림) |
| `/키워드 추가 단어` | 서식 목록에 없는 제목 키워드 직접 추가 |

- 봇 가동 시간(평일 07:20~19:50)에는 **약 1분 안에** 반영·응답.
  저녁(20~23시 정각)·주말(2시간 간격)에도 명령 접수용으로 깨어난다.
- 변경된 목록은 마감 스캔(공매도 등 관심종목 섹션)에도 그대로 쓰인다.
- 명령은 `TELEGRAM_CHAT_ID`로 등록된 대화방만 처리한다 (다른 사람이 봇을 찾아도 무시).

## 최초 설정

### 1. 텔레그램 봇 만들기
1. 텔레그램에서 `@BotFather` 검색 → `/newbot` → 이름/아이디 입력 → **토큰** 받기
2. 만든 봇에게 아무 메시지 전송 후
   `https://api.telegram.org/bot<토큰>/getUpdates` 접속 → `"chat":{"id":숫자}` 가 **챗 ID**

### 2. GitHub Secrets 등록 (Settings → Secrets and variables → Actions)
| 시크릿 | 값 |
|---|---|
| `TELEGRAM_BOT_TOKEN` | 위에서 받은 새 봇 토큰 |
| `TELEGRAM_CHAT_ID` | 위에서 확인한 챗 ID |
| `DART_API_KEY` | opendart.fss.or.kr 인증키 |
| `KRX_AUTH_KEY` | KRX OpenAPI 키 (data.krx.co.kr 신청) |
| `KRX_ID` / `KRX_PW` | (등록 금지 — 휴면) KRX가 2026-08부터 암호화 로그인만 허용, 봇 로그인 실패가 쌓이면 계정 잠금 |
| `GEMINI_API_KEY` | (선택) 규칙 파서가 못 잡는 공시(철회·정정·소송·합병 등)를 Gemini로 요약 — 🤖 표시 |
| `WATCHLIST` | (선택) `005930:삼성전자,000660:SK하이닉스` — 등록하면 watchlist.csv 대신 사용 |

### 3. cron-job.org 세팅 (1분 단위 공시 감시의 핵심)
GitHub 자체 cron은 10~30분씩 밀리므로, cron-job.org가 15분마다 정시에 워크플로를 깨운다.

1. GitHub **파인그레인드 토큰** 발급: Settings → Developer settings → Fine-grained tokens
   → 이 repo만 선택, 권한 **Actions: Read and write** → 토큰 복사
2. [cron-job.org](https://cron-job.org) 새 작업(Create cronjob):
   - **URL**: `https://api.github.com/repos/<계정>/kr-stock-watch/actions/workflows/dart-watch.yml/dispatches`
   - **Schedule**: Every 15 minutes, 평일 07:00~19:45 (KST 기준으로 요일·시간 지정, 타임존 Asia/Seoul 선택)
   - **Advanced**:
     - Request method: `POST`
     - Headers:
       - `Authorization`: `Bearer <파인그레인드 토큰>`
       - `Accept`: `application/vnd.github+json`
       - `Content-Type`: `application/json`
     - Request body: `{"ref":"main","inputs":{"trigger_source":"cron-job.org"}}`
3. 저장 후 "Test run"으로 202 응답 확인 → Actions 탭에 DART Watch 실행이 뜨면 성공

잡 하나가 약 14.5분 동안 60초 간격으로 폴링하므로, 15분마다 깨우면 사실상 끊김 없는 1분 감시가 된다.
GitHub 백업 cron(20분 간격)도 걸려 있어 cron-job.org가 죽어도 감시는 유지된다(정밀도만 하락).

## 운영 메모
- **상태 저장**: Actions cache (`.state/`). 유실돼도 최초 1회 시드만 다시 하고 알림 홍수는 없음.
- **DART 호출량**: 이 봇 약 800~1,000건/일. 같은 키의 kr-earnings-pulse(~1.1만)와 합쳐도 일 2만 한도 내.
- **텔레그램 읽기 타임아웃은 재시도 금지** — 도착으로 간주(중복 발송 사고 예방, common.py).
- 임계값 조정: `FLOW_STREAK`, `FLOW_UNIVERSE`, `HIGH_VOL_MULT`, `SHORT_RATIO_MULT`,
  `CREDIT_SPIKE_PCT` 등 환경변수 (daily_scan.py 상단 참고).
- 로컬 테스트: `.env`에 키 넣고 `DRY_RUN=1 python daily_scan.py`,
  `DRY_RUN=1 POLL_TOTAL_SECONDS=0 python dart_watch.py`
