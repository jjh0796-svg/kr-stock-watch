"""10/8: ROWSPAN 병합셀 파싱 + 발행결정 1차 카드 + 접수 시각 둘째 줄."""
import os

os.environ.setdefault("DRY_RUN", "1")

from issue_terms import TableRows
from cb_investors import fund_map
from summarize import _quick_bond_card
import dart_watch


_TABLE = """<TABLE><TR><TD>구분</TD><TD>집합투자기구</TD><TD>집합투자업자</TD><TD>신탁업자</TD></TR>
<TR><TD>본건 펀드1</TD><TD>SP 코스닥벤처 8호</TD><TD ROWSPAN="2"><P>에스피자산운용 주식회사</P></TD><TD>케이비증권㈜</TD></TR>
<TR><TD>본건 펀드2</TD><TD>SP 코스닥벤처 9호</TD><TD>삼성증권㈜</TD></TR>
<TR><TD>본건 펀드13</TD><TD>IBK 2호</TD><TD ROWSPAN="3">IBK투자증권 주식회사</TD><TD ROWSPAN="3">삼성증권㈜</TD></TR>
<TR><TD>본건 펀드14</TD><TD>IBK 3호</TD></TR><TR><TD>본건 펀드15</TD><TD>IBK 4호</TD></TR></TABLE>"""


def test_rowspan_cells_fill_following_rows():
    parser = TableRows(); parser.feed(_TABLE)
    assert parser.rows[2] == ["본건 펀드2", "SP 코스닥벤처 9호", "에스피자산운용 주식회사", "삼성증권㈜"]
    assert parser.rows[4] == ["본건 펀드14", "IBK 3호", "IBK투자증권 주식회사", "삼성증권㈜"]
    assert parser.rows[5][2] == "IBK투자증권 주식회사"


def test_fund_map_uses_merged_manager_cell():
    funds = fund_map(_TABLE)
    assert funds[2] == ("SP 코스닥벤처 9호", "에스피자산운용")   # 예전엔 신탁업자(삼성증권)가 운용사로 읽혔다
    assert funds[14][1] == "IBK투자증권" and funds[15][1] == "IBK투자증권"


def test_quick_bond_card_from_structured_row():
    row = {"bd_tm": "2", "bdis_mthn": "사모", "bd_fta": "20,000,000,000", "cv_prc": "8,918",
           "cvisstk_knd": "나노팀 주식회사 기명식 보통주", "cvisstk_cnt": "2,242,655", "cvisstk_tisstk_vs": "10.00",
           "bd_intr_ex": "0.0", "bd_intr_sf": "0.0", "pymd": "2026년 10월 20일", "bd_mtd": "2031년 10월 20일",
           "cvrqpd_bgd": "2027년 10월 20일", "cvrqpd_edd": "2031년 09월 20일", "act_mktprcfl_cvprc_lwtrsprc": "-",
           "fdpp_fclt": "15,000,000,000", "fdpp_op": "5,000,000,000", "fdpp_etc": "-"}
    card = _quick_bond_card(row, "CB")
    assert "CB 2회차 · 사모" in card and "전환가액: 8,918원" in card
    assert "전환대상: 나노팀 주식회사 기명식 보통주 · 2,242,655주" in card
    assert "납입일: 2026년 10월 20일 · 만기일: 2031년 10월 20일" in card
    assert "전환청구: 2027년 10월 20일 ~ 2031년 09월 20일" in card
    assert "자금용도: 시설 150억 · 운영 50억" in card
    assert "최저가" not in card


def test_stamp_receipt_second_line():
    base = "🧾 <b>[CB] 나노팀</b> (417010)\n주요사항보고서\nhttps://dart.fss.or.kr/x"
    item = {"rcept_no": "20261007000391", "rcept_dt": "20261007"}
    stamped = dart_watch.stamp_receipt(base, item, {"20261007000391": "16:44"})
    assert stamped.split("\n")[1] == "🕒 접수 2026.10.07 16:44"
    fallback = dart_watch.stamp_receipt(base, item, {})
    assert fallback.split("\n")[1].startswith("🕒 감지 ") and "미확인" not in fallback
    assert fallback.split("\n")[0] == base.split("\n")[0] and fallback.endswith("https://dart.fss.or.kr/x")
