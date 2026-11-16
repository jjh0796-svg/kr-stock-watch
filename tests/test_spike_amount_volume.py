"""스파이크 감시 한 회차(tick)를 통째로 돌려 '거래대금 상위 → 거래량 폭발' 알림을 확인한다 — 네트워크·발송 없이.

이 알림은 네이버가 옛 화면 주소를 옮기면서 9/11~11/16 두 달 동안 조용히 멈춰 있었다(11/16 복구).
"""
import datetime as real
import json
import types

import spike_watch as sw

DAY = "20261116"


def quote(name, rate, volume, price=10000.0, value=5e10, mcap=5e11):
    return {"name": name, "price": price, "rate": rate, "volume": volume, "value": value, "mcap": mcap}


class Bench:
    """tick()이 바깥에서 받아 오는 것들을 전부 가짜로 바꾼다."""

    def __init__(self, tmp_path, monkeypatch):
        self.mp, self.state = monkeypatch, tmp_path / "state"
        self.sent, self.quotes, self.amount, self.ranked = [], {}, set(), {}
        self.holdings, self.avg = {}, {}
        mp = monkeypatch
        mp.setattr(sw, "STATE_DIR", self.state)
        mp.setattr(sw, "read_holdings", lambda: dict(self.holdings))
        mp.setattr(sw, "read_watchlist", lambda: {})
        mp.setattr(sw, "read_morning_candidates", lambda: {})
        mp.setattr(sw, "read_handoff", lambda: {})
        mp.setattr(sw, "fetch_rank", lambda sort, market, n=20, page=1: list(self.ranked.get((sort, market), [])))
        mp.setattr(sw, "fetch_amount_rank", lambda: set(self.amount))
        mp.setattr(sw, "naver_avg20", lambda code: self.avg.get(code, 0))
        mp.setattr(sw, "fetch_quotes", lambda codes: {c: self.quotes[c] for c in codes if c in self.quotes})
        mp.setattr(sw, "send", lambda lines, reply_to=None: self.sent.append(list(lines)) or len(self.sent))
        mp.setattr(sw, "why_line", lambda code, name="": "")
        mp.setattr(sw, "theme_lines", lambda sigs: [])
        mp.setattr(sw, "log_signal", lambda *a, **k: None)
        mp.setattr(sw, "reason_followups", lambda *a, **k: None)
        mp.setattr(sw, "listing_day", lambda code, day: False)

    def tick(self, hhmm):
        hour, minute = hhmm

        class Now(real.datetime):
            @classmethod
            def now(cls, tz=None):
                return cls(2026, 11, 16, hour, minute, 5)     # 월요일

        class Today(real.date):
            @classmethod
            def today(cls):
                return cls(2026, 11, 16)

        self.mp.setattr(sw, "datetime", types.SimpleNamespace(datetime=Now, date=Today, time=real.time,
                                                              timedelta=real.timedelta))
        sw.tick()

    def volume_lines(self):
        return [ln for msg in self.sent for ln in msg if "거래량 x" in ln]


def test_top_value_stock_with_tripled_volume_goes_into_market_digest(tmp_path, monkeypatch):
    b = Bench(tmp_path, monkeypatch)
    b.amount = {"111111", "222222", "0013Y0"}
    b.avg = {"111111": 1_000_000, "222222": 1_000_000, "0013Y0": 2_000_000}
    b.quotes = {"111111": quote("거래폭발주", 1.2, 3_500_000),          # 3.5배 → 알림
                "222222": quote("평소거래주", 0.4, 2_900_000),          # 2.9배 → 아직 아님
                "0013Y0": quote("엠에스바이오", -2.0, 9_000_000)}       # 영문 섞인 코드도 4.5배 → 알림
    b.tick((10, 3))
    assert b.sent == []                                   # 5분 묶음 시각이 아니면 모아 두기만 한다
    b.tick((10, 5))
    assert len(b.sent) == 1
    msg = b.sent[0]
    assert msg[0].startswith("📡 시장 스파이크 10:03~10:05")
    assert "📢 거래량·52주" in msg
    assert "· 엠에스바이오 거래대금상위 · 거래량 x4.5 🔵-2.0% · 10,000원" in msg
    assert "· 거래폭발주 거래대금상위 · 거래량 x3.5 🔴+1.2% · 10,000원" in msg
    assert not any("평소거래주" in ln for ln in msg)
    assert msg.index("· 엠에스바이오 거래대금상위 · 거래량 x4.5 🔵-2.0% · 10,000원") < \
        msg.index("· 거래폭발주 거래대금상위 · 거래량 x3.5 🔴+1.2% · 10,000원")      # 배수가 큰 것부터

    b.quotes["111111"] = quote("거래폭발주", 2.0, 5_000_000)
    b.tick((10, 10))
    assert len(b.sent) == 1                               # 같은 종목은 하루 한 번만
    cache = json.loads((b.state / "marketvol_cache.json").read_text(encoding="utf-8"))
    assert set(cache) == {"111111", "222222", "0013Y0"} and all(v["asof"] == DAY for v in cache.values())


def test_small_or_etf_names_are_not_alerted(tmp_path, monkeypatch):
    b = Bench(tmp_path, monkeypatch)
    b.amount = {"333333", "444444", "555555"}
    b.avg = {"333333": 100_000, "444444": 100_000, "555555": 0}
    b.quotes = {"333333": quote("소형주", 3.0, 900_000, mcap=5e10),            # 시총 500억 → 유동성 관문 탈락
                "444444": quote("KODEX 어떤상품", 0.1, 900_000),               # ETF 이름
                "555555": quote("상장첫날주", 20.0, 9_000_000)}                # 지난 일봉 없음(평균 0)
    b.tick((10, 5))
    assert b.volume_lines() == []


def test_own_stock_gets_one_volume_alert_not_two(tmp_path, monkeypatch):
    """보유 종목이 거래대금 상위에도 들면 '거래대금상위' 줄과 내 종목 전용 거래량 줄이 같은 사건으로 두 번 나가던 것."""
    b = Bench(tmp_path, monkeypatch)
    b.holdings = {"666666": "내보유주"}
    b.amount = {"666666"}
    b.avg = {"666666": 1_000_000}
    b.quotes = {"666666": quote("내보유주", 4.0, 3_200_000)}
    b.state.mkdir(parents=True)
    (b.state / f"prep_{DAY}.json").write_text(json.dumps(
        {"666666": {"name": "내보유주", "avg20_vol": 1_000_000, "high52": 9e9, "low52": 0}}), encoding="utf-8")
    b.tick((10, 7))                                       # 내 종목은 묶음 시각을 기다리지 않고 바로 나간다
    assert b.volume_lines() == ["💼 내보유주 거래대금상위 · 거래량 x3.2 🔴+4.0% · 10,000원"]
    b.quotes["666666"] = quote("내보유주", 6.0, 6_000_000)
    b.tick((10, 8))
    assert len(b.volume_lines()) == 1                     # 다음 회차에 다른 쪽 줄이 뒤늦게 나오지도 않는다


def test_volume_line_is_skipped_when_a_step_alert_already_went_out(tmp_path, monkeypatch):
    """'+10% 돌파' 같은 단계 알림이 이미 나간(또는 같은 회차에 나가는) 종목은 거래량 줄을 보내지 않는다.
    이 알림은 '주가는 조용한데 거래만 터진 종목'용이다(11/16 복구 첫날 4건 중 3건이 이미 단계 알림이 나간 종목)."""
    b = Bench(tmp_path, monkeypatch)
    b.amount = {"111111", "222222", "333333"}
    b.avg = {"111111": 1_000_000, "222222": 1_000_000, "333333": 1_000_000}
    b.quotes = {"111111": quote("이미급등주", 12.0, 2_000_000),      # 10:01 +10% 돌파, 거래량은 아직 2배
                "222222": quote("조용한주", -0.6, 4_400_000),         # 주가는 조용한데 거래량 4.4배
                "333333": quote("동시주", 10.5, 3_500_000)}           # 같은 회차에 +10% 돌파와 거래량 3.5배
    b.tick((10, 1))
    b.quotes["111111"] = quote("이미급등주", 13.0, 3_300_000)        # 10:03 거래량이 3.3배가 됐지만 단계 알림이 먼저 나갔다
    b.tick((10, 3))
    b.tick((10, 5))
    assert len(b.sent) == 1
    msg = b.sent[0]
    assert b.volume_lines() == ["· 조용한주 거래대금상위 · 거래량 x4.4 🔵-0.6% · 10,000원"]
    assert "· 이미급등주 +10% 돌파 · 현재 +12.0% · 10,000원" in msg
    assert "· 동시주 +10% 돌파 · 현재 +10.5% · 10,000원" in msg
    b.quotes["111111"] = quote("이미급등주", 14.0, 9_000_000)
    b.tick((10, 10))
    assert len(b.volume_lines()) == 1                      # 그날 끝까지 거래량 줄은 나오지 않는다


def test_own_stock_keeps_its_own_volume_line_after_a_step_alert(tmp_path, monkeypatch):
    """내 종목은 단계 알림이 나갔어도 전용 거래량 알림(T2)이 그대로 온다 — 표시는 그 종목의 실제 구분(💼)."""
    b = Bench(tmp_path, monkeypatch)
    b.holdings = {"666666": "내보유주"}
    b.amount = {"666666"}
    b.avg = {"666666": 1_000_000}
    b.state.mkdir(parents=True)
    (b.state / f"prep_{DAY}.json").write_text(json.dumps(
        {"666666": {"name": "내보유주", "avg20_vol": 1_000_000, "high52": 9e9, "low52": 0}}), encoding="utf-8")
    b.quotes = {"666666": quote("내보유주", 6.0, 2_000_000)}          # +5% 돌파(내 종목 단계)
    b.tick((10, 7))
    assert b.volume_lines() == []
    b.quotes["666666"] = quote("내보유주", 7.0, 3_200_000)            # 거래량 3.2배
    b.tick((10, 8))
    assert b.volume_lines() == ["💼 내보유주 거래량 x3.2 🔴+7.0%"]
    b.tick((10, 9))
    assert len(b.volume_lines()) == 1


def test_step_line_and_volume_line_do_not_overwrite_each_other(tmp_path, monkeypatch):
    """같은 5분 안에 거래량 폭발(10:01)과 +10% 돌파(10:03)가 이어지면 두 줄 다 묶음에 남는다."""
    b = Bench(tmp_path, monkeypatch)
    b.amount = {"777777"}
    b.avg = {"777777": 1_000_000}
    b.quotes = {"777777": quote("급등주", 6.0, 3_100_000)}
    b.tick((10, 1))
    b.quotes["777777"] = quote("급등주", 10.4, 3_600_000, price=11040.0)
    b.tick((10, 3))
    b.tick((10, 5))
    assert len(b.sent) == 1
    msg = b.sent[0]
    assert "· 급등주 +10% 돌파 · 현재 +10.4% · 11,040원" in msg
    assert "· 급등주 거래대금상위 · 거래량 x3.1 🔴+6.0% · 10,000원" in msg


def test_empty_amount_list_is_reported_for_ops(tmp_path, monkeypatch, capsys):
    """등락률 순위는 오는데 거래대금 순위만 비면(주소 변경·차단) 운영 점검이 읽는 문구를 10분에 한 번 남긴다."""
    b = Bench(tmp_path, monkeypatch)
    b.ranked = {("up", "KOSPI"): [{"itemCode": "888888"}]}
    b.quotes = {"888888": quote("상승주", 1.0, 1000)}
    b.tick((10, 10))
    assert "거래대금 상위 수집 0건" in capsys.readouterr().out
    b.tick((10, 11))
    assert "수집 0건" not in capsys.readouterr().out      # 매분 찍지 않는다
    b.ranked = {}
    b.tick((10, 20))
    assert "수집 0건" not in capsys.readouterr().out      # 휴장일처럼 순위가 전부 비면 경고하지 않는다
    b.amount = {"888888"}
    b.ranked = {("up", "KOSPI"): [{"itemCode": "888888"}]}
    b.tick((10, 30))
    assert "수집 0건" not in capsys.readouterr().out
