"""10/12: 마감 스캔 — 아침 정식(KRX) / 전날 저녁 예비본(네이버) / 옛 날짜 차단."""
from datetime import datetime, timedelta, timezone
from unittest.mock import patch, Mock

import daily_scan as d

KST = timezone(timedelta(hours=9))


def run_main(state, *, krx, expected="20261008", now="0735", report_ok=True, env=None):
    sent, saved = [], []
    env = {"KRX_AUTH_KEY": "x", **(env or {})}
    with patch.object(d, "KRX_AUTH_KEY", "x"), \
         patch.object(d, "effective_watchlist", return_value={}), \
         patch.object(d, "load_state", return_value=state), \
         patch.object(d, "save_state", side_effect=lambda f, s: saved.append(dict(s))), \
         patch.object(d, "latest_trading_day", return_value=expected), \
         patch.object(d, "find_base_date", side_effect=krx) as find, \
         patch.object(d, "build_report", side_effect=lambda b, r, w, s: (f"📊 <b>마감 스캔</b> · {b}\n본문", report_ok)), \
         patch.object(d, "tg_send_long", side_effect=sent.append), \
         patch.object(d.time, "sleep"), \
         patch.object(d, "now_kst", return_value=datetime(2026, 10, 12, int(now[:2]), int(now[2:]), tzinfo=KST)), \
         patch.dict(d.os.environ, env, clear=False):
        for k in ("SCAN_MODE", "FORCE_RESCAN", "DRY_RUN"):
            if k not in env:
                d.os.environ.pop(k, None)
        d.main()
    return sent, saved, find


def test_fresh_krx_sends_official_scan():
    state = {"last_scanned": "20261007"}
    sent, saved, _ = run_main(state, krx=lambda: ("20261008", [{}]))
    assert sent == ["📊 <b>마감 스캔</b> · 20261008\n본문"] and state["last_scanned"] == "20261008"


def test_stale_krx_never_sends_older_day_without_backup():
    # 10/12 사고: KRX가 10/8을 비워 10/7을 집었다 — 이제는 보내지 않는다
    state = {"last_scanned": "20261007"}
    sent, _, find = run_main(state, krx=lambda: ("20261007", [{}]), env={"DATA_WAIT_TRIES": "3"})
    assert sent == [] and state["last_scanned"] == "20261007" and find.call_count == 3


def test_older_than_last_scanned_is_skipped():
    state = {"last_scanned": "20261008"}
    sent, _, find = run_main(state, krx=lambda: ("20261007", [{}]))
    assert sent == [] and find.call_count == 0          # 이미 보낸 기준일 — KRX도 안 묻는다


def test_stale_krx_sends_prepared_after_fallback_time():
    state = {"last_scanned": "20261007",
             "prepared": {"base": "20261008", "text": "📊 <b>마감 스캔</b> · 2026-10-08\n52주…", "made_at": "x"}}
    sent, _, find = run_main(state, krx=lambda: ("20261007", [{}]), now="0821", env={"DATA_WAIT_TRIES": "14"})
    assert find.call_count == 1                          # 08:20이 지나 바로 예비본으로
    assert len(sent) == 1 and sent[0].startswith("📊 <b>마감 스캔</b> · 2026-10-08\n🗂 <i>예비본")
    assert sent[0].endswith("52주…") and state["last_scanned"] == "20261008"


def test_prepared_for_other_day_is_not_used():
    state = {"last_scanned": "20261006", "prepared": {"base": "20261007", "text": "옛 예비본", "made_at": "x"}}
    sent, _, _ = run_main(state, krx=lambda: ("20261007", [{}]), now="0821", env={"DATA_WAIT_TRIES": "2"})
    assert sent == [] and state["last_scanned"] == "20261006"


def test_official_52w_failure_uses_prepared():
    state = {"last_scanned": "20261007", "prepared": {"base": "20261008", "text": "📊 h\n예비 본문", "made_at": "x"}}
    sent, _, _ = run_main(state, krx=lambda: ("20261008", [{}]), report_ok=False)
    assert len(sent) == 1 and "예비 본문" in sent[0] and "예비본" in sent[0]


def test_prepare_mode_saves_without_sending():
    state = {"last_scanned": "20261007"}
    with patch.object(d, "naver_daily_rows", return_value=[{}] * 2000):
        sent, saved, find = run_main(state, krx=lambda: (None, None), expected="20261012",
                                     env={"SCAN_MODE": "prepare"})
    assert sent == [] and find.call_count == 0
    assert state["prepared"]["base"] == "20261012" and state["prepared"]["text"].startswith("📊")
    assert state["last_scanned"] == "20261007"


def test_prepare_mode_skips_holiday():
    state = {}
    with patch.object(d, "naver_daily_rows") as rows:
        sent, _, _ = run_main(state, krx=lambda: (None, None), expected="20261008", env={"SCAN_MODE": "prepare"})
    rows.assert_not_called()
    assert "prepared" not in state and sent == []


def test_hangeul_won():
    assert d._hangeul_won("1조 2,345억원") == 1.2345e12
    assert d._hangeul_won("3,048억원") == 3.048e11
    assert d._hangeul_won("-") is None


def test_naver_daily_rows_mapping():
    def stock(code, end="stock", trend="RISING", ratio="2.50", day="2026-10-12"):
        return {"itemCode": code, "stockName": f"종목{code}", "stockEndType": end, "closePrice": "16,710",
                "fluctuationsRatio": ratio, "compareToPreviousPrice": {"name": trend},
                "accumulatedTradingVolume": "1,234", "accumulatedTradingValue": "3,048",
                "accumulatedTradingValueKrwHangeul": "30억 4,800만원", "localTradedAt": f"{day}T15:30:00+09:00"}
    pages = {"KOSPI": [stock("000001"), stock("000002", trend="FALLING", ratio="1.20"), stock("000003", end="etf"),
                       stock("000004", day="2026-10-08")] + [stock(f"1{i:05d}") for i in range(900)],
             "KOSDAQ": [stock(f"2{i:05d}") for i in range(900)]}

    def fake_get(url, params, **_):
        mkt = url.rsplit("/", 1)[1]
        items = pages[mkt][(params["page"] - 1) * 100: params["page"] * 100]
        return Mock(status_code=200, raise_for_status=lambda: None,
                    json=lambda: {"totalCount": len(pages[mkt]), "stocks": items})
    with patch.object(d.requests, "get", side_effect=fake_get), patch.object(d.time, "sleep"):
        rows = d.naver_daily_rows("20261012")
    by = {r["ISU_CD"]: r for r in rows}
    assert "000003" not in by and "000004" not in by          # ETF·다른 날짜 제외
    assert by["000001"]["FLUC_RT"] == "2.5" and by["000002"]["FLUC_RT"] == "-1.2"
    assert by["000001"]["ACC_TRDVAL"] == "3048000000" and by["000001"]["_mkt"] == "코스피"
    assert by["200000"]["_mkt"] == "코스닥" and len(rows) == 1802
