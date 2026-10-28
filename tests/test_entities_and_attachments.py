"""10/27~28 텔레그램에서 발견된 두 문제의 회귀 테스트.
1) 우진아이엔에스 공급계약 '계약: … HVAC&amp;소방공사' — 원문의 문자 참조가 두 번 이스케이프돼 화면에 '&amp;'가 보였다.
2) 애드바이오텍 [첨부정정] CB 2건(18:01·18:02)·썬테크 [첨부정정] 유증이 '요약 없음' 카드로 나갔다.
   두 통은 같은 공시의 중복 발송이 아니라 접수번호가 다른 첨부정정 2건(20261027000381·000383)이었다."""
import io
import zipfile
from unittest.mock import patch, Mock

import common
import dart_watch as d
import summarize

WOOJIN_DOC = (
    '<?xml version="1.0" encoding="utf-8"?><DOCUMENT><BODY><TABLE>'
    '<TR><TD>1. 판매ㆍ공급계약 구분</TD><TD>공사수주</TD></TR>'
    '<TR><TD>- 체결계약명</TD><TD>용인 Cluster 1기 Ph-4 OBL Ph4 HVAC&amp;소방공사</TD></TR>'
    '<TR><TD>2. 계약내역</TD><TD>계약금액(원)</TD><TD>25,600,000,000</TD></TR>'
    '<TR><TD>최근 매출액(원)</TD><TD>164,000,000,000</TD></TR>'
    '<TR><TD>매출액 대비(%)</TD><TD>15.6</TD></TR>'
    '<TR><TD>3. 계약상대</TD><TD>에스케이에코플랜트(주) &lt;R&amp;D&gt;</TD></TR>'
    '<TR><TD>- 회사와의 관계</TD><TD>-</TD></TR>'
    '<TR><TD>4. 판매ㆍ공급지역</TD><TD>대한민국</TD></TR>'
    '<TR><TD>5. 계약기간</TD><TD>시작일</TD><TD>2026-10-26</TD><TD>종료일</TD><TD>2028-12-31</TD></TR>'
    '</TABLE></BODY></DOCUMENT>'
)


def _zip_response(text):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w') as zf:
        zf.writestr('20261028800112.xml', text.encode('utf-8'))
    resp = Mock(content=buf.getvalue())
    resp.raise_for_status = Mock()
    return resp


def test_supply_contract_name_is_escaped_exactly_once():
    with patch.object(summarize.requests, 'get', return_value=_zip_response(WOOJIN_DOC)):
        out = summarize._sum_supply('k', '20261028800112')
    assert '계약: 용인 Cluster 1기 Ph-4 OBL Ph4 HVAC&amp;소방공사' in out   # 텔레그램 HTML에선 '&'로 보인다
    assert '&amp;amp;' not in out
    # 상대방 칸의 <, > 도 한 번만 이스케이프 — 태그로 오인되거나 &lt; 가 그대로 보이면 안 된다
    assert '상대: 에스케이에코플랜트(주) &lt;R&amp;D&gt;' in out
    assert '&amp;lt;' not in out


def test_doc_text_returns_plain_text_without_entities():
    with patch.object(summarize.requests, 'get', return_value=_zip_response(WOOJIN_DOC)):
        text = summarize._doc_text('k', '20261028800112')
    assert 'HVAC&소방공사' in text and '&amp;' not in text and '<R&D>' in text


def test_esc_is_idempotent():
    assert common.esc('HVAC&소방공사') == 'HVAC&amp;소방공사'
    assert common.esc('HVAC&amp;소방공사') == 'HVAC&amp;소방공사'          # 이미 문자 참조로 온 값
    assert common.esc(common.esc('A<B>&C')) == 'A&lt;B&gt;&amp;C'


def test_quick_bond_card_escapes_structured_api_text():
    row = {'bd_tm': '3', 'bdis_mthn': '사모&공모', 'bd_fta': '10000000000', 'cv_prc': '5,000',
           'cvisstk_knd': '기명식 <보통주>', 'cvisstk_cnt': '2,000,000'}
    card = summarize._quick_bond_card(row, 'CB')
    assert 'CB 3회차 · 사모&amp;공모' in card
    assert '전환대상: 기명식 &lt;보통주&gt; · 2,000,000주' in card


# ─── [첨부정정]·[첨부추가] ────────────────────────────────────────────────────

def test_attachment_only_titles():
    assert d.attachment_only('[첨부정정]주요사항보고서(전환사채권발행결정)')
    assert d.attachment_only('[첨부추가]주요사항보고서(유상증자결정)')
    assert d.attachment_only('[첨부정정] 주식등의대량보유상황보고서(일반)')
    assert not d.attachment_only('[기재정정]주요사항보고서(유상증자결정)')
    assert not d.attachment_only('[기재정정][첨부정정]주요사항보고서(유상증자결정)')   # 본문도 바뀐 정정은 알린다
    assert not d.attachment_only('주요사항보고서(유상증자결정)')
    assert not d.attachment_only('')


def _item(rn, title, code='179530', corp='애드바이오텍'):
    return {'rcept_no': rn, 'report_nm': title, 'corp_name': corp, 'stock_code': code,
            'corp_code': '00000001', 'corp_cls': 'K', 'rcept_dt': rn[:8], 'flr_nm': corp}


def _poll(state, items, watch):
    with patch.object(d, 'fetch_today_list', return_value=items), \
         patch.object(d, 'receipt_times', return_value={}), \
         patch.object(d, 'merged_watchlist', return_value=watch), \
         patch.object(d, 'summarize', return_value=None), \
         patch.object(d, 'issuance_quick', return_value=None), \
         patch.object(d, 'stock_snapshot', return_value=None), \
         patch.object(d, 'save_state'), \
         patch.object(d, 'send_disclosure', return_value=True) as send:
        d.poll_once('k', state, {})
    return [c.args[1]['rcept_no'] for c in send.call_args_list]


def test_attachment_corrections_are_not_alerted_but_marked_seen():
    # 10/27 실제 목록 그대로: 애드바이오텍 [첨부정정] 2건 + 썬테크 [첨부정정]·[기재정정]
    items = [_item('20261027000383', '[첨부정정]주요사항보고서(전환사채권발행결정)'),
             _item('20261027000381', '[첨부정정]주요사항보고서(전환사채권발행결정)'),
             _item('20261027000238', '[기재정정]주요사항보고서(유상증자결정)', '217320', '썬테크'),
             _item('20261027000236', '[첨부정정]주요사항보고서(유상증자결정)', '217320', '썬테크')]
    state = {'seen': {'old': '20261026'}}
    sent = _poll(state, items, {'179530': '애드바이오텍', '217320': '썬테크'})
    assert sent == ['20261027000238']                       # 기재정정만 알림
    assert all(it['rcept_no'] in state['seen'] for it in items)   # 첨부정정은 본 것으로 기록 → 다시 검사하지 않음
    assert set(state.get('pending_sum', {})) == {'20261027000238'}


def test_same_receipt_is_never_sent_twice_across_polls():
    item = _item('20261027000500', '주요사항보고서(전환사채권발행결정)')
    state = {'seen': {'old': '20261026'}}
    assert _poll(state, [item], {'179530': '애드바이오텍'}) == ['20261027000500']
    assert _poll(state, [item], {'179530': '애드바이오텍'}) == []


def test_distinct_receipts_with_same_title_are_separate_filings():
    # 접수번호가 다르면 제목이 같아도 별개 공시(같은 날 공급계약 2건 등) — 제목으로 합치지 않는다.
    items = [_item('20261027000502', '단일판매ㆍ공급계약체결'), _item('20261027000501', '단일판매ㆍ공급계약체결')]
    state = {'seen': {'old': '20261026'}}
    assert _poll(state, items, {'179530': '애드바이오텍'}) == ['20261027000501', '20261027000502']
