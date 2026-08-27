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


def read_wl():
    out = {}
    if WATCHLIST_FILE.exists():
        for line in WATCHLIST_FILE.read_text(encoding="utf-8-sig").splitlines():
            if "," in line:
                code, _, name = line.partition(",")
                if code.strip().isdigit():
                    out[code.strip()] = name.strip()
    return out


def save_wl(wl):
    WATCHLIST_FILE.parent.mkdir(parents=True, exist_ok=True)
    WATCHLIST_FILE.write_text(
        "\n".join(f"{c},{n}" for c, n in sorted(wl.items())) + "\n",
        encoding="utf-8")


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


HELP = (
    "📋 관심종목 관리 명령\n"
    "/추가 005930 — 코드로 추가 (이름 자동)\n"
    "/추가 005930 삼성전자 — 이름 지정\n"
    "/삭제 005930\n"
    "/목록 — 현재 관심종목\n"
    "관심종목은 장중 스파이크 감시(거래량 폭증·52주 신고저)에 바로 반영됩니다."
)


def handle(text):
    """명령 처리 → 응답 문자열."""
    parts = text.strip().split()
    if not parts:
        return None
    cmd = parts[0].lstrip("/").lower()

    if cmd in ("start", "도움말", "help"):
        return HELP

    if cmd in ("추가", "add"):
        if len(parts) < 2 or not parts[1].isdigit() or len(parts[1]) != 6:
            return "형식: /추가 종목코드6자리 [이름]"
        code = parts[1]
        name = " ".join(parts[2:]) if len(parts) > 2 else resolve_name(code)
        if not name:
            return f"❓ {code} 종목명을 찾지 못했습니다. `/추가 {code} 이름` 으로 지정해주세요."
        wl = read_wl()
        wl[code] = name
        save_wl(wl)
        return f"✅ 추가됨: {name}({code}) — 총 {len(wl)}종목"

    if cmd in ("삭제", "제거", "del", "remove"):
        if len(parts) < 2:
            return "형식: /삭제 종목코드"
        code = parts[1]
        wl = read_wl()
        if code in wl:
            name = wl.pop(code)
            save_wl(wl)
            return f"🗑 삭제됨: {name}({code}) — 총 {len(wl)}종목"
        return f"목록에 없는 코드입니다: {code}"

    if cmd in ("목록", "list"):
        wl = read_wl()
        if not wl:
            return "관심종목이 비어 있습니다. /추가 로 등록하세요."
        lines = [f"· {n}({c})" for c, n in sorted(wl.items())]
        return f"📋 관심종목 {len(wl)}종목\n" + "\n".join(lines)

    return None  # 모르는 명령은 무시 (다른 봇 메시지와 혼선 방지)


def main():
    load_env()
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    allowed = os.environ.get("TELEGRAM_CHAT_ID", "")
    if not token or not allowed:
        sys.exit("TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID 필요 (~/bots/krwatch.env)")
    api = f"https://api.telegram.org/bot{token}"
    offset = 0
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
