"""공시 유형별 과거 주가 반응 통계 — 공시 알림 밑에 붙일 '📊 과거 비슷한 공시 …' 한 줄.

무엇을 하나
  1) OpenDART list.json(주요사항보고 B + 거래소공시 I, 코스피·코스닥)으로 최근 약 24개월 공시를 모은다.
     제목(report_nm)으로 유형을 나누고, [기재정정]·[첨부정정] 등 머리표가 붙은 정정 공시는 통계에서 뺀다.
  2) 접수 시각: list.json엔 날짜만 있어서 DART 최근공시 목록(dart.fss.or.kr/dsac001/search.ax)에서
     15:30 이후 접수분만 날짜별로 긁는다(목록이 시각 역순이라 1~3쪽이면 끝). 그 목록에 없으면 장 마감 전 접수.
       - 15:30 전 접수(장전·장중) → 그날이 '첫 반응일'
       - 15:30 후·휴일 접수      → 다음 거래일이 '첫 반응일'
     기준가는 첫 반응일 직전 거래일 종가(= 공시가 시장에 알려지기 전 마지막 종가).
  3) 속성(같은 유형 안에서 반응이 갈리는 것):
       공급계약 = 매출액 대비 계약금액 %(원문 표본 파싱), 유상증자 = 배정방식(piicDecsn),
       전환사채 = 주식총수 대비 전환주식 %(cvbdIsDecsn), 잠정실적 = 영업이익 전년동기 대비(원문 표본 파싱).
  4) 네이버 일봉으로 T+1(첫 반응일)/T+5/T+20 수익률, 같은 기간 지수(코스피/코스닥) 대비 초과,
     첫날 종가 이후 추가 흐름(drift)을 계산해 구간(bucket)별로 집계 → data/event_reaction_stats.json

    python event_reaction_stats.py --build            # 전체(캐시 재사용, 중단 후 재실행 가능)
    python event_reaction_stats.py --stats-only       # 캐시만으로 통계 재계산
    python event_reaction_stats.py --show             # 구간 표 출력

다른 코드에서:
    from event_reaction_stats import reaction_line, attrs_from_card
    reaction_line("단일판매ㆍ공급계약체결", {"market": "K", "ratio": 45.2})
    → '📊 과거 비슷한 공시(공급계약 · 매출 대비 30~100% · 코스닥) 312건: …'  (표본 30건 미만이면 None)
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import math
import os
import random
import re
import statistics
import sys
import time
import zipfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

KST = timezone(timedelta(hours=9))
HERE = Path(__file__).resolve().parent
CACHE = Path(os.environ.get("EVENT_REACTION_CACHE", str(Path.home() / "codex" / ".research_cache" / "event_reaction")))
STATS_PATH = Path(os.environ.get("EVENT_REACTION_STATS", str(HERE / "data" / "event_reaction_stats.json")))

MIN_COUNT = 30                 # 구간 표본이 이보다 적으면 한 단계 넓은 구간으로, 끝까지 모자라면 표시 안 함
DART_BUDGET = int(os.environ.get("EVENT_REACTION_DART_BUDGET", "3900"))  # 이번 빌드 전체 DART 호출 상한
DART_SLEEP = 0.15
NAVER_SLEEP = 0.2
WEB_SLEEP = 0.5
CUTOFF = "15:30"               # 이 시각 이후 접수는 다음 거래일이 첫 반응일
LIMIT_MOVE = 0.305             # 하루 ±30% 가격제한폭 — 넘으면 무상증자·감자 등 수정주가 왜곡으로 보고 제외
CLUSTER_DAYS = 5               # 같은 종목·같은 유형이 5거래일 안에 또 나오면 겹치는 창이라 뒤엣것 제외
HORIZONS = (1, 5, 20)
DRIFTS = (5, 20)

MARKET_LABEL = {"Y": "코스피", "K": "코스닥", "ALL": "코스피·코스닥"}
INDEX_SYMBOL = {"Y": "KOSPI", "K": "KOSDAQ"}

# ─── 유형 분류 ────────────────────────────────────────────────────────────────
# (키, 짧은 이름, 제목 정규식) — 공백 제거한 제목에 순서대로 매칭(앞의 것이 우선).
EVENT_TYPES: list[tuple[str, str, str]] = [
    ("rights_bonus", "유무상증자", r"유무상증자결정"),
    ("rights", "유상증자", r"유상증자결정"),
    ("bonus", "무상증자", r"무상증자결정"),
    ("cb", "전환사채 발행", r"전환사채권발행결정"),
    ("bw", "신주인수권부사채 발행", r"신주인수권부사채권발행결정"),
    ("eb", "교환사채 발행", r"교환사채권발행결정"),
    ("refix", "전환가액 조정", r"전환가액의조정"),
    ("buyback_trust_end", "자사주 신탁 해지", r"자기주식취득신탁계약해지결정"),
    ("buyback_trust", "자사주 신탁계약", r"자기주식취득신탁계약체결결정"),
    ("buyback", "자사주 취득", r"자기주식취득결정"),
    ("treasury_sell", "자사주 처분", r"자기주식처분결정"),
    ("cancel", "자사주 소각", r"자기주식소각결정|주식소각결정"),
    ("dividend", "현금배당", r"현금ㆍ현물배당결정|현금·현물배당결정|현금배당결정"),
    ("stock_dividend", "주식배당", r"주식배당결정"),
    ("reduction", "감자", r"감자결정"),
    ("split", "액면분할", r"주식분할결정"),
    ("merger", "합병", r"회사합병결정"),
    ("spinoff", "회사분할", r"회사분할(합병)?결정"),
    ("equity_buy", "타법인 주식 취득", r"타법인주식및출자증권취득결정"),
    ("control_deal", "최대주주 변경 수반 주식양수도", r"최대주주변경을수반하는주식양수도계약"),
    ("control", "최대주주 변경", r"최대주주변경"),
    ("supply_cancel", "공급계약 해지", r"공급계약해지"),
    ("supply", "공급계약", r"단일판매|공급계약체결"),
    ("earnings", "잠정실적", r"영업\(잠정\)실적"),
]
TYPE_LABEL = {k: label for k, label, _ in EVENT_TYPES}
_TYPE_RE = [(k, re.compile(p)) for k, _, p in EVENT_TYPES]
_AMEND_RE = re.compile(r"^\[[^\]]+\]")          # [기재정정] [첨부정정] [첨부추가] [발행조건확정] …
_SUBSIDIARY_RE = re.compile(r"(자|종속)회사의주요경영사항")
_NON_EVENT_RE = re.compile(r"기타경영사항|철회|취소|정정신고|풍문|조회공시")


def classify_title(title: str) -> dict | None:
    """제목 → {'type','amended','subsidiary'} 또는 None(대상 아님)."""
    compact = re.sub(r"\s+", "", title or "")
    amended = bool(_AMEND_RE.match(compact))
    body = _AMEND_RE.sub("", compact)
    if _NON_EVENT_RE.search(body):
        return None
    for key, pat in _TYPE_RE:
        if pat.search(body):
            return {"type": key, "amended": amended, "subsidiary": bool(_SUBSIDIARY_RE.search(body))}
    return None


# ─── 속성 → 구간 ──────────────────────────────────────────────────────────────

def _f(v) -> float | None:
    s = str(v if v is not None else "").replace(",", "").replace("△", "-").replace("%", "").strip()
    if not s or s in ("-", "."):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def ratio_levels(ratio: float | None) -> list[str]:
    """공급계약 매출액 대비 % → 세밀→거친 구간 이름."""
    if ratio is None or ratio < 0:
        return []
    if ratio < 10:
        return ["매출 대비 10% 미만"]
    if ratio < 30:
        return ["매출 대비 10~30%"]
    if ratio < 100:
        return ["매출 대비 30~100%", "매출 대비 30% 이상"]
    return ["매출 대비 100% 이상", "매출 대비 30% 이상"]


def dilution_levels(pct: float | None) -> list[str]:
    """전환사채 전환주식의 주식총수 대비 %."""
    if pct is None or pct < 0:
        return []
    if pct < 5:
        return ["주식총수 대비 5% 미만"]
    if pct < 10:
        return ["주식총수 대비 5~10%"]
    if pct < 20:
        return ["주식총수 대비 10~20%", "주식총수 대비 10% 이상"]
    return ["주식총수 대비 20% 이상", "주식총수 대비 10% 이상"]


def method_levels(method: str | None) -> list[str]:
    """유상증자 증자방식."""
    m = re.sub(r"\s+", "", method or "")
    if not m:
        return []
    if "제3자" in m:
        return ["제3자배정"]
    if "주주배정" in m:
        return ["주주배정"]
    if "일반공모" in m:
        return ["일반공모"]
    return []


def earnings_levels(op_now: float | None, op_prev: float | None, yoy: float | None = None) -> list[str]:
    """잠정실적 영업이익(당해 vs 전년동기). yoy만 아는 경우(카드 문구)도 받는다."""
    if op_now is not None and op_now <= 0:
        return ["영업이익 적자", "실적 악화"]
    if op_now is not None and op_prev is not None:
        if op_prev <= 0:
            return ["영업이익 흑자전환", "실적 개선"]
        yoy = (op_now / op_prev - 1) * 100
    if yoy is None:
        return []
    if yoy >= 30:
        return ["영업이익 30% 이상 증가", "실적 개선"]
    if yoy <= -30:
        return ["영업이익 30% 이상 감소", "실적 악화"]
    return ["영업이익 ±30% 이내"]


def attr_levels(event_type: str, attrs: dict | None) -> list[str]:
    a = attrs or {}
    if event_type == "supply":
        return ratio_levels(_f(a.get("ratio")))
    if event_type == "cb":
        return dilution_levels(_f(a.get("dilution")))
    if event_type == "rights":
        return method_levels(a.get("method"))
    if event_type == "earnings":
        return earnings_levels(_f(a.get("op_now")), _f(a.get("op_prev")), _f(a.get("op_yoy")))
    return []


def _market(attrs: dict | None) -> str | None:
    m = str((attrs or {}).get("market") or "").strip()
    return {"Y": "Y", "K": "K", "코스피": "Y", "유가": "Y", "유가증권": "Y", "KOSPI": "Y",
            "코스닥": "K", "KOSDAQ": "K"}.get(m)


def bucket_key(event_type: str, attr: str | None, market: str) -> str:
    return f"{event_type}|{attr or '*'}|{market}"


def bucket_chain(event_type: str, attrs: dict | None) -> list[str]:
    """세밀→거친 순 구간 키. 통계와 표시가 같은 규칙을 쓴다."""
    mkt = _market(attrs)
    chain = []
    for lvl in attr_levels(event_type, attrs):
        if mkt:
            chain.append(bucket_key(event_type, lvl, mkt))
        chain.append(bucket_key(event_type, lvl, "ALL"))
    if mkt:
        chain.append(bucket_key(event_type, None, mkt))
    chain.append(bucket_key(event_type, None, "ALL"))
    return chain


def bucket_label(key: str) -> str:
    t, attr, mkt = key.split("|")
    parts = [TYPE_LABEL.get(t, t)]
    if attr != "*":
        parts.append(attr)
    parts.append(MARKET_LABEL.get(mkt, mkt))
    return " · ".join(parts)


# ─── 수익률 계산 ──────────────────────────────────────────────────────────────

def first_reaction_pos(calendar: list[str], rcept_dt: str, after_close: bool) -> int | None:
    """첫 반응일의 달력 위치. 15:30 전 접수면 접수일(거래일일 때), 아니면 다음 거래일."""
    for i, d in enumerate(calendar):
        if d > rcept_dt or (d == rcept_dt and not after_close):
            return i
    return None


def event_returns(calendar: list[str], stock: dict[str, tuple[float, float]], index: dict[str, float],
                  rcept_dt: str, after_close: bool) -> dict | None:
    """stock: {날짜: (종가, 거래량)}, index: {날짜: 종가}.
    반환: {'t0': 날짜, 'r1','r5','r20','x1','x5','x20','d5','d20','xd5','xd20','same_day'} (없는 값은 키 생략).
    기준가(직전 종가)나 첫 반응일에 시세가 없거나 거래량 0(거래정지)이면 None."""
    pos = first_reaction_pos(calendar, rcept_dt, after_close)
    if pos is None or pos == 0:
        return None
    pre = calendar[pos - 1]
    if pre not in stock or calendar[pos] not in stock or pre not in index:
        return None
    if stock[pre][1] <= 0 or stock[calendar[pos]][1] <= 0:
        return None

    def ok_path(a: int, b: int) -> bool:
        """a~b 구간 모두 시세·거래량 있고 하루 변동이 가격제한폭 이내."""
        for j in range(a, b + 1):
            d = calendar[j]
            if d not in stock or stock[d][1] <= 0 or d not in index:
                return False
            if j > a:
                prev = stock[calendar[j - 1]][0]
                if prev <= 0 or abs(stock[d][0] / prev - 1) > LIMIT_MOVE:
                    return False
        return True

    out: dict = {"t0": calendar[pos]}
    base, ibase = stock[pre][0], index[pre]
    for h in HORIZONS:
        end = pos + h - 1
        if end >= len(calendar) or not ok_path(pos - 1, end):
            break
        r = stock[calendar[end]][0] / base - 1
        x = r - (index[calendar[end]] / ibase - 1)
        out[f"r{h}"], out[f"x{h}"] = r, x
    if "r1" not in out:
        return None
    t0 = calendar[pos]
    for h in DRIFTS:
        end = pos + h
        if end >= len(calendar) or not ok_path(pos, end):
            break
        r = stock[calendar[end]][0] / stock[t0][0] - 1
        out[f"d{h}"] = r
        out[f"xd{h}"] = r - (index[calendar[end]] / index[t0] - 1)
    # 장 마감 후 공시: 접수 당일(공시 전) 움직임을 따로 기록 — 정보 선반영 여부 확인용
    if after_close and rcept_dt in stock and pos >= 2 and calendar[pos - 1] == rcept_dt:
        p2 = calendar[pos - 2]
        if p2 in stock and stock[p2][0] > 0 and ok_path(pos - 2, pos - 1):
            out["same_day"] = stock[rcept_dt][0] / stock[p2][0] - 1
    return out


def winsorize(vals: list[float], p: float = 0.01) -> list[float]:
    if len(vals) < 50:
        return list(vals)
    s = sorted(vals)
    lo, hi = s[int(len(s) * p)], s[int(len(s) * (1 - p)) - 1]
    return [min(max(v, lo), hi) for v in vals]


def summarize_values(raw: list[float], excess: list[float] | None = None) -> dict:
    """구간 한 칸의 요약: 평균(상하위 1% 눌러 담은 값)·중앙값·오른 비율·지수 대비 초과·t값."""
    n = len(raw)
    if n == 0:
        return {"n": 0}
    w = winsorize(raw)
    out = {"n": n, "mean": statistics.fmean(w), "median": statistics.median(raw),
           "pos": sum(1 for v in raw if v > 0) / n}
    if excess:
        xw = winsorize(excess)
        out["ex_mean"] = statistics.fmean(xw)
        out["ex_median"] = statistics.median(excess)
        out["ex_pos"] = sum(1 for v in excess if v > 0) / len(excess)
        sd = statistics.stdev(xw) if len(xw) > 1 else 0.0
        out["t"] = out["ex_mean"] / (sd / math.sqrt(len(xw))) if sd > 0 else 0.0
    return out


def aggregate(events: list[dict]) -> dict[str, dict]:
    """이벤트(수익률·구간 체인 포함) → 구간별 통계."""
    groups: dict[str, list[dict]] = {}
    for ev in events:
        for key in ev["chain"]:
            groups.setdefault(key, []).append(ev)
    out = {}
    for key, evs in sorted(groups.items()):
        rec = {"label": bucket_label(key), "n": len(evs)}
        for h in HORIZONS:
            raw = [e[f"r{h}"] for e in evs if f"r{h}" in e]
            exc = [e[f"x{h}"] for e in evs if f"x{h}" in e]
            rec[f"h{h}"] = summarize_values(raw, exc)
        for h in DRIFTS:
            raw = [e[f"d{h}"] for e in evs if f"d{h}" in e]
            exc = [e[f"xd{h}"] for e in evs if f"xd{h}" in e]
            rec[f"drift{h}"] = summarize_values(raw, exc)
        sd = [e["same_day"] for e in evs if "same_day" in e]
        rec["same_day_after_close"] = summarize_values(sd)
        rec["after_close_share"] = sum(1 for e in evs if e.get("after_close")) / len(evs)
        out[key] = rec
    return out


# ─── 표시 한 줄 ───────────────────────────────────────────────────────────────

_STATS_CACHE: dict | None = None


def load_stats(path: Path | str | None = None) -> dict:
    global _STATS_CACHE
    if path is not None:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    if _STATS_CACHE is None:
        _STATS_CACHE = json.loads(STATS_PATH.read_text(encoding="utf-8"))
    return _STATS_CACHE


def _pct(v: float) -> str:
    s = f"{v * 100:+.1f}"
    return "0.0" if s in ("+0.0", "-0.0") else s


def format_line(rec: dict) -> str:
    h1, h5 = rec["h1"], rec.get("h5", {})
    line = (f"📊 과거 비슷한 공시({rec['label']}) {h1['n']:,}건: "
            f"첫날 평균 {_pct(h1['mean'])}%·중앙 {_pct(h1['median'])}% (오른 비율 {h1['pos'] * 100:.0f}%)")
    if h5.get("n", 0) >= MIN_COUNT:
        line += f" · 5일 평균 {_pct(h5['mean'])}% (지수 대비 {_pct(h5['ex_mean'])}%p)"
    weak = abs(h1.get("t", 0)) < 2 and abs(h5.get("t", 0) if h5.get("n", 0) >= MIN_COUNT else 0) < 2
    if weak:
        line += " · 뚜렷한 경향 없음"
    return line


def reaction_line(event_type: str, attrs: dict | None = None, stats: dict | None = None) -> str | None:
    """event_type: 유형 키('supply') 또는 공시 제목. attrs: {'market': 'K'|'코스닥', 'ratio': 45.2, …}.
    가장 세밀한 구간부터 보고 표본 30건 이상인 첫 구간으로 한 줄. 없으면 None."""
    key = event_type if event_type in TYPE_LABEL else (classify_title(event_type) or {}).get("type")
    if not key:
        return None
    try:
        st = stats if stats is not None else load_stats()
    except (OSError, ValueError):
        return None
    buckets = st.get("buckets", {})
    for b in bucket_chain(key, attrs):
        rec = buckets.get(b)
        if rec and rec.get("h1", {}).get("n", 0) >= MIN_COUNT:
            return format_line(rec)
    return None


def attrs_from_card(title: str, card_text: str, corp_cls: str = "") -> dict:
    """알림 카드 본문(kr-stock-watch summarize 결과)에서 속성 뽑기 — 연동 시 추가 호출 없이 쓰려고."""
    text = re.sub(r"<[^>]+>", "", card_text or "")
    attrs: dict = {"market": corp_cls}
    if not corp_cls:
        m = re.search(r"\((코스피|코스닥)\)", text)
        if m:
            attrs["market"] = m.group(1)
    kind = (classify_title(title) or {}).get("type")
    if kind == "supply":
        m = re.search(r"금액:[^\n]*?매출대비\s*([\d.,]+)%", text) or re.search(r"매출대비\s*([\d.,]+)%", text)
        if m:
            attrs["ratio"] = _f(m.group(1))
    elif kind == "cb":
        m = re.search(r"주식총수\s*대비:\s*([\d.,]+)\s*%", text)
        if m:
            attrs["dilution"] = _f(m.group(1))
    elif kind == "rights":
        m = re.search(r"방식:\s*([^\n·]+)", text)
        if m:
            attrs["method"] = m.group(1).strip()
    elif kind == "earnings":
        m = re.search(r"영업익:\s*(-?[\d,.]+)\s*(조|억)?", text)
        if m:
            attrs["op_now"] = _f(m.group(1))
        y = re.search(r"영업익:[^\n]*YoY\s*([+\-]?[\d.,]+)%", text)
        if y:
            attrs["op_yoy"] = _f(y.group(1))
        elif re.search(r"영업익:[^\n]*YoY\s*흑자전환", text):
            attrs["op_prev"] = -1.0   # 카드엔 전년 값이 없고 '흑자전환'만 — 부호만 알면 구간이 정해진다
    return attrs


# ─── 수집(네트워크) ───────────────────────────────────────────────────────────

def _dart_key() -> str:
    key = os.environ.get("DART_API_KEY", "")
    if not key:
        raise SystemExit("DART_API_KEY가 없습니다 (set -a; source ~/bots/codex.env; set +a)")
    return key


def _calls_log() -> Path:
    return CACHE / "_dart_calls.log"


def dart_calls_used() -> int:
    p = _calls_log()
    return sum(1 for _ in p.open(encoding="utf-8")) if p.exists() else 0


class BudgetExceeded(RuntimeError):
    pass


def dart_get(endpoint: str, params: dict, binary: bool = False):
    """OpenDART 호출(캐시·예산·0.15초 간격). 키는 로그·캐시에 남기지 않는다."""
    import requests
    tag = hashlib.sha1(json.dumps([endpoint, params], sort_keys=True).encode()).hexdigest()[:20]
    sub = CACHE / "dart" / endpoint.replace(".", "_")
    path = sub / (tag + (".bin" if binary else ".json"))
    if path.exists():
        return path.read_bytes() if binary else json.loads(path.read_text(encoding="utf-8"))
    if dart_calls_used() >= DART_BUDGET:
        raise BudgetExceeded(f"DART 예산 {DART_BUDGET}건 소진")
    sub.mkdir(parents=True, exist_ok=True)
    resp = None
    for attempt in range(3):
        try:
            resp = requests.get(f"https://opendart.fss.or.kr/api/{endpoint}",
                                params={**params, "crtfc_key": _dart_key()}, timeout=30)
            break
        except requests.RequestException:
            time.sleep(2 + attempt * 3)
    with _calls_log().open("a", encoding="utf-8") as fh:
        fh.write(f"{datetime.now(KST):%Y-%m-%d %H:%M:%S}\t{endpoint}\t{json.dumps(params, ensure_ascii=False)}\n")
    time.sleep(DART_SLEEP)
    if resp is None:
        raise RuntimeError(f"DART {endpoint} 연결 실패")
    if binary:
        data = resp.content
        if data[:2] == b"PK":
            path.write_bytes(data)
        return data
    data = resp.json()
    if data.get("status") == "020":
        raise BudgetExceeded("DART 일일 한도 초과(020)")
    if data.get("status") in ("000", "013"):
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return data


def collect_list(bgn: str, end: str) -> list[dict]:
    """주요사항보고(B)·거래소공시(I) × 코스피(Y)·코스닥(K), 3개월 창으로 끊어서."""
    out: dict[str, dict] = {}
    d0 = datetime.strptime(bgn, "%Y%m%d")
    d_end = datetime.strptime(end, "%Y%m%d")
    while d0 <= d_end:
        d1 = min(d0 + timedelta(days=88), d_end)
        for ty in ("B", "I"):
            for cls in ("Y", "K"):
                page = 1
                while True:
                    data = dart_get("list.json", {"bgn_de": f"{d0:%Y%m%d}", "end_de": f"{d1:%Y%m%d}",
                                                  "pblntf_ty": ty, "corp_cls": cls,
                                                  "page_no": str(page), "page_count": "100"})
                    for it in data.get("list", []):
                        out[it["rcept_no"]] = it
                    if page >= int(data.get("total_page") or 1):
                        break
                    page += 1
        print(f"[list] {d0:%Y%m%d}~{d1:%Y%m%d} 누적 {len(out):,}건 · DART {dart_calls_used()}건", flush=True)
        d0 = d1 + timedelta(days=1)
    return list(out.values())


def fetch_after_close_times(day: str) -> dict:
    """그날 15:30 이후 접수분의 {접수번호: 'HH:MM'} (DART 최근공시 목록, 시각 역순)."""
    import requests
    path = CACHE / "times" / f"{day}.json"
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    path.parent.mkdir(parents=True, exist_ok=True)
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
               "Referer": "https://dart.fss.or.kr/dsac001/mainAll.do", "X-Requested-With": "XMLHttpRequest"}
    after: dict[str, str] = {}
    complete = False
    for page in range(1, 30):
        resp = None
        for attempt in range(3):
            try:
                resp = requests.post("https://dart.fss.or.kr/dsac001/search.ax", headers=headers, timeout=(5, 15),
                                     data={"currentPage": page, "maxResults": 100,
                                           "selectDate": f"{day[:4]}.{day[4:6]}.{day[6:]}", "mdayCnt": 0})
                break
            except requests.RequestException:
                time.sleep(3 + attempt * 5)
        time.sleep(WEB_SLEEP)
        if resp is None or resp.status_code != 200:
            break
        rows = re.findall(r"<tr\b.*?</tr>", resp.text, re.S | re.I)
        earliest = None
        for tr in rows:
            t = re.search(r"<td>\s*(\d{2}:\d{2})\s*</td>", tr)
            n = re.search(r"rcpNo=(\d{14})", tr)
            if t and n:
                earliest = t[1] if earliest is None or t[1] < earliest else earliest
                if t[1] >= CUTOFF:
                    after[n[1]] = t[1]
        pg = re.search(r"\[(\d+)/(\d+)\]", resp.text)
        last = (not pg) or int(pg[1]) >= int(pg[2]) or len(rows) < 50
        if (earliest is not None and earliest < CUTOFF) or last:
            complete = earliest is not None or last
            break
    rec = {"after": after, "complete": complete}
    if complete:
        path.write_text(json.dumps(rec, ensure_ascii=False), encoding="utf-8")
    return rec


_ROW_RE = re.compile(r'\["(\d{8})",\s*([\d.]+),\s*([\d.]+),\s*([\d.]+),\s*([\d.]+),\s*([\d.]+)')


def fetch_prices(symbol: str, start: str, end: str) -> dict[str, tuple[float, float]]:
    """네이버 일봉 {날짜: (종가, 거래량)} — 종목당 1회 요청, 캐시."""
    import requests
    path = CACHE / "prices" / f"{symbol}_{start}_{end}.json"
    if path.exists():
        return {d: tuple(v) for d, v in json.loads(path.read_text(encoding="utf-8")).items()}
    path.parent.mkdir(parents=True, exist_ok=True)
    text = ""
    for attempt in range(3):
        try:
            resp = requests.get("https://api.finance.naver.com/siseJson.naver",
                                params={"symbol": symbol, "requestType": 1, "startTime": start, "endTime": end,
                                        "timeframe": "day"},
                                headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}, timeout=20)
            resp.raise_for_status()
            text = resp.text
            break
        except Exception:
            time.sleep(2 + attempt * 4)
    time.sleep(NAVER_SLEEP)
    out = {m.group(1): (float(m.group(5)), float(m.group(6))) for m in _ROW_RE.finditer(text)}
    if text:
        path.write_text(json.dumps(out), encoding="utf-8")
    return out


def match_rows(event_nos: list[str], rows: list[dict]) -> dict[str, dict]:
    """구조화 API(piicDecsn·cvbdIsDecsn) 행 ↔ 원공시 접수번호.
    정정된 결정은 API 행의 접수번호가 '마지막 정정본' 번호로 바뀌어 있어(2026-10 확인) 원번호로는 안 맞는다.
    1) 번호가 같으면 그대로, 2) 남은 원공시는 순서대로 '그보다 뒤에 접수된 남은 행 중 가장 이른 것'과 짝."""
    by_no = {r.get("rcept_no"): r for r in rows if r.get("rcept_no")}
    out = {n: by_no[n] for n in event_nos if n in by_no}
    left = sorted(n for n in by_no if n not in out)
    for n in sorted(x for x in event_nos if x not in out):
        later = [r for r in left if r > n]
        if later:
            out[n] = by_no[later[0]]
            left.remove(later[0])
    return out


def doc_text(rcept_no: str) -> str:
    raw = dart_get("document.xml", {"rcept_no": rcept_no}, binary=True)
    if raw[:2] != b"PK":
        return ""
    zf = zipfile.ZipFile(io.BytesIO(raw))
    parts = []
    for name in zf.namelist():
        b = zf.read(name)
        for enc in ("utf-8", "euc-kr", "cp949"):
            try:
                parts.append(b.decode(enc))
                break
            except UnicodeDecodeError:
                continue
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", " ".join(parts)))


def parse_supply_ratio(text: str) -> float | None:
    """단일판매ㆍ공급계약 원문 → 매출액 대비 %(kr-stock-watch summarize._sum_supply와 같은 규칙)."""
    m = re.search(r"매출액\s*대비\s*\(%\)\s*([\d.,\-]+)", text)
    ratio = _f(m.group(1)) if m else None
    if ratio is not None:
        return ratio
    total = re.search(r"계약금액(?:\s*총액)?\s*\(원\)\s*([\d,\-]+)", text)
    sales = re.search(r"최근\s*매출액\s*\(원\)\s*([\d,\-]+)", text)
    t, s = (_f(total.group(1)) if total else None), (_f(sales.group(1)) if sales else None)
    if t and s and s > 0:
        return t / s * 100
    return None


def parse_earnings(text: str) -> dict | None:
    """잠정실적 원문 '영업이익 당해실적 …' 줄에서 당해·전년동기 영업이익.
    유가 서식: 당해 / 전기 / 전기대비% / 전년동기 / 전년동기대비%
    코스닥 서식: 당해 / 전기 / 증감 (증감률) / 전년동기 / 증감 (증감률)
    7칸 서식: 당해 / 전기 / 증감률 / 흑적전환 / 전년동기 / 증감률 / 흑적전환
    괄호 안(증감률·흑자전환 등)을 지우고 남은 한글 낱말은 빈칸 표시로 바꾸면 둘 다 4번째가 전년동기."""
    m = re.search(r"영업이익(?:\s*\(손실\))?\s*당해실적\s*(.{0,240}?)(?:누계실적|법인세|당기순이익|$)", text)
    if not m:
        return None
    seg = re.sub(r"\([^)]*\)", " ", m.group(1))
    seg = re.sub(r"[가-힣]+", " - ", seg)
    toks = re.findall(r"△?-?[\d,]*\d(?:\.\d+)?|(?<!\S)-(?!\S)", seg)
    if len(toks) < 4:
        return None
    # 7칸 서식(당해/전기/증감률/전환여부/전년동기/증감률/전환여부)은 전년동기가 5번째
    now, prev_y = _f(toks[0]), _f(toks[4] if len(toks) >= 7 else toks[3])
    if now is None:
        return None
    return {"op_now": now, "op_prev": prev_y}


# ─── 빌드 ─────────────────────────────────────────────────────────────────────

def _period(months: int = 24) -> tuple[str, str]:
    today = datetime.now(KST).date()
    end = today - timedelta(days=1)
    start = date(end.year - months // 12, end.month, end.day) + timedelta(days=1)
    return f"{start:%Y%m%d}", f"{end:%Y%m%d}"


def build(args) -> None:
    CACHE.mkdir(parents=True, exist_ok=True)
    bgn, end = args.bgn or _period()[0], args.end or _period()[1]
    price_start = (datetime.strptime(bgn, "%Y%m%d") - timedelta(days=20)).strftime("%Y%m%d")
    price_end = datetime.now(KST).strftime("%Y%m%d")
    print(f"[build] 기간 {bgn}~{end} · 시세 {price_start}~{price_end} · DART 사용 {dart_calls_used()}건", flush=True)

    # 1) 공시 목록
    raw = collect_list(bgn, end)
    events, n_amend, n_sub = [], 0, 0
    for it in raw:
        c = classify_title(it.get("report_nm", ""))
        if not c or not (it.get("stock_code") or "").strip() or it.get("corp_cls") not in ("Y", "K"):
            continue
        if c["amended"]:
            n_amend += 1
            continue
        if c["subsidiary"]:
            n_sub += 1
            continue
        events.append({"rcept_no": it["rcept_no"], "rcept_dt": it["rcept_dt"], "type": c["type"],
                       "code": it["stock_code"].strip(), "corp_code": it["corp_code"], "corp": it["corp_name"],
                       "market": it["corp_cls"], "title": it["report_nm"]})
    events.sort(key=lambda e: e["rcept_no"])
    by_type: dict[str, int] = {}
    for e in events:
        by_type[e["type"]] = by_type.get(e["type"], 0) + 1
    print(f"[classify] 원공시 {len(events):,}건 · 정정 제외 {n_amend:,} · 자회사 제외 {n_sub:,}", flush=True)
    print("[classify] " + ", ".join(f"{TYPE_LABEL[k]} {v}" for k, v in sorted(by_type.items(), key=lambda x: -x[1])), flush=True)
    (CACHE / "events_raw.json").write_text(json.dumps(events, ensure_ascii=False), encoding="utf-8")
    if args.list_only:
        return

    # 2) 속성 (DART 구조화 API·원문 표본)
    attrs: dict[str, dict] = {}
    rng = random.Random(20261028)
    try:
        for api, etype in (("piicDecsn.json", "rights"), ("cvbdIsDecsn.json", "cb")):
            corps = sorted({e["corp_code"] for e in events if e["type"] == etype})
            print(f"[attrs] {api} 회사 {len(corps)}곳", flush=True)
            for cc in corps:
                data = dart_get(api, {"corp_code": cc, "bgn_de": bgn, "end_de": end})
                corp_events = [e["rcept_no"] for e in events if e["type"] == etype and e["corp_code"] == cc]
                for rn, row in match_rows(corp_events, data.get("list", []) or []).items():
                    if etype == "rights":
                        attrs.setdefault(rn, {})["method"] = row.get("ic_mthn")
                        new, before = _f(row.get("nstk_ostk_cnt")), _f(row.get("bfic_tisstk_ostk"))
                        if new and before:
                            attrs[rn]["new_share_pct"] = new / before * 100
                    else:
                        attrs.setdefault(rn, {})["dilution"] = _f(row.get("cvisstk_tisstk_vs"))
                        attrs[rn]["refix_floor"] = row.get("act_mktprc_fl_cvprc_lwtrsprc")
                        attrs[rn]["bond_method"] = row.get("bdis_mthn")
        for etype, n_sample, parser in (("supply", args.supply_sample, "supply"),
                                        ("earnings", args.earnings_sample, "earnings")):
            pool = [e for e in events if e["type"] == etype]
            rng.shuffle(pool)
            pool = sorted(pool[:n_sample], key=lambda e: e["rcept_no"])
            print(f"[attrs] {etype} 원문 표본 {len(pool)}/{sum(1 for e in events if e['type'] == etype)}건", flush=True)
            for i, e in enumerate(pool, 1):
                pc = CACHE / "parsed" / f"{e['rcept_no']}.json"
                if pc.exists():
                    attrs[e["rcept_no"]] = {**attrs.get(e["rcept_no"], {}), **json.loads(pc.read_text(encoding="utf-8"))}
                    continue
                text = doc_text(e["rcept_no"])
                rec = ({"ratio": parse_supply_ratio(text)} if parser == "supply" else (parse_earnings(text) or {}))
                rec["sampled"] = True
                pc.parent.mkdir(parents=True, exist_ok=True)
                pc.write_text(json.dumps(rec, ensure_ascii=False), encoding="utf-8")
                attrs[e["rcept_no"]] = {**attrs.get(e["rcept_no"], {}), **rec}
                if i % 100 == 0:
                    print(f"[attrs] {etype} {i}/{len(pool)} · DART {dart_calls_used()}건", flush=True)
    except BudgetExceeded as exc:
        print(f"[attrs] 예산 소진으로 중단: {exc} — 모은 데까지만 사용", flush=True)
    (CACHE / "attrs.json").write_text(json.dumps(attrs, ensure_ascii=False), encoding="utf-8")
    print(f"[attrs] 완료 · DART 누적 {dart_calls_used()}건", flush=True)

    # 3) 접수 시각 (15:30 이후분만, DART 웹 목록)
    days = sorted({e["rcept_dt"] for e in events if datetime.strptime(e["rcept_dt"], "%Y%m%d").weekday() < 5})
    print(f"[times] {len(days)}일", flush=True)
    for i, d in enumerate(days, 1):
        fetch_after_close_times(d)
        if i % 50 == 0:
            print(f"[times] {i}/{len(days)}", flush=True)

    # 4) 시세
    codes = sorted({e["code"] for e in events})
    print(f"[prices] 종목 {len(codes)}개", flush=True)
    for sym in ("KOSPI", "KOSDAQ"):
        fetch_prices(sym, price_start, price_end)
    for i, code in enumerate(codes, 1):
        fetch_prices(code, price_start, price_end)
        if i % 100 == 0:
            print(f"[prices] {i}/{len(codes)}", flush=True)
    compute_stats(bgn, end, price_start, price_end)


def compute_stats(bgn: str | None = None, end: str | None = None, price_start: str | None = None,
                  price_end: str | None = None) -> dict:
    events = json.loads((CACHE / "events_raw.json").read_text(encoding="utf-8"))
    attrs = json.loads((CACHE / "attrs.json").read_text(encoding="utf-8")) if (CACHE / "attrs.json").exists() else {}
    if not bgn:
        bgn, end = min(e["rcept_dt"] for e in events), max(e["rcept_dt"] for e in events)
    if not price_start:
        files = sorted((CACHE / "prices").glob("KOSPI_*.json"))
        _, price_start, price_end = files[-1].stem.split("_")
    idx = {m: {d: v[0] for d, v in fetch_prices(INDEX_SYMBOL[m], price_start, price_end).items()} for m in ("Y", "K")}
    calendar = sorted(idx["Y"])
    times: dict[str, dict] = {}
    for p in (CACHE / "times").glob("*.json"):
        times[p.stem] = json.loads(p.read_text(encoding="utf-8"))

    kept, dropped = [], {"no_price": 0, "halt_or_gap": 0, "cluster": 0, "time_unknown": 0}
    last_t0: dict[tuple[str, str], int] = {}
    pos_of = {d: i for i, d in enumerate(calendar)}
    price_cache: dict[str, dict] = {}
    for e in sorted(events, key=lambda x: x["rcept_no"]):
        day = times.get(e["rcept_dt"])
        if e["rcept_no"] in (day or {}).get("after", {}):
            after_close, rtime = True, day["after"][e["rcept_no"]]
        elif datetime.strptime(e["rcept_dt"], "%Y%m%d").weekday() >= 5:
            after_close, rtime = True, None
        elif day and day.get("complete"):
            after_close, rtime = False, None
        else:
            after_close, rtime = True, None   # 시각 모름 → 보수적으로 다음 거래일 기준
            dropped["time_unknown"] += 1
        if e["code"] not in price_cache:
            path = CACHE / "prices" / f"{e['code']}_{price_start}_{price_end}.json"
            price_cache[e["code"]] = ({d: tuple(v) for d, v in json.loads(path.read_text()).items()}
                                      if path.exists() else {})
        stock = price_cache[e["code"]]
        if not stock:
            dropped["no_price"] += 1
            continue
        res = event_returns(calendar, stock, idx[e["market"]], e["rcept_dt"], after_close)
        if not res:
            dropped["halt_or_gap"] += 1
            continue
        k = (e["code"], e["type"])
        p0 = pos_of[res["t0"]]
        if k in last_t0 and p0 - last_t0[k] < CLUSTER_DAYS:
            dropped["cluster"] += 1
            continue
        last_t0[k] = p0
        a = {"market": e["market"], **attrs.get(e["rcept_no"], {})}
        kept.append({**res, "type": e["type"], "after_close": after_close, "time": rtime,
                     "chain": bucket_chain(e["type"], a), "rcept_no": e["rcept_no"]})
    buckets = aggregate(kept)
    n_after = sum(1 for e in kept if e["after_close"])
    stats = {
        "generated_at": datetime.now(KST).isoformat(timespec="seconds"),
        "period": {"from": bgn, "to": end}, "prices_to": price_end,
        "min_count": MIN_COUNT,
        "definitions": {
            "첫날(h1)": "공시가 처음 반영된 거래일 종가 / 직전 거래일 종가 - 1 (15:30 전 접수=당일, 이후·휴일=다음 거래일)",
            "h5,h20": "같은 기준가(직전 종가) 대비 5·20거래일째 종가",
            "drift5,drift20": "첫날 종가에 산 경우 그 뒤 5·20거래일 수익률",
            "same_day_after_close": "장 마감 후 공시의 접수 당일(공시 전) 수익률 — 선반영 확인용",
            "mean": "상·하위 1% 눌러 담은 평균(50건 이상일 때)", "ex_*": "같은 기간 코스피(유가)/코스닥 지수 대비 초과",
            "t": "초과수익 평균의 t값(|t|<2면 우연과 구분 어려움)",
            "제외": "정정공시·자회사 공시, 시세 없음(상폐 등), 첫날 거래정지, 창 안 ±30% 초과 변동(수정주가 왜곡), "
                    "같은 종목·유형 5거래일 안 반복",
        },
        "counts": {"events_classified": len(events), "events_used": len(kept), "after_close": n_after,
                   "dropped": dropped, "by_type": {}},
        "dart_calls": dart_calls_used(),
        "buckets": buckets,
    }
    for e in kept:
        stats["counts"]["by_type"][e["type"]] = stats["counts"]["by_type"].get(e["type"], 0) + 1
    STATS_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATS_PATH.write_text(json.dumps(stats, ensure_ascii=False, indent=1), encoding="utf-8")
    (CACHE / "events_used.json").write_text(json.dumps(kept, ensure_ascii=False), encoding="utf-8")
    print(f"[stats] 사용 {len(kept):,}건 · 제외 {dropped} · 구간 {len(buckets)}개 → {STATS_PATH}", flush=True)
    return stats


def show(min_n: int = MIN_COUNT) -> None:
    st = load_stats(STATS_PATH)
    print(f"기간 {st['period']} · 사용 {st['counts']['events_used']:,}건 · DART {st['dart_calls']}건")
    hdr = f"{'구간':<44}{'N':>6}{'첫날평균':>9}{'중앙':>7}{'오름%':>6}{'초과1':>7}{'t1':>6}{'5일':>7}{'초과5':>7}{'t5':>6}{'20일':>7}{'초과20':>7}{'drift5':>7}"
    print(hdr)
    for key, r in st["buckets"].items():
        h1, h5, h20, d5 = r["h1"], r["h5"], r["h20"], r["drift5"]
        if h1["n"] < min_n:
            continue

        def p(x, k):
            return f"{x[k] * 100:+.1f}" if x.get("n", 0) and k in x else "-"
        print(f"{r['label']:<44}{h1['n']:>6}{p(h1, 'mean'):>9}{p(h1, 'median'):>7}{h1['pos'] * 100:>6.0f}"
              f"{p(h1, 'ex_mean'):>7}{h1.get('t', 0):>6.1f}{p(h5, 'mean'):>7}{p(h5, 'ex_mean'):>7}{h5.get('t', 0):>6.1f}"
              f"{p(h20, 'mean'):>7}{p(h20, 'ex_mean'):>7}{p(d5, 'ex_mean'):>7}")


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--build", action="store_true")
    ap.add_argument("--list-only", action="store_true")
    ap.add_argument("--stats-only", action="store_true")
    ap.add_argument("--show", action="store_true")
    ap.add_argument("--bgn")
    ap.add_argument("--end")
    ap.add_argument("--supply-sample", type=int, default=int(os.environ.get("EVENT_REACTION_SUPPLY_SAMPLE", "1100")))
    ap.add_argument("--earnings-sample", type=int, default=int(os.environ.get("EVENT_REACTION_EARNINGS_SAMPLE", "500")))
    args = ap.parse_args(argv)
    if args.build or args.list_only:
        build(args)
    elif args.stats_only:
        compute_stats()
    if args.show:
        show()


if __name__ == "__main__":
    main()
