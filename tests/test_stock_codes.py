"""종목코드에 영문이 섞인 종목(2024년 이후 신규 상장주·일부 우선주)도 등록되고 감시 목록에 읽힌다.

11/9 사용자가 /보유추가 엠에스바이오(0013Y0)·티앤이코리아(0053F0)를 이름으로도 코드로도 등록하지 못했다 —
봇이 '숫자 6자리'만 종목으로 봤기 때문. 네트워크 없이 그 경로를 확인한다.
"""
import watch_bot as wb
import spike_watch as sw

LISTED = {            # 코드 → 이름 (가짜 네이버 응답 재료)
    "0013Y0": "엠에스바이오",
    "0053F0": "티앤이코리아",
    "394420": "리센스메디컬",
    "000660": "SK하이닉스",
    "0162Z0": "RISE 삼성전자SK하이닉스채권혼합50",
    "00104K": "CJ4우(전환)",
}


class _Resp:
    status_code = 200

    def __init__(self, data):
        self._data = data

    def json(self):
        return self._data

    def raise_for_status(self):
        return None


def fake_get(url, params=None, **kw):
    """자동완성은 이름·코드 부분일치(대소문자 무시), 시세는 코드 정확히 일치."""
    if "ac.stock.naver.com" in url:
        q = str((params or {}).get("q", "")).replace(" ", "").upper()
        items = [{"code": c, "name": n, "nationCode": "KOR"} for c, n in LISTED.items()
                 if q and (q in n.replace(" ", "").upper() or q == c)]
        items.append({"code": "TSLA", "name": q, "nationCode": "USA"})   # 해외 종목은 걸러져야 한다
        return _Resp({"items": items})
    code = url.rsplit("/", 1)[-1]
    return _Resp({"datas": [{"stockName": LISTED[code]}] if code in LISTED else []})


def setup(tmp_path, monkeypatch, get=fake_get):
    monkeypatch.setattr(wb, "HOLDINGS_FILE", tmp_path / "holdings.csv")
    monkeypatch.setattr(wb, "WATCHLIST_FILE", tmp_path / "watchlist.csv")
    monkeypatch.setattr(wb.requests, "get", get)


def test_norm_code():
    assert wb.norm_code("005930") == "005930"
    assert wb.norm_code(" 0013y0 ") == "0013Y0"
    assert wb.norm_code("00104K") == "00104K"
    for bad in ("", None, "12345", "1234567", "ABCDEF", "삼성전자", "00-930", "0013Y 0"):
        assert wb.norm_code(bad) == ""
    assert sw.norm_code("0053f0") == "0053F0" and sw.norm_code("삼성전자") == ""


def test_add_new_listing_by_name(tmp_path, monkeypatch):
    setup(tmp_path, monkeypatch)
    assert "엠에스바이오(0013Y0)" in wb.handle("/보유추가 엠에스바이오")
    assert "티앤이코리아(0053F0)" in wb.handle("/hold 티앤이코리아")
    assert wb.read_hd() == {"0013Y0": "엠에스바이오", "0053F0": "티앤이코리아"}


def test_add_new_listing_by_code(tmp_path, monkeypatch):
    setup(tmp_path, monkeypatch)
    assert "엠에스바이오(0013Y0)" in wb.handle("/보유추가 0013Y0")
    assert "티앤이코리아(0053F0)" in wb.handle("/보유추가 0053f0")          # 소문자로 쳐도 된다
    assert "CJ4우(전환)(00104K)" in wb.handle("/스파이크추가 00104K")        # 영문으로 끝나는 우선주
    assert "직접 적은 이름(0099Z0)" in wb.handle("/보유추가 0099Z0 직접 적은 이름")   # 코드+이름은 조회 없이 등록
    assert set(wb.read_hd()) == {"0013Y0", "0053F0", "0099Z0"}
    assert wb.read_wl() == {"00104K": "CJ4우(전환)"}


def test_saved_list_keeps_both_kinds_and_delete_works(tmp_path, monkeypatch):
    setup(tmp_path, monkeypatch)
    (tmp_path / "holdings.csv").write_text("394420,리센스메디컬\n0013Y0,엠에스바이오\n", encoding="utf-8")
    wb.handle("/보유추가 티앤이코리아")       # 저장할 때 영문 섞인 기존 줄이 지워지면 안 된다
    assert wb.read_hd() == {"394420": "리센스메디컬", "0013Y0": "엠에스바이오", "0053F0": "티앤이코리아"}
    assert "보유 3종목" in wb.handle("/보유목록") and "엠에스바이오(0013Y0)" in wb.handle("/보유목록")
    assert "보유 해제: 엠에스바이오(0013Y0)" in wb.handle("/보유삭제 0013y0")
    assert "보유 해제: 티앤이코리아(0053F0)" in wb.handle("/보유삭제 티앤이")
    assert wb.read_hd() == {"394420": "리센스메디컬"}


def test_name_match_ignores_case_and_spaces(tmp_path, monkeypatch):
    setup(tmp_path, monkeypatch)
    # 영문 섞인 코드의 ETF가 후보에 같이 나와도, 이름이 정확히 같은 종목을 고른다
    assert "SK하이닉스(000660)" in wb.handle("/보유추가 sk하이닉스")
    assert "엠에스바이오(0013Y0)" in wb.handle("/보유추가 엠에스 바이오")


def test_not_found_and_search_down_are_told_apart(tmp_path, monkeypatch):
    setup(tmp_path, monkeypatch)
    reply = wb.handle("/보유추가 없는회사이름")
    assert "찾지 못했습니다" in reply and "0013Y0" in reply      # 영문 섞인 코드도 된다고 안내
    assert "999999 종목을 찾지 못했습니다" in wb.handle("/보유추가 999999")
    assert wb.read_hd() == {}

    def boom(url, **kw):
        raise wb.requests.exceptions.ConnectionError("down")

    setup(tmp_path, monkeypatch, get=boom)
    assert "응답하지 않습니다" in wb.handle("/보유추가 엠에스바이오")
    assert wb.read_hd() == {}


def test_wrong_numeric_code_never_registers_a_lookalike(tmp_path, monkeypatch):
    def get(url, params=None, **kw):
        if "ac.stock.naver.com" in url:      # 없는 코드인데 검색이 비슷한 다른 종목을 돌려주는 경우
            return _Resp({"items": [{"code": "394420", "name": "리센스메디컬", "nationCode": "KOR"}]})
        return _Resp({"datas": []})

    setup(tmp_path, monkeypatch, get=get)
    assert "394421 종목을 찾지 못했습니다" in wb.handle("/보유추가 394421")
    assert wb.read_hd() == {}


def test_quotes_survive_a_stock_without_trades(monkeypatch):
    """한 종목의 거래량이 '-'(개장 직후 무거래·거래정지)여도 같은 묶음의 다른 종목 시세는 읽는다."""
    datas = [
        {"itemCode": "0013Y0", "stockName": "엠에스바이오", "closePrice": "5,510", "fluctuationsRatio": "-1.78",
         "accumulatedTradingVolume": "650,097", "accumulatedTradingValueRaw": "3600000000", "marketValueFullRaw": "120000000000"},
        {"itemCode": "950260", "stockName": "거래 없는 종목", "closePrice": "1,000", "fluctuationsRatio": "0.00",
         "accumulatedTradingVolume": "-", "accumulatedTradingValueRaw": None, "marketValueFullRaw": None},
        {"itemCode": "000001", "stockName": "값 없는 종목", "closePrice": "-", "fluctuationsRatio": "-", "accumulatedTradingVolume": "-"},
    ]
    monkeypatch.setattr(sw.requests, "get", lambda url, **kw: _Resp({"datas": datas}))
    quotes = sw.fetch_quotes(["0013Y0", "950260", "000001"])
    assert quotes["0013Y0"] == {"name": "엠에스바이오", "price": 5510.0, "rate": -1.78, "volume": 650097.0,
                                "value": 3.6e9, "mcap": 1.2e11}
    assert quotes["950260"]["price"] == 1000.0 and quotes["950260"]["volume"] == 0.0
    assert "000001" not in quotes        # 가격이 없는 종목만 빠진다


def test_spike_watch_reads_alnum_codes(tmp_path, monkeypatch):
    hf, wf = tmp_path / "holdings.csv", tmp_path / "watchlist.csv"
    hf.write_text("394420,리센스메디컬\n0013y0,엠에스바이오\n12345,자릿수 부족\nABCDEF,코드 아님\n", encoding="utf-8")
    wf.write_text("0053F0,티앤이코리아\n", encoding="utf-8")
    monkeypatch.setenv("HOLDINGS_FILE", str(hf))
    monkeypatch.setenv("WATCHLIST_FILE", str(wf))
    monkeypatch.setenv("WATCHLIST", "00104K:CJ4우(전환),005930:삼성전자")
    assert sw.read_holdings() == {"394420": "리센스메디컬", "0013Y0": "엠에스바이오"}
    wl = sw.read_watchlist()
    assert wl["0053F0"] == "티앤이코리아" and wl["00104K"] == "CJ4우(전환)" and wl["005930"] == "삼성전자"
