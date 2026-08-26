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
    wl = read_watchlist()
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
    if now.weekday() >= 5:
        return False
    t = now.time()
    return datetime.time(9, 5) <= t <= datetime.time(15, 30)


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

    wl = read_watchlist()
    ranked = {}   # code -> rate (등락 상위 리스트 출신)
    for market in ("KOSPI", "KOSDAQ"):
        for sort in ("up", "down"):
            for s in fetch_rank(sort, market):
                code = s.get("itemCode", "")
                if code:
                    ranked[code] = s
    universe = set(wl) | set(ranked)
    quotes = fetch_quotes(universe)

    alerts = []

    def mark(code, kind, extra=None):
        sent.setdefault(code, {})[kind] = extra if extra is not None else True

    for code, q in quotes.items():
        name, price, rate = q["name"], q["price"], q["rate"]
        tag = "⭐" if code in wl else "·"

        # 이력 적재 (최근 10분)
        h = hist.setdefault(code, [])
        h.append([ts, price])
        del h[:-11]

        s = sent.get(code, {})

        # T1: 5분 급변
        if len(h) >= 6:
            base = h[-6][1]
            if base > 0:
                chg5 = (price / base - 1) * 100
                last_alert_price = s.get("t1")
                need = (last_alert_price is None or
                        abs(price / last_alert_price - 1) * 100 >= CHG_5MIN)
                if abs(chg5) >= CHG_5MIN and need:
                    alerts.append(f"{tag}⚡ {name}({code}) 5분 {chg5:+.1f}% → "
                                  f"{price:,.0f}원 (당일 {rate:+.1f}%)")
                    mark(code, "t1", price)

        # T1b: 등락 상위 최초 진입 (±7% 이상)
        if code in ranked and abs(rate) >= CHG_ENTRY and not s.get("entry"):
            arrow = "🔺" if rate > 0 else "🔻"
            alerts.append(f"{tag}{arrow} {name}({code}) 등락 상위 진입 "
                          f"{rate:+.1f}% → {price:,.0f}원")
            mark(code, "entry")

        # 워치리스트 전용 트리거
        p = prep_cache.get(code)
        if p and code in wl:
            # T2: 거래량 폭증
            if (p["avg20_vol"] > 0 and q["volume"] >= VOL_MULT * p["avg20_vol"]
                    and not s.get("t2")):
                mult = q["volume"] / p["avg20_vol"]
                alerts.append(f"⭐📢 {name}({code}) 거래량 20일평균 x{mult:.1f} "
                              f"({rate:+.1f}%)")
                mark(code, "t2")
            # T3: 52주 신고/신저
            if price >= p["high52"] and not s.get("t3h"):
                alerts.append(f"⭐🚀 {name}({code}) 52주 신고가 {price:,.0f}원")
                mark(code, "t3h")
            if price <= p["low52"] and not s.get("t3l"):
                alerts.append(f"⭐🧊 {name}({code}) 52주 신저가 {price:,.0f}원")
                mark(code, "t3l")

    save_json(STATE_DIR / f"intraday_{day}.json", hist)
    save_json(STATE_DIR / f"sent_{day}.json", sent)

    if alerts:
        send([f"📡 스파이크 감시 {ts}"] + alerts[:15]
             + ([f"… 외 {len(alerts)-15}건"] if len(alerts) > 15 else []))
        print(f"{ts} 알림 {len(alerts)}건")


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
