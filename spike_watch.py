# -*- coding: utf-8 -*-
"""
장중 스파이크 감시 (spike_watch)
- 유니버스: 워치리스트(watchlist.csv) + 코스피·코스닥 등락률 상위/하위 각 20종목
- 트리거:
  T1 급변: 최근 5분 ±3% 이상 (전 유니버스) / 등락 상위 리스트 최초 진입(±7%↑)
  T2 거래량: 당일 누적 거래량이 20일 평균의 300% 돌파 (워치리스트만)
  T3 52주 신고가/신저가 터치 (워치리스트만)
- 노이즈 억제: 종목·트리거당 1일 1회, T1 재알림은 직전 알림가 대비 추가 ±3%시.
  장 시작 직후(09:00~09:05) 제외, 폴링 매분 09:05~15:30.

사용 (오라클 서버 cron):
  python spike_watch.py --prep   # 평일 08:40 — pykrx로 20일 평균 거래량·52주 고저 캐시
  python spike_watch.py --tick   # 평일 9~15시 매분 — 감시 1회
환경: TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID (미설정 시 발송 없이 로그만 — 관찰 모드)
"""
import argparse
import datetime
import json
import os
import sys
from pathlib import Path

import requests

for _s in (sys.stdout, sys.stderr):
    if hasattr(_s, "reconfigure"):
        _s.reconfigure(encoding="utf-8", errors="replace")

BASE_DIR = Path(__file__).resolve().parent
STATE_DIR = Path(os.environ.get("SPIKE_STATE_DIR",
                                str(Path.home() / ".spike_watch")))
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}

CHG_5MIN = 3.0        # T1: 5분 변동 임계 (%)
CHG_ENTRY = 7.0       # T1b: 등락 상위 최초 진입 알림 임계 (%)
VOL_MULT = 3.0        # T2: 20일 평균 거래량 대비 배수
RANK_N = 20           # 등락 상위/하위 각 종목 수


# ------------------------------------------------------------------ 환경/상태

def load_env():
    """스크립트 옆 .env와 ~/bots/krwatch.env를 환경변수로 (기존 값 유지)."""
    for p in (BASE_DIR / ".env", Path.home() / "bots" / "krwatch.env"):
        if not p.exists():
            continue
        for line in p.read_text(encoding="utf-8-sig").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                os.environ.setdefault(k.strip(), v.strip())


def today_str():
    return datetime.date.today().strftime("%Y%m%d")


def load_json(path, default):
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return default


def save_json(path, obj):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")


def read_watchlist():
    out = {}
    path = BASE_DIR / "watchlist.csv"
    if path.exists():
        for line in path.read_text(encoding="utf-8-sig").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split(",")
            if len(parts) >= 2 and parts[0].strip().isdigit():
                out[parts[0].strip()] = parts[1].strip()
    # 비공개 관심종목: env WATCHLIST="005930:삼성전자,000660:SK하이닉스" (daily_scan과 동일 형식)
    for item in os.environ.get("WATCHLIST", "").split(","):
        if ":" in item:
            code, _, name = item.strip().partition(":")
            if code.strip().isdigit():
                out[code.strip()] = name.strip()
    # 대화형 봇(watch_bot)이 관리하는 동적 관심종목 파일 ("코드,이름" 줄 단위)
    wf = Path(os.environ.get("WATCHLIST_FILE",
                             str(Path.home() / "bots" / "watchlist.csv")))
    if wf.exists():
        for line in wf.read_text(encoding="utf-8-sig").splitlines():
            if "," in line:
                code, _, name = line.partition(",")
                if code.strip().isdigit():
                    out[code.strip()] = name.strip()
    return out


def read_holdings():
    """보유 종목 (watch_bot /보유추가 관리, 전 봇 공용 — 2026-08-29).
    스파이크 감시에선 워치리스트와 동일 대우 + 💼 태그."""
    out = {}
    hf = Path(os.environ.get("HOLDINGS_FILE",
                             str(Path.home() / "bots" / "holdings.csv")))
    if hf.exists():
        for line in hf.read_text(encoding="utf-8-sig").splitlines():
            if "," in line:
                code, _, name = line.partition(",")
                if code.strip().isdigit():
                    out[code.strip()] = name.strip()
    return out


# ------------------------------------------------------------------ 수집

def fetch_rank(sort, market):
    """네이버 등락률 상위/하위. sort: up|down, market: KOSPI|KOSDAQ"""
    url = f"https://m.stock.naver.com/api/stocks/{sort}/{market}"
    try:
        r = requests.get(url, params={"page": 1, "pageSize": RANK_N},
                         headers=UA, timeout=10)
        r.raise_for_status()
        return r.json().get("stocks", [])
    except Exception as e:
        print(f"[warn] rank {sort}/{market}: {e}")
        return []


def fetch_quotes(codes):
    """네이버 폴링 API 배치 시세. 반환 {code: {name, price, rate, volume}}"""
    out = {}
    codes = list(codes)
    for i in range(0, len(codes), 30):
        chunk = ",".join(codes[i:i + 30])
        url = f"https://polling.finance.naver.com/api/realtime/domestic/stock/{chunk}"
        try:
            r = requests.get(url, headers=UA, timeout=10)
            r.raise_for_status()
            for d in r.json().get("datas", []):
                code = d.get("itemCode", "")
                price = float(str(d.get("closePrice", "0")).replace(",", "") or 0)
                rate = float(str(d.get("fluctuationsRatio", "0")).replace(",", "") or 0)
                vol = float(str(d.get("accumulatedTradingVolume", "0")).replace(",", "") or 0)
                if code and price > 0:
                    out[code] = {"name": d.get("stockName", code),
                                 "price": price, "rate": rate, "volume": vol}
        except Exception as e:
            print(f"[warn] quotes: {e}")
    return out


# ------------------------------------------------------------------ prep

def prep():
    """아침: 워치리스트의 20일 평균 거래량·52주 고저를 pykrx로 캐시."""
    from pykrx import stock
    wl = {**read_watchlist(), **read_holdings()}  # 보유 종목도 트리거 캐시 대상
    end = datetime.date.today().strftime("%Y%m%d")
    start_1y = (datetime.date.today() - datetime.timedelta(days=370)).strftime("%Y%m%d")
    cache = {}
    for code, name in wl.items():
        try:
            df = stock.get_market_ohlcv(start_1y, end, code)
            if df is None or df.empty:
                continue
            cache[code] = {
                "name": name,
                "avg20_vol": float(df["거래량"].tail(20).mean()),
                "high52": float(df["고가"].max()),
                "low52": float(df["저가"].min()),
            }
        except Exception as e:
            print(f"[warn] prep {code}: {e}")
    save_json(STATE_DIR / f"prep_{today_str()}.json", cache)
    print(f"prep 완료: {len(cache)}/{len(wl)}종목")


# ------------------------------------------------------------------ tick

def in_session(now):
    """이력 적재 창 — 09:00부터 가격을 쌓아야 5분 트리거가 09:05에 바로 발동한다
    (2026-08-31: 기존 09:05 시작이라 첫 5분 급변 알림이 09:10에야 열리던 지연 해소)."""
    if now.weekday() >= 5:
        return False
    t = now.time()
    return datetime.time(9, 0) <= t <= datetime.time(15, 30)


def alerts_allowed(now):
    """알림 발송 창 — 개장 직후 동시호가 왜곡(09:00~09:04)은 적재만 하고 침묵."""
    return now.time() >= datetime.time(9, 5)


def log_signal(code, name, direction, note):
    """알림 성과 자동 평가용 신호 기록 (CODEX signal_scorecard가 주간 채점).
    실패해도 알림에 영향 없도록 전부 삼킨다."""
    try:
        path = Path(os.environ.get("SIGNAL_LOG_FILE",
                                   str(Path.home() / "bots" / "signals.jsonl")))
        path.parent.mkdir(parents=True, exist_ok=True)
        entry = {"ts": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
                 "bot": "spike", "code": code, "name": name[:40],
                 "dir": direction, "note": note[:120]}
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        pass


def send(lines):
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat = os.environ.get("TELEGRAM_CHAT_ID")
    text = "\n".join(lines)
    if not token or not chat:
        print("[관찰모드] 발송 생략:\n" + text)
        return
    requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                  json={"chat_id": chat, "text": text,
                        "disable_web_page_preview": True}, timeout=20)


def tick():
    now = datetime.datetime.now()
    if not in_session(now):
        return
    day = today_str()
    prep_cache = load_json(STATE_DIR / f"prep_{day}.json", {})
    hist = load_json(STATE_DIR / f"intraday_{day}.json", {})
    sent = load_json(STATE_DIR / f"sent_{day}.json", {})
    ts = now.strftime("%H:%M")

    hd = read_holdings()
    wl = {**read_watchlist(), **hd}  # 보유는 워치와 동일 트리거 + 💼 태그
    ranked = {}   # code -> rate (등락 상위 리스트 출신)
    for market in ("KOSPI", "KOSDAQ"):
        for sort in ("up", "down"):
            for s in fetch_rank(sort, market):
                code = s.get("itemCode", "")
                if code:
                    ranked[code] = s
    universe = set(wl) | set(ranked)
    quotes = fetch_quotes(universe)

    ups, downs, others = [], [], []   # (정렬키, 줄) — 🔴급등/🔵급락/📢거래량·52주
    sigs = []                          # (code, name, dir, note) — 성과 채점용
    # 09:00~09:04 침묵 창 — 트리거 평가 없이 이력만 적재해야 sent 오염(미발송 신호가
    # 중복방지에 기록돼 09:05 이후 영영 침묵)이 없다
    silent = not alerts_allowed(now)

    def mark(code, kind, extra=None):
        sent.setdefault(code, {})[kind] = extra if extra is not None else True

    for code, q in quotes.items():
        name, price, rate = q["name"], q["price"], q["rate"]
        tag = "💼" if code in hd else ("⭐" if code in wl else "·")

        # 이력 적재 (최근 10분)
        h = hist.setdefault(code, [])
        h.append([ts, price])
        del h[:-11]

        if silent:
            continue

        s = sent.get(code, {})

        # T1: 5분 급변 — 방향은 5분 변동 기준
        if len(h) >= 6:
            base = h[-6][1]
            if base > 0:
                chg5 = (price / base - 1) * 100
                last_alert_price = s.get("t1")
                need = (last_alert_price is None or
                        abs(price / last_alert_price - 1) * 100 >= CHG_5MIN)
                if abs(chg5) >= CHG_5MIN and need:
                    # 섹션은 "5분 변동" 기준 — 당일과 방향이 다르면 반전 표시
                    flip = ""
                    if chg5 > 0 > rate:
                        flip = " ↗반등중"
                    elif chg5 < 0 < rate:
                        flip = " ↘반락중"
                    line = (f"{tag} {name} 5분 {chg5:+.1f}% · "
                            f"당일 {rate:+.1f}%{flip} · {price:,.0f}원")
                    (ups if chg5 > 0 else downs).append((abs(chg5), line))
                    sigs.append((code, name, "up" if chg5 > 0 else "down", f"5분 {chg5:+.1f}%"))
                    mark(code, "t1", price)

        # T1b: 등락 상위 최초 진입 (±7% 이상) — 방향은 당일 등락 기준
        if code in ranked and abs(rate) >= CHG_ENTRY and not s.get("entry"):
            line = f"{tag} {name} 당일 {rate:+.1f}% 상위진입 · {price:,.0f}원"
            (ups if rate > 0 else downs).append((abs(rate), line))
            sigs.append((code, name, "up" if rate > 0 else "down", f"상위진입 {rate:+.1f}%"))
            mark(code, "entry")

        # 워치리스트 전용 트리거
        p = prep_cache.get(code)
        if p and code in wl:
            # T2: 거래량 폭증 (당일 등락을 색으로 병기)
            if (p["avg20_vol"] > 0 and q["volume"] >= VOL_MULT * p["avg20_vol"]
                    and not s.get("t2")):
                mult = q["volume"] / p["avg20_vol"]
                dot = "🔴" if rate > 0 else ("🔵" if rate < 0 else "⚪")
                others.append((mult, f"⭐ {name} 거래량 x{mult:.1f} {dot}{rate:+.1f}%"))
                mark(code, "t2")
            # T3: 52주 신고/신저
            if price >= p["high52"] and not s.get("t3h"):
                others.append((999, f"🚀 {name} 52주 신고가 {price:,.0f}원"))
                sigs.append((code, name, "up", "52주 신고가"))
                mark(code, "t3h")
            if price <= p["low52"] and not s.get("t3l"):
                others.append((999, f"🧊 {name} 52주 신저가 {price:,.0f}원"))
                sigs.append((code, name, "down", "52주 신저가"))
                mark(code, "t3l")

    save_json(STATE_DIR / f"intraday_{day}.json", hist)
    save_json(STATE_DIR / f"sent_{day}.json", sent)

    def section(title, items, n):
        if not items:
            return []
        items = sorted(items, key=lambda x: -x[0])
        lines = ["", title] + [l for _, l in items[:n]]
        if len(items) > n:
            lines.append(f"… 외 {len(items)-n}건")
        return lines

    if silent:
        print(f"{ts} 개장 직후 침묵 창 — 이력만 적재")
        return

    total = len(ups) + len(downs) + len(others)
    if total:
        send([f"📡 스파이크 {ts}"]
             + section("🔴 급등 신호 (5분/진입)", ups, 8)
             + section("🔵 급락 신호 (5분/진입)", downs, 8)
             + section("📢 거래량·52주 (⭐관심종목)", others, 6))
        for code, name, direction, note in sigs:
            log_signal(code, name, direction, note)
        print(f"{ts} 알림 {total}건")


def main():
    load_env()
    ap = argparse.ArgumentParser()
    ap.add_argument("--prep", action="store_true")
    ap.add_argument("--tick", action="store_true")
    args = ap.parse_args()
    if args.prep:
        prep()
    elif args.tick:
        tick()
    else:
        ap.print_help()


if __name__ == "__main__":
    main()
