"""Native replies using DART's explicit document family, never company/title guesses."""
import hashlib
import html
import re
from html.parser import HTMLParser
import requests

class FamilyParser(HTMLParser):
    def __init__(self):
        super().__init__();self.inside=False;self.receipts=[]
    def handle_starttag(self,tag,attrs):
        attrs=dict(attrs)
        if tag=='select':self.inside=attrs.get('id')=='family'
        if self.inside and tag=='option':
            match=re.search(r'rcpNo=(\d{14})(?:&|$)',attrs.get('value',''))
            if match and '효력발생' not in attrs.get('title',''):self.receipts.append(match[1])
    def handle_endtag(self,tag):
        if tag=='select':self.inside=False

def related_receipts(receipt):
    try:
        response=requests.get('https://dart.fss.or.kr/dsaf001/main.do',params={'rcpNo':receipt},headers={'User-Agent':'Mozilla/5.0'},timeout=(8,15))
        response.raise_for_status();parser=FamilyParser();parser.feed(response.text)
        # Unexpected HTML must never connect unrelated messages.
        return parser.receipts if receipt in parser.receipts else [receipt]
    except Exception:return [receipt]

def send_filing(state,item,text,sender,save,token,chat,*,parse_mode='HTML',dry_run=False):
    receipt=item['rcept_no']
    if dry_run:return sender(text,parse_mode=parse_mode)
    scope=hashlib.sha256(f'{token}|{chat}'.encode()).hexdigest()
    book=state.setdefault('filing_replies',{}).setdefault(scope,{'seq':0,'receipts':{},'deliveries':{}})
    receipts=book['receipts'];family=related_receipts(receipt)
    current=receipts.get(receipt,{})
    candidates=[receipts[r] for r in family if receipts.get(r,{}).get('message_id')]
    parent=max(candidates,key=lambda x:x['seq'])['message_id'] if candidates else current.get('message_id')
    digest=hashlib.sha256((receipt+'|'+text).encode()).hexdigest()
    # Long HTML is converted to plain text before splitting; no broken tags.
    if len(text.encode('utf-16-le'))//2>3800:
        text=html.unescape(re.sub('<[^>]+>','',text));parse_mode=None
        chunks=[text[i:i+1800] for i in range(0,len(text),1800)]
    else:chunks=[text]
    for i,chunk in enumerate(chunks):
        key=digest+':'+str(i)
        if key in book['deliveries']:
            delivered=book['deliveries'][key]
            if delivered=='uncertain':return True  # never automatically repeat ambiguous delivery
            parent=delivered;continue
        result=sender(chunk,parse_mode=parse_mode,reply_to_message_id=parent)
        if not result:return False
        if type(result) is int:
            book['seq']+=1;parent=result
            receipts[receipt]={'message_id':result,'seq':book['seq']}
            book['deliveries'][key]=result
        else:book['deliveries'][key]='uncertain'
        save(state)
    return True
