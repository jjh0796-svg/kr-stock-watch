# ==========================================
# 📄 탐지된 공시의 내용 요약
#   - 주요사항보고서 계열: DART 구조화 JSON API (자사주·증자·CB·감자 등)
#   - 잠정실적: 원문(zip) 표 파싱 (kr-earnings-pulse에서 검증된 방식)
#   - 5% 대량보유: majorstock API
# 요약은 "탐지되어 알림이 나가는 건"에만 호출된다 (건당 DART 1~2회 + 네이버 1회).
# 실패하면 None을 돌려주고 알림은 요약 없이 그대로 나간다.
# ==========================================
import io
import os
import re
import zipfile
from datetime import datetime, timedelta

import requests

from common import UA_HEADERS, esc

DART_BASE = "https://opendart.fss.or.kr/api"

# ─── Gemini 폴백 (규칙 파싱이 실패한 공시의 원문 요약) ────────────────────────
GEMINI_KEY = os.environ.get("GEMINI_API_KEY", "")
_GEMINI_MODELS = [m for m in (
    os.environ.get("GEMINI_MODEL", ""),
    "gemini-flash-lite-latest", "gemini-flash-latest", "gemini-2.5-flash",
) if m]
_gemini_model_ok: str | None = None


def _llm_summary(title: str, dart_key: str, rcept_no: str) -> str | None:
    """규칙 파서가 못 잡은 공시를 Gemini로 2~3줄 요약 (키 없거나 실패 시 None)."""
    global _gemini_model_ok
    if not GEMINI_KEY:
        return None
    try:
        text = _doc_text(dart_key, rcept_no)
    except Exception:
        return None
    body = re.sub(r"^.*?xforms_input\{[^}]*\}", "", text)[:6000]  # 앞머리 CSS 제거
    if len(body) < 150:
        return None
    prompt = (
        "다음은 한국 상장사의 DART 공시 원문이다. 투자자 관점에서 핵심만 2~3줄로 "
        "요약하라. 금액·수량·당사자·사유·일정 등 구체 정보를 우선하고, 과장이나 "
        "해석 없이 사실만. 기재정정이면 무엇이 어떻게 바뀌었는지를 중심으로. "
        "한국어 평문으로만 답하고 머리기호나 마크다운은 쓰지 마라.\n\n"
        f"공시 제목: {title}\n원문: {body}"
    )
    models = [_gemini_model_ok] if _gemini_model_ok else _GEMINI_MODELS
    for model in models:
        try:
            r = requests.post(
                f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                params={"key": GEMINI_KEY},
                json={"contents": [{"parts": [{"text": prompt}]}],
                      "generationConfig": {"temperature": 0.2, "maxOutputTokens": 400}},
                timeout=40)
            if r.status_code in (404, 429):
                continue  # 모델 폐기(404)·무료쿼터 회수(429) 시 다음 후보
            if r.status_code != 200:
                print(f"[Gemini] HTTP {r.status_code}: {r.text[:120]}")
                return None
            out = r.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
            if not out:
                return None
            _gemini_model_ok = model
            return "🤖 " + esc(out[:600])
        except Exception as e:
            print(f"[Gemini] {type(e).__name__}: {e}")
            return None
    return None


def _num(v) -> float | None:
    """DART 숫자 문자열('1,234', '-', '') → float"""
    s = str(v or "").replace(",", "").replace("△", "-").strip()
    if not s or s in ("-", "."):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _eok(won: float | None) -> str:
    if won is None:
        return "-"
    if abs(won) >= 1e12:
        return f"{won / 1e12:,.1f}조"
    return f"{won / 1e8:,.0f}억"


def _shares(v) -> str:
    n = _num(v)
    return f"{n:,.0f}주" if n is not None else "-"


# ─── 시가총액 (네이버) ─────────────────────────────────────────────────────────

def market_cap(code: str) -> float | None:
    """네이버 종목 API의 '시총'("1,335조 8,747억") → 원 단위 float"""
    try:
        r = requests.get(f"https://m.stock.naver.com/api/stock/{code}/integration",
                         headers=UA_HEADERS, timeout=8)
        for info in r.json().get("totalInfos", []):
            if info.get("key") == "marketValue" or info.get("code") == "marketValue" \
                    or "시총" in str(info.get("key", "")):
                txt = str(info.get("value", ""))
                jo = re.search(r"([\d,]+)\s*조", txt)
                eok = re.search(r"([\d,]+)\s*억", txt)
                total = 0.0
                if jo:
                    total += float(jo.group(1).replace(",", "")) * 1e12
                if eok:
                    total += float(eok.group(1).replace(",", "")) * 1e8
                return total or None
    except Exception:
        pass
    return None


def _vs_cap(won: float | None, code: str) -> str:
    if not won:
        return ""
    cap = market_cap(code)
    if not cap:
        return ""
    return f" · 시총대비 {won / cap * 100:.1f}%"


# ─── DART 구조화 API 공통 ──────────────────────────────────────────────────────

def _dart_rows(api: str, api_key: str, corp_code: str, rcept_dt: str) -> list[dict]:
    # [기재정정]은 원 결정일 기준으로 등록돼 있어 조회 범위를 90일 넓게 잡는다
    try:
        bgn = (datetime.strptime(rcept_dt, "%Y%m%d") - timedelta(days=90)).strftime("%Y%m%d")
    except ValueError:
        bgn = rcept_dt
    try:
        r = requests.get(f"{DART_BASE}/{api}.json",
                         params={"crtfc_key": api_key, "corp_code": corp_code,
                                 "bgn_de": bgn, "end_de": rcept_dt},
                         headers=UA_HEADERS, timeout=15)
        d = r.json()
        if d.get("status") != "000":
            return []
        return d.get("list", [])
    except Exception:
        return []


def _pick(rows: list[dict], rcept_no: str) -> dict | None:
    for row in rows:
        if row.get("rcept_no") == rcept_no:
            return row
    return rows[-1] if rows else None


# ─── 서식별 요약 ───────────────────────────────────────────────────────────────
# 서명: fn(row, code, api_key, rcept_no) — 뒤 두 인자는 원문 보조 파싱용

def _issue_targets(api_key: str, rcept_no: str) -> str | None:
    """유증·CB 등 발행 공시의 대상자 테이블에서 발행대상 추출 (원문 파싱)."""
    try:
        text = _doc_text(api_key, rcept_no)
    except Exception:
        return None
    sec = re.search(r"【(?:특정인에\s*대한\s*)?(?:제3자배정\s*)?대상자별[^】]*】(.{0,1200})", text)
    if not sec:
        return None
    seg = sec.group(1)
    m = re.search(r"비\s*고\s*(.+?)\s+(?:없음|최대주주|특수관계\S*|계열\S*|-\s)", seg)
    if not m:
        return None
    name = m.group(1).strip()
    if not name or len(name) > 60:
        return None
    n_rows = len(re.findall(r"[\d,]{9,}", seg))
    extra = f" 외 {n_rows - 1}곳" if n_rows > 1 else ""
    return f"대상: {esc(name)}{extra}"


def _sum_treasury_buy(row: dict, code: str, api_key: str = "", rcept_no: str = "") -> str:
    prc = _num(row.get("aqpln_prc_ostk"))
    lines = [f"취득: 보통주 {_shares(row.get('aqpln_stk_ostk'))} ({_eok(prc)})"
             + _vs_cap(prc, code)]
    if row.get("aq_pp"):
        lines.append(f"목적: {esc(str(row['aq_pp'])[:60])}")
    if row.get("aq_mth"):
        lines.append(f"방법: {esc(row['aq_mth'])}")
    return "\n".join(lines)


def _sum_treasury_sell(row: dict, code: str, api_key: str = "", rcept_no: str = "") -> str:
    prc = _num(row.get("dppln_prc_ostk"))
    lines = [f"처분: 보통주 {_shares(row.get('dppln_stk_ostk'))} ({_eok(prc)})"
             + _vs_cap(prc, code)]
    if row.get("dp_pp"):
        lines.append(f"목적: {esc(str(row['dp_pp'])[:60])}")
    return "\n".join(lines)


def _sum_trust(row: dict, code: str, api_key: str = "", rcept_no: str = "") -> str:
    prc = _num(row.get("ctr_prc"))
    out = f"신탁계약금액: {_eok(prc)}" + _vs_cap(prc, code)
    if row.get("ctr_pd_bgd"):
        out += f"\n계약기간: {row.get('ctr_pd_bgd')} ~ {row.get('ctr_pd_edd', '')}"
    return out


def _sum_rights_issue(row: dict, code: str, api_key: str = "", rcept_no: str = "") -> str:
    ostk = _num(row.get("nstk_ostk_cnt") or row.get("piic_nstk_ostk_cnt"))
    estk = _num(row.get("nstk_estk_cnt") or row.get("piic_nstk_estk_cnt"))
    total = sum(filter(None, (
        _num(row.get(k)) for k in
        ("fdpp_fclt", "fdpp_bsninh", "fdpp_op", "fdpp_dtrp", "fdpp_ocsa", "fdpp_etc",
         "piic_fdpp_fclt", "piic_fdpp_bsninh", "piic_fdpp_op", "piic_fdpp_dtrp",
         "piic_fdpp_ocsa", "piic_fdpp_etc"))))
    mth = row.get("ic_mthn") or row.get("piic_ic_mthn") or ""
    kinds = []
    if ostk:
        kinds.append(f"보통주 {ostk:,.0f}주")
    if estk:
        kinds.append(f"기타주식(우선주 등) {estk:,.0f}주")
    if not kinds and not total and api_key and rcept_no:
        # 기재정정 등으로 구조화 값이 전부 '-'인 경우 — 원문에서 직접 추출
        doc = _sum_rights_doc(api_key, rcept_no, code)
        if doc:
            return doc
    out = "신주: " + (" + ".join(kinds) if kinds else "-")
    if total:
        out += f" · 조달 {_eok(total)}" + _vs_cap(total, code)
    if mth:
        out += f"\n방식: {esc(mth)}"
    if api_key and rcept_no and "3자" in mth:
        target = _issue_targets(api_key, rcept_no)
        if target:
            out += f"\n{target}"
    return out


def _sum_bonus_issue(row: dict, code: str, api_key: str = "", rcept_no: str = "") -> str:
    cnt = row.get("nstk_ostk_cnt") or row.get("fric_nstk_ostk_cnt")
    per = row.get("nstk_ascnt_ps_ostk") or row.get("fric_nstk_ascnt_ps_ostk")
    std = row.get("nstk_asstd") or row.get("fric_nstk_asstd") or ""
    out = f"신주: 보통주 {_shares(cnt)}"
    if _num(per) is not None:
        out += f" · 1주당 {_num(per):g}주 배정"
    if std:
        out += f"\n기준일: {std}"
    return out


def _sum_capital_reduction(row: dict, code: str, api_key: str = "", rcept_no: str = "") -> str:
    rt = row.get("cr_rt_ostk") or row.get("cr_rt")
    out = f"감자: 보통주 {_shares(row.get('crstk_ostk_cnt'))}"
    if rt:
        out += f" · 감자비율 {rt}%"
    if row.get("crsc_mtd"):
        out += f"\n방법: {esc(str(row['crsc_mtd'])[:60])}"
    return out


def _sum_cb(row: dict, code: str, kind: str, api_key: str = "", rcept_no: str = "") -> str:
    fta = _num(row.get("bd_fta"))
    prc = _num(row.get("cv_prc") or row.get("ex_prc") or row.get("act_prc"))
    tm = row.get("bd_tm", "")
    out = f"{kind} {tm}회차 · 권면총액 {_eok(fta)}" + _vs_cap(fta, code)
    if prc:
        out += f"\n전환/행사가: {prc:,.0f}원"
        if fta:
            out += f" → 전환가능 약 {fta / prc:,.0f}주"
    beg = row.get("cv_rqpd_bgd") or row.get("ex_rqpd_bgd") or row.get("expd_bgd")
    if beg:
        out += f"\n청구가능: {beg}부터"
    if api_key and rcept_no:
        target = _issue_targets(api_key, rcept_no)
        if target:
            out += f"\n{target}"
    return out


def _sum_cb_doc(api_key: str, rcept_no: str, code: str, kind: str) -> str | None:
    """CB/BW/EB 발행: 구조화 API가 비어 있을 때 원문에서 직접 추출하는 폴백."""
    text = _doc_text(api_key, rcept_no)
    tm = (re.search(r"회차\s*(\d{1,3})\s", text) or [None, None])[1]
    fta = _num((re.search(r"권면\(전자등록\)총액\s*\(원\)\s*([\d,]+)", text) or [None, None])[1])
    prc = _num((re.search(r"(?:전환|행사|교환)가액\s*\([^)]*\)\s*([\d,]+)", text) or [None, None])[1])
    if not fta:
        return None
    out = f"{kind} {tm or '-'}회차 · 권면총액 {_eok(fta)}" + _vs_cap(fta, code)
    if prc:
        out += f"\n전환/행사가: {prc:,.0f}원 → 전환가능 약 {fta / prc:,.0f}주"
    beg = re.search(r"(?:전환|행사|교환)청구기간\s*시작일\s*(\d{4}-\d{2}-\d{2})", text)
    if beg:
        out += f"\n청구가능: {beg.group(1)}부터"
    m = re.search(r"비\s*고\s*(.+?)\s+(?:없음|최대주주|특수관계\S*|계열\S*|-\s)",
                  (re.search(r"【(?:특정인에\s*대한\s*)?대상자별[^】]*】(.{0,1200})", text)
                   or [None, ""])[1] or "")
    if m and 0 < len(m.group(1).strip()) <= 60:
        out += f"\n대상: {esc(m.group(1).strip())}"
    return out


def _sum_major_stock(row: dict, code: str, api_key: str = "", rcept_no: str = "") -> str:
    lines = []
    if row.get("repror"):
        lines.append(f"대표보고: {esc(row['repror'])}")
    rt, delta = _num(row.get("stkrt")), _num(row.get("stkrt_irds"))
    if rt is not None:
        line = f"보유비율: {rt:.2f}%"
        if delta:
            line += f" ({delta:+.2f}%p)"
        lines.append(line)
    if row.get("report_tp"):
        lines.append(f"보고구분: {esc(row['report_tp'])}")
    if row.get("report_resn"):
        lines.append(f"사유: {esc(str(row['report_resn'])[:70])}")
    return "\n".join(lines)


# ─── 잠정실적 원문 파싱 (kr-earnings-pulse 검증 로직) ─────────────────────────

def _doc_text(api_key: str, rcept_no: str) -> str:
    resp = requests.get(f"{DART_BASE}/document.xml",
                        params={"crtfc_key": api_key, "rcept_no": rcept_no},
                        headers=UA_HEADERS, timeout=30)
    resp.raise_for_status()
    zf = zipfile.ZipFile(io.BytesIO(resp.content))
    texts = []
    for name in zf.namelist():
        raw = zf.read(name)
        for enc in ("utf-8", "euc-kr", "cp949"):
            try:
                texts.append(raw.decode(enc))
                break
            except UnicodeDecodeError:
                continue
    text = re.sub(r"<[^>]+>", " ", " ".join(texts))
    return re.sub(r"\s+", " ", text)


def _detect_unit_won(text: str) -> float:
    if re.search(r"단위\s*[:：]?\s*조\s*원", text):
        return 1e12
    if re.search(r"단위\s*[:：]?\s*억\s*원", text):
        return 1e8
    if re.search(r"단위\s*[:：]?\s*천\s*원", text):
        return 1e3
    return 1e6  # 공정공시 기본은 백만원


def _clip(m: re.Match | None, limit: int = 70) -> str | None:
    if not m:
        return None
    val = m.group(1).strip().strip("-").strip()
    return esc(val[:limit]) if val else None


def _sum_supply(api_key: str, rcept_no: str, ctx: dict | None = None) -> str | None:
    """단일판매ㆍ공급계약체결 — 계약내용·상대·금액·매출대비·기간"""
    text = _doc_text(api_key, rcept_no)
    dot = r"[ㆍ·・]?\s*"
    # 코스닥형: "판매ㆍ공급계약 내용 ..." / 유가형: "체결계약명 ..."
    what = (_clip(re.search(rf"판매{dot}공급계약\s*내용\s*(.+?)\s*2\.\s*계약내역", text))
            or _clip(re.search(r"체결계약명\s*(.+?)\s*2\.\s*계약내역", text)))
    total = _num((re.search(r"계약금액(?:\s*총액)?\s*\(원\)\s*([\d,\-]+)", text) or [None, None])[1])
    sales = _num((re.search(r"최근\s*매출액\s*\(원\)\s*([\d,\-]+)", text) or [None, None])[1])
    ratio = _num((re.search(r"매출액\s*대비\s*\(%\)\s*([\d.,\-]+)", text) or [None, None])[1])
    party = _clip(re.search(r"\d\.\s*계약상대방?\s*(.+?)\s*(?:-\s*)?(?:최근\s*매출액|회사와의\s*관계)", text))
    region = _clip(re.search(rf"판매{dot}공급지역\s*(.+?)\s*\d\.\s*계약기간", text))
    period = re.search(r"계약기간\s*시작일\s*([\d\-]+)\s*종료일\s*([\d\-]+)", text)

    if total is None and not what:
        return None
    lines = []
    if what:
        lines.append(f"계약: {what}")
    if party or region:
        seg = [f"상대: {party}" if party else None, f"지역: {region}" if region else None]
        lines.append(" · ".join(s for s in seg if s))
    if total is not None:
        line = f"금액: {_eok(total)}"
        if ratio is not None:
            line += f" (매출대비 {ratio:.1f}%)"
        elif sales:
            line += f" (매출대비 {total / sales * 100:.1f}%)"
        lines.append(line)
    if period:
        beg, end = period.group(1), period.group(2)
        months = ""
        try:
            b = (int(beg[:4]), int(beg[5:7]))
            e = (int(end[:4]), int(end[5:7]))
            months = f" ({(e[0] - b[0]) * 12 + e[1] - b[1]}개월)"
        except ValueError:
            pass
        lines.append(f"기간: {beg} ~ {end}{months}")
    return "\n".join(lines) if lines else None


def _sum_ir(api_key: str, rcept_no: str, ctx: dict | None = None) -> str | None:
    """기업설명회(IR)개최 — 일시·방법·목적·내용 (코스닥/유가 서식 모두 대응)"""
    text = _doc_text(api_key, rcept_no)
    # 코스닥형: "시작일 종료일 시작시간 종료시간 2026-07-29 2026-07-29 14:00 15:00"
    when = re.search(r"시작일\s*종료일\s*시작시간\s*종료시간\s*([\d\-]+)\s*[\d\-]+\s*([\d:]+)", text)
    # 유가형: "1. 일시 및 장소 일시 2026-08-05 16:00 장소 ..."
    if not when:
        when = re.search(r"일시\s*(\d{4}-[\d\-]+)\s*([\d:]+)\s*장소", text)
    method = (_clip(re.search(r"실시방법\s*(.+?)\s*\d\.\s*주요내용", text))
              or _clip(re.search(r"개최방법\s*(.+?)\s*\d\.\s*", text)))
    purpose = (_clip(re.search(r"실시목적\s*(.+?)\s*\d\.\s*실시방법", text))
               or _clip(re.search(r"개최목적\s*(.+?)\s*\d\.\s*개최방법", text)))
    detail = (_clip(re.search(r"주요\s*설명회내용(?:\(요약\))?\s*(.+?)\s*\d\.\s*", text), 90)
              or _clip(re.search(r"주요내용\s*(.+?)\s*\d\.\s*", text), 90))

    lines = []
    if when:
        line = f"일시: {when.group(1)} {when.group(2)}"
        if method:
            line += f" · {method}"
        lines.append(line)
    if purpose:
        lines.append(f"목적: {purpose}")
    if detail:
        lines.append(f"내용: {detail}")
    return "\n".join(lines) if lines else None


# 네이버 재무: 과거 실적(추이) + 컨센서스(isConsensus=Y), 단위 억원
def _naver_finance(code: str, period: str) -> tuple[list[tuple[str, dict]], tuple[str, dict] | None]:
    r = requests.get(f"https://m.stock.naver.com/api/stock/{code}/finance/{period}",
                     headers=UA_HEADERS, timeout=10)
    info = r.json().get("financeInfo", {})
    cols = [(t.get("key"), t.get("isConsensus") == "Y") for t in info.get("trTitleList", [])]
    rows = {row.get("title"): row.get("columns", {})
            for row in info.get("rowList", [])
            if row.get("title") in ("매출액", "영업이익", "당기순이익")}

    def cell(title, key):
        return _num((rows.get(title, {}).get(key) or {}).get("value"))

    past, cons = [], None
    for key, is_cons in cols:
        if not key:
            continue
        vals = {"rev": cell("매출액", key), "op": cell("영업이익", key),
                "ni": cell("당기순이익", key)}
        if is_cons:
            cons = (key, vals)
        elif vals["rev"] is not None:
            past.append((key, vals))
    past.sort(key=lambda x: x[0], reverse=True)
    return past, cons


def _naver_quarters(code: str):
    return _naver_finance(code, "quarter")


# ─── 기업 컨텍스트 (지표 한 줄 · /기업 카드) ──────────────────────────────────

_infos_cache: dict[str, dict] = {}


def _naver_infos(code: str) -> dict[str, str]:
    """네이버 종목 지표 {'시총': '1,335조 8,747억', 'PER': '18.47배', ...} (런당 캐시)"""
    if code in _infos_cache:
        return _infos_cache[code]
    out: dict[str, str] = {}
    try:
        r = requests.get(f"https://m.stock.naver.com/api/stock/{code}/integration",
                         headers=UA_HEADERS, timeout=10)
        for info in r.json().get("totalInfos", []):
            k, v = info.get("key"), info.get("value")
            if k and v:
                out[str(k)] = str(v)
    except Exception:
        pass
    _infos_cache[code] = out
    return out


def _compact_cap(txt: str) -> str | None:
    jo = re.search(r"([\d,]+)\s*조", txt)
    eok = re.search(r"([\d,]+)\s*억", txt)
    if jo:
        whole = float(jo.group(1).replace(",", ""))
        frac = float(eok.group(1).replace(",", "")) / 10000 if eok else 0
        return f"{whole + frac:,.1f}조".replace(".0조", "조")
    if eok:
        return f"{eok.group(1)}억"
    return None


def stock_snapshot(code: str) -> str | None:
    """알림 하단용 지표 한 줄: 📌 시총 · PER · PBR · 배당"""
    infos = _naver_infos(code)
    parts = []
    cap = _compact_cap(infos.get("시총", ""))
    if cap:
        parts.append(f"시총 {cap}")
    for label, key in (("PER", "PER"), ("PBR", "PBR")):
        v = infos.get(key, "").replace("배", "").strip()
        if v and v not in ("-", "N/A"):
            parts.append(f"{label} {v}")
    dy = infos.get("배당수익률", "").strip()
    if dy and dy not in ("-", "N/A"):
        parts.append(f"배당 {dy}")
    return "📌 " + " · ".join(parts) if parts else None


def _fin_table(title: str, past: list, cons, label_fn, rows_n: int) -> list[str]:
    lines = [f"{title} (매출/영업익/순이익)"]

    def val(v):
        return _eok(v * 1e8) if v is not None else "-"

    if cons:
        k, v = cons
        lines.append(f" {label_fn(k)}E {val(v['rev'])}/ {val(v['op'])}/ {val(v['ni'])} ← 컨센서스")
    for k, v in past[:rows_n]:
        lines.append(f" {label_fn(k)} {val(v['rev'])}/ {val(v['op'])}/ {val(v['ni'])}")
    return lines if len(lines) > 1 else []


def company_card(code: str, name: str) -> str | None:
    """/기업 명령: 기업 개요·지표·연간/분기 실적 카드"""
    infos = _naver_infos(code)
    # 시세·시장 구분 (다음)
    market, price_line = "", ""
    try:
        r = requests.get(f"https://finance.daum.net/api/quotes/A{code}",
                         params={"summary": "false"},
                         headers={**UA_HEADERS, "Referer": "https://finance.daum.net/"},
                         timeout=10)
        q = r.json()
        market = {"KOSPI": "코스피", "KOSDAQ": "코스닥"}.get(q.get("market", ""), "")
        price = q.get("tradePrice")
        rate = q.get("changeRate")
        if price:
            price_line = f"현재가 {price:,.0f}"
            if rate is not None:
                price_line += f" ({rate * 100:+.1f}%)"
    except Exception:
        pass

    head = f"🏢 <b>{esc(name)} ({code})</b>"
    if market:
        head += f" · {market}"
    lines = [head]
    cap = _compact_cap(infos.get("시총", ""))
    seg = [s for s in (price_line, f"시총 {cap}" if cap else "") if s]
    if seg:
        lines.append(" · ".join(seg))
    metrics = []
    for label, key in (("PER", "PER"), ("추정PER", "추정PER"), ("PBR", "PBR")):
        v = infos.get(key, "").replace("배", "").strip()
        if v and v not in ("-", "N/A"):
            metrics.append(f"{label} {v}")
    dy = infos.get("배당수익률", "").strip()
    if dy and dy not in ("-", "N/A"):
        metrics.append(f"배당 {dy}")
    if metrics:
        lines.append(" · ".join(metrics))
    hi, lo = infos.get("52주 최고", ""), infos.get("52주 최저", "")
    if hi and lo:
        lines.append(f"52주 {hi} / {lo}")

    try:
        apast, acons = _naver_finance(code, "annual")
        tbl = _fin_table("연간", apast, acons, lambda k: f"FY{k[2:4]}", 3)
        if tbl:
            lines.append("")
            lines.extend(tbl)
    except Exception:
        pass
    try:
        qpast, qcons = _naver_finance(code, "quarter")
        tbl = _fin_table("분기", qpast, qcons, _qlabel, 4)
        if tbl:
            lines.append("")
            lines.extend(tbl)
    except Exception:
        pass
    return "\n".join(lines) if len(lines) > 1 else None


def _infer_quarter(text: str, rcept_no: str) -> str:
    """공시 원문 또는 접수월로 대상 분기 키('202606')를 추정."""
    m = re.search(r"(20\d{2})\s*년\s*(?:제?\s*)?([1-4])\s*분기", text)
    if m:
        return f"{m.group(1)}{int(m.group(2)) * 3:02d}"
    year, month = int(rcept_no[:4]), int(rcept_no[4:6])
    q_by_month = {1: 4, 2: 4, 3: 4, 4: 1, 5: 1, 6: 1, 7: 2, 8: 2,
                  9: 2, 10: 3, 11: 3, 12: 3}
    q = q_by_month[month]
    if q == 4:
        year -= 1
    return f"{year}{q * 3:02d}"


def _qlabel(key: str) -> str:
    return f"{key[:4]}.{int(key[4:6]) // 3}Q"


def _sum_repricing(api_key: str, rcept_no: str, ctx: dict | None = None) -> str | None:
    """전환가액(행사가액·교환가액)의조정 — 조정 전→후, 전환가능주식수, 사유"""
    text = _doc_text(api_key, rcept_no)
    m = re.search(r"조정전\s*(전환|행사|교환)가액\s*\(원\)\s*조정후\s*\1가액\s*\(원\)"
                  r"\s*(\d{1,3})\s+(\S+)\s+([\d,]+)\s+([\d,]+)", text)
    lines = []
    if m:
        kind_label = {"전환": "CB 전환가", "행사": "BW 행사가", "교환": "EB 교환가"}[m.group(1)]
        before, after = _num(m.group(4)), _num(m.group(5))
        if before and after:
            lines.append(f"{m.group(2)}회차 · {kind_label} {before:,.0f}원 → {after:,.0f}원"
                         f" ({(after / before - 1) * 100:+.1f}%)")
    sec = re.search(r"주식수\s*변동(.+?)조정사유", text)
    if sec:
        nums = [n for n in (_num(x) for x in re.findall(r"[\d,]{4,}", sec.group(1))) if n]
        if len(nums) >= 2:
            face = max(nums)                                  # 미전환 권면총액(원)
            shares = [n for n in nums if n != face]
            after_shares = shares[-1] if shares else None
            if face >= 1e7 and after_shares:
                code = (ctx or {}).get("code", "")
                lines.append(f"미전환 권면 {_eok(face)}{_vs_cap(face, code) if code else ''}"
                             f" → 조정 후 전환가능 {after_shares:,.0f}주")
    reason = _clip(re.search(r"조정사유\s*(.+?)\s*\d\.\s*조정근거", text))
    if reason:
        lines.append(f"사유: {reason}")
    return "\n".join(lines) if lines else None


def _sum_asset(api_key: str, rcept_no: str, ctx: dict | None = None) -> str | None:
    """유형자산 양수ㆍ양도결정 — 자산·금액·자산총액대비·목적"""
    text = _doc_text(api_key, rcept_no)
    verb = "양수" if "양수" in text[:2000] else "양도"
    asset = _clip(re.search(r"자산구분\s*(.+?)\s*(?:2\.|양[수도]내역|자산명)", text))
    amount = _num((re.search(rf"{verb}금액\s*\(원\)?\s*([\d,]+)", text) or [None, None])[1])
    ratio = _num((re.search(r"자산총액대비\s*\(?%?\)?\s*([\d.,]+)", text) or [None, None])[1])
    purpose = _clip(re.search(rf"{verb}목적\s*(.+?)\s*\d\.\s*", text))
    lines = []
    if amount:
        line = f"{verb}금액: {_eok(amount)}"
        if ratio is not None:
            line += f" (자산총액대비 {ratio:.1f}%)"
        lines.append(line)
    if asset:
        lines.append(f"자산: {asset}")
    if purpose:
        lines.append(f"목적: {purpose}")
    return "\n".join(lines) if lines else None


def _sum_exercise(api_key: str, rcept_no: str, ctx: dict | None = None) -> str | None:
    """전환청구권·신주인수권·교환청구권 행사 — 행사주식수·총수대비·잔여 물량"""
    text = _doc_text(api_key, rcept_no)
    shares = _num((re.search(r"행사주식수\s*누계\s*\(주\)[^\d]*([\d,]+)", text) or [None, None])[1])
    pct = _num((re.search(r"발행주식총수\s*대비\s*\(%\)\s*([\d.,]+)", text) or [None, None])[1])
    lines = []
    if shares:
        line = f"행사: {shares:,.0f}주"
        if pct is not None:
            line += f" (발행주식총수 대비 {pct:.2f}%)"
        lines.append(line)
    row = re.search(r"(\d{4}-\d{2}-\d{2})\s+\d{1,3}\s+\S.{0,60}?([\d,]{6,})\s*원\s+([\d,]+)\s+([\d,]+)\s+(\d{4}-\d{2}-\d{2})", text)
    if row:
        prc = _num(row.group(3))
        if prc:
            lines.append(f"전환/행사가 {prc:,.0f}원 · 상장예정 {row.group(5)}")
    rem = re.search(r"잔액.{0,200}?([\d,]{7,})\s*KRW[^0-9]*([\d,]{7,})\s*KRW[^0-9]*([\d,]+)\s+([\d,]+)", text)
    if rem:
        remaining, convertible = _num(rem.group(2)), _num(rem.group(4))
        if remaining and convertible:
            lines.append(f"미전환 잔액 {_eok(remaining)} → 추가 전환가능 {convertible:,.0f}주")
    return "\n".join(lines) if lines else None


def _sum_rights_doc(api_key: str, rcept_no: str, code: str) -> str | None:
    """유상증자: 구조화 API가 비어 있을 때 원문에서 직접 추출하는 폴백."""
    text = _doc_text(api_key, rcept_no)
    ostk = _num((re.search(r"보통주식\s*\(주\)\s*([\d,\-]+)", text) or [None, None])[1])
    estk = _num((re.search(r"기타주식\s*\(주\)\s*([\d,\-]+)", text) or [None, None])[1])
    funds = [_num(m) for m in re.findall(
        r"(?:시설자금|영업양수자금|운영자금|채무상환자금|타법인\s*증권취득자금|기타자금)\s*\(원\)\s*([\d,\-]+)", text)]
    total = sum(f for f in funds if f)
    mth = (re.search(r"\d\.\s*증자방식\s*(\S+)", text) or [None, ""])[1]
    kinds = []
    if ostk:
        kinds.append(f"보통주 {ostk:,.0f}주")
    if estk:
        kinds.append(f"기타주식(우선주 등) {estk:,.0f}주")
    if not kinds and not total:
        return None
    out = "신주: " + (" + ".join(kinds) if kinds else "-")
    if total:
        out += f" · 조달 {_eok(total)}" + _vs_cap(total, code)
    if mth:
        out += f"\n방식: {esc(mth)}"
    sec = re.search(r"【(?:특정인에\s*대한\s*)?(?:제3자배정\s*)?대상자별[^】]*】(.{0,1200})", text)
    if sec:
        m = re.search(r"비\s*고\s*(.+?)\s+(?:없음|최대주주|특수관계\S*|계열\S*|-\s)", sec.group(1))
        if m and 0 < len(m.group(1).strip()) <= 60:
            out += f"\n대상: {esc(m.group(1).strip())}"
    return out


def _sum_earnings(api_key: str, rcept_no: str, ctx: dict | None = None) -> str | None:
    text = _doc_text(api_key, rcept_no)
    unit = _detect_unit_won(text)

    def metric(label: str) -> tuple[float, float | None] | None:
        m = re.search(re.escape(label)
                      + r"\s*당해실적((?:\s*(?:-|△?[\d,]+(?:\.\d+)?)){3,9})", text)
        if not m:
            return None
        toks = re.findall(r"△?[\d,]+(?:\.\d+)?", m.group(1))
        nums = []
        for t in toks:
            try:
                nums.append(float(t.replace(",", "").replace("△", "-")))
            except ValueError:
                return None
        if not nums:
            return None
        yoy = nums[-1] if len(nums) >= 3 and abs(nums[-1]) < 5000 else None
        return nums[0], yoy

    rev, op, ni = metric("매출액"), metric("영업이익"), metric("당기순이익")
    if not rev or not op:
        return None
    if rev[0] <= 0 or abs(op[0]) > rev[0] * 3:
        return None  # 파싱 결과가 수상하면 요약 생략

    # 컨센서스·최근 추이 (네이버 분기 재무 — 실패해도 기본 요약은 나간다)
    code = (ctx or {}).get("code", "")
    cur_key = _infer_quarter(text, rcept_no)
    past, cons = [], None
    if code:
        try:
            past, cons = _naver_quarters(code)
        except Exception:
            pass
    cons_vals = cons[1] if cons and cons[0] == cur_key else None
    # 네이버 컨센서스는 연결 기준 — 별도 실적 공시에는 기준을 명시해 혼동 방지
    # (원문 앞부분은 CSS 잡음이라 헤더가 나오는 구간까지 넉넉히 본다)
    est_label = "예상" if "연결" in text[:1600] else "예상(연결)"

    def fmt(name, m, cons_key):
        if not m:
            return f"{name}: -"
        line = f"{name}: {_eok(m[0] * unit)}"
        notes = []
        if m[1] is not None:
            notes.append(f"YoY {m[1]:+.1f}%")
        est = cons_vals.get(cons_key) if cons_vals else None
        if est:
            actual_eok = m[0] * unit / 1e8
            notes.append(f"{est_label} {_eok(est * 1e8)} 대비 {(actual_eok / est - 1) * 100:+.0f}%")
        if notes:
            line += f" ({' · '.join(notes)})"
        return line

    lines = [fmt("매출액", rev, "rev"), fmt("영업익", op, "op"), fmt("순이익", ni, "ni")]

    trend = [(k, v) for k, v in past if k < cur_key][:4]
    if trend:
        lines.append("최근 추이 (매출/영업익/순이익)")
        for k, v in trend:
            lines.append(f" {_qlabel(k)} {_eok((v['rev'] or 0) * 1e8)}/"
                         f" {_eok(v['op'] * 1e8) if v['op'] is not None else '-'}/"
                         f" {_eok(v['ni'] * 1e8) if v['ni'] is not None else '-'}")
    if code:
        try:
            apast, acons = _naver_finance(code, "annual")
            lines.extend(_fin_table("연간", apast, acons, lambda k: f"FY{k[2:4]}", 3))
        except Exception:
            pass
    return "\n".join(lines)


# ─── 디스패치 ──────────────────────────────────────────────────────────────────

# 원문 파싱 계열: kind → (요약 함수, 후속 메시지 라벨, 이모지)
DOC_SUMMARIZERS: dict[str, tuple[object, str, str]] = {
    "earnings": (_sum_earnings, "실적요약", "📈"),
    "supply": (_sum_supply, "계약요약", "📝"),
    "ir": (_sum_ir, "IR요약", "🔔"),
    "repricing": (_sum_repricing, "리픽싱요약", "⚠️"),
    "asset": (_sum_asset, "자산양수도요약", "🚨"),
    "exercise": (_sum_exercise, "행사요약", "⚠️"),
}


def doc_kind(title: str) -> str | None:
    """원문 파싱으로 요약하는 서식인지 판별 (제목은 공백 제거 후)."""
    t = re.sub(r"\s+", "", title or "")
    if re.search(r"영업\(잠정\)실적", t):
        return "earnings"
    if re.search(r"단일판매|공급계약체결", t):
        return "supply"
    if re.search(r"기업설명회", t):
        return "ir"
    if re.search(r"가액의?조정", t):
        return "repricing"
    if re.search(r"유형자산(양[수도]|취득|처분)", t):
        return "asset"
    if re.search(r"(전환청구권|신주인수권|교환청구권)행사", t):
        return "exercise"
    return None


def summarizable(title: str) -> bool:
    """요약 수단이 있는 서식인지 — 지연 재시도 큐 대상 판별용."""
    if doc_kind(title):
        return True
    t = re.sub(r"\s+", "", title or "")
    return any(re.search(pat, t) for pat, _, _ in _RULES)

# (제목 정규식, DART API 이름, 요약 함수) — None API는 원문 파싱 계열
_RULES: list[tuple[str, str | None, object]] = [
    (r"자기주식취득신탁계약체결결정", "tsstkAqTrctrCnsDecsn", _sum_trust),
    (r"자기주식취득신탁계약해지결정", "tsstkAqTrctrCcDecsn", _sum_trust),
    (r"자기주식취득결정", "tsstkAqDecsn", _sum_treasury_buy),
    (r"자기주식처분결정", "tsstkDpDecsn", _sum_treasury_sell),
    (r"유무상증자결정", "pifricDecsn", _sum_rights_issue),
    (r"유상증자결정", "piicDecsn", _sum_rights_issue),
    (r"무상증자결정", "fricDecsn", _sum_bonus_issue),
    (r"감자결정", "crDecsn", _sum_capital_reduction),
    (r"전환사채권발행결정", "cvbdIsDecsn",
     lambda row, code, ak="", rn="": _sum_cb(row, code, "CB", ak, rn)),
    (r"신주인수권부사채권발행결정", "bdwtIsDecsn",
     lambda row, code, ak="", rn="": _sum_cb(row, code, "BW", ak, rn)),
    (r"교환사채권발행결정", "exbdIsDecsn",
     lambda row, code, ak="", rn="": _sum_cb(row, code, "EB", ak, rn)),
    (r"대량보유상황보고서", "majorstock", _sum_major_stock),
]


def _informative(s: str | None) -> bool:
    """숫자 정보가 사실상 없는 껍데기 요약은 발송하지 않는다."""
    return bool(s) and bool(re.search(r"\d{2,}|\d\s*[억조원주%회]", s))


def summarize(item: dict, api_key: str) -> str | None:
    """탐지된 공시 1건의 내용 요약 (실패 시 None — 알림은 요약 없이 나간다)."""
    title = re.sub(r"\s+", "", item.get("report_nm") or "")
    code = (item.get("stock_code") or "").strip()
    corp_code = item.get("corp_code") or ""
    rcept_no = item.get("rcept_no") or ""
    rcept_dt = item.get("rcept_dt") or ""

    result = None
    try:
        # 원문 파싱 계열 (잠정실적·공급계약·IR·리픽싱·행사 등)
        kind = doc_kind(title)
        if kind:
            fn = DOC_SUMMARIZERS[kind][0]
            result = fn(api_key, rcept_no, {"code": code, "corp_code": corp_code})
        # 주요사항보고서 계열 — 구조화 API
        elif corp_code and rcept_dt:
            cb_kinds = {"cvbdIsDecsn": "CB", "bdwtIsDecsn": "BW", "exbdIsDecsn": "EB"}
            for pat, api, fn in _RULES:
                if re.search(pat, title):
                    row = _pick(_dart_rows(api, api_key, corp_code, rcept_dt), rcept_no)
                    if row:
                        result = fn(row, code, api_key, rcept_no)
                    elif api in cb_kinds:  # 구조화 API 누락 시 원문 폴백
                        result = _sum_cb_doc(api_key, rcept_no, code, cb_kinds[api])
                    break
    except Exception as e:
        print(f"[요약 실패] {rcept_no} {title[:30]}: {type(e).__name__}: {e}")
    if _informative(result):
        return result
    # 규칙 파싱이 없거나 실패한 공시(철회·정정·합병·소송 등) — Gemini 폴백
    return _llm_summary(title, api_key, rcept_no)
