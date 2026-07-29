# ==========================================
# 🚨 DART 주요공시 1분 감시 (아이디어 1·2·3)
#   1) 관심종목 주요공시 키워드 알림
#   2) 관심종목 CB 리픽싱·전환청구 등 오버행 경고
#   3) 국민연금 등 주요 보고자의 5% 대량보유 공시
#
# 실행 방식: cron-job.org가 15분마다 workflow_dispatch를 깨우면
#   잡 하나가 POLL_TOTAL_SECONDS 동안 POLL_INTERVAL 간격으로 반복 폴링.
#   (kr-earnings-pulse에서 검증된 "15분 잡 내부 폴링" 패턴)
# DART 호출량: 분당 1~3건 × 하루 약 12시간 ≈ 800~1,000건/일
#   (같은 키를 쓰는 kr-earnings-pulse ~1.1만 건과 합쳐도 일 한도 2만 건 이내)
# ==========================================
import os
import re
import time

import requests

from common import KST, UA_HEADERS, esc, load_state, load_watchlist, now_kst, save_state, tg_send

DART_LIST_URL = "https://opendart.fss.or.kr/api/list.json"
DART_VIEWER = "https://dart.fss.or.kr/dsaf001/main.do?rcpNo="

POLL_TOTAL_SECONDS = int(os.environ.get("POLL_TOTAL_SECONDS", "870"))   # 잡 하나가 폴링하는 총 시간
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL", "60"))              # 폴링 간격(초)
STATE_FILE = "dart_seen.json"
SEEN_CAP = 8000

# 감시 시간대(KST): DART 공시는 대략 07:30~19:30에 나온다
WINDOW_START = (7, 20)
WINDOW_END = (19, 50)

# 1) 주요공시 키워드 (관심종목 대상)
MAJOR_RE = re.compile(
    r"유상증자|무상증자|전환사채|신주인수권부사채|교환사채|자기주식|소각|감자|주식병합|주식분할"
    r"|회사분할|분할합병|합병|주식교환|주식이전|공개매수|영업양수|영업양도|최대주주변경|경영권"
    r"|공급계약|수주|소송|회생절차|파산|해산사유|매매거래정지|상장폐지|관리종목|불성실공시"
    r"|횡령|배임|유형자산취득|유형자산양수|타법인주식및출자증권취득|타법인주식및출자증권처분"
    r"|조회공시|풍문또는보도|영업실적|잠정.{0,3}실적|매출액또는손익|배당|자산재평가|감사의견"
)

# 2) 오버행(물량 부담) 신호 (관심종목 대상)
OVERHANG_RE = re.compile(
    r"전환가액[의]?조정|전환청구권[의]?행사|신주인수권[의]?행사|행사가액[의]?조정"
    r"|교환가액[의]?조정|교환청구권|조기상환청구"
)

# 3) 대량보유 보고자 키워드 (전 종목 대상, 쉼표로 추가 가능)
#    환경변수가 빈 값으로 넘어와도 기본값이 살아야 한다 (Actions vars 미설정 시)
FILER_KEYWORDS = [k.strip() for k in (
    os.environ.get("FILER_KEYWORDS") or "국민연금"
).split(",") if k.strip()]


def fetch_today_list(api_key: str, first_run: bool, seen: dict) -> list[dict]:
    """오늘자 공시 목록 (최신순). 새 공시가 100건 넘게 밀렸을 때만 다음 페이지."""
    today = now_kst().strftime("%Y%m%d")
    items: list[dict] = []
    for page in range(1, 4):
        params = {
            "crtfc_key": api_key,
            "bgn_de": today,
            "end_de": today,
            "sort": "date",
            "sort_mth": "desc",
            "page_no": page,
            "page_count": 100,
        }
        r = requests.get(DART_LIST_URL, params=params, headers=UA_HEADERS, timeout=15)
        data = r.json()
        status = data.get("status")
        if status == "013":      # 조회 결과 없음
            break
        if status == "020":      # 사용 한도 초과
            print("[DART] API 사용 한도 초과 — 이번 회차 건너뜀")
            break
        if status != "000":
            print(f"[DART] list 오류: {status} {data.get('message')}")
            break
        page_items = data.get("list", [])
        items.extend(page_items)
        all_new = all(it.get("rcept_no") not in seen for it in page_items)
        if first_run or len(page_items) < 100 or not all_new:
            break
        time.sleep(0.3)
    return items


def classify(item: dict, watch: dict[str, str]) -> str | None:
    """공시 1건을 분류해 알림 메시지를 만들거나 None(무시)."""
    code = (item.get("stock_code") or "").strip()
    corp = item.get("corp_name") or ""
    title = item.get("report_nm") or ""
    filer = item.get("flr_nm") or ""
    rcept_no = item.get("rcept_no") or ""
    link = DART_VIEWER + rcept_no
    compact = re.sub(r"\s+", "", title)

    # 3) 대량보유 보고 (전 종목)
    if "대량보유상황보고서" in compact and any(k in filer for k in FILER_KEYWORDS):
        return (f"🏛 <b>[5% 보고] {esc(corp)}</b>{f' ({code})' if code else ''}\n"
                f"{esc(title)}\n보고자: {esc(filer)}\n{link}")

    if not code or code not in watch:
        return None

    # 2) 오버행 경고 (관심종목)
    if OVERHANG_RE.search(compact):
        return (f"⚠️ <b>[오버행 주의] {esc(corp)}</b> ({code})\n"
                f"{esc(title)}\n{link}")

    # 1) 주요공시 (관심종목)
    if MAJOR_RE.search(compact):
        return (f"🚨 <b>[주요공시] {esc(corp)}</b> ({code})\n"
                f"{esc(title)}"
                f"{f'{chr(10)}제출: {esc(filer)}' if filer and filer != corp else ''}\n"
                f"{link}")

    return None


def poll_once(api_key: str, watch: dict[str, str], state: dict) -> None:
    seen: dict = state.setdefault("seen", {})
    first_run = not seen
    items = fetch_today_list(api_key, first_run, seen)
    new_items = [it for it in items if it.get("rcept_no") and it["rcept_no"] not in seen]
    if not new_items:
        return

    alerts = 0
    for it in reversed(new_items):  # 오래된 것부터
        seen[it["rcept_no"]] = it.get("rcept_dt", "")
        if first_run:
            continue  # 첫 가동은 현재 목록을 '본 것'으로만 등록 (알림 홍수 방지)
        msg = classify(it, watch)
        if msg:
            tg_send(msg)
            alerts += 1
            time.sleep(0.5)

    if len(seen) > SEEN_CAP:  # 오래된 접수번호부터 정리
        for k in sorted(seen, key=lambda x: seen[x])[: len(seen) - SEEN_CAP]:
            del seen[k]
    save_state(STATE_FILE, state)
    print(f"[{now_kst():%H:%M:%S}] 신규 {len(new_items)}건 / 알림 {alerts}건"
          f"{' (첫 가동 시드)' if first_run else ''}")


def in_window() -> bool:
    t = now_kst()
    if t.weekday() >= 5:
        return False
    hm = (t.hour, t.minute)
    return WINDOW_START <= hm <= WINDOW_END


def main() -> None:
    api_key = os.environ.get("DART_API_KEY", "")
    if not api_key:
        raise SystemExit("DART_API_KEY 환경변수가 필요합니다")
    watch = load_watchlist()
    print(f"관심종목 {len(watch)}개 | 보고자 키워드 {FILER_KEYWORDS} "
          f"| 총 {POLL_TOTAL_SECONDS}s, 간격 {POLL_INTERVAL}s")

    state = load_state(STATE_FILE, {})
    deadline = time.monotonic() + POLL_TOTAL_SECONDS

    while True:
        if not in_window():
            print(f"[{now_kst():%a %H:%M}] 감시 시간대 밖 — 종료")
            break
        try:
            poll_once(api_key, watch, state)
        except Exception as e:  # 일시 오류로 잡 전체가 죽지 않게
            print(f"[폴링 오류] {type(e).__name__}: {e}")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(POLL_INTERVAL, max(remaining, 1)))


if __name__ == "__main__":
    main()
