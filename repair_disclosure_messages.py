"""Repair the two user-reported existing messages; never send new messages."""
import os
from common import load_state,save_state
from dart_watch import STATE_FILE,edit_existing_disclosure,_summary_ready,_complete_card
from summarize import summarize

state=load_state(STATE_FILE,{})
for rn,corp,code in [('20260916800188','애경케미칼','161000'),('20260916800191','AK홀딩스','006840')]:
    info=state.get('pending_sum',{}).get(rn) or state.get('unresolved_summaries',{}).get(rn) or {}
    item={'rcept_no':rn,'corp_name':corp,'stock_code':code,'corp_code':info.get('corp_code',''),
          'rcept_dt':'20260916','report_nm':info.get('title','[기재정정]유상증자결정(종속회사의주요경영사항)')}
    assert edit_existing_disclosure(state,item),'Could not attach buttons to existing message'
    summary=summarize(item,os.environ['DART_API_KEY'])
    if _summary_ready(item,summary):
        base=f'🧾 {corp}\n{item["report_nm"]}\nhttps://dart.fss.or.kr/dsaf001/main.do?rcpNo={rn}'
        assert edit_existing_disclosure(state,item,_complete_card(item,base,summary))
        state.get('pending_sum',{}).pop(rn,None);state.get('unresolved_summaries',{}).pop(rn,None)
        save_state(STATE_FILE,state)
    else:print('ORIGINAL_NOT_READY_NO_NEW_MESSAGE',rn)
