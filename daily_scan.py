# ==========================================
# 📊 마감 후 데일리 스캔 (아이디어 4·5·6·7)
#   4) 외국인 연속 순매수 + 기관 동반 (네이버 종목별 매매동향)
#   5) 공매도 급증 — 관심종목 (pykrx + KRX 로그인, 실패 시 사유 보고)
#   6) 신용융자 잔고 추이 (금융투자협회 KOFIA)
#   7) 52주 신고가/신저가 돌파 + 거래량 (KRX OpenAPI + 다음증권 52주 고저)
#
# 스케줄: 평일 19시대 실행. KRX OpenAPI에 당일 데이터가 아직 없으면
#   다음날 아침 백업 실행이 전일 기준으로 처리(기준일 중복실행 방지 상태 보관).
# ==========================================
import os
import re
import time
from datetime import datetime, timedelta

import requests

from common import UA_HEADERS, effective_watchlist, esc, load_state, now_kst, save_state, tg_send_long

KRX_AUTH_KEY = os.environ.get("KRX_AUTH_KEY", "")
KRX_STO_URL = "http://data-dbg.krx.co.kr/svc/apis/sto/{api}"

STATE_FILE = "daily_scan.json"

# 조정 가능한 임계값 (환경변수로 덮어쓰기 가능)
FLOW_UNIVERSE = int(os.environ.get("FLOW_UNIVERSE", "350"))        # 수급 검사 대상: 거래대금 상위 N
FLOW_STREAK = int(os.environ.get("FLOW_STREAK", "5"))              # 외국인 연속 순매수 일수
FLOW_MIN_VALUE = float(os.environ.get("FLOW_MIN_VALUE", "3e9"))    # 누적 순매수 최소금액(원) ≈ 30억
HIGH_VOL_MULT = float(os.environ.get("HIGH_VOL_MULT", "3.0"))      # 신고가: 거래량 20일 평균 대비 배수
HIGH_MIN_TRDVAL = float(os.environ.get("HIGH_MIN_TRDVAL", "3e9"))  # 신고가 후보 최소 거래대금
LOW_MIN_TRDVAL = float(os.environ.get("LOW_MIN_TRDVAL", "1e9"))    # 신저가 후보 최소 거래대금
SHORT_RATIO_MULT = float(os.environ.get("SHORT_RATIO_MULT", "2.0"))  # 공매도 비중 평균 대비 배수
SHORT_RATIO_MIN = float(os.environ.get("SHORT_RATIO_MIN", "3.0"))    # 공매도 비중 최소값(%)
CREDIT_SPIKE_PCT = float(os.environ.get("CREDIT_SPIKE_PCT", "2.0"))  # 신용잔고 5일 증가율 경고(%)

EXCLUDE_NAME_RE = re.compile(r"스팩|[0-9]*우(B|C)?$|우\(전환\)$|ETN")


def eok(v: float) -> str:
    return f"{v / 1e8:,.0f}억"


# ─── KRX OpenAPI: 전종목 일별 시세 ─────────────────────────────────────────────

def krx_daily(bas_dd: str) -> list[dict]:
    """KOSPI+KOSDAQ 전종목 일별 시세. 휴장일이면 빈 목록."""
    rows: list[dict] = []
    for api in ("stk_bydd_trd", "ksq_bydd_trd"):
        r = requests.get(KRX_STO_URL.format(api=api), params={"basDd": bas_dd},
                         headers={"AUTH_KEY": KRX_AUTH_KEY, "Accept": "application/json"},
                         timeout=25)
        r.raise_for_status()
        rows.extend(r.json().get("OutBlock_1", []))
        time.sleep(0.4)
    # 휴장일엔 시가/고가/저가가 전부 0으로 오는 경우가 있어 걸러낸다
    if rows and all(float(x.get("TDD_HGPRC", "0").replace(",", "") or 0) == 0 for x in rows[:50]):
        return []
    return rows


def find_base_date() -> tuple[str, list[dict]] | tuple[None, None]:
    """오늘부터 거슬러 올라가며 데이터가 있는 최근 거래일을 찾는다."""
    d = now_kst()
    for _ in range(7):
        if d.weekday() < 5:
            bas = d.strftime("%Y%m%d")
            try:
                rows = krx_daily(bas)
            except Exception as e:
                print(f"[KRX] {bas} 조회 실패: {e}")
                rows = []
            if rows:
                return bas, rows
        d -= timedelta(days=1)
    return None, None


def volume_history(base_dd: str, days: int = 20) -> dict[str, list[float]]:
    """기준일 이전 최대 `days` 거래일의 종목별 거래량 이력."""
    hist: dict[str, list[float]] = {}
    d = datetime.strptime(base_dd, "%Y%m%d") - timedelta(days=1)
    collected = 0
    for _ in range(45):
        if collected >= days:
            break
        if d.weekday() < 5:
            try:
                rows = krx_daily(d.strftime("%Y%m%d"))
            except Exception:
                rows = []
            if rows:
                for row in rows:
                    code = row.get("ISU_CD", "")
                    vol = float(row.get("ACC_TRDVOL", "0").replace(",", "") or 0)
                    hist.setdefault(code, []).append(vol)
                collected += 1
        d -= timedelta(days=1)
    return hist


# ─── 7) 52주 신고가/신저가 (다음증권 52주 고저로 확인) ────────────────────────

def daum_52w(code: str) -> dict | None:
    try:
        r = requests.get(f"https://finance.daum.net/api/quotes/A{code}",
                         params={"summary": "false"},
                         headers={**UA_HEADERS, "Referer": f"https://finance.daum.net/quotes/A{code}"},
                         timeout=10)
        if r.status_code != 200:
            return None
        return r.json()
    except Exception:
        return None


def scan_52w(base_dd: str, rows: list[dict], vol_hist: dict[str, list[float]]) -> tuple[list[str], list[str]]:
    base_iso = f"{base_dd[:4]}-{base_dd[4:6]}-{base_dd[6:]}"
    highs: list[tuple[float, str]] = []
    lows: list[tuple[float, str]] = []

    def parsed(row):
        return {
            "code": row.get("ISU_CD", ""),
            "name": row.get("ISU_NM", ""),
            "close": float(row.get("TDD_CLSPRC", "0").replace(",", "") or 0),
            "fluc": float(row.get("FLUC_RT", "0").replace(",", "") or 0),
            "vol": float(row.get("ACC_TRDVOL", "0").replace(",", "") or 0),
            "val": float(row.get("ACC_TRDVAL", "0").replace(",", "") or 0),
        }

    hi_cands, lo_cands = [], []
    for row in rows:
        p = parsed(row)
        if not p["code"] or EXCLUDE_NAME_RE.search(p["name"]) or p["close"] < 1000:
            continue
        h = vol_hist.get(p["code"], [])
        avg_vol = sum(h) / len(h) if len(h) >= 10 else None
        if p["fluc"] > 0 and p["val"] >= HIGH_MIN_TRDVAL and avg_vol and p["vol"] >= HIGH_VOL_MULT * avg_vol:
            hi_cands.append(p)
        elif p["fluc"] < 0 and p["val"] >= LOW_MIN_TRDVAL and avg_vol and p["vol"] >= 2.0 * avg_vol:
            lo_cands.append(p)

    hi_cands.sort(key=lambda x: -x["val"])
    lo_cands.sort(key=lambda x: -x["val"])

    for p in hi_cands[:35]:
        q = daum_52w(p["code"])
        time.sleep(0.15)
        if q and q.get("high52wDate") == base_iso:
            highs.append((p["val"], f" • {esc(p['name'])} ({p['code']}) {p['close']:,.0f} "
                                    f"+{p['fluc']:.1f}% · 대금 {eok(p['val'])}"))
    for p in lo_cands[:25]:
        q = daum_52w(p["code"])
        time.sleep(0.15)
        if q and q.get("low52wDate") == base_iso:
            lows.append((p["val"], f" • {esc(p['name'])} ({p['code']}) {p['close']:,.0f} "
                                   f"{p['fluc']:.1f}% · 대금 {eok(p['val'])}"))

    highs.sort(key=lambda x: -x[0])
    lows.sort(key=lambda x: -x[0])
    return [s for _, s in highs[:12]], [s for _, s in lows[:8]]


# ─── 4) 외국인 연속 순매수 + 기관 동반 (네이버 매매동향) ──────────────────────

def _num(s) -> float:
    try:
        return float(str(s).replace(",", "").replace("+", ""))
    except (ValueError, TypeError):
        return 0.0


def naver_trend(code: str, size: int = 10) -> list[dict]:
    r = requests.get(f"https://m.stock.naver.com/api/stock/{code}/trend",
                     params={"pageSize": size, "page": 1},
                     headers=UA_HEADERS, timeout=10)
    d = r.json()
    return d if isinstance(d, list) else []


def scan_flow(base_dd: str, rows: list[dict]) -> tuple[list[str], str]:
    universe = []
    for row in rows:
        name = row.get("ISU_NM", "")
        code = row.get("ISU_CD", "")
        val = float(row.get("ACC_TRDVAL", "0").replace(",", "") or 0)
        if code and not EXCLUDE_NAME_RE.search(name):
            universe.append((val, code, name))
    universe.sort(key=lambda x: -x[0])
    universe = universe[:FLOW_UNIVERSE]

    results: list[tuple[float, str]] = []
    flow_date = None
    for _, code, name in universe:
        try:
            trend = naver_trend(code)
        except Exception:
            continue
        time.sleep(0.12)
        if not trend:
            continue
        latest = trend[0]
        if flow_date is None:
            flow_date = latest.get("bizdate")
        streak, acc_value = 0, 0.0
        for t in trend:
            fq = _num(t.get("foreignerPureBuyQuant"))
            if fq <= 0:
                break
            streak += 1
            acc_value += fq * _num(t.get("closePrice"))
        organ_today = _num(latest.get("organPureBuyQuant"))
        if streak >= FLOW_STREAK and organ_today > 0 and acc_value >= FLOW_MIN_VALUE:
            organ_val = organ_today * _num(latest.get("closePrice"))
            results.append((acc_value,
                            f" • {esc(name)} ({code}) 외인 {streak}일 연속 "
                            f"~{eok(acc_value)} · 당일 기관 +{eok(organ_val)}"))

    results.sort(key=lambda x: -x[0])
    note = ""
    if flow_date and flow_date != base_dd:
        note = f" (수급 기준일 {flow_date[4:6]}/{flow_date[6:]})"
    return [s for _, s in results[:15]], note


# ─── 5) 공매도 급증 — 관심종목 (pykrx, KRX 로그인 필요) ────────────────────────

_KRX_LOGIN_BASE = "https://data.krx.co.kr/contents/MDC/COMS/client"


def krx_login_diag() -> str | None:
    """pykrx와 같은 흐름으로 로그인을 시도해 실패 사유를 돌려준다 (성공이면 None).
    비밀번호는 어떤 경우에도 출력하지 않는다 — 오류 코드/메시지만."""
    kid, kpw = os.environ.get("KRX_ID", ""), os.environ.get("KRX_PW", "")
    if not (kid and kpw):
        return "KRX_ID/KRX_PW 시크릿 미설정"
    try:
        s = requests.Session()
        ua = {"User-Agent": UA_HEADERS["User-Agent"]}
        s.get(f"{_KRX_LOGIN_BASE}/MDCCOMS001.cmd", headers=ua, timeout=15)
        s.get(f"{_KRX_LOGIN_BASE}/view/login.jsp?site=mdc",
              headers={**ua, "Referer": f"{_KRX_LOGIN_BASE}/MDCCOMS001.cmd"}, timeout=15)
        payload = {"mbrNm": "", "telNo": "", "di": "", "certType": "",
                   "mbrId": kid, "pw": kpw}
        r = s.post(f"{_KRX_LOGIN_BASE}/MDCCOMS001D1.cmd", data=payload,
                   headers={**ua, "Referer": f"{_KRX_LOGIN_BASE}/MDCCOMS001.cmd"},
                   timeout=15)
        d = r.json()
        code = d.get("_error_code", "")
        if code in ("CD001", "CD011"):   # 정상 / 중복 로그인(무시 가능)
            return None
        return f"{code} {d.get('_error_message', '')}".strip()
    except Exception as e:
        return f"로그인 진단 실패: {type(e).__name__}"


def scan_short(base_dd: str, watch: dict[str, str]) -> tuple[list[str], str | None]:
    if not watch:
        return [], "관심종목 없음"
    if not (os.environ.get("KRX_ID") and os.environ.get("KRX_PW")):
        # 2026-08 KRX가 로그인에 nProtect 암호화를 도입해 봇 로그인이 막힘(CD006).
        # 실패 시도가 쌓이면 계정이 잠기므로 시크릿을 빼고 이 섹션은 휴면 처리.
        return [], "휴면 — KRX 로그인 자동화 중단(사이트 보안정책 변경)"
    diag = krx_login_diag()
    if diag:
        return [], f"KRX 로그인 불가: {diag}"
    try:
        from pykrx import stock  # import 시 KRX 로그인 시도 — 실패해도 다른 섹션은 살린다
    except Exception as e:
        return [], f"pykrx 로드 실패({type(e).__name__}: {str(e)[:120]})"

    frm = (now_kst() - timedelta(days=45)).strftime("%Y%m%d")
    out: list[str] = []
    errors = 0
    for code, name in watch.items():
        try:
            dfv = stock.get_shorting_volume_by_date(frm, base_dd, code)
            if dfv is not None and len(dfv) >= 6:
                ratios = dfv["비중"].astype(float)
                latest = float(ratios.iloc[-1])
                avg = float(ratios.iloc[:-1].tail(20).mean())
                if latest >= max(SHORT_RATIO_MULT * avg, SHORT_RATIO_MIN):
                    out.append(f" • {esc(name)} ({code}) 공매도 비중 {latest:.1f}% "
                               f"(20일 평균 {avg:.1f}%) — {dfv.index[-1]:%m/%d}")
            dfb = stock.get_shorting_balance_by_date(frm, base_dd, code)
            if dfb is not None and len(dfb) >= 6:
                ratios = dfb["비중"].astype(float)
                latest, prev5 = float(ratios.iloc[-1]), float(ratios.iloc[-6])
                if latest >= 2.0 and latest - prev5 >= 0.3:
                    out.append(f" • {esc(name)} ({code}) 공매도 잔고 {latest:.2f}% "
                               f"(5일 전 {prev5:.2f}%) — {dfb.index[-1]:%m/%d}")
            time.sleep(0.5)
        except Exception:
            errors += 1
    err_note = f"{errors}/{len(watch)}종목 조회 실패" if errors else None
    return out, err_note


# ─── 6) 신용융자 잔고 (KOFIA) ──────────────────────────────────────────────────

def scan_credit() -> str | None:
    frm = (now_kst() - timedelta(days=550)).strftime("%Y%m%d")
    to = now_kst().strftime("%Y%m%d")
    payload = {"dmSearch": {"tmpV40": "1000000000", "tmpV41": "1", "tmpV1": "D",
                            "tmpV45": frm, "tmpV46": to,
                            "OBJ_NM": "STATSCU0100000070BO"}}
    r = requests.post("https://freesis.kofia.or.kr/meta/getMetaDataList.do",
                      json=payload,
                      headers={**UA_HEADERS, "Referer": "https://freesis.kofia.or.kr/",
                               "Content-Type": "application/json"},
                      timeout=25)
    rows = r.json().get("ds1", [])
    if not rows:
        return None
    # 최신순 정렬 (TMPV1=날짜, TMPV2=신용융자 계, TMPV3=유가, TMPV4=코스닥 — 단위 십억)
    rows.sort(key=lambda x: x["TMPV1"], reverse=True)
    total = [float(x["TMPV2"]) for x in rows]
    latest, date = total[0], rows[0]["TMPV1"]
    jo = lambda v: f"{v / 1000:.1f}조"

    def delta(n: int) -> float:
        return (latest / total[n] - 1) * 100 if len(total) > n and total[n] else 0.0

    d1, d5, d20 = delta(1), delta(5), delta(20)
    yr_max = max(total[:250]) if len(total) >= 2 else latest
    flags = []
    if latest >= yr_max:
        flags.append("🔺 1년 신고점")
    if d5 >= CREDIT_SPIKE_PCT:
        flags.append(f"⚠️ 5일 +{d5:.1f}% 급증")
    flag_txt = " " + " ".join(flags) if flags else ""
    return (f" 총 {jo(latest)} (유가 {jo(float(rows[0]['TMPV3']))} · "
            f"코스닥 {jo(float(rows[0]['TMPV4']))})\n"
            f" 전일 {d1:+.2f}% · 5일 {d5:+.2f}% · 20일 {d20:+.2f}%{flag_txt}\n"
            f" 기준일 {date[4:6]}/{date[6:]}")


# ─── 7) pykrx 신버전 감시 ──────────────────────────────────────────────────────
# KRX가 로그인에 nProtect 암호화를 도입해(2026-08) pykrx 로그인이 막혔다.
# 보완 버전이 나오면 공매도 섹션 부활을 검토할 수 있게 PyPI 배포를 감시한다.
# 새 버전이 나와도 자동 복귀는 하지 않는다 — 로그인 우회 방식이면 계정 잠금 재발 위험.

def check_pykrx_release(state: dict) -> str | None:
    r = requests.get("https://pypi.org/pypi/pykrx/json", timeout=15)
    latest = r.json()["info"]["version"]
    seen = state.get("pykrx_seen_version")
    state["pykrx_seen_version"] = latest
    if seen and latest != seen:
        return latest
    return None


# ─── 조립 ──────────────────────────────────────────────────────────────────────

def main() -> None:
    if not KRX_AUTH_KEY:
        raise SystemExit("KRX_AUTH_KEY 환경변수가 필요합니다")
    watch = effective_watchlist()  # 시드 + 텔레그램 /추가·/삭제 반영본
    state = load_state(STATE_FILE, {})

    base_dd, rows = find_base_date()
    if not base_dd:
        print("최근 거래일 데이터를 찾지 못함 — 종료")
        return
    if state.get("last_scanned") == base_dd and not os.environ.get("FORCE_RESCAN"):
        print(f"{base_dd} 이미 스캔함 — 종료")
        return
    print(f"기준일 {base_dd} · 종목 {len(rows)}개")

    sections: list[str] = []
    head_date = f"{base_dd[:4]}-{base_dd[4:6]}-{base_dd[6:]}"
    sections.append(f"📊 <b>마감 스캔</b> · {head_date}")

    try:
        vol_hist = volume_history(base_dd)
        highs, lows = scan_52w(base_dd, rows, vol_hist)
        sections.append(f"\n🏔 <b>52주 신고가 돌파</b> (거래량 {HIGH_VOL_MULT:.0f}배↑)\n"
                        + ("\n".join(highs) if highs else " • 해당 없음"))
        sections.append("\n🧊 <b>52주 신저가</b> (거래량 2배↑)\n"
                        + ("\n".join(lows) if lows else " • 해당 없음"))
    except Exception as e:
        sections.append(f"\n⚠️ 52주 스캔 실패: {type(e).__name__}: {str(e)[:100]}")

    try:
        flows, note = scan_flow(base_dd, rows)
        sections.append(f"\n🤝 <b>외국인 {FLOW_STREAK}일↑ 연속 순매수 + 기관 동반</b>"
                        f" (거래대금 상위 {FLOW_UNIVERSE}){note}\n"
                        + ("\n".join(flows) if flows else " • 해당 없음"))
    except Exception as e:
        sections.append(f"\n⚠️ 수급 스캔 실패: {type(e).__name__}: {str(e)[:100]}")

    try:
        shorts, err = scan_short(base_dd, watch)
        body = "\n".join(shorts) if shorts else " • 해당 없음"
        if err:
            body += f"\n ⚠️ {err}"
        sections.append(f"\n🩳 <b>공매도 급증</b> (관심종목 {len(watch)}개)\n{body}")
    except Exception as e:
        sections.append(f"\n⚠️ 공매도 스캔 실패: {type(e).__name__}: {str(e)[:100]}")

    try:
        credit = scan_credit()
        sections.append("\n💳 <b>신용융자 잔고</b> (KOFIA)\n" + (credit or " • 데이터 없음"))
    except Exception as e:
        sections.append(f"\n⚠️ 신용잔고 조회 실패: {type(e).__name__}: {str(e)[:100]}")

    try:
        new_ver = check_pykrx_release(state)
        if new_ver:
            sections.append(f"\n🔔 <b>pykrx {new_ver} 배포 감지</b>\n"
                            " • KRX 로그인(nProtect) 대응 여부 확인 후 공매도 섹션 부활 검토")
    except Exception:
        pass

    msg = "\n".join(sections)
    if os.environ.get("DRY_RUN"):
        print("DRY_RUN — 텔레그램 발송·상태 저장 생략. 메시지 미리보기:\n" + msg)
        return
    tg_send_long(msg)
    state["last_scanned"] = base_dd
    save_state(STATE_FILE, state)
    print("발송 완료")


if __name__ == "__main__":
    main()
