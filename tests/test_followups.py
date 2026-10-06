from datetime import date
from unittest.mock import Mock,patch
from followup_watch import observe,refresh,dates_from_summary,parse_date
from filing_threads import send_filing

def seed(payment='2026-09-18',scope='scope',corp='A',receipt='20260916000001'):
    state={}
    with patch('followup_watch.day',return_value=date(2026,9,16)):
        observe(state,scope,{'rcept_no':receipt,'corp_name':corp,'corp_code':'00000001','report_nm':'유상증자결정'},[receipt],{'payment':payment})
    return state

def test_date_parsing_and_invalid_dates():
    assert parse_date('2026년 09월 18일')=='2026-09-18'
    assert parse_date('9/18') is None
    assert parse_date('2026-02-30') is None
    assert dates_from_summary('납입일: 2026년 09월 18일 · 만기일: 2030년 09월 18일')=={'payment':'2026-09-18'}

def test_reminder_then_unconfirmed_only_once_each_after_successful_scan():
    state=seed();send=Mock(return_value=True);fetch=Mock(return_value=[])
    for today in [date(2026,9,17),date(2026,9,17),date(2026,9,20),date(2026,9,21)]:
        refresh(state,'scope','key',send,lambda s:None,today=today,fetch=fetch)
    assert send.call_count==2
    assert '다가오는 일정' in send.call_args_list[0].args[1]
    assert '결과 공시를 확인하지 못했습니다' in send.call_args_list[1].args[1]

def test_failed_scan_and_other_chat_never_claim_no_result():
    state=seed();send=Mock()
    refresh(state,'scope','key',send,lambda s:None,today=date(2026,9,20),fetch=Mock(side_effect=RuntimeError()))
    refresh(state,'another','key',send,lambda s:None,today=date(2026,9,20),fetch=Mock(return_value=[]))
    send.assert_not_called()

def test_amendment_replaces_old_due_date_not_company_guess():
    state=seed();rn='20260916000001'
    observe(state,'scope',{'rcept_no':'20260917000002','corp_name':'A','corp_code':'00000001'},[rn,'20260917000002'],{'payment':'2026-10-18'})
    send=Mock(return_value=True)
    refresh(state,'scope','key',send,lambda s:None,today=date(2026,9,20),fetch=lambda *a:[])
    send.assert_not_called()
    observe(state,'scope',{'rcept_no':'independent','corp_name':'A','corp_code':'00000001'},['independent'],{'payment':'2026-10-19'})
    assert len(state['followup_events']['scope'])==2

def test_linked_withdrawal_closes_and_unrelated_result_does_not():
    state=seed();send=Mock(return_value=True);rn='20260916000001'
    items=[{'rcept_no':'20260919000001','report_nm':'증권발행결과'},{'rcept_no':'20260919000002','report_nm':'철회신고서'}]
    lookup=lambda r:[r,'unrelated'] if r.endswith('1') else [rn,r]
    refresh(state,'scope','key',send,lambda s:None,today=date(2026,9,20),fetch=lambda *a:items,family_lookup=lookup)
    assert send.call_count==1 and '철회 공시 확인' in send.call_args.args[1]
    assert not state['followup_events']['scope'][rn]['active']

def test_result_receipt_never_claims_payment_completed():
    state=seed();rn='20260916000001';send=Mock(return_value=True)
    refresh(state,'scope','key',send,lambda s:None,today=date(2026,9,20),fetch=lambda *a:[{'rcept_no':'result','report_nm':'증권발행실적보고서'}],family_lookup=lambda r:[rn,r])
    assert '실제 납입금액·완료 여부는 원문 확인' in send.call_args.args[1]

def test_uncertain_or_dry_delivery_does_not_register_schedule():
    for sender,dry in [(Mock(return_value=True),False),(Mock(return_value=123),True)]:
        state={}
        with patch('filing_threads.related_receipts',return_value=['r']):
            send_filing(state,{'rcept_no':'r'},'x',sender,lambda s:None,'b','c',dry_run=dry,followup_dates={'payment':'2026-09-18'})
        assert not state.get('followup_events')

def test_explicit_family_includes_future_receipts_but_does_not_mark_them_observed():
    state=seed();event=next(iter(state['followup_events']['scope'].values()))
    event['family'].append('future');send=Mock(return_value=True)
    refresh(state,'scope','key',send,lambda s:None,today=date(2026,9,20),fetch=lambda *a:[{'rcept_no':'future','report_nm':'철회신고서'}],family_lookup=lambda r:event['family'])
    assert send.call_count==1 and not event['active']

def _two_events():
    state={}
    with patch('followup_watch.day',return_value=date(2026,10,6)):
        observe(state,'scope',{'rcept_no':'20261001000001','corp_name':'관심회사','corp_code':'00000001','stock_code':'111111','report_nm':'유상증자결정'},['20261001000001'],{'payment':'2026-10-08'})
        observe(state,'scope',{'rcept_no':'20261001000002','corp_name':'전체회사','corp_code':'00000002','stock_code':'222222','report_nm':'유상증자결정'},['20261001000002'],{'payment':'2026-10-08','listing':'2026-10-08'})
    return state

def test_watchlist_reply_others_bundled_once():
    """10/7: 관심종목은 답글, 전체 구독 회사는 하루 한 통 — 같은 날 다시 돌아도 반복 안 함."""
    from datetime import datetime
    from followup_watch import KST
    state=_two_events();send=Mock(return_value=True);plain=Mock(return_value=1)
    for _ in range(3):
        refresh(state,'scope','key',send,lambda s:None,today=date(2026,10,7),fetch=lambda *a:[],
                watch={'111111':'관심회사'},send_plain=plain,now=datetime(2026,10,7,7,30,tzinfo=KST))
    assert send.call_count==1 and '관심회사 · 다가오는 일정' in send.call_args.args[1]
    assert plain.call_count==1
    text=plain.call_args.args[0]
    assert '유증·CB 일정 모음' in text and '전체회사' in text and '관심회사' not in text
    assert '신주 상장 예정' in text and '10/8(목)' in text

def test_bundle_waits_until_checks_done_or_0830():
    from datetime import datetime
    from followup_watch import KST
    state=_two_events();plain=Mock(return_value=1)
    # 전체회사가 아직 확인 안 됨(조회 실패) → 08:30 전엔 보내지 않음
    fetch=lambda key,corp,*a:(_ for _ in ()).throw(RuntimeError()) if corp=='00000002' else []
    refresh(state,'scope','key',Mock(return_value=True),lambda s:None,today=date(2026,10,7),fetch=fetch,
            watch={'111111':'관심회사'},send_plain=plain,now=datetime(2026,10,7,7,30,tzinfo=KST))
    plain.assert_not_called()
