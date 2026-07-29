# ==========================================
# 🚨 DART 주요공시 1분 감시 (아이디어 1·2·3)
#   1) 관심종목 주요공시 키워드 알림
#   2) 관심종목 CB 리픽싱·전환청구 등 오버행 경고
#   3) 국민연금 등 주요 보고자의 5% 대량보유 공시
#
# 실행 방식: cron-job.org가 15분마다 workflow_dispatch를 깨우면
#   잡 하나가 POLL_TOTAL_SECONDS 동안 POLL_INTERVAL 간격으로 반복 폴링.
#   (kr-earnings-pulse에서 검증된 "15분 잡 내부 폴링" 패턴)
# 감시 시간대 밖에서 깨어나면 텔레그램 명령만 처리하고 종료.
#
# 텔레그램 명령 (봇 대화방에서):
#   /추가 005930 삼성전자   /삭제 005930   /목록
#   /유형                  /유형 실적 끄기
#   /키워드 추가 무상소각    /키워드 삭제 무상소각
#
# DART 호출량: 분당 1~3건 × 하루 약 12시간 ≈ 800~1,000건/일
#   (같은 키를 쓰는 kr-earnings-pulse ~1.1만 건과 합쳐도 일 한도 2만 건 이내)
# ==========================================
import os
import re
import time

import requests

from common import (DRY_RUN, UA_HEADERS, esc, load_state, load_watch_config,
                    load_watchlist, now_kst, save_state, save_watch_config, tg_send)

DART_LIST_URL = "https://opendart.fss.or.kr/api/list.json"
DART_VIEWER = "https://dart.fss.or.kr/dsaf001/main.do?rcpNo="

POLL_TOTAL_SECONDS = int(os.environ.get("POLL_TOTAL_SECONDS", "870"))   # 잡 하나가 폴링하는 총 시간
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL", "60"))              # 폴링 간격(초)
STATE_FILE = "dart_seen.json"
SEEN_CAP = 8000

# 감시 시간대(KST): DART 공시는 대략 07:30~19:30에 나온다
WINDOW_START = (7, 20)
WINDOW_END = (19, 50)

# 1) 주요공시 키워드 — 유형(그룹)별로 텔레그램 /유형 명령으로 켜고 끌 수 있다
KEYWORD_GROUPS: dict[str, str] = {
    "자금조달": r"유상증자|무상증자|전환사채|신주인수권부사채|교환사채",
    "주주환원": r"자기주식|소각|배당",
    "지배구조": r"감자|주식병합|주식분할|회사분할|분할합병|합병|주식교환|주식이전|공개매수"
              r"|영업양수|영업양도|최대주주변경|경영권|타법인주식및출자증권취득"
              r"|타법인주식및출자증권처분|유형자산취득|유형자산양수|자산재평가",
    "계약수주": r"공급계약|수주",
    "리스크": r"소송|회생절차|파산|해산사유|매매거래정지|상장폐지|관리종목|불성실공시"
            r"|횡령|배임|감사의견",
    "실적": r"영업실적|잠정.{0,3}실적|매출액또는손익",
    "시장조치": r"조회공시|풍문또는보도",
}
SPECIAL_GROUPS = ["오버행", "5%보고"]  # 키워드 그룹 외 특수 유형 (역시 /유형으로 토글)

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


def build_major_re(cfg: dict) -> re.Pattern | None:
    off = set(cfg.get("groups_off", []))
    parts = [pat for g, pat in KEYWORD_GROUPS.items() if g not in off]
    parts += [re.escape(k) for k in cfg.get("keywords_extra", [])]
    return re.compile("|".join(parts)) if parts else None


def merged_watchlist(cfg: dict) -> dict[str, str]:
    merged = dict(load_watchlist())
    merged.update(cfg.get("add", {}))
    for code in cfg.get("remove", []):
        merged.pop(code, None)
    return merged


# ─── 텔레그램 명령 처리 ────────────────────────────────────────────────────────

HELP_TEXT = (
    "🤖 <b>사용법</b>\n"
    "/추가 005930 삼성전자 — 관심종목 추가\n"
    "/삭제 005930 — 관심종목 제외\n"
    "/목록 — 현재 관심종목·설정 보기\n"
    "/유형 — 공시 유형별 켜짐/꺼짐 보기\n"
    "/유형 실적 끄기 — 해당 유형 알림 중단 (켜기로 복원)\n"
    "/키워드 추가 단어 — 감시 키워드 직접 추가\n"
    "/키워드 삭제 단어\n\n"
    "변경은 폴링 주기(약 1분) 안에 적용되고, 장 마감 스캔에도 같은 목록이 쓰입니다."
)


def handle_command(text: str, cfg: dict) -> str | None:
    parts = text.split()
    cmd = parts[0].lower()

    if cmd in ("/추가", "/add"):
        if len(parts) < 2 or not re.fullmatch(r"[0-9A-Z]{6}", parts[1]):
            return "형식: /추가 종목코드6자리 [이름]\n예: /추가 005930 삼성전자"
        code = parts[1]
        name = " ".join(parts[2:]) or code
        cfg["add"][code] = name
        cfg["remove"] = [c for c in cfg["remove"] if c != code]
        return f"✅ {esc(name)} ({code}) 추가됨 · 현재 {len(merged_watchlist(cfg))}종목"

    if cmd in ("/삭제", "/제거", "/remove"):
        if len(parts) < 2:
            return "형식: /삭제 종목코드6자리"
        code = parts[1]
        name = merged_watchlist(cfg).get(code, code)
        cfg["add"].pop(code, None)
        if code not in cfg["remove"]:
            cfg["remove"].append(code)
        return f"🗑 {esc(name)} ({code}) 제외됨 · 현재 {len(merged_watchlist(cfg))}종목"

    if cmd in ("/목록", "/list"):
        watch = merged_watchlist(cfg)
        lines = [f" • {esc(n)} ({c})" for c, n in sorted(watch.items(), key=lambda x: x[1])]
        off = cfg.get("groups_off", [])
        extra = cfg.get("keywords_extra", [])
        msg = f"📋 <b>관심종목 {len(watch)}개</b>\n" + ("\n".join(lines) if lines else " (없음)")
        if off:
            msg += f"\n\n🚫 꺼진 유형: {', '.join(off)}"
        if extra:
            msg += f"\n➕ 추가 키워드: {', '.join(esc(k) for k in extra)}"
        return msg

    if cmd in ("/유형", "/types"):
        all_groups = list(KEYWORD_GROUPS) + SPECIAL_GROUPS
        if len(parts) >= 3 and parts[2] in ("끄기", "켜기", "off", "on"):
            g = parts[1]
            if g not in all_groups:
                return f"없는 유형입니다. 가능: {', '.join(all_groups)}"
            off = set(cfg.get("groups_off", []))
            if parts[2] in ("끄기", "off"):
                off.add(g)
                verb = "껐습니다"
            else:
                off.discard(g)
                verb = "켰습니다"
            cfg["groups_off"] = sorted(off)
            return f"🔧 [{g}] 알림을 {verb}"
        off = set(cfg.get("groups_off", []))
        lines = [f" {'🚫' if g in off else '✅'} {g}" for g in all_groups]
        return ("🔧 <b>공시 유형</b> (끄기: /유형 이름 끄기)\n" + "\n".join(lines))

    if cmd in ("/키워드", "/keyword"):
        if len(parts) >= 3 and parts[1] in ("추가", "삭제"):
            word = " ".join(parts[2:]).strip()
            extra = cfg.get("keywords_extra", [])
            if parts[1] == "추가":
                if word not in extra:
                    extra.append(word)
                cfg["keywords_extra"] = extra
                return f"➕ 키워드 '{esc(word)}' 추가됨 (제목에 포함되면 알림)"
            if word in extra:
                extra.remove(word)
                cfg["keywords_extra"] = extra
                return f"🗑 키워드 '{esc(word)}' 삭제됨"
            return f"'{esc(word)}' 는 등록돼 있지 않습니다"
        extra = cfg.get("keywords_extra", [])
        return ("➕ 추가 키워드: " + (", ".join(esc(k) for k in extra) if extra else "(없음)")
                + "\n형식: /키워드 추가 단어 · /키워드 삭제 단어")

    return HELP_TEXT


def process_commands(cfg: dict) -> bool:
    """봇 대화방의 새 메시지를 명령으로 처리. 설정이 바뀌면 True."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
    if not token or not chat_id or DRY_RUN:
        return False
    try:
        r = requests.get(f"https://api.telegram.org/bot{token}/getUpdates",
                         params={"offset": cfg.get("tg_offset", 0) + 1, "timeout": 0},
                         timeout=10)
        updates = r.json().get("result", [])
    except Exception as e:
        print(f"[TG] getUpdates 실패: {e}")
        return False

    changed = False
    for up in updates:
        cfg["tg_offset"] = max(cfg.get("tg_offset", 0), up.get("update_id", 0))
        changed = True  # offset 전진도 저장 대상
        msg = up.get("message") or up.get("edited_message") or {}
        if str(msg.get("chat", {}).get("id", "")) != str(chat_id):
            continue  # 주인 외 무시
        text = (msg.get("text") or "").strip()
        if not text:
            continue
        reply = handle_command(text, cfg) if text.startswith("/") else HELP_TEXT
        if reply:
            tg_send(reply)
            time.sleep(0.3)
    return changed


# ─── DART 폴링 ─────────────────────────────────────────────────────────────────

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


def classify(item: dict, watch: dict[str, str], cfg: dict,
             major_re: re.Pattern | None) -> str | None:
    """공시 1건을 분류해 알림 메시지를 만들거나 None(무시)."""
    off = set(cfg.get("groups_off", []))
    code = (item.get("stock_code") or "").strip()
    corp = item.get("corp_name") or ""
    title = item.get("report_nm") or ""
    filer = item.get("flr_nm") or ""
    rcept_no = item.get("rcept_no") or ""
    link = DART_VIEWER + rcept_no
    compact = re.sub(r"\s+", "", title)

    # 3) 대량보유 보고 (전 종목)
    if ("5%보고" not in off and "대량보유상황보고서" in compact
            and any(k in filer for k in FILER_KEYWORDS)):
        return (f"🏛 <b>[5% 보고] {esc(corp)}</b>{f' ({code})' if code else ''}\n"
                f"{esc(title)}\n보고자: {esc(filer)}\n{link}")

    if not code or code not in watch:
        return None

    # 2) 오버행 경고 (관심종목)
    if "오버행" not in off and OVERHANG_RE.search(compact):
        return (f"⚠️ <b>[오버행 주의] {esc(corp)}</b> ({code})\n"
                f"{esc(title)}\n{link}")

    # 1) 주요공시 (관심종목)
    if major_re and major_re.search(compact):
        return (f"🚨 <b>[주요공시] {esc(corp)}</b> ({code})\n"
                f"{esc(title)}"
                f"{f'{chr(10)}제출: {esc(filer)}' if filer and filer != corp else ''}\n"
                f"{link}")

    return None


def poll_once(api_key: str, state: dict, cfg: dict) -> None:
    watch = merged_watchlist(cfg)
    major_re = build_major_re(cfg)
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
        msg = classify(it, watch, cfg, major_re)
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
    cfg = load_watch_config()
    print(f"관심종목 {len(merged_watchlist(cfg))}개 | 보고자 키워드 {FILER_KEYWORDS} "
          f"| 총 {POLL_TOTAL_SECONDS}s, 간격 {POLL_INTERVAL}s")

    state = load_state(STATE_FILE, {})
    deadline = time.monotonic() + POLL_TOTAL_SECONDS

    while True:
        if process_commands(cfg):
            save_watch_config(cfg)
        if not in_window():
            print(f"[{now_kst():%a %H:%M}] 감시 시간대 밖 — 명령만 처리하고 종료")
            break
        try:
            poll_once(api_key, state, cfg)
        except Exception as e:  # 일시 오류로 잡 전체가 죽지 않게
            print(f"[폴링 오류] {type(e).__name__}: {e}")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(POLL_INTERVAL, max(remaining, 1)))


if __name__ == "__main__":
    main()
