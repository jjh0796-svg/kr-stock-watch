"""Update only the three existing messages named by the user; no sends."""
import os
from common import load_state,save_state
from dart_watch import STATE_FILE,edit_existing_disclosure,_summary_ready,_complete_card
from summarize import summarize

ROWS=[
 ('20260916000279','모아라이프플러스','142760','주요사항보고서(유상증자결정)',['20.0억원']),
 ('20260916800291','아시아나IDT','267850','단일판매ㆍ공급계약체결',['46.7억원','매출대비 2.4%','33개월']),
 ('20260916000208','링크솔루션','474650','[기재정정]주요사항보고서(전환사채권발행결정)',['30.0% 할증','아트만자산운용 70.0억원','HNB 코스닥벤처']),
]

def main():
    state=load_state(STATE_FILE,{})
    rows=ROWS
    if os.environ.get('FORMAT_TARGET')=='kcc':
        rows=[('20260916900511','KCC건설','021320','[기재정정]단일판매ㆍ공급계약체결(자율공시)',['27.6억원','매출대비 0.4%','7,415일'])]
    for rn,corp,code,title,expected in rows:
        if os.environ.get('FORMAT_REQUIRE_COMPLETED'):
            assert rn not in state.get('pending_sum',{}) and rn not in state.get('unresolved_summaries',{}), 'Message still being processed'
        item={'rcept_no':rn,'corp_name':corp,'stock_code':code,'corp_code':'','rcept_dt':'20260916','report_nm':title}
        summary=summarize(item,os.environ['DART_API_KEY'])
        assert _summary_ready(item,summary) and all(v in summary for v in expected), f'Summary validation failed {rn}'
        base=f'🧾 <b>{corp} ({code})</b>\n{title}\nhttps://dart.fss.or.kr/dsaf001/main.do?rcpNo={rn}'
        if os.environ.get('FORMAT_TARGET')=='kcc':
            base=f'🌐📝 <b>[단일판매ㆍ공급계약체결·전체] (코스닥){corp} ({code})</b>\n{title}\nhttps://dart.fss.or.kr/dsaf001/main.do?rcpNo={rn}'
        message=_complete_card(item,base,summary)
        assert len(message.encode('utf-16-le'))//2<=3800
        assert edit_existing_disclosure(state,item,message), f'Existing message edit failed {rn}'
        save_state(STATE_FILE,state)
        print('FORMAT_REPAIRED',rn)

if __name__=='__main__':main()
