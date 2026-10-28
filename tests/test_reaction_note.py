"""공시 카드 밑 '📊 과거 비슷한 공시' 한 줄 붙이는 규칙."""
from dart_watch import reaction_note


def test_rights_offering_gets_line():
    line = reaction_note({"report_nm": "주요사항보고서(유상증자결정)", "corp_cls": "K"}, "방식: 주주배정후 실권주 일반공모\n")
    assert line and line.startswith("📊 과거 비슷한 공시(")


def test_skips_amended_subsidiary_and_konex():
    assert reaction_note({"report_nm": "[기재정정]주요사항보고서(유상증자결정)", "corp_cls": "K"}, "") is None
    assert reaction_note({"report_nm": "주요사항보고서(유상증자결정)", "corp_cls": "N"}, "") is None
    assert reaction_note({"report_nm": "임원ㆍ주요주주특정증권등소유상황보고서", "corp_cls": "Y"}, "") is None


def test_operating_loss_card_uses_loss_bucket():
    card = "매출액: 15억 (QoQ +26.9% · YoY +7.7%)\n영업익: -40억 (QoQ 적자지속 · YoY 적자지속 · OPM -265.4%)"
    line = reaction_note({"report_nm": "연결재무제표기준영업(잠정)실적(공정공시)", "corp_cls": "K"}, card)
    assert line and "영업이익 적자" in line
