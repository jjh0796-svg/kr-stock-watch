# -*- coding: utf-8 -*-
"""
장중 스파이크 감시 (spike_watch)
- 유니버스: 워치리스트(watchlist.csv+holdings.csv) + 코스피·코스닥 등락률 상위/하위
  각 20종목 + 거래대금 상위 각 20종목
- 트리거:
  T1 급변: 최근 5분 ±3% 이상 (전 유니버스) / 등락 상위 리스트 최초 진입(±7%↑)
  T2 거래량: 당일 누적 거래량이 20일 평균의 300% 돌파 (워치리스트만)
  T2b 거래량 폭발: 거래대금 상위 종목이 당일 누적 20일 평균 3배 돌파 (시장 전체 입구)
  T3 52주 신고가/신저가 터치 (워치리스트만)
- 노이즈 억제: 종목·트리거당 1일 1회, T1 재알림은 직전 알림가 대비 추가 ±3%시.
  장 시작 직후(09:00~09:05) 제외, 폴링 매분 09:05~15:30.
- 실전성 개편(2026-09-18): ①시장 스캔분은 시총 2,000억↑·거래대금(경과시간 비례 100억) 통과만
  (⭐💼 면제) ②내 종목은 즉시, 시장분은 5분 묶음 ③T1 재알림 하루 상한(시장 2·내 종목 4),
  급변+상위진입 한 줄 통합 ④당일 종목뉴스 '왜' 한 줄 ⑤같은 업종 동반 신호는 🧩 테마 줄.

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
AMOUNT_N = 20         # 거래대금 상위 스캔 종목 수 (시장별) — 2026-09-01 추가

# 실전성 개편 (2026-09-18): 시장 스캔분은 매매 가능한 종목만, 5분 묶음으로
MIN_MKTCAP = 2e11         # 시장 스캔분 시총 하한 2,000억 (⭐관심·💼보유는 면제)
MIN_VALUE_FULLDAY = 1e10  # 거래대금 하한 — 종일 기준 100억, 장중엔 경과시간 비례(최소 20%)
T1_MAX_MARKET = 2         # 시장 종목의 5분 급변 알림 하루 최대 횟수
T1_MAX_MINE = 4           # 내 종목은 좀 더 허용
DIGEST_MIN = 5            # 시장 신호 묶음 발송 주기(분) — 내 종목 신호는 즉시


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


def fetch_amount_rank():
    """네이버 PC 거래대금 상위(코스피 sosok=0·코스닥 1) → 종목코드 집합.
    모바일 API에는 거래대금 정렬이 없어 PC 페이지에서 코드만 뽑는다(이름·시세는 quotes로)."""
    import re as _re
    codes = set()
    for sosok in ("0", "1"):
        try:
            r = requests.get("https://finance.naver.com/sise/sise_quant_high.naver",
                             params={"sosok": sosok}, headers=UA, timeout=10)
            found = _re.findall(r'/item/main\.naver\?code=(\d{6})', r.text)
            codes.update(found[:AMOUNT_N])
        except Exception as e:
            print(f"[warn] amount rank sosok={sosok}: {e}")
    return codes


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
                try:
                    value = float(d.get("accumulatedTradingValueRaw") or 0)   # 당일 누적 거래대금(원)
                    mcap = float(d.get("marketValueFullRaw") or 0)            # 시가총액(원)
                except (TypeError, ValueError):
                    value, mcap = 0.0, 0.0
                if code and price > 0:
                    out[code] = {"name": d.get("stockName", code),
                                 "price": price, "rate": rate, "volume": vol,
                                 "value": value, "mcap": mcap}
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


# T2b 기준선 — KRX 전종목 API는 서버(해외 IP)에서 차단이라 네이버 일봉으로 종목별 계산
def naver_avg20(code):
    """네이버 일봉 20일 평균 거래량 (당일 미완결 봉 제외, 실패 시 0)."""
    try:
        end = datetime.date.today()
        start = end - datetime.timedelta(days=45)
        r = requests.get("https://api.finance.naver.com/siseJson.naver",
                         params={"symbol": code, "requestType": 1,
                                 "startTime": start.strftime("%Y%m%d"),
                                 "endTime": end.strftime("%Y%m%d"),
                                 "timeframe": "day"},
                         headers=UA, timeout=10)
        rows = json.loads(r.text.replace("'", '"').replace("\n", ""))
        today = end.strftime("%Y%m%d")
        vols = [row[5] for row in rows[1:]
                if str(row[0]) != today and isinstance(row[5], (int, float))]
        vols = vols[-20:]
        return sum(vols) / len(vols) if vols else 0
    except Exception:
        return 0


# 거래대금 상위엔 ETF·ETN·선물형이 섞인다 — 개별 종목 신호가 아니므로 제외
ETF_KEYWORDS = ("KODEX", "TIGER", "RISE", "ACE ", "SOL ", "PLUS ", "HANARO",
                "KIWOOM ", "KoAct", "ARIRANG", "ETN", "레버리지", "인버스", "선물")


# ------------------------------------------------------------------ 실전성 필터·맥락 (2026-09-18)

def liquid_ok(q, now):
    """시장 스캔 종목의 유동성 관문 — 시총 2,000억↑ + 거래대금(경과시간 비례) 통과만.
    등락률 랭킹은 동전주·소형 테마주가 점령하므로 매매 가능한 종목만 남긴다."""
    if q.get("mcap", 0) < MIN_MKTCAP:
        return False
    elapsed = (now.hour * 60 + now.minute) - 9 * 60
    frac = max(0.2, min(1.0, elapsed / 390.0))   # 09:00~15:30 = 390분
    return q.get("value", 0) >= MIN_VALUE_FULLDAY * frac


def why_line(code, name=""):
    """신호 종목의 '왜' — 오늘자 종목뉴스 중 종목명이 들어간 기사를 우선, 없으면
    시황성 기사(코스피·증시·마감 등 종목 무관)를 뺀 첫 기사. 마땅치 않으면 ''."""
    import html as _html
    import re as _re
    try:
        r = requests.get(f"https://m.stock.naver.com/api/news/stock/{code}",
                         params={"pageSize": 5, "page": 1}, headers=UA, timeout=6)
        titles = []
        for group in r.json() or []:
            for item in group.get("items", []):
                if str(item.get("datetime", ""))[:8] == today_str():
                    titles.append(_html.unescape(str(item.get("title", ""))).strip())
        if not titles:
            return ""
        macro = _re.compile(r"코스피|코스닥|증시|마감|시황|인사이트|특징주 모음|오늘의")
        pick = next((t for t in titles if name and name in t), None)
        if pick is None:
            pick = next((t for t in titles if not macro.search(t)), None)
        if not pick:
            return ""
        if len(pick) > 46:
            pick = pick[:46] + "…"
        return f"   └ 📰 {pick}"
    except Exception:
        return ""


def industry_of(code, cache):
    """종목의 네이버 업종 번호 (integration API, 파일 캐시 — 업종은 거의 안 바뀐다)."""
    if code in cache:
        return cache[code]
    no = None
    try:
        r = requests.get(f"https://m.stock.naver.com/api/stock/{code}/integration",
                         headers=UA, timeout=6)
        no = r.json().get("industryCode")
    except Exception:
        pass
    cache[code] = no
    return no


def industry_names():
    """업종 번호→이름 (하루 1회 캐시)."""
    path = STATE_DIR / f"industry_names_{today_str()}.json"
    names = load_json(path, {})
    if names:
        return names
    try:
        for page in (1, 2):   # 업종은 80여 개 — 페이지 크기 상한(100) 안에서 두 번이면 충분
            r = requests.get("https://m.stock.naver.com/api/stocks/industry",
                             params={"page": page, "pageSize": 100}, headers=UA, timeout=10)
            groups = r.json().get("groups", [])
            for g in groups:
                names[str(g.get("no"))] = g.get("name", "")
            if len(groups) < 100:
                break
        if names:
            save_json(path, names)
    except Exception:
        pass
    return names


def theme_lines(signals):
    """같은 묶음 안에서 같은 업종·같은 방향 신호가 2건 이상이면 테마 동반 줄을 만든다."""
    cache_path = STATE_DIR / "industry_cache.json"
    cache = load_json(cache_path, {})
    before = len(cache)
    groups = {}
    for s in signals:
        if s["dir"] not in ("up", "down"):
            continue
        no = industry_of(s["code"], cache)
        if no:
            groups.setdefault((str(no), s["dir"]), []).append(s["name"])
    if len(cache) != before:
        save_json(cache_path, cache)
    names = industry_names() if groups else {}
    out = []
    for (no, direction), members in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        uniq = list(dict.fromkeys(members))
        if len(uniq) < 2:
            continue
        label = names.get(no) or f"업종{no}"
        word = "동반 급등" if direction == "up" else "동반 급락"
        out.append(f"🧩 {label} {len(uniq)}종 {word}: " + "·".join(uniq[:5]))
    return out


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
    # 실패가 조용히 사라지면 "보냈는데 안 온" 사고를 진단할 수 없다 — 결과를 로그에 남긴다
    try:
        r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                          json={"chat_id": chat, "text": text,
                              "disable_web_page_preview": True}, timeout=20)
        if r.status_code != 200:
            print(f"send 실패 HTTP {r.status_code}: {r.text[:150]}")
    except Exception as e:
        print(f"send 예외: {e}")


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
    # 거래대금 상위 — "주가는 조용한데 거래량 폭발" 입구 (2026-09-01)
    amount_set = fetch_amount_rank()
    mvol = load_json(STATE_DIR / "marketvol_cache.json", {})
    dirty = False
    for c in amount_set:
        if mvol.get(c, {}).get("asof") != day:
            mvol[c] = {"avg": naver_avg20(c), "asof": day}
            dirty = True
    if dirty:
        save_json(STATE_DIR / "marketvol_cache.json", mvol)
    universe = set(wl) | set(ranked) | amount_set
    quotes = fetch_quotes(universe)

    # 신호: {code, name, sec(up|down|other 섹션), dir(성과 채점용), key(정렬), line, mine, note}
    signals = []
    # 09:00~09:04 침묵 창 — 트리거 평가 없이 이력만 적재해야 sent 오염(미발송 신호가
    # 중복방지에 기록돼 09:05 이후 영영 침묵)이 없다
    silent = not alerts_allowed(now)

    def mark(code, kind, extra=None):
        sent.setdefault(code, {})[kind] = extra if extra is not None else True

    for code, q in quotes.items():
        name, price, rate = q["name"], q["price"], q["rate"]
        mine = code in wl
        tag = "💼" if code in hd else ("⭐" if mine else "·")

        # 이력 적재 (최근 10분)
        h = hist.setdefault(code, [])
        h.append([ts, price])
        del h[:-11]

        if silent:
            continue

        # 시장 스캔분은 유동성 관문(시총·거래대금) 통과 + 개별 종목만 — 내 종목은 면제
        if not mine and (not liquid_ok(q, now) or any(k in name for k in ETF_KEYWORDS)):
            continue

        s = sent.get(code, {})

        # T1(5분 급변) + T1b(등락 상위 최초 진입) — 같은 틱에 겹치면 한 줄로 통합
        t1_hit, chg5 = False, 0.0
        if len(h) >= 6 and h[-6][1] > 0:
            chg5 = (price / h[-6][1] - 1) * 100
            last_alert_price = s.get("t1")
            count = int(s.get("t1n", 0) or 0)
            cap = T1_MAX_MINE if mine else T1_MAX_MARKET   # 하루 재알림 상한
            need = (last_alert_price is None or
                    abs(price / last_alert_price - 1) * 100 >= CHG_5MIN)
            t1_hit = abs(chg5) >= CHG_5MIN and need and count < cap
        entry_hit = code in ranked and abs(rate) >= CHG_ENTRY and not s.get("entry")
        if t1_hit or entry_hit:
            parts, flip = [], ""
            if t1_hit:
                # 섹션은 "5분 변동" 기준 — 당일과 방향이 다르면 반전 표시
                if chg5 > 0 > rate:
                    flip = " ↗반등중"
                elif chg5 < 0 < rate:
                    flip = " ↘반락중"
                parts.append(f"5분 {chg5:+.1f}%")
            parts.append(f"당일 {rate:+.1f}%{flip}")
            if entry_hit:
                parts.append("상위진입")
            direction = ("up" if chg5 > 0 else "down") if t1_hit else ("up" if rate > 0 else "down")
            signals.append({
                "code": code, "name": name, "sec": direction, "dir": direction,
                "key": abs(chg5) if t1_hit else abs(rate), "mine": mine,
                "line": f"{tag} {name} " + " · ".join(parts) + f" · {price:,.0f}원",
                "note": " / ".join(parts),
            })
            if t1_hit:
                mark(code, "t1", price)
                mark(code, "t1n", int(s.get("t1n", 0) or 0) + 1)
            if entry_hit:
                mark(code, "entry")

        # T2b: 거래대금 상위 종목의 거래량 폭발 — 당일 누적이 20일 평균의 3배 이상
        mv = mvol.get(code, {}).get("avg", 0)
        if (code in amount_set and mv > 0 and q["volume"] >= VOL_MULT * mv
                and not any(k in name for k in ETF_KEYWORDS)
                and not s.get("t2m")):
            mult = q["volume"] / mv
            dot = "🔴" if rate > 0 else ("🔵" if rate < 0 else "⚪")
            signals.append({
                "code": code, "name": name, "sec": "other",
                "dir": "up" if rate >= 0 else "down", "key": mult, "mine": mine,
                "line": f"{tag} {name} 거래대금상위 · 거래량 x{mult:.1f} {dot}{rate:+.1f}% · {price:,.0f}원",
                "note": f"거래량 x{mult:.1f}",
            })
            mark(code, "t2m")

        # 워치리스트 전용 트리거
        p = prep_cache.get(code)
        if p and mine:
            # T2: 거래량 폭증 (당일 등락을 색으로 병기)
            if (p["avg20_vol"] > 0 and q["volume"] >= VOL_MULT * p["avg20_vol"]
                    and not s.get("t2")):
                mult = q["volume"] / p["avg20_vol"]
                dot = "🔴" if rate > 0 else ("🔵" if rate < 0 else "⚪")
                signals.append({
                    "code": code, "name": name, "sec": "other",
                    "dir": "up" if rate >= 0 else "down", "key": mult, "mine": True,
                    "line": f"⭐ {name} 거래량 x{mult:.1f} {dot}{rate:+.1f}%",
                    "note": f"거래량 x{mult:.1f}",
                })
                mark(code, "t2")
            # T3: 52주 신고/신저
            if price >= p["high52"] and not s.get("t3h"):
                signals.append({"code": code, "name": name, "sec": "other", "dir": "up",
                                "key": 999, "mine": True,
                                "line": f"🚀 {name} 52주 신고가 {price:,.0f}원", "note": "52주 신고가"})
                mark(code, "t3h")
            if price <= p["low52"] and not s.get("t3l"):
                signals.append({"code": code, "name": name, "sec": "other", "dir": "down",
                                "key": 999, "mine": True,
                                "line": f"🧊 {name} 52주 신저가 {price:,.0f}원", "note": "52주 신저가"})
                mark(code, "t3l")

    save_json(STATE_DIR / f"intraday_{day}.json", hist)
    save_json(STATE_DIR / f"sent_{day}.json", sent)

    if silent:
        print(f"{ts} 개장 직후 침묵 창 — 이력만 적재")
        return

    def compose(title, sigs, with_theme):
        lines = [title]
        if with_theme:
            themes = theme_lines(sigs)
            if themes:
                lines += [""] + themes

        def section(header, items, n):
            if not items:
                return []
            items = sorted(items, key=lambda x: -x["key"])
            out = ["", header]
            for it in items[:n]:
                out.append(it["line"])
                why = why_line(it["code"], it["name"])   # 당일 종목뉴스가 있으면 '왜' 한 줄
                if why:
                    out.append(why)
            if len(items) > n:
                out.append(f"… 외 {len(items) - n}건")
            return out

        lines += section("🔴 급등 신호 (5분/진입)", [x for x in sigs if x["sec"] == "up"], 8)
        lines += section("🔵 급락 신호 (5분/진입)", [x for x in sigs if x["sec"] == "down"], 8)
        lines += section("📢 거래량·52주", [x for x in sigs if x["sec"] == "other"], 6)
        return lines

    for sig in signals:   # 성과 채점 로그는 감지 시점에 기록
        log_signal(sig["code"], sig["name"], sig["dir"], sig["note"])

    # 내 종목(⭐관심·💼보유)은 즉시 단독 발송
    mine_sigs = [x for x in signals if x["mine"]]
    if mine_sigs:
        send(compose(f"📡 내 종목 스파이크 {ts}", mine_sigs, with_theme=False))

    # 시장 스캔분은 큐에 모았다가 DIGEST_MIN분 주기로 묶음 발송 (같은 종목은 최신 줄로 통합)
    pending_path = STATE_DIR / f"pending_{day}.json"
    pending = load_json(pending_path, [])
    for sig in (x for x in signals if not x["mine"]):
        sig["ts"] = ts
        pending = [p for p in pending if p["code"] != sig["code"]] + [sig]
    closing = now.time() >= datetime.time(15, 29)
    if pending and (now.minute % DIGEST_MIN == 0 or closing):
        first = min(p.get("ts", ts) for p in pending)
        span = ts if first == ts else f"{first}~{ts}"
        send(compose(f"📡 시장 스파이크 {span} · 시총 2천억↑·거래대금 필터", pending, with_theme=True))
        print(f"{ts} 시장 묶음 발송 {len(pending)}건")
        pending = []
    save_json(pending_path, pending)
    if signals:
        print(f"{ts} 신호 {len(signals)}건 (내 종목 {len(mine_sigs)})")


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
