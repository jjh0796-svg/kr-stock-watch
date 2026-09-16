import html,re
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch
from issue_terms import summarize_xml,eok_amount,fund_investors,Fields
import summarize
ROOT=Path(__file__).parent/'fixtures'

def test_rounding_and_no_won_duplicate():
    assert eok_amount('2050000000')=='20.5억원'
    assert eok_amount('2005000000')=='20.1억원'
    text=summarize_xml((ROOT/'20260916000279.xml').read_text(encoding='utf-8'),'유상증자결정')
    assert '20.0억원' in text and '2,000,000,000원' not in text
    assert '1,000원' in text  # per-share prices retain won units


def test_actual_fund_managers_and_premium():
    raw=(ROOT/'20260916000208.xml').read_text(encoding='utf-8')
    text=summarize_xml(raw,'전환사채권발행결정')
    for value in ['아트만자산운용 70.0억원','에스피자산운용 50.0억원','휴먼앤드브릿지자산운용 5.0억원','HNB 코스닥벤처 일반 사모투자신탁 제4호','30.0% 할증','리픽싱 없음','한도 50%']:
        assert value in text
    assert '한국투자증권' not in text
    amounts=re.findall(r'^• .+ ([\d,.]+)억원$',text,re.M)
    assert sum(Decimal(x.replace(',','')) for x in amounts)==300
    assert text.count('  └ ')==28
    assert len(text.encode('utf-16-le'))//2<3000
    fields=Fields();fields.feed(raw)
    assert fund_investors(raw,fields.fields,'31000000000') is None


def test_supply_annualization_actual_contract():
    raw=(ROOT/'20260916800291.xml').read_text(encoding='utf-8')
    text=re.sub(r'\s+',' ',html.unescape(re.sub('<[^>]+>',' ',raw)))
    with patch.object(summarize,'_doc_text',return_value=text):
        out=summarize._sum_supply('','')
    for value in ['46.7억원 (매출대비 2.4%)','33개월','소프트웨어 통합 유지보수','30일이내','부가세 제외']:
        assert value in out
