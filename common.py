# ==========================================
# 공용 유틸: 텔레그램 발송, 상태 저장, 관심종목 로드
# ==========================================
import html
import json
import os
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import requests

try:  # 로컬 실행 편의용 (.env) — Actions에서는 없어도 됨
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

KST = ZoneInfo("Asia/Seoul")
UA_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
}

DRY_RUN = os.environ.get("DRY_RUN", "").strip().lower() in ("1", "true", "yes")
STATE_DIR = os.environ.get("STATE_DIR", ".state")


def now_kst() -> datetime:
    return datetime.now(KST)


def esc(s: str) -> str:
    return html.escape(str(s or ""), quote=False)


# ─── 텔레그램 ──────────────────────────────────────────────────────────────────
# 주의: 읽기 타임아웃은 "서버 도착 후 응답만 유실"로 간주하고 재시도하지 않는다.
# (재시도했다가 같은 메시지가 12통 도착한 사고 이력 있음 — 연결 실패만 재시도)

def tg_send(text: str) -> bool:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
    if DRY_RUN or not token or not chat_id:
        reason = "DRY_RUN" if DRY_RUN else "토큰/챗ID 없음"
        print(f"[TG:{reason}] {text[:3900]}")
        return True

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    for attempt in range(2):
        try:
            r = requests.post(url, json=payload, timeout=(10, 30))
            if r.status_code == 200:
                return True
            if r.status_code == 429:
                try:
                    wait = int(r.json().get("parameters", {}).get("retry_after", 5))
                except Exception:
                    wait = 5
                time.sleep(wait + 1)
                continue
            print(f"[TG] HTTP {r.status_code}: {r.text[:200]}")
            return False
        except requests.exceptions.ReadTimeout:
            print("[TG] 읽기 타임아웃 — 도착으로 간주, 재시도 안 함")
            return True
        except requests.exceptions.ConnectionError as e:
            print(f"[TG] 연결 실패 ({attempt + 1}/2): {e}")
            time.sleep(3)
    return False


def tg_send_long(text: str, limit: int = 3800) -> None:
    """텔레그램 4096자 한도 대비 — 줄 단위로 잘라 나눠 보낸다."""
    if len(text) <= limit:
        tg_send(text)
        return
    chunk: list[str] = []
    size = 0
    for line in text.split("\n"):
        if size + len(line) + 1 > limit and chunk:
            tg_send("\n".join(chunk))
            time.sleep(1)
            chunk, size = [], 0
        chunk.append(line)
        size += len(line) + 1
    if chunk:
        tg_send("\n".join(chunk))


# ─── 상태 저장 (Actions cache에 실림 — 유실될 수 있다는 전제로 설계) ──────────

def load_state(name: str, default):
    path = os.path.join(STATE_DIR, name)
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save_state(name: str, data) -> None:
    os.makedirs(STATE_DIR, exist_ok=True)
    path = os.path.join(STATE_DIR, name)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    os.replace(tmp, path)


# ─── 관심종목 ──────────────────────────────────────────────────────────────────
# 우선순위: WATCHLIST 환경변수("005930:삼성전자,000660:SK하이닉스")
#           → watchlist.csv (한 줄에 "종목코드,종목명")

def load_watchlist() -> dict[str, str]:
    env_val = os.environ.get("WATCHLIST", "").strip()
    watch: dict[str, str] = {}
    if env_val:
        for part in env_val.split(","):
            part = part.strip()
            if not part:
                continue
            code, _, name = part.partition(":")
            code = code.strip()
            if code:
                watch[code] = name.strip() or code
        return watch

    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "watchlist.csv")
    try:
        with open(path, encoding="utf-8-sig") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                code, _, name = line.partition(",")
                code = code.strip()
                if code:
                    watch[code] = name.strip() or code
    except FileNotFoundError:
        pass
    return watch
