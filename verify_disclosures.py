"""Manual check, labelled historical filings; uses the production reply-state cache."""
import os
import requests
from common import load_state
from dart_watch import send_disclosure,STATE_FILE
from issue_terms import issuance_summary
from filing_threads import related_receipts

def main():
    token=os.environ['TELEGRAM_BOT_TOKEN'];key=os.environ['DART_API_KEY']
    identity=requests.get(f'https://api.telegram.org/bot{token}/getMe',timeout=15).json()
    assert identity.get('ok') and '공시모니터링' in identity['result']['first_name']
    latest='20260915000270';family=related_receipts(latest)
    root=min(family);assert root!=latest
    items=[{'rcept_no':rn,'report_nm':'주요사항보고서(전환사채권발행결정)','corp_name':'다보링크'} for rn in [root,latest]]
    items.append({'rcept_no':'20260915000331','report_nm':'주요사항보고서(유상증자결정)','corp_name':'광진실업'})
    cards=[issuance_summary(i,key) for i in items];assert all(cards),'Original document unavailable; no test sent'
    assert '3,260원' in cards[-1] and '더블유에이치에너지조합' in cards[-1]
    state=load_state(STATE_FILE,{});receipts=[];original=requests.post
    def record(*a,**kw):
        r=original(*a,**kw)
        if r.status_code==200:
            result=r.json().get('result',{});receipts.append((result.get('message_id'),result.get('reply_to_message',{}).get('message_id')))
        return r
    requests.post=record
    for i,card in zip(items,cards):
        label='정정공시' if i['rcept_no']==latest else '기존 공시'
        text=f"🧪 [지난 공시 · 개선 확인] {i['corp_name']} · {label}\n{card}\nhttps://dart.fss.or.kr/dsaf001/main.do?rcpNo={i['rcept_no']}"
        assert send_disclosure(state,i,text)
    if receipts:assert len(receipts)==3 and receipts[1][1]==receipts[0][0]
    print('DISCLOSURE_NATIVE_REPLIES',receipts or 'already recorded')

if __name__=='__main__':main()
