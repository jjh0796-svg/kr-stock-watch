"""event_reaction_stats 단위 시험 (네트워크 없음).

    PYTHONPATH=. ~/venv/bin/python -m unittest tests.test_event_reaction_stats
"""

from __future__ import annotations

import unittest

import event_reaction_stats as ers


def _cal(n: int) -> list[str]:
    return [f"202501{d:02d}" for d in range(1, n + 1)]


class ClassifyTest(unittest.TestCase):
    def test_types(self):
        cases = {
            "단일판매ㆍ공급계약체결": "supply",
            "단일판매ㆍ공급계약체결(자율공시)": "supply",
            "단일판매ㆍ공급계약해지": "supply_cancel",
            "주요사항보고서(유상증자결정)": "rights",
            "주요사항보고서(유무상증자결정)": "rights_bonus",
            "주요사항보고서(무상증자결정)": "bonus",
            "주요사항보고서(전환사채권발행결정)": "cb",
            "주요사항보고서(신주인수권부사채권발행결정)": "bw",
            "주요사항보고서(자기주식취득결정)": "buyback",
            "주요사항보고서(자기주식취득신탁계약체결결정)": "buyback_trust",
            "주요사항보고서(자기주식취득신탁계약해지결정)": "buyback_trust_end",
            "주식소각결정": "cancel",
            "현금ㆍ현물배당결정": "dividend",
            "최대주주변경": "control",
            "최대주주변경을수반하는주식양수도계약체결": "control_deal",
            "주요사항보고서(감자결정)": "reduction",
            "연결재무제표기준영업(잠정)실적(공정공시)": "earnings",
            "영업(잠정)실적(공정공시)": "earnings",
        }
        for title, want in cases.items():
            with self.subTest(title=title):
                self.assertEqual(ers.classify_title(title)["type"], want)

    def test_amendments_and_subsidiaries_flagged(self):
        c = ers.classify_title("[기재정정]주요사항보고서(유상증자결정)")
        self.assertEqual((c["type"], c["amended"]), ("rights", True))
        self.assertTrue(ers.classify_title("[첨부정정]단일판매ㆍ공급계약체결")["amended"])
        self.assertFalse(ers.classify_title("단일판매ㆍ공급계약체결")["amended"])
        self.assertTrue(ers.classify_title("단일판매ㆍ공급계약체결(자회사의주요경영사항)")["subsidiary"])
        self.assertTrue(ers.classify_title("유상증자결정(종속회사의주요경영사항)")["subsidiary"])

    def test_non_targets(self):
        for title in ("기타경영사항(자율공시)", "주주총회소집결의", "유상증자결정철회", "조회공시요구(풍문또는보도)"):
            with self.subTest(title=title):
                self.assertIsNone(ers.classify_title(title))


class BucketTest(unittest.TestCase):
    def test_ratio_boundaries(self):
        self.assertEqual(ers.ratio_levels(9.99), ["매출 대비 10% 미만"])
        self.assertEqual(ers.ratio_levels(10), ["매출 대비 10~30%"])
        self.assertEqual(ers.ratio_levels(30), ["매출 대비 30~100%", "매출 대비 30% 이상"])
        self.assertEqual(ers.ratio_levels(250), ["매출 대비 100% 이상", "매출 대비 30% 이상"])
        self.assertEqual(ers.ratio_levels(None), [])

    def test_dilution_method_earnings(self):
        self.assertEqual(ers.dilution_levels(4.9), ["주식총수 대비 5% 미만"])
        self.assertEqual(ers.dilution_levels(25)[-1], "주식총수 대비 10% 이상")
        self.assertEqual(ers.method_levels("제3자배정증자"), ["제3자배정"])
        self.assertEqual(ers.method_levels("주주배정후 실권주 일반공모"), ["주주배정"])
        self.assertEqual(ers.method_levels("일반공모증자"), ["일반공모"])
        self.assertEqual(ers.earnings_levels(100, -5), ["영업이익 흑자전환", "실적 개선"])
        self.assertEqual(ers.earnings_levels(-3, 10)[0], "영업이익 적자")
        self.assertEqual(ers.earnings_levels(140, 100)[0], "영업이익 30% 이상 증가")
        self.assertEqual(ers.earnings_levels(110, 100), ["영업이익 ±30% 이내"])
        self.assertEqual(ers.earnings_levels(None, None, -45)[0], "영업이익 30% 이상 감소")

    def test_chain_fine_to_coarse(self):
        chain = ers.bucket_chain("supply", {"market": "코스닥", "ratio": 45})
        self.assertEqual(chain, [
            "supply|매출 대비 30~100%|K", "supply|매출 대비 30~100%|ALL",
            "supply|매출 대비 30% 이상|K", "supply|매출 대비 30% 이상|ALL",
            "supply|*|K", "supply|*|ALL"])
        self.assertEqual(ers.bucket_chain("dividend", {"market": "Y"}), ["dividend|*|Y", "dividend|*|ALL"])
        self.assertEqual(ers.bucket_chain("buyback", None), ["buyback|*|ALL"])
        self.assertEqual(ers.bucket_label("supply|매출 대비 10~30%|K"), "공급계약 · 매출 대비 10~30% · 코스닥")


class ReturnMathTest(unittest.TestCase):
    def setUp(self):
        self.cal = _cal(30)
        self.idx = {d: 100.0 for d in self.cal}
        self.stock = {d: (1000.0, 10.0) for d in self.cal}

    def test_intraday_vs_after_close(self):
        self.assertEqual(ers.first_reaction_pos(self.cal, "20250105", False), 4)
        self.assertEqual(ers.first_reaction_pos(self.cal, "20250105", True), 5)
        # 휴일(달력에 없는 날) 접수 → 다음 거래일
        cal = ["20250103", "20250106"]
        self.assertEqual(ers.first_reaction_pos(cal, "20250104", False), 1)

    def test_returns_and_excess(self):
        s = dict(self.stock)
        s["20250106"] = (1100.0, 10.0)          # 첫 반응일(after_close로 05일 접수 → 06일)
        for d in self.cal[6:]:
            s[d] = (1210.0, 10.0)
        idx = dict(self.idx)
        for d in self.cal[5:]:
            idx[d] = 102.0
        r = ers.event_returns(self.cal, s, idx, "20250105", True)
        self.assertEqual(r["t0"], "20250106")
        self.assertAlmostEqual(r["r1"], 0.10)
        self.assertAlmostEqual(r["x1"], 0.10 - 0.02)
        self.assertAlmostEqual(r["r5"], 0.21)
        self.assertAlmostEqual(r["d5"], 0.10)          # 첫날 종가 1100 → 1210
        self.assertAlmostEqual(r["xd5"], 0.10)         # 지수는 첫날 이후 그대로
        self.assertAlmostEqual(r["same_day"], 0.0)     # 접수 당일(공시 전) 움직임
        self.assertIn("r20", r)

    def test_intraday_uses_same_day(self):
        s = dict(self.stock)
        s["20250105"] = (1050.0, 10.0)
        r = ers.event_returns(self.cal, s, self.idx, "20250105", False)
        self.assertEqual(r["t0"], "20250105")
        self.assertAlmostEqual(r["r1"], 0.05)
        self.assertNotIn("same_day", r)

    def test_halt_and_limit_exclusion(self):
        s = dict(self.stock)
        s["20250106"] = (1000.0, 0.0)               # 첫날 거래량 0 = 거래정지
        self.assertIsNone(ers.event_returns(self.cal, s, self.idx, "20250105", True))
        s = dict(self.stock)
        s.pop("20250104")                          # 직전 종가 없음
        self.assertIsNone(ers.event_returns(self.cal, s, self.idx, "20250105", False))
        s = dict(self.stock)
        s["20250108"] = (500.0, 10.0)               # -50% = 수정주가 왜곡 → 5일·20일 제외, 첫날은 유지
        r = ers.event_returns(self.cal, s, self.idx, "20250105", True)
        self.assertIn("r1", r)
        self.assertNotIn("r5", r)

    def test_recent_event_has_no_t20(self):
        r = ers.event_returns(self.cal, self.stock, self.idx, "20250125", True)
        self.assertIn("r5", r)
        self.assertNotIn("r20", r)

    def test_winsorized_summary(self):
        vals = [0.01] * 98 + [5.0, -5.0]
        s = ers.summarize_values(vals, vals)
        self.assertEqual(s["n"], 100)
        self.assertLess(s["mean"], 0.1)              # 극단값 눌림
        self.assertAlmostEqual(s["median"], 0.01)
        self.assertAlmostEqual(s["pos"], 0.99)


def _rec(n1: int, mean1: float, t1: float, n5: int = 0, mean5: float = 0.0, ex5: float = 0.0, t5: float = 0.0,
         label: str = "공급계약 · 코스닥") -> dict:
    return {"label": label, "n": n1,
            "h1": {"n": n1, "mean": mean1, "median": 0.008 if mean1 > 0 else -0.002, "pos": 0.58, "ex_mean": mean1, "t": t1},
            "h5": {"n": n5, "mean": mean5, "median": 0, "pos": 0.5, "ex_mean": ex5, "t": t5}}


class LineTest(unittest.TestCase):
    def test_min_count_and_fallback(self):
        stats = {"buckets": {
            "supply|매출 대비 30~100%|K": _rec(29, 0.03, 3),        # 29건 → 건너뜀
            "supply|매출 대비 30~100%|ALL": _rec(31, 0.021, 3, 31, 0.034, 0.028, 2.5,
                                              label="공급계약 · 매출 대비 30~100% · 코스피·코스닥"),
        }}
        line = ers.reaction_line("단일판매ㆍ공급계약체결", {"market": "K", "ratio": 45}, stats)
        self.assertEqual(line, "📊 과거 비슷한 공시(공급계약 · 매출 대비 30~100% · 코스피·코스닥) 31건: "
                               "첫날 평균 +2.1%·중앙 +0.8% (오른 비율 58%) · 5일 평균 +3.4% (지수 대비 +2.8%p)")
        only_small = {"buckets": {"supply|*|ALL": _rec(29, 0.01, 1)}}
        self.assertIsNone(ers.reaction_line("supply", {"market": "K"}, only_small))
        self.assertIsNone(ers.reaction_line("주주총회소집결의", {}, stats))

    def test_weak_marker_and_hidden_5d(self):
        line = ers.format_line(_rec(40, -0.004, 0.5, 10, 0.0, 0.0, 0.0, label="현금배당 · 코스피"))
        self.assertTrue(line.endswith("(오른 비율 58%) · 뚜렷한 경향 없음"), line)
        self.assertNotIn("5일", line)
        self.assertIn("첫날 평균 -0.4%", line)

    def test_attrs_from_card(self):
        card = "📝 <b>[단일판매ㆍ공급계약체결] (코스닥)테스트</b> (123456)\n금액: 120.5억원 (매출대비 45.2%)\n"
        a = ers.attrs_from_card("단일판매ㆍ공급계약체결", card)
        self.assertEqual((a["market"], a["ratio"]), ("코스닥", 45.2))
        a = ers.attrs_from_card("주요사항보고서(전환사채권발행결정)", "주식총수 대비: 12.34% (공시 기재 기준)", "K")
        self.assertEqual(a["dilution"], 12.34)
        a = ers.attrs_from_card("주요사항보고서(유상증자결정)", "방식: 제3자배정증자\n", "Y")
        self.assertEqual(ers.method_levels(a["method"]), ["제3자배정"])
        a = ers.attrs_from_card("영업(잠정)실적(공정공시)", "영업익: 120억원 (QoQ +5.0% · YoY +42.0% · OPM 9%)", "K")
        self.assertEqual(ers.earnings_levels(a.get("op_now"), None, a.get("op_yoy"))[0], "영업이익 30% 이상 증가")

    def test_match_rows_amended_numbers(self):
        rows = [{"rcept_no": "20241111000334", "m": "a"}, {"rcept_no": "20241111000338", "m": "b"},
                {"rcept_no": "20241121000382", "m": "c"}]
        got = ers.match_rows(["20241028000397", "20241028000414", "20241121000382"], rows)
        self.assertEqual({k: v["m"] for k, v in got.items()},
                         {"20241121000382": "c", "20241028000397": "a", "20241028000414": "b"})
        self.assertEqual(ers.match_rows(["20250101000001"], [{"rcept_no": "20241201000001"}]), {})

    def test_parsers(self):
        self.assertAlmostEqual(ers.parse_supply_ratio("계약금액 (원) 1,000 최근 매출액 (원) 4,000 매출액 대비 (%) 25.0"), 25.0)
        self.assertAlmostEqual(ers.parse_supply_ratio("계약금액 총액(원) 1,000 최근매출액(원) 4,000 매출액대비(%) -"), 25.0)
        p = ers.parse_earnings("영업이익 당해실적 1,200 900 33.3 △300 흑자전환 당기순이익")
        self.assertEqual((p["op_now"], p["op_prev"]), (1200.0, -300.0))
        # 코스닥 서식(증감액·괄호 증감률) / 유가 서식 — 실제 원문에서 따온 줄
        p = ers.parse_earnings("영업이익 당해실적 971 931 40 (4.3%) -47 1,019 (흑자전환) 누계실적 2,335 - - 1,801")
        self.assertEqual((p["op_now"], p["op_prev"]), (971.0, -47.0))
        p = ers.parse_earnings("영업이익 당해실적 41,753 47,453 -12.0 77,465 -46.1 누계실적 184,637")
        self.assertEqual((p["op_now"], p["op_prev"]), (41753.0, 77465.0))
        p = ers.parse_earnings("영업이익 당해실적 -3,960 -4,457 11.15 - -2,680 -47.76 - 누계실적 -11,669")
        self.assertEqual((p["op_now"], p["op_prev"]), (-3960.0, -2680.0))


if __name__ == "__main__":
    unittest.main()
