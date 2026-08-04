# ==========================================
# 🚨 DART 주요공시 1분 감시 (아이디어 1·2·3)
#   1) 관심종목 주요공시 알림 (DART 정식 서식명 기준, 개별 켜기/끄기)
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
#   /유형                  /유형 7 끄기   /유형 전환가액의조정 켜기
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
from summarize import summarize

DART_LIST_URL = "https://opendart.fss.or.kr/api/list.json"
DART_VIEWER = "https://dart.fss.or.kr/dsaf001/main.do?rcpNo="

POLL_TOTAL_SECONDS = int(os.environ.get("POLL_TOTAL_SECONDS", "870"))   # 잡 하나가 폴링하는 총 시간
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL", "60"))              # 폴링 간격(초)
STATE_FILE = "dart_seen.json"
SEEN_CAP = 8000

# 감시 시간대(KST): DART 공시는 대략 07:30~19:30에 나온다
WINDOW_START = (7, 20)
WINDOW_END = (19, 50)

# ─── 감시 대상 공시 (DART/KIND 정식 서식명 기준) ──────────────────────────────
# (서식명, 제목 매칭 정규식, 이모지) — 제목은 공백 제거 후 매칭.
# "주요사항보고서(유상증자결정)", "[기재정정]유상증자결정"처럼 서식명이
# 제목 안에 포함되는 구조라 부분 매칭으로 잡는다.
# /유형 명령으로 번호·이름 단위 켜기/끄기.
DISCLOSURE_TYPES: list[tuple[str, str, str]] = [
    # 자금조달 (신주·사채 발행)
    ("유무상증자결정", r"유무상증자결정", "🚨"),
    ("유상증자결정", r"유상증자결정", "🚨"),
    ("무상증자결정", r"무상증자결정", "🚨"),
    ("전환사채권발행결정", r"전환사채권발행결정", "🚨"),
    ("신주인수권부사채권발행결정", r"신주인수권부사채권발행결정", "🚨"),
    ("교환사채권발행결정", r"교환사채권발행결정", "🚨"),
    # 오버행 (기발행 물량의 주식화)
    ("전환가액의조정", r"전환가액의조정", "⚠️"),
    ("전환청구권행사", r"전환청구권행사", "⚠️"),
    ("신주인수권행사", r"신주인수권행사", "⚠️"),
    ("교환청구권행사", r"교환청구권행사", "⚠️"),
    ("사채조기상환청구", r"조기상환청구", "⚠️"),
    # 주주환원
    ("자기주식취득결정", r"자기주식취득결정", "💰"),
    ("자기주식처분결정", r"자기주식처분결정", "💰"),
    ("자기주식소각결정", r"자기주식소각결정|주식소각결정", "💰"),
    ("자기주식취득신탁계약체결결정", r"자기주식취득신탁계약체결결정", "💰"),
    ("자기주식취득신탁계약해지결정", r"자기주식취득신탁계약해지결정", "💰"),
    ("현금ㆍ현물배당결정", r"현금ㆍ현물배당결정|현금·현물배당결정|현금배당결정", "💰"),
    ("주식배당결정", r"주식배당결정", "💰"),
    # 자본·조직 변경
    ("감자결정", r"감자결정", "🚨"),
    ("주식분할결정", r"주식분할결정", "🔔"),
    ("주식병합결정", r"주식병합결정", "🔔"),
    ("회사합병결정", r"회사합병결정", "🚨"),
    ("회사분할결정", r"회사분할(합병)?결정", "🚨"),
    ("주식의포괄적교환ㆍ이전결정", r"포괄적교환", "🚨"),
    ("영업양수ㆍ양도결정", r"영업양[수도]결정", "🚨"),
    ("유형자산양수ㆍ양도결정", r"유형자산(양[수도]|취득|처분)결정", "🚨"),
    ("타법인주식및출자증권취득결정", r"타법인주식및출자증권취득결정", "🚨"),
    ("타법인주식및출자증권처분결정", r"타법인주식및출자증권처분결정", "🚨"),
    ("공개매수신고서", r"공개매수", "🚨"),
    ("최대주주변경", r"최대주주변경", "🚨"),
    # 계약·실적
    ("단일판매ㆍ공급계약체결", r"단일판매|공급계약체결", "📝"),
    ("영업(잠정)실적(공정공시)", r"영업\(잠정\)실적", "📈"),
    ("영업실적등에대한전망(공정공시)", r"영업실적등에대한전망", "📈"),
    ("매출액또는손익구조30%이상변동", r"매출액또는손익구조", "📈"),
    # 리스크
    ("소송등의제기ㆍ판결", r"소송등의(제기|판결)", "⚖️"),
    ("회생절차개시신청", r"회생절차", "⚖️"),
    ("파산신청", r"파산신청", "⚖️"),
    ("해산사유발생", r"해산사유발생", "⚖️"),
    ("매매거래정지", r"매매거래정지", "⚖️"),
    ("관리종목지정", r"관리종목", "⚖️"),
    ("상장폐지관련", r"상장폐지", "⚖️"),
    ("불성실공시법인지정", r"불성실공시", "⚖️"),
    ("횡령ㆍ배임혐의발생", r"횡령|배임", "⚖️"),
    ("감사보고서제출", r"감사보고서제출|감사의견", "⚖️"),
    # 시장 조치·해명·안내
    ("조회공시요구", r"조회공시요구", "🔔"),
    ("풍문또는보도에대한해명", r"풍문또는보도", "🔔"),
    ("기업설명회(IR)개최", r"기업설명회", "🔔"),
    # 지분 공시 (전 종목 — 보고자 키워드 필터)
    ("주식등의대량보유상황보고서", r"대량보유상황보고서", "🏛"),
]
TYPES_BY_NAME = {name: (re.compile(pat), emoji) for name, pat, emoji in DISCLOSURE_TYPES}
LARGE_HOLDING = "주식등의대량보유상황보고서"
MARKET_LABEL = {"Y": "코스피", "K": "코스닥", "N": "코넥스"}  # DART corp_cls

# 3) 대량보유 보고자 키워드 (전 종목 대상, 쉼표로 추가 가능)
#    환경변수가 빈 값으로 넘어와도 기본값이 살아야 한다 (Actions vars 미설정 시)
FILER_KEYWORDS = [k.strip() for k in (
    os.environ.get("FILER_KEYWORDS") or "국민연금"
).split(",") if k.strip()]


def merged_watchlist(cfg: dict) -> dict[str, str]:
    merged = dict(load_watchlist())
    merged.update(cfg.get("add", {}))
    for code in cfg.get("remove", []):
        merged.pop(code, None)
    return merged


# ─── 종목명 ↔ 종목코드 자동 매칭 (다음증권) ───────────────────────────────────

_DAUM_HEADERS = {**UA_HEADERS, "Referer": "https://finance.daum.net/"}


def daum_name_of(code: str) -> str | None:
    """종목코드 → 종목명"""
    try:
        r = requests.get(f"https://finance.daum.net/api/quotes/A{code}",
                         params={"summary": "false"}, headers=_DAUM_HEADERS, timeout=8)
        if r.status_code == 200:
            return r.json().get("name")
    except Exception:
        pass
    return None


def daum_search(query: str) -> list[tuple[str, str]]:
    """검색어 → [(종목코드, 종목명)] (국내 상장 종목만)"""
    try:
        r = requests.get("https://finance.daum.net/api/search/quotes",
                         params={"q": query}, headers=_DAUM_HEADERS, timeout=8)
        out = []
        for q in r.json().get("quotes", [])[:8]:
            sym = q.get("symbolCode") or ""
            if re.fullmatch(r"A[0-9A-Z]{6}", sym):
                out.append((sym[1:], q.get("name") or sym[1:]))
        return out
    except Exception:
        return []


# ─── 텔레그램 명령 처리 ────────────────────────────────────────────────────────

HELP_TEXT = (
    "🤖 <b>사용법</b>\n"
    "/추가 005930 또는 /추가 에프에스티 — 관심종목 추가 (이름·코드 자동 매칭)\n"
    "/삭제 005930 또는 /삭제 에프에스티 — 관심종목 제외\n"
    "/목록 — 현재 관심종목·설정 보기\n"
    "/유형 — 감시 중인 공시 서식 목록(번호) 보기\n"
    "/유형 7 끄기 — 7번 서식 알림 중단 (여러 개: /유형 7 8 9 끄기)\n"
    "/유형 전환가액의조정 켜기 — 이름으로도 가능\n"
    "/전체 31 켜기 — 해당 서식을 관심종목 무관 <b>전 종목</b> 구독 (/전체 로 확인)\n"
    "/보고자 추가 이름 — 5% 보고 감시 보고자 확대 (기본: 국민연금)\n"
    "/키워드 추가 단어 — 서식 목록에 없는 제목 키워드 직접 추가\n"
    "/키워드 삭제 단어\n\n"
    "변경은 폴링 주기(약 1분) 안에 적용되고, 장 마감 스캔에도 같은 목록이 쓰입니다."
)


def types_off(cfg: dict) -> set[str]:
    return set(cfg.get("types_off", []))


def handle_command(text: str, cfg: dict) -> str | None:
    parts = text.split()
    cmd = parts[0].lower()

    if cmd in ("/추가", "/add"):
        if len(parts) < 2:
            return "형식: /추가 종목코드 또는 /추가 종목명\n예: /추가 005930 · /추가 에프에스티"
        arg = " ".join(parts[1:]).strip()
        if re.fullmatch(r"[0-9A-Z]{6}", parts[1]):        # 코드로 추가 → 이름 자동 매칭
            code = parts[1]
            name = " ".join(parts[2:]).strip() or daum_name_of(code) or code
        else:                                              # 이름으로 추가 → 코드 자동 매칭
            results = daum_search(arg)
            exact = [r for r in results if r[1] == arg]
            if exact:
                code, name = exact[0]
            elif len(results) == 1:
                code, name = results[0]
            elif len(results) > 1:
                lines = [f" • {esc(n)} ({c})" for c, n in results[:5]]
                return ("🔎 여러 종목이 검색됐습니다. 코드로 지정해 주세요:\n"
                        + "\n".join(lines) + "\n예: /추가 " + results[0][0])
            else:
                return f"'{esc(arg)}' 종목을 찾지 못했습니다. 코드로 시도해 보세요."
        cfg["add"][code] = name
        cfg["remove"] = [c for c in cfg["remove"] if c != code]
        return f"✅ {esc(name)} ({code}) 추가됨 · 현재 {len(merged_watchlist(cfg))}종목"

    if cmd in ("/삭제", "/제거", "/remove"):
        if len(parts) < 2:
            return "형식: /삭제 종목코드 또는 /삭제 종목명"
        arg = " ".join(parts[1:]).strip()
        watch = merged_watchlist(cfg)
        if re.fullmatch(r"[0-9A-Z]{6}", arg):
            code = arg
        else:                                              # 이름으로 삭제
            hits = [c for c, n in watch.items() if n == arg]
            if not hits:
                hits = [c for c, n in watch.items() if arg in n]
            if not hits:
                return f"관심종목에서 '{esc(arg)}' 을 찾지 못했습니다. /목록 으로 확인하세요."
            if len(hits) > 1:
                lines = [f" • {esc(watch[c])} ({c})" for c in hits[:5]]
                return "🔎 여러 개가 일치합니다. 코드로 지정해 주세요:\n" + "\n".join(lines)
            code = hits[0]
        name = watch.get(code, code)
        cfg["add"].pop(code, None)
        if code not in cfg["remove"]:
            cfg["remove"].append(code)
        return f"🗑 {esc(name)} ({code}) 제외됨 · 현재 {len(merged_watchlist(cfg))}종목"

    if cmd in ("/목록", "/list"):
        # 코드만 등록돼 이름이 비어 있는 종목은 이름을 자동 보완
        for code, name in list(cfg.get("add", {}).items())[:10]:
            if name == code:
                found = daum_name_of(code)
                if found:
                    cfg["add"][code] = found
        watch = merged_watchlist(cfg)
        lines = [f" • {esc(n)} ({c})" for c, n in sorted(watch.items(), key=lambda x: x[1])]
        off = sorted(types_off(cfg))
        extra = cfg.get("keywords_extra", [])
        msg = f"📋 <b>관심종목 {len(watch)}개</b>\n" + ("\n".join(lines) if lines else " (없음)")
        if off:
            msg += f"\n\n🚫 꺼진 서식({len(off)}): " + ", ".join(off)
        subs = cfg.get("types_all", [])
        if subs:
            msg += f"\n🌐 전 종목 구독: {', '.join(subs)}"
        if extra:
            msg += f"\n➕ 추가 키워드: {', '.join(esc(k) for k in extra)}"
        return msg

    if cmd in ("/유형", "/types"):
        names = [name for name, _, _ in DISCLOSURE_TYPES]
        # 토글: /유형 7 끄기 · /유형 7 8 9 켜기 · /유형 전환가액의조정 끄기
        if len(parts) >= 3 and parts[-1] in ("끄기", "켜기", "off", "on"):
            turn_off = parts[-1] in ("끄기", "off")
            targets: list[str] = []
            for tok in parts[1:-1]:
                if tok.isdigit() and 1 <= int(tok) <= len(names):
                    targets.append(names[int(tok) - 1])
                elif tok in TYPES_BY_NAME:
                    targets.append(tok)
                else:
                    return f"'{esc(tok)}' 은 목록에 없습니다. /유형 으로 번호를 확인하세요."
            off = types_off(cfg)
            for t in targets:
                (off.add if turn_off else off.discard)(t)
            cfg["types_off"] = sorted(off)
            verb = "껐습니다" if turn_off else "켰습니다"
            return f"🔧 {', '.join(targets)} 알림을 {verb} (꺼진 서식 {len(off)}개)"
        off = types_off(cfg)
        lines = [f" {'🚫' if name in off else '✅'} {i:>2} {emoji} {name}"
                 for i, (name, _, emoji) in enumerate(DISCLOSURE_TYPES, 1)]
        return ("🔧 <b>감시 공시 서식</b> (DART 서식명 기준)\n"
                "끄기: /유형 번호 끄기 · 켜기: /유형 번호 켜기\n" + "\n".join(lines))

    if cmd in ("/전체", "/all"):
        names = [name for name, _, _ in DISCLOSURE_TYPES]
        subs = cfg.setdefault("types_all", [])
        if len(parts) >= 3 and parts[-1] in ("끄기", "켜기", "off", "on"):
            turn_on = parts[-1] in ("켜기", "on")
            targets: list[str] = []
            for tok in parts[1:-1]:
                if tok.isdigit() and 1 <= int(tok) <= len(names):
                    targets.append(names[int(tok) - 1])
                elif tok in TYPES_BY_NAME:
                    targets.append(tok)
                else:
                    return f"'{esc(tok)}' 은 목록에 없습니다. /유형 으로 번호를 확인하세요."
            for t in targets:
                if t == LARGE_HOLDING:
                    return "5% 보고는 원래 전 종목 감시입니다 (/유형 으로 켜고 끄세요)."
                if turn_on and t not in subs:
                    subs.append(t)
                if not turn_on and t in subs:
                    subs.remove(t)
            verb = "전 종목 구독 시작" if turn_on else "전 종목 구독 해제"
            return f"🌐 {', '.join(targets)} — {verb} (현재 {len(subs)}종)"
        if subs:
            return ("🌐 <b>전 종목 구독 중인 서식</b>\n"
                    + "\n".join(f" • {s}" for s in subs)
                    + "\n해제: /전체 이름(또는 번호) 끄기")
        return "🌐 전 종목 구독 중인 서식 없음\n예: /전체 31 켜기 (번호는 /유형 참고)"

    if cmd in ("/보고자", "/filer"):
        extra = cfg.setdefault("filers_extra", [])
        if len(parts) >= 3 and parts[1] in ("추가", "삭제"):
            word = " ".join(parts[2:]).strip()
            if parts[1] == "추가":
                if word not in extra:
                    extra.append(word)
                return f"➕ 보고자 키워드 '{esc(word)}' 추가됨 (5% 보고 전 종목 감시 대상)"
            if word in extra:
                extra.remove(word)
                return f"🗑 보고자 키워드 '{esc(word)}' 삭제됨"
            return f"'{esc(word)}' 는 추가된 보고자가 아닙니다 (기본 {', '.join(FILER_KEYWORDS)}는 고정)"
        return ("🏛 <b>5% 보고 감시 보고자</b>\n"
                f" 기본: {', '.join(FILER_KEYWORDS)}\n"
                f" 추가: {', '.join(esc(k) for k in extra) if extra else '(없음)'}\n"
                "관심종목의 5% 보고는 보고자와 무관하게 전부 알립니다.\n"
                "형식: /보고자 추가 이름 · /보고자 삭제 이름")

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


# 텔레그램 "/" 팝업 메뉴 (봇 명령은 영문만 등록 가능 — 한국어 명령과 병행 동작)
COMMAND_MENU = [
    ("list", "관심종목·설정 보기 (=/목록)"),
    ("types", "감시 공시 서식 보기·켜기/끄기 (=/유형)"),
    ("all", "전 종목 구독 관리 (=/전체)"),
    ("add", "종목 추가 — 뒤에 이름이나 코드 (=/추가)"),
    ("remove", "종목 제외 (=/삭제)"),
    ("keyword", "감시 키워드 추가/삭제 (=/키워드)"),
    ("help", "사용법 안내"),
]


def register_menu() -> None:
    """'/' 입력 시 뜨는 명령 팝업 메뉴 등록 (잡 시작마다 1회, 멱등)."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    if not token or DRY_RUN:
        return
    try:
        requests.post(f"https://api.telegram.org/bot{token}/setMyCommands",
                      json={"commands": [{"command": c, "description": d}
                                         for c, d in COMMAND_MENU]},
                      timeout=10)
    except Exception as e:
        print(f"[TG] setMyCommands 실패: {e}")


def process_commands(cfg: dict, wait: int = 0) -> bool:
    """봇 대화방의 새 메시지를 명령으로 처리. 설정이 바뀌면 True.

    wait>0 이면 텔레그램 롱폴링으로 최대 wait초 대기 — 메시지가 오는 즉시
    깨어나 답하므로 감시 시간대에는 명령 응답이 수 초 안에 온다.
    (대기 시간이 DART 폴링 사이의 sleep 역할을 겸한다)
    """
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
    if not token or not chat_id or DRY_RUN:
        if wait:
            time.sleep(wait)
        return False
    try:
        r = requests.get(f"https://api.telegram.org/bot{token}/getUpdates",
                         params={"offset": cfg.get("tg_offset", 0) + 1, "timeout": wait},
                         timeout=(10, wait + 15))
        updates = r.json().get("result", [])
    except Exception as e:
        print(f"[TG] getUpdates 실패: {e}")
        time.sleep(min(wait or 5, 10))
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


def classify(item: dict, watch: dict[str, str], cfg: dict) -> str | None:
    """공시 1건을 분류해 알림 메시지를 만들거나 None(무시)."""
    off = types_off(cfg)
    code = (item.get("stock_code") or "").strip()
    corp = item.get("corp_name") or ""
    title = item.get("report_nm") or ""
    filer = item.get("flr_nm") or ""
    rcept_no = item.get("rcept_no") or ""
    link = DART_VIEWER + rcept_no
    compact = re.sub(r"\s+", "", title)
    mkt = MARKET_LABEL.get(item.get("corp_cls", ""), "")
    corp_disp = f"({mkt}){esc(corp)}" if mkt else esc(corp)

    in_watch = bool(code) and code in watch

    # 대량보유(5%) 보고 — 관심종목은 보고자 무관 전부,
    # 그 외 종목은 보고자 키워드(국민연금 + /보고자 추가분)에 걸릴 때만
    if LARGE_HOLDING not in off:
        pattern, emoji = TYPES_BY_NAME[LARGE_HOLDING]
        if pattern.search(compact):
            filers = FILER_KEYWORDS + cfg.get("filers_extra", [])
            if in_watch or any(k in filer for k in filers):
                return (f"{emoji} <b>[5% 보고] {corp_disp}</b>{f' ({code})' if code else ''}\n"
                        f"{esc(title)}\n보고자: {esc(filer)}\n{link}")
            return None  # 5% 보고이긴 하나 필터 밖 — 다른 서식으로 오분류 방지

    # 전 종목 구독 서식 (관심종목 무관)
    if not in_watch:
        for name in cfg.get("types_all", []):
            if name == LARGE_HOLDING or name not in TYPES_BY_NAME or not code:
                continue
            pattern, emoji = TYPES_BY_NAME[name]
            if pattern.search(compact):
                return (f"🌐{emoji} <b>[{name}·전체] {corp_disp}</b> ({code})\n"
                        f"{esc(title)}\n{link}")
        return None

    # 관심종목: 서식 목록에서 첫 매칭
    for name, _, _ in DISCLOSURE_TYPES:
        if name == LARGE_HOLDING or name in off:
            continue
        pattern, emoji = TYPES_BY_NAME[name]
        if pattern.search(compact):
            return (f"{emoji} <b>[{name}] {corp_disp}</b> ({code})\n"
                    f"{esc(title)}"
                    f"{f'{chr(10)}제출: {esc(filer)}' if filer and filer != corp else ''}\n"
                    f"{link}")

    # 사용자 추가 키워드
    for word in cfg.get("keywords_extra", []):
        if word and word in compact:
            return (f"🔍 <b>[키워드: {esc(word)}] {corp_disp}</b> ({code})\n"
                    f"{esc(title)}\n{link}")

    return None


def poll_once(api_key: str, state: dict, cfg: dict) -> None:
    watch = merged_watchlist(cfg)
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
        msg = classify(it, watch, cfg)
        if msg:
            summary = summarize(it, api_key)  # 탐지된 건만 내용 요약 (실패 시 None)
            if summary:
                head, _, link = msg.rpartition("\n")
                msg = f"{head}\n{summary}\n{link}"
            else:
                # 접수 직후엔 원문 파일·구조화 API 등록이 늦을 수 있다 — 알림은
                # 먼저 보내고, 요약(규칙 또는 Gemini)은 데이터가 올라오는 대로
                # 후속 메시지로 발송
                state.setdefault("pending_sum", {})[it["rcept_no"]] = {
                    "tries": 0,
                    "corp": it.get("corp_name", ""),
                    "code": (it.get("stock_code") or "").strip(),
                    "corp_code": it.get("corp_code", ""),
                    "rcept_dt": it.get("rcept_dt", ""),
                    "title": it.get("report_nm", ""),
                }
            tg_send(msg)
            alerts += 1
            time.sleep(0.5)

    if len(seen) > SEEN_CAP:  # 오래된 접수번호부터 정리
        for k in sorted(seen, key=lambda x: seen[x])[: len(seen) - SEEN_CAP]:
            del seen[k]
    save_state(STATE_FILE, state)
    print(f"[{now_kst():%H:%M:%S}] 신규 {len(new_items)}건 / 알림 {alerts}건"
          f"{' (첫 가동 시드)' if first_run else ''}")


def retry_pending_summaries(api_key: str, state: dict) -> None:
    """데이터 등록 지연으로 미뤄둔 요약을 재시도.
    처음 10분은 매 사이클, 이후엔 5사이클마다, 약 2.5시간까지 (원문 등록이
    2시간 넘게 걸린 사례 확인됨)."""
    pending: dict = state.get("pending_sum", {})
    if not pending:
        return
    for rcept_no, info in list(pending.items()):
        if "title" not in info:      # 구버전 큐 항목은 정리
            del pending[rcept_no]
            continue
        info["tries"] = info.get("tries", 0) + 1
        tries = info["tries"]
        if tries > 10 and tries % 5 != 0:
            continue
        item = {"rcept_no": rcept_no, "report_nm": info.get("title", ""),
                "stock_code": info.get("code", ""), "corp_code": info.get("corp_code", ""),
                "corp_name": info.get("corp", ""), "rcept_dt": info.get("rcept_dt", "")}
        summary = summarize(item, api_key)
        if summary:
            code = info.get("code", "")
            code_tag = f" ({code})" if code else ""
            tg_send(f"🧾 <b>[요약] {esc(info.get('corp', ''))}</b>{code_tag}\n"
                    f"{esc(info.get('title', ''))}\n{summary}\n{DART_VIEWER}{rcept_no}")
            del pending[rcept_no]
        elif tries >= 150:
            print(f"[요약 포기] {rcept_no} — 데이터 미등록 (약 2.5시간 경과)")
            del pending[rcept_no]
    save_state(STATE_FILE, state)


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
    print(f"관심종목 {len(merged_watchlist(cfg))}개 | 서식 {len(DISCLOSURE_TYPES)}종"
          f"(꺼짐 {len(types_off(cfg))}) | 보고자 키워드 {FILER_KEYWORDS} "
          f"| 총 {POLL_TOTAL_SECONDS}s, 간격 {POLL_INTERVAL}s")

    state = load_state(STATE_FILE, {})
    deadline = time.monotonic() + POLL_TOTAL_SECONDS
    register_menu()

    # 시작 직후 밀린 명령 먼저 처리
    if process_commands(cfg):
        save_watch_config(cfg)

    while True:
        if not in_window():
            print(f"[{now_kst():%a %H:%M}] 감시 시간대 밖 — 명령만 처리하고 종료")
            break
        try:
            poll_once(api_key, state, cfg)
            retry_pending_summaries(api_key, state)
        except Exception as e:  # 일시 오류로 잡 전체가 죽지 않게
            print(f"[폴링 오류] {type(e).__name__}: {e}")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        # DART 폴링 사이 대기 = 텔레그램 롱폴링 (명령 오면 즉시 처리)
        if process_commands(cfg, wait=int(min(POLL_INTERVAL, max(remaining, 1)))):
            save_watch_config(cfg)


if __name__ == "__main__":
    main()
