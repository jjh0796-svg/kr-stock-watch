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


HELP = (
    "📋 종목 관리 — 목적별로 세 가지입니다\n"
    "\n"
    "💼 보유 종목 (전 봇 공용 — 알림에 💼 표시 + 방산 야간알림 등 승격)\n"
    "/보유추가 005930 [이름] · /보유삭제 · /보유목록  (영문: /hold /hold_del /holds)\n"
    "\n"
    "⭐ 주가 스파이크 감시 (장중 급등락·거래량·52주 — 보유 종목은 자동 포함)\n"
    "/스파이크추가 005930 · /스파이크삭제 · /스파이크목록  (영문: /spike /spike_del /spikes)\n"
    "\n"
    "📢 공시감시 (DART 실시간 — 설정 변경은 다음 사이클 ≤15분 반영)\n"
    "/추가 005930 · /삭제 · /목록 · /유형 · /전체 · /키워드 · /기업 삼성전자\n"
    "\n"
    "※ 마감스캔 대상은 공시감시 목록과 동일합니다"
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
        if len(parts) < 2 or not parts[1].isdigit() or len(parts[1]) != 6:
            return "형식: /보유추가 종목코드6자리 [이름]"
        code = parts[1]
        name = " ".join(parts[2:]) if len(parts) > 2 else resolve_name(code)
        if not name:
            return f"❓ {code} 종목명을 찾지 못했습니다. `/보유추가 {code} 이름` 으로 지정해주세요."
        hd = read_hd()
        hd[code] = name
        save_hd(hd)
        return f"💼 보유 등록: {name}({code}) — 총 {len(hd)}종목\n(전 봇에서 💼 표시·알림 승격 적용)"

    if cmd in ("보유삭제", "보유제거"):
        if len(parts) < 2:
            return "형식: /보유삭제 종목코드"
        code = parts[1]
        hd = read_hd()
        if code in hd:
            name = hd.pop(code)
            save_hd(hd)
            return f"🗑 보유 해제: {name}({code}) — 총 {len(hd)}종목"
        return f"보유 목록에 없는 코드입니다: {code}"

    if cmd in ("보유목록",):
        hd = read_hd()
        if not hd:
            return "보유 종목이 비어 있습니다. /보유추가 로 등록하세요."
        lines = [f"💼 {n}({c})" for c, n in sorted(hd.items())]
        return f"💼 보유 {len(hd)}종목\n" + "\n".join(lines)

    # 주의: 한글 /추가·/삭제·/목록은 공시감시(DART) 명령 — 스파이크는 전용 명령만 받는다
    if cmd in ("스파이크추가", "스파이크"):
        if len(parts) < 2 or not parts[1].isdigit() or len(parts[1]) != 6:
            return "형식: /스파이크추가 종목코드6자리 [이름]"
        code = parts[1]
        name = " ".join(parts[2:]) if len(parts) > 2 else resolve_name(code)
        if not name:
            return f"❓ {code} 종목명을 찾지 못했습니다. `/추가 {code} 이름` 으로 지정해주세요."
        wl = read_wl()
        wl[code] = name
        save_wl(wl)
        return f"⭐ 스파이크(주가) 감시 추가: {name}({code}) — 총 {len(wl)}종목"

    if cmd in ("스파이크삭제", "스파이크제거"):
        if len(parts) < 2:
            return "형식: /스파이크삭제 종목코드"
        code = parts[1]
        wl = read_wl()
        if code in wl:
            name = wl.pop(code)
            save_wl(wl)
            return f"⭐ 스파이크 감시 해제: {name}({code}) — 총 {len(wl)}종목"
        return f"목록에 없는 코드입니다: {code}"

    if cmd in ("스파이크목록",):
        wl = read_wl()
        if not wl:
            return "스파이크 감시 종목이 비어 있습니다. /스파이크추가 로 등록하세요."
        lines = [f"· {n}({c})" for c, n in sorted(wl.items())]
        return f"📋 스파이크 감시 {len(wl)}종목\n" + "\n".join(lines)

    # 📢 공시감시 명령 허브 (2026-08-29): dart_watch의 처리 로직을 재사용하고,
    # 바뀐 설정을 watch_config_repo.json으로 커밋해 Actions 감시가 다음 사이클에 반영.
    # (같은 토큰 getUpdates 충돌 해소 — 텔레그램 수신은 이 상주 봇이 전담)
    dart_reply = handle_dart(text)
    if dart_reply:
        return dart_reply

    return None  # 모르는 명령은 무시 (다른 봇 메시지와 혼선 방지)


DART_CFG_FILE = BASE_DIR / "watch_config_repo.json"
DART_MUTATING = ("추가", "add", "삭제", "제거", "remove", "유형", "types",
                 "전체", "all", "키워드", "keyword", "보고자", "reporter")
DART_QUERY = ("목록", "list", "기업", "co", "company")


def handle_dart(text):
    cmd = text.strip().split()[0].lstrip("/").lower() if text.strip() else ""
    if cmd not in DART_MUTATING and cmd not in DART_QUERY:
        return None
    import json
    import re as _re
    import subprocess

    import dart_watch  # 같은 폴더(서버 clone) — 명령 로직 재사용

    from common import DEFAULT_CONFIG

    cfg = {k: (dict(v) if isinstance(v, dict) else list(v) if isinstance(v, list) else v)
           for k, v in DEFAULT_CONFIG.items()}
    if DART_CFG_FILE.exists():
        try:
            for k, v in json.loads(DART_CFG_FILE.read_text(encoding="utf-8")).items():
                cfg[k] = v
        except Exception:
            pass
    before = json.dumps({k: v for k, v in cfg.items() if k != "tg_offset"},
                        ensure_ascii=False, sort_keys=True)
    try:
        reply = dart_watch.handle_command(text.strip(), cfg)
    except Exception as e:
        return f"📢 공시감시 명령 처리 오류: {type(e).__name__}: {e}"
    if not reply:
        return None
    after = {k: v for k, v in cfg.items() if k != "tg_offset"}
    changed = json.dumps(after, ensure_ascii=False, sort_keys=True) != before
    if changed:
        try:
            DART_CFG_FILE.write_text(
                json.dumps(after, ensure_ascii=False, indent=1), encoding="utf-8")
            subprocess.run(["git", "-C", str(BASE_DIR), "pull", "-q", "--rebase"],
                           capture_output=True, timeout=60)
            subprocess.run(["git", "-C", str(BASE_DIR), "add", "watch_config_repo.json"],
                           capture_output=True, timeout=30)
            subprocess.run(["git", "-C", str(BASE_DIR),
                            "-c", "user.name=watch-hub", "-c", "user.email=bot@server",
                            "commit", "-q", "-m", f"watch hub: {text.strip()[:50]}"],
                           capture_output=True, timeout=30)
            push = subprocess.run(["git", "-C", str(BASE_DIR), "push", "-q"],
                                  capture_output=True, timeout=60)
            note = "\n\n📢 공시감시 설정 저장됨 — 다음 감시 사이클(≤15분)부터 적용"
            if push.returncode != 0:
                note = "\n\n⚠️ 설정 저장은 됐지만 전송 실패 — 다음 명령 때 재시도됩니다"
        except Exception as e:
            note = f"\n\n⚠️ 설정 전송 오류: {e}"
        reply += note
    # dart 응답은 HTML 태그 포함 — 이 봇은 평문 발송이라 태그 제거
    return _re.sub(r"</?b>", "", reply)


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
