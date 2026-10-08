"""원문(document.xml)이 늦게 열리는 공시: 탐지 즉시 1차 카드 한 통, 원문이 열리면 같은 메시지를 고쳐 쓴다 (새 메시지 없음).
10/8 전까지는 완성 카드가 될 때까지 아무것도 안 보냈는데, DART 원문이 15~45분 늦는 일이 흔해 탐지가 그만큼 늦었다."""
from unittest.mock import patch,Mock
import time
import dart_watch as d

def compact(text):
    return '\n'.join(line for line in text.splitlines() if line.strip())   # 카드 조립 시 끼는 빈 줄 무시

ITEM={'rcept_no':'20260916000188','report_nm':'유상증자결정(종속회사)','corp_name':'A','corp_code':'00000001','stock_code':'123456'}
BASE='공시 A\n유상증자결정\nhttps://example.com/original'
READY='발행금액: 100억원\n납입일: 2026-09-30\n대상: B'

def test_unavailable_sends_first_card_then_edits_same_message():
    state={'seen':{'old':'20260915'}}
    with patch.object(d,'fetch_today_list',return_value=[ITEM]),patch.object(d,'receipt_times',return_value={}),patch.object(d,'merged_watchlist',return_value={}),patch.object(d,'classify',return_value=BASE),patch.object(d,'summarize',return_value=None),patch.object(d,'issuance_quick',return_value=None),patch.object(d,'stock_snapshot',return_value=None),patch.object(d,'save_state'),patch.object(d,'send_disclosure',return_value=True) as send:
        d.poll_once('k',state,{})
        text=send.call_args.args[2]
        assert send.call_count==1 and '요약 준비 중' in text
        assert text.startswith('공시 A') and text.endswith('https://example.com/original')
    assert state['pending_sum'][ITEM['rcept_no']]['single_delivery'] is False
    with patch.object(d,'summarize',return_value=READY),patch.object(d,'stock_snapshot',return_value=None),patch.object(d,'save_state'),patch.object(d,'send_disclosure') as send,patch.object(d,'edit_existing_disclosure',return_value=True) as edit:
        d.retry_pending_summaries('k',state);d.retry_pending_summaries('k',state)
        send.assert_not_called()
        assert edit.call_count==1 and READY in compact(edit.call_args.args[2]) and '요약 준비 중' not in edit.call_args.args[2]
    assert not state['pending_sum']

def test_issuance_first_card_comes_from_structured_api():
    state={'seen':{'old':'20260915'}}
    quick='CB 2회차 · 사모\n발행금액: 200억'
    with patch.object(d,'fetch_today_list',return_value=[ITEM]),patch.object(d,'receipt_times',return_value={}),patch.object(d,'merged_watchlist',return_value={}),patch.object(d,'classify',return_value=BASE),patch.object(d,'summarize',return_value=None),patch.object(d,'issuance_quick',return_value=quick),patch.object(d,'stock_snapshot',return_value=None),patch.object(d,'save_state'),patch.object(d,'send_disclosure',return_value=True) as send:
        d.poll_once('k',state,{})
        text=send.call_args.args[2]
        assert quick in compact(text) and '투자자·운용사·조정 조항은' in text and '요약 준비 중' not in text
    assert state['pending_sum'][ITEM['rcept_no']]['single_delivery'] is False

def test_first_card_send_failure_leaves_item_unseen_for_next_poll():
    state={'seen':{'old':'20260915'}}
    with patch.object(d,'fetch_today_list',return_value=[ITEM]),patch.object(d,'receipt_times',return_value={}),patch.object(d,'merged_watchlist',return_value={}),patch.object(d,'classify',return_value=BASE),patch.object(d,'summarize',return_value=None),patch.object(d,'issuance_quick',return_value=None),patch.object(d,'stock_snapshot',return_value=None),patch.object(d,'save_state'),patch.object(d,'send_disclosure',return_value=False):
        d.poll_once('k',state,{})
    assert ITEM['rcept_no'] not in state['seen'] and not state.get('pending_sum')

def test_partial_summary_is_never_written_before_complete():
    info=d._pending_info(ITEM,BASE);info['single_delivery']=False
    state={'pending_sum':{ITEM['rcept_no']:info}}
    with patch.object(d,'summarize',return_value='발행금액: 100억원\n대상: 미확인'),patch.object(d,'save_state'),patch.object(d,'send_disclosure') as send,patch.object(d,'edit_existing_disclosure') as edit:
        d.retry_pending_summaries('k',state);send.assert_not_called();edit.assert_not_called()
    assert state['pending_sum']

def test_expired_source_failure_is_retained_for_review():
    info=d._pending_info(ITEM,BASE);info['first_try']=time.time()-4*3600;info['single_delivery']=False
    state={'pending_sum':{ITEM['rcept_no']:info}}
    with patch.object(d,'summarize',return_value=None),patch.object(d,'save_state'),patch.object(d,'send_disclosure') as send:
        d.retry_pending_summaries('k',state);send.assert_not_called()
    assert not state['pending_sum'] and ITEM['rcept_no'] in state['unresolved_summaries']

def test_legacy_single_delivery_entry_still_sends_once():
    # 10/8 이전 상태 파일에 남은 항목(1차 카드를 안 보낸 것)은 완성되면 새 메시지로 한 번 보낸다.
    state={'pending_sum':{ITEM['rcept_no']:d._pending_info(ITEM,BASE)}}
    assert state['pending_sum'][ITEM['rcept_no']]['single_delivery']
    with patch.object(d,'summarize',return_value=READY),patch.object(d,'stock_snapshot',return_value=None),patch.object(d,'save_state'),patch.object(d,'send_disclosure',return_value=True) as send,patch.object(d,'edit_existing_disclosure') as edit:
        d.retry_pending_summaries('k',state)
        assert send.call_count==1 and READY in compact(send.call_args.args[2]);edit.assert_not_called()
    assert not state['pending_sum']

def test_ready_first_fetch_sends_one_complete_card():
    state={'seen':{'old':'20260915'}}
    with patch.object(d,'fetch_today_list',return_value=[ITEM]),patch.object(d,'receipt_times',return_value={}),patch.object(d,'merged_watchlist',return_value={}),patch.object(d,'classify',return_value=BASE),patch.object(d,'summarize',return_value=READY),patch.object(d,'stock_snapshot',return_value=None),patch.object(d,'save_state'),patch.object(d,'send_disclosure',return_value=True) as send:
        d.poll_once('k',state,{});d.poll_once('k',state,{})
        assert send.call_count==1 and READY in '\n'.join(line for line in send.call_args.args[2].splitlines() if line.strip())
