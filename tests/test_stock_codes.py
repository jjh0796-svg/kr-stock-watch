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


def _rank_row(code, name, kind="stock"):
    return {"itemCode": code, "stockName": name, "stockEndType": kind}


def test_amount_rank_takes_top_stocks_only(monkeypatch):
    """거래대금 순위(priceTop)에서 ETF·ETN을 빼고 시장별 주식 AMOUNT_N개 — 영문 섞인 코드도 포함."""
    calls = []
    kospi = ([_rank_row("069500", "KODEX 200", "etf"), _rank_row("005930", "삼성전자"), _rank_row("0126Z0", "삼성에피스홀딩스"),
              _rank_row("122630", "KODEX 레버리지", "stock")]            # 종류 표시가 틀려도 이름으로 한 번 더 거른다
             + [_rank_row(f"1{i:05d}", f"코스피{i}") for i in range(30)])
    kosdaq = [_rank_row("0013Y0", "엠에스바이오"), _rank_row("", "코드 없음"), _rank_row("580001", "어떤 ETN", "etn")]

    def get(url, params=None, **kw):
        calls.append((url, dict(params or {})))
        return _Resp({"stocks": kospi if url.endswith("/KOSPI") else kosdaq})

    monkeypatch.setattr(sw.requests, "get", get)
    codes = sw.fetch_amount_rank()
    assert [u.rsplit("/", 2)[-2:] for u, _ in calls] == [["priceTop", "KOSPI"], ["priceTop", "KOSDAQ"]]
    assert all(p["pageSize"] == sw.AMOUNT_FETCH for _, p in calls)
    assert {"005930", "0126Z0", "0013Y0"} <= codes
    assert not {"069500", "122630", "580001", ""} & codes
    assert len(codes) == sw.AMOUNT_N + 1        # 코스피 20(삼성전자·삼성에피스 + 18) + 코스닥 1
    assert "100017" in codes and "100018" not in codes


def test_amount_rank_reads_second_page_when_etfs_fill_the_first(monkeypatch):
    calls = []
    page1 = [_rank_row(f"3{i:05d}", f"KODEX 상품{i}", "etf") for i in range(sw.AMOUNT_FETCH - 5)] + \
            [_rank_row(f"0{i:05d}", f"첫쪽주식{i}") for i in range(5)]
    page2 = [_rank_row(f"2{i:05d}", f"둘째쪽주식{i}") for i in range(sw.AMOUNT_FETCH)]

    def get(url, params=None, **kw):
        calls.append((url.rsplit("/", 1)[-1], params["page"]))
        if url.endswith("/KOSDAQ"):
            return _Resp({"stocks": []})
        return _Resp({"stocks": page1 if params["page"] == 1 else page2})

    monkeypatch.setattr(sw.requests, "get", get)
    codes = sw.fetch_amount_rank()
    assert calls == [("KOSPI", 1), ("KOSPI", 2), ("KOSDAQ", 1)]      # 코스닥은 첫 쪽이 덜 찼으면 더 묻지 않는다
    assert len(codes) == sw.AMOUNT_N
    assert {f"0{i:05d}" for i in range(5)} <= codes and "200014" in codes and "200015" not in codes


def test_amount_rank_empty_when_source_fails(monkeypatch):
    def boom(url, **kw):
        raise sw.requests.exceptions.ConnectionError("down")

    monkeypatch.setattr(sw.requests, "get", boom)
    assert sw.fetch_amount_rank() == set()


def test_market_avg_cache_refresh():
    """20일 평균 거래량 캐시: 지난 날짜 것은 버리고, 한 회차 조회 수를 제한하고, 실패는 하루 3번까지만 다시 묻는다."""
    asked = []

    def lookup(code):
        asked.append(code)
        return 0 if code == "000002" else 1000.0

    mvol = {"999999": {"avg": 5.0, "asof": "20260910"}, "000001": {"avg": 700.0, "asof": "20261116"}}
    assert sw.refresh_market_avg({"000001", "000002", "000003"}, mvol, "20261116", lookup) is True
    assert "999999" not in mvol                                   # 두 달 전 것은 버린다
    assert asked == ["000002", "000003"]                           # 오늘 이미 있는 000001은 다시 묻지 않는다
    assert mvol["000001"]["avg"] == 700.0 and mvol["000003"]["avg"] == 1000.0
    assert mvol["000002"] == {"avg": 0, "asof": "20261116", "tries": 1}

    for _ in range(4):                                             # 조회가 안 되는 종목은 3번까지만
        sw.refresh_market_avg({"000001", "000002", "000003"}, mvol, "20261116", lookup)
    assert asked.count("000002") == 3 and mvol["000002"]["tries"] == 3
    assert sw.refresh_market_avg({"000001", "000002", "000003"}, mvol, "20261116", lookup) is False

    asked.clear()                                                  # 한 회차에 AVG_LOOKUPS종목까지만
    many = {f"{i:06d}" for i in range(100, 140)}
    fresh = {}
    sw.refresh_market_avg(many, fresh, "20261116", lookup)
    assert len(asked) == sw.AVG_LOOKUPS and len(fresh) == sw.AVG_LOOKUPS
    sw.refresh_market_avg(many, fresh, "20261116", lookup)
    sw.refresh_market_avg(many, fresh, "20261116", lookup)
    assert set(fresh) == many                                      # 세 회차면 40종목이 다 찬다


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
