from unittest.mock import patch,Mock
import time
import dart_watch as d

ITEM={'rcept_no':'20260916000188','report_nm':'유상증자결정(종속회사)','corp_name':'A','corp_code':'00000001','stock_code':'123456'}
BASE='공시 A\n유상증자결정\nhttps://example.com/original'
READY='발행금액: 100억원\n납입일: 2026-09-30\n대상: B'

def test_unavailable_then_complete_is_one_delivery_with_full_card():
    state={'seen':{'old':'20260915'}}
    with patch.object(d,'fetch_today_list',return_value=[ITEM]),patch.object(d,'merged_watchlist',return_value={}),patch.object(d,'classify',return_value=BASE),patch.object(d,'summarize',return_value=None),patch.object(d,'save_state'),patch.object(d,'send_disclosure') as send:
        d.poll_once('k',state,{})
        send.assert_not_called()
    assert state['pending_sum'][ITEM['rcept_no']]['single_delivery']
    with patch.object(d,'summarize',return_value=READY),patch.object(d,'stock_snapshot',return_value=None),patch.object(d,'save_state'),patch.object(d,'send_disclosure',return_value=True) as send:
        d.retry_pending_summaries('k',state);d.retry_pending_summaries('k',state)
        assert send.call_count==1 and READY in send.call_args.args[2]
        assert '원문 확인 중' not in send.call_args.args[2]

def test_partial_summary_is_never_sent_before_complete():
    state={'pending_sum':{ITEM['rcept_no']:d._pending_info(ITEM,BASE)}}
    with patch.object(d,'summarize',return_value='발행금액: 100억원\n대상: 미확인'),patch.object(d,'save_state'),patch.object(d,'send_disclosure') as send:
        d.retry_pending_summaries('k',state);send.assert_not_called()
    assert state['pending_sum']

def test_expired_source_failure_is_retained_for_review_without_placeholder():
    info=d._pending_info(ITEM,BASE);info['first_try']=time.time()-4*3600
    state={'pending_sum':{ITEM['rcept_no']:info}}
    with patch.object(d,'summarize',return_value=None),patch.object(d,'save_state'),patch.object(d,'send_disclosure') as send:
        d.retry_pending_summaries('k',state);send.assert_not_called()
    assert not state['pending_sum'] and ITEM['rcept_no'] in state['unresolved_summaries']

def test_legacy_placeholder_is_edited_not_followed_by_new_message():
    info=d._pending_info(ITEM,BASE);info.pop('single_delivery')
    state={'pending_sum':{ITEM['rcept_no']:info}}
    with patch.object(d,'summarize',return_value=READY),patch.object(d,'stock_snapshot',return_value=None),patch.object(d,'save_state'),patch.object(d,'send_disclosure') as send,patch.object(d,'edit_existing_disclosure',return_value=True) as edit:
        d.retry_pending_summaries('k',state)
        send.assert_not_called();assert edit.call_count==1 and not state['pending_sum']

def test_ready_first_fetch_sends_one_complete_card():
    state={'seen':{'old':'20260915'}}
    with patch.object(d,'fetch_today_list',return_value=[ITEM]),patch.object(d,'merged_watchlist',return_value={}),patch.object(d,'classify',return_value=BASE),patch.object(d,'summarize',return_value=READY),patch.object(d,'stock_snapshot',return_value=None),patch.object(d,'save_state'),patch.object(d,'send_disclosure',return_value=True) as send:
        d.poll_once('k',state,{});d.poll_once('k',state,{})
        assert send.call_count==1 and READY in send.call_args.args[2]
