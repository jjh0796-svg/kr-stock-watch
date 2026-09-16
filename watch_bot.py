# -*- coding: utf-8 -*-
"""
안녕하재 종목모니터링 — 대화형 관심종목 관리 봇 (서버 상주)

텔레그램에서 명령으로 관심종목을 관리하면 스파이크 감시(spike_watch)에 즉시 반영된다.
  /추가 005930          → 코드로 추가 (이름은 네이버에서 자동 조회)
  /추가 005930 삼성전자  → 이름 직접 지정
  /삭제 005930
  /목록
  /도움말

- 저장: WATCHLIST_FILE (기본 ~/bots/watchlist.csv, "코드,이름" 줄 단위)
- 보안: TELEGRAM_CHAT_ID 로 지정된 대화만 응답 (그 외 무시)
- 운영: systemd 상주 서비스 (긴 폴링, 죽으면 자동 재시작)
"""
import os
import sys
import time
from pathlib import Path

import requests

for _s in (sys.stdout, sys.stderr):
    if hasattr(_s, "reconfigure"):
        _s.reconfigure(encoding="utf-8", errors="replace")

BASE_DIR = Path(__file__).resolve().parent
UA = {"User-Agent": "Mozilla/5.0"}


def load_env():
    for p in (BASE_DIR / ".env", Path.home() / "bots" / "krwatch.env"):
        if not p.exists():
            continue
        for line in p.read_text(encoding="utf-8-sig").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                os.environ.setdefault(k.strip(), v.strip())


WATCHLIST_FILE = Path(os.environ.get("WATCHLIST_FILE",
                                     str(Path.home() / "bots" / "watchlist.csv")))
# 보유 종목 — 전 봇 공용 포지션 파일 (2026-08-29): 스파이크·방산·뉴스봇 등이
# 💼 마킹과 알림 승격에 사용한다. 형식은 watchlist와 동일("코드,이름").
HOLDINGS_FILE = Path(os.environ.get("HOLDINGS_FILE",
                                    str(Path.home() / "bots" / "holdings.csv")))


def _read_csv(path):
    out = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8-sig").splitlines():
            if "," in line:
                code, _, name = line.partition(",")
                if code.strip().isdigit():
                    out[code.strip()] = name.strip()
    return out


def _save_csv(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(f"{c},{n}" for c, n in sorted(data.items())) + "\n",
        encoding="utf-8")


def read_wl():
    return _read_csv(WATCHLIST_FILE)


def save_wl(wl):
    _save_csv(WATCHLIST_FILE, wl)


def read_hd():
    return _read_csv(HOLDINGS_FILE)


def save_hd(hd):
    _save_csv(HOLDINGS_FILE, hd)


def resolve_name(code):
    """네이버 시세 API로 종목명 조회 (실패 시 None)."""
    try:
        url = f"https://polling.finance.naver.com/api/realtime/domestic/stock/{code}"
        r = requests.get(url, headers=UA, timeout=10)
        datas = r.json().get("datas", [])
        if datas:
            return datas[0].get("stockName")
    except Exception:
        pass
    return None


def search_stock(query):
    """종목명(부분명 가능) → [(code, name)] 후보. 네이버 자동완성, 국내 종목만."""
    try:
        r = requests.get("https://ac.stock.naver.com/ac",
                         params={"q": query, "target": "stock,ipo"},
                         headers=UA, timeout=10)
        out = []
        for it in r.json().get("items", []):
            code, name = it.get("code", ""), it.get("name", "")
            if it.get("nationCode") == "KOR" and len(code) == 6 and code.isdigit():
                out.append((code, name))
        return out
    except Exception:
        return []


def parse_target(args, usage):
    """추가 명령 인자 → (code, name, 오류문). 6자리 코드 또는 종목명 허용."""
    if not args:
        return None, None, usage
    if args[0].isdigit() and len(args[0]) == 6:
        code = args[0]
        name = " ".join(args[1:]) if len(args) > 1 else resolve_name(code)
        if not name:
            return None, None, f"❓ {code} 종목명을 찾지 못했습니다. 코드 뒤에 이름을 함께 적어주세요."
        return code, name, None
    query = " ".join(args)
    cands = search_stock(query)
    if not cands:
        return None, None, f"❓ '{query}' 매칭 종목을 찾지 못했습니다. 6자리 코드로 시도해주세요."
    norm = query.replace(" ", "")
    exact = [c for c in cands if c[1].replace(" ", "") == norm]
    if exact:
        return exact[0][0], exact[0][1], None
    # 첫 후보명에 검색어가 들어 있으면 자동 채택, 아니면 후보 제시
    if norm in cands[0][1].replace(" ", "") or len(cands) == 1:
        return cands[0][0], cands[0][1], None
    lines = [f"· {n} ({c})" for c, n in cands[:4]]
    return None, None, f"❓ '{query}' 후보가 여럿입니다. 코드로 다시 시도해주세요:\n" + "\n".join(lines)


def find_in(book, key):
    """저장 목록에서 코드 또는 이름(부분일치)으로 찾기 → 코드 or None."""
    if key in book:
        return key
    norm = key.replace(" ", "")
    hits = [c for c, n in book.items() if norm and norm in n.replace(" ", "")]
    return hits[0] if len(hits) == 1 else None


HELP = (
    "📋 종목 관리 — 목적별로 세 가지입니다\n"
    "\n"
    "💼 보유 종목 (전 봇 공용 — 알림에 💼 표시 + 방산 야간알림 등 승격)\n"
    "/보유추가 005930 또는 종목명 · /보유삭제 · /보유목록  (영문: /hold /hold_del /holds)\n"
    "\n"
    "⭐ 주가 스파이크 감시 (장중 급등락·거래량·52주 — 보유 종목은 자동 포함)\n"
    "/스파이크추가 005930 또는 종목명 · /스파이크삭제 · /스파이크목록  (영문: /spike /spike_del /spikes)\n"
    "\n"
    "📢 공시감시(DART)는 별도 봇: '안녕하재 공시모니터링'\n"
    "→ @filingsmonitor_0796_bot 에서 /추가·/목록·/유형 등 사용"
)


def handle(text):
    """명령 처리 → 응답 문자열."""
    parts = text.strip().split()
    if not parts:
        return None
    cmd = parts[0].lstrip("/").lower()

    if cmd in ("start", "도움말", "help"):
        return HELP

    # 텔레그램 메뉴 버튼용 영문 별칭 (메뉴 등록은 영문만 허용)
    cmd = {"hold": "보유추가", "hold_del": "보유삭제", "holds": "보유목록",
           "spike": "스파이크추가", "spike_del": "스파이크삭제",
           "spikes": "스파이크목록"}.get(cmd, cmd)

    if cmd in ("보유추가", "보유"):
        code, name, err = parse_target(parts[1:], "형식: /보유추가 종목코드 또는 종목명\n예: /보유추가 005930 · /보유추가 삼성전자")
        if err:
            return err
        hd = read_hd()
        hd[code] = name
        save_hd(hd)
        return f"💼 보유 등록: {name}({code}) — 총 {len(hd)}종목\n(전 봇에서 💼 표시·알림 승격 적용)"

    if cmd in ("보유삭제", "보유제거"):
        if len(parts) < 2:
            return "형식: /보유삭제 종목코드 또는 종목명"
        hd = read_hd()
        code = find_in(hd, " ".join(parts[1:]))
        if code:
            name = hd.pop(code)
            save_hd(hd)
            return f"🗑 보유 해제: {name}({code}) — 총 {len(hd)}종목"
        return f"보유 목록에서 찾지 못했습니다: {' '.join(parts[1:])}"

    if cmd in ("보유목록",):
        hd = read_hd()
        if not hd:
            return "보유 종목이 비어 있습니다. /보유추가 로 등록하세요."
        lines = [f"💼 {n}({c})" for c, n in sorted(hd.items())]
        return f"💼 보유 {len(hd)}종목\n" + "\n".join(lines)

    # 주의: 한글 /추가·/삭제·/목록은 공시감시(DART) 명령 — 스파이크는 전용 명령만 받는다
    if cmd in ("스파이크추가", "스파이크"):
        code, name, err = parse_target(parts[1:], "형식: /스파이크추가 종목코드 또는 종목명\n예: /스파이크추가 005930 · /스파이크추가 삼성전자")
        if err:
            return err
        wl = read_wl()
        wl[code] = name
        save_wl(wl)
        return f"⭐ 스파이크(주가) 감시 추가: {name}({code}) — 총 {len(wl)}종목"

    if cmd in ("스파이크삭제", "스파이크제거"):
        if len(parts) < 2:
            return "형식: /스파이크삭제 종목코드 또는 종목명"
        wl = read_wl()
        code = find_in(wl, " ".join(parts[1:]))
        if code:
            name = wl.pop(code)
            save_wl(wl)
            return f"⭐ 스파이크 감시 해제: {name}({code}) — 총 {len(wl)}종목"
        return f"스파이크 목록에서 찾지 못했습니다: {' '.join(parts[1:])}"

    if cmd in ("스파이크목록",):
        wl = read_wl()
        if not wl:
            return "스파이크 감시 종목이 비어 있습니다. /스파이크추가 로 등록하세요."
        lines = [f"· {n}({c})" for c, n in sorted(wl.items())]
        return f"📋 스파이크 감시 {len(wl)}종목\n" + "\n".join(lines)

    # 📢 공시감시 명령은 2026-08-29 봇 분리로 전용 봇이 처리 — 길 안내만
    if cmd in ("추가", "add", "삭제", "제거", "remove", "목록", "list", "유형",
               "types", "전체", "all", "키워드", "keyword", "기업", "co",
               "company", "보고자", "reporter"):
        return ("📢 공시감시 명령은 '안녕하재 공시모니터링' 봇으로 분리됐습니다\n"
                "→ @filingsmonitor_0796_bot 대화방에서 입력해 주세요\n"
                "(이 봇은 💼 보유 /hold · ⭐ 주가 스파이크 /spike 전용)")

    return None  # 모르는 명령은 무시 (다른 봇 메시지와 혼선 방지)


MY_MENU = [
    ("hold", "💼 보유 추가 — 전 봇 마킹·알림 승격 (=/보유추가)"),
    ("holds", "💼 보유 목록 (=/보유목록)"),
    ("hold_del", "💼 보유 삭제 (=/보유삭제)"),
    ("spike", "⭐ 주가 스파이크 감시 추가 (=/스파이크추가)"),
    ("spikes", "⭐ 주가 스파이크 목록 (=/스파이크목록)"),
    ("spike_del", "⭐ 주가 스파이크 삭제 (=/스파이크삭제)"),
    ("help", "사용법 안내"),
]


def register_menu(token):
    """'/' 팝업 메뉴 등록 (시작 시 1회, 멱등) — 이 봇은 보유·스파이크 전용."""
    try:
        requests.post(f"https://api.telegram.org/bot{token}/setMyCommands",
                      json={"commands": [{"command": c, "description": d}
                                         for c, d in MY_MENU]},
                      timeout=10)
    except Exception as e:
        print(f"[TG] setMyCommands 실패: {e}")


def main():
    load_env()
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    allowed = os.environ.get("TELEGRAM_CHAT_ID", "")
    if not token or not allowed:
        sys.exit("TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID 필요 (~/bots/krwatch.env)")
    api = f"https://api.telegram.org/bot{token}"
    offset = 0
    register_menu(token)
    print("watch_bot 시작")
    while True:
        try:
            r = requests.get(f"{api}/getUpdates",
                             params={"offset": offset, "timeout": 50},
                             timeout=60).json()
            for u in r.get("result", []):
                offset = u["update_id"] + 1
                msg = u.get("message") or {}
                chat_id = str((msg.get("chat") or {}).get("id", ""))
                text = msg.get("text", "")
                if chat_id != str(allowed) or not text:
                    continue
                reply = handle(text)
                if reply:
                    requests.post(f"{api}/sendMessage",
                                  json={"chat_id": chat_id, "text": reply},
                                  timeout=20)
        except Exception as e:
            print(f"[warn] {e}")
            time.sleep(10)


if __name__ == "__main__":
    main()
