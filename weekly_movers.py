# ==========================================
# 🌍 주간 상승률 톱20 (Dean's Ticker 스타일)
#   한국: KRX OpenAPI 전종목 (전주 동일 시점 종가 대비)
#   미국/유럽/일본: 지수 구성종목(data/global_universe.json, S&P500·주요 유럽지수·닛케이225)
#     — yfinance 일봉으로 주간 등락 계산
#   종목 한글명·한줄 설명: Gemini 생성 캐시(data/movers_names.json)
#     — 새 종목만 추가 생성, 실패 시 원어 표기. 워크플로가 캐시를 repo에 재커밋.
# 스케줄: 토요일 아침 (미국 금요일 마감 후)
# ==========================================
import json
import os
import re
import time
from datetime import timedelta
from pathlib import Path

import requests

from common import esc, now_kst, tg_send_long
from daily_scan import EXCLUDE_NAME_RE, krx_daily

UNIVERSE_FILE = Path("data/global_universe.json")
NAMES_FILE = Path("data/movers_names.json")

TOP_N = int(os.environ.get("MOVERS_TOP_N", "20"))
KR_MIN_MKTCAP = float(os.environ.get("KR_MIN_MKTCAP", "1e11"))     # 시총 1,000억↑
KR_MIN_TRDVAL = float(os.environ.get("KR_MIN_TRDVAL", "1e9"))      # 거래대금 10억↑
KR_EXTRA_EXCLUDE = re.compile(r"리츠|인프라|하이일드|채권|투자회사")

GEMINI_MODELS = ("gemini-2.5-flash", "gemini-2.0-flash")


def _num(s) -> float:
    try:
        return float(str(s).replace(",", "") or 0)
    except ValueError:
        return 0.0


# ─── 한국: KRX 전종목 주간 등락 ───────────────────────────────────────────────

def kr_weekly() -> tuple[list[dict], str, str]:
    """(톱리스트, 기준일, 전주기준일). 데이터 없으면 ([], '', '')."""
    d = now_kst()
    base_dd, base_rows = None, None
    for _ in range(7):
        if d.weekday() < 5:
            rows = krx_daily(d.strftime("%Y%m%d"))
            if rows:
                base_dd, base_rows = d.strftime("%Y%m%d"), rows
                break
        d -= timedelta(days=1)
    if not base_dd:
        return [], "", ""
    p = d - timedelta(days=7)
    prev_dd, prev_rows = None, None
    for _ in range(7):
        if p.weekday() < 5:
            rows = krx_daily(p.strftime("%Y%m%d"))
            if rows:
                prev_dd, prev_rows = p.strftime("%Y%m%d"), rows
                break
        p -= timedelta(days=1)
    if not prev_dd:
        return [], "", ""

    prev_close = {r.get("ISU_CD"): _num(r.get("TDD_CLSPRC")) for r in prev_rows}
    out = []
    for r in base_rows:
        name = r.get("ISU_NM", "")
        code = r.get("ISU_CD", "")
        close = _num(r.get("TDD_CLSPRC"))
        pc = prev_close.get(code, 0)
        if (not code or not pc or not close
                or EXCLUDE_NAME_RE.search(name) or KR_EXTRA_EXCLUDE.search(name)
                or _num(r.get("MKTCAP")) < KR_MIN_MKTCAP
                or _num(r.get("ACC_TRDVAL")) < KR_MIN_TRDVAL):
            continue
        pct = (close / pc - 1) * 100
        out.append({"id": f"KR:{code}", "name": name, "pct": pct})
    out.sort(key=lambda x: -x["pct"])
    return out[:TOP_N], base_dd, prev_dd


# ─── 해외: yfinance 주간 등락 ─────────────────────────────────────────────────

def global_weekly(tickers: dict[str, str]) -> list[dict]:
    import yfinance as yf

    out = []
    symbols = list(tickers.keys())
    for i in range(0, len(symbols), 120):
        chunk = symbols[i:i + 120]
        try:
            data = yf.download(chunk, period="1mo", interval="1d", group_by="ticker",
                               progress=False, threads=True, auto_adjust=True)
        except Exception as e:
            print(f"[yf] 청크 {i} 실패: {e}")
            continue
        for sym in chunk:
            try:
                closes = (data[sym]["Close"] if len(chunk) > 1 else data["Close"]).dropna()
                if len(closes) < 4:
                    continue
                last_dt = closes.index[-1]
                target = last_dt - timedelta(days=7)
                prev = closes[closes.index <= target]
                if prev.empty:
                    continue
                pct = (closes.iloc[-1] / prev.iloc[-1] - 1) * 100
                out.append({"id": sym, "name": tickers[sym], "pct": float(pct)})
            except Exception:
                continue
        time.sleep(1)
    out.sort(key=lambda x: -x["pct"])
    return out[:TOP_N]


# ─── Gemini: 한글명·한줄 설명 캐시 ────────────────────────────────────────────

def load_names() -> dict:
    try:
        return json.loads(NAMES_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def enrich_names(entries: list[dict], cache: dict) -> None:
    """캐시에 없는 종목만 Gemini로 한글 표기명·5단어 이내 설명 생성."""
    key = (os.environ.get("GEMINI_API_KEY") or "").strip()
    todo = [e for e in entries if e["id"] not in cache]
    if not key or not todo:
        return
    payload = [{"id": e["id"], "name": e["name"]} for e in todo]
    prompt = (
        "다음은 상장기업 목록이다(id가 KR:로 시작하면 한국 기업, 그 외는 티커).\n"
        "각 기업에 대해 kr(한국 언론이 쓰는 한글 표기명, 한국 기업이면 입력 그대로)과 "
        "desc(핵심 사업 한 줄, 8자 이내 명사구 — 예: 낸드플래시, 정유, 반도체 장비)를 답하라.\n"
        "확실히 모르는 기업의 desc는 \"-\" 로 답하라(추측 금지).\n"
        '다음 JSON 형식으로만: {"<id>": {"kr": "...", "desc": "..."}, ...}\n\n'
        + json.dumps(payload, ensure_ascii=False)
    )
    body = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"responseMimeType": "application/json", "temperature": 0.1},
    }
    for model in GEMINI_MODELS:
        try:
            resp = requests.post(
                f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}",
                json=body, timeout=90,
            )
            if resp.status_code != 200:
                continue
            parsed = json.loads(resp.json()["candidates"][0]["content"]["parts"][0]["text"])
            added = 0
            for e in todo:
                v = parsed.get(e["id"])
                if isinstance(v, dict) and isinstance(v.get("kr"), str):
                    cache[e["id"]] = {"kr": v["kr"].strip() or e["name"],
                                      "desc": (v.get("desc") or "-").strip()[:20]}
                    added += 1
            print(f"[Gemini] 신규 종목 설명 {added}/{len(todo)}개 생성 ({model})")
            NAMES_FILE.parent.mkdir(exist_ok=True)
            NAMES_FILE.write_text(json.dumps(cache, ensure_ascii=False, indent=1), encoding="utf-8")
            return
        except Exception as exc:
            print(f"[Gemini] {model} 실패: {exc}")
    print("[Gemini] 사용 불가 — 원어 표기로 발송")


# ─── 메시지 ──────────────────────────────────────────────────────────────────

def fmt_section(title: str, entries: list[dict], cache: dict) -> str:
    lines = [f"\n<b>{title}</b>"]
    for e in entries:
        meta = cache.get(e["id"], {})
        name = meta.get("kr") or e["name"]
        desc = meta.get("desc", "")
        desc_txt = f"｜{esc(desc)}" if desc and desc != "-" else ""
        lines.append(f"+{e['pct']:.0f}%｜{esc(name)}{desc_txt}")
    return "\n".join(lines)


def main() -> None:
    universe = json.loads(UNIVERSE_FILE.read_text(encoding="utf-8"))
    cache = load_names()

    def safe(fn, *args, default=None):
        try:
            return fn(*args)
        except Exception as exc:
            print(f"⚠️ {fn.__name__} 실패: {type(exc).__name__}: {str(exc)[:120]}")
            return default

    kr, base_dd, prev_dd = safe(kr_weekly, default=([], "", ""))
    us = safe(global_weekly, universe.get("us", {}), default=[])
    eu = safe(global_weekly, universe.get("europe", {}), default=[])
    jp = safe(global_weekly, universe.get("japan", {}), default=[])

    for section in (kr, us, eu, jp):
        enrich_names(section, cache)

    period = ""
    if base_dd and prev_dd:
        period = f" · {prev_dd[4:6]}/{prev_dd[6:]}~{base_dd[4:6]}/{base_dd[6:]}"
    sections = [f"🌍 <b>주간 상승률 톱{TOP_N}</b>{period}"]
    if kr:
        sections.append(fmt_section("🇰🇷 Korea (시총 1,000억↑)", kr, cache))
    if us:
        sections.append(fmt_section("🇺🇸 US (S&P500·나스닥100)", us, cache))
    if eu:
        sections.append(fmt_section("🇪🇺 Europe (주요지수)", eu, cache))
    if jp:
        sections.append(fmt_section("🇯🇵 Japan (닛케이225)", jp, cache))
    sections.append("\n<i>해외는 지수 구성종목 기준 · 설명은 AI 생성 참고용</i>")

    msg = "\n".join(sections)
    if os.environ.get("DRY_RUN"):
        print("DRY_RUN — 발송 생략. 미리보기:\n" + msg)
        return
    tg_send_long(msg)
    print("발송 완료")


if __name__ == "__main__":
    main()
