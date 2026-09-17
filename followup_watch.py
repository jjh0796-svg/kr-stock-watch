"""Evidence-bound follow-ups in the existing filing state; no extra Telegram poller."""
import hashlib
import html
import re
from datetime import date, datetime, timedelta, timezone
import requests

KST=timezone(timedelta(hours=9))
URL='https://dart.fss.or.kr/dsaf001/main.do?rcpNo='
LABELS={'payment':'납입 예정','subscription_start':'청약 시작 예정','subscription_end':'청약 마감 예정','listing':'상장 예정'}

def day():return datetime.now(KST).date()

def parse_date(value):
    m=re.search(r'(20\d{2})\s*[년./-]\s*(\d{1,2})\s*[월./-]\s*(\d{1,2})',str(value or ''))
    if not m:return None
    try:return date(*map(int,m.groups())).isoformat()
    except ValueError:return None

def dates_from_summary(text):
    text=html.unescape(re.sub('<[^>]+>','',text))
    result={}
    for key,label in [('payment','납입일:'),('listing','상장예정:')]:
        m=re.search(re.escape(label)+r'([^\n·]+)',text)
        if m:result[key]=parse_date(m[1])
    return result

def scope_for(token,chat):return hashlib.sha256(f'{token}|{chat}'.encode()).hexdigest()

def observe(state,scope,item,family,dates):
    """Only call after acknowledged delivery. An omitted date in a correction is unknown."""
    events=state.setdefault('followup_events',{}).setdefault(scope,{})
    matches=[(k,e) for k,e in events.items() if set(e['family']) & set(family)]
    if len(matches)>1:return  # ambiguous families never merge
    key,event=matches[0] if matches else (item['rcept_no'],None)
    if event and item['rcept_no']<event['latest']:return  # delayed old summaries cannot restore obsolete dates
    if event is None:
        if not dates or not any(dates.values()):return
        event={'family':[],'observed':[],'dates':{},'notices':{},'created':day().isoformat()}
        events[key]=event
    event.update(corp=item.get('corp_name',''),corp_code=item.get('corp_code',''),
                 latest=item['rcept_no'],title=item.get('report_nm',''),active=True)
    event['family']=sorted(set(event['family'])|set(family)|{item['rcept_no']})
    event['observed']=sorted(set(event['observed'])|{item['rcept_no']})
    if dates is not None:event['dates']={k:parse_date(v) for k,v in dates.items() if k in LABELS}
    event['checked']=None

def fetch_company(key,corp,start,end):
    items=[]
    for page in range(1,4):
        r=requests.get('https://opendart.fss.or.kr/api/list.json',params={
            'crtfc_key':key,'corp_code':corp,'bgn_de':start,'end_de':end,
            'page_no':page,'page_count':100,'sort':'date','sort_mth':'asc'},timeout=(10,20))
        r.raise_for_status();data=r.json()
        if data.get('status')=='013':return items
        if data.get('status')!='000':raise RuntimeError('DART list unavailable')
        items.extend(data.get('list',[]))
        if page>=int(data.get('total_page',1)):return items
    raise RuntimeError('DART list truncated')

def refresh(state,scope,key,send,save,*,today=None,fetch=fetch_company,family_lookup=None):
    """Check up to ten companies/run. Failed/incomplete checks never become no-result claims."""
    from filing_threads import related_receipts
    family_lookup=family_lookup or related_receipts
    today=today or day();stamp=today.isoformat()
    events=state.get('followup_events',{}).get(scope,{})
    for e in events.values():
        if (today-date.fromisoformat(e['created'])).days>90:e['active']=False
    eligible=[e for e in events.values() if e.get('active') and e.get('user_tracking',True) and e.get('corp_code')]
    attempts=state.setdefault('followup_scan_attempts',{}).setdefault(scope,{})
    hour=stamp+':'+str(datetime.now(KST).hour)
    corps=[]
    lookup_budget=12
    for e in eligible:
        if e.get('checked')!=stamp and attempts.get(e['corp_code'])!=hour and e['corp_code'] not in corps:corps.append(e['corp_code'])
    for corp in corps[:10]:
        attempts[corp]=hour;save(state)
        group=[e for e in eligible if e['corp_code']==corp]
        start=min([e.get('checked') or e['created'] for e in group])
        start=max(date.fromisoformat(start)-timedelta(days=1),today-timedelta(days=90))
        try:items=fetch(key,corp,start.strftime('%Y%m%d'),today.strftime('%Y%m%d'))
        except Exception as exc:
            print('[followup scan unavailable]',corp,type(exc).__name__);continue
        complete=True
        # No association by company/name alone. DART must explicitly link the receipts.
        for item in sorted(items,key=lambda i:i['rcept_no']):
            title=item.get('report_nm','');rn=item['rcept_no']
            if not any(w in title for w in ('정정','철회','발행실적','발행결과','발행조건확정')):continue
            if any(rn in e['observed'] for e in group):continue
            if all(rn<e['latest'] for e in group):continue
            if lookup_budget<=0:
                complete=False;break
            lookup_budget-=1
            family=family_lookup(rn)
            if len(family)<=1:
                # Could be an independent filing or a blocked family lookup: never claim a complete check.
                complete=False;continue
            matches=[e for e in group if set(e['family']) & set(family)]
            if len(matches)!=1:continue
            e=matches[0]
            if rn in e['observed']:continue
            outcome='철회 공시 확인' if '철회' in title else '발행결과 공시 확인' if any(w in title for w in ('발행실적','발행결과')) else '후속 공시 확인'
            note='결과 공시 접수 사실입니다. 실제 납입금액·완료 여부는 원문 확인이 필요합니다.' if '발행결과' in outcome else '기존 예정 일정을 다시 확인해야 합니다.'
            text=f'🔎 [{e["corp"]} · {outcome}]\n{title}\n{note}\n이전 공시: {URL}{e["latest"]}\n{URL}{rn}'
            # Reply through a known receipt; explicit family was verified above.
            parent={'rcept_no':e['latest']}
            if send(parent,text):
                e['family']=sorted(set(e['family'])|set(family));e['latest']=rn
                e['observed'].append(rn)
                e['dates']={}  # Never remind stale dates after an unparsed amendment.
                if '철회' in title or '발행결과' in outcome:e['active']=False
                save(state)
            else:complete=False
        if complete:
            for e in group:e['checked']=stamp
            save(state)
    sent=0
    for e in events.values():
        if not e.get('active') or not e.get('user_tracking',True) or e.get('mute_reminders') or e.get('checked')!=stamp:continue
        for kind,raw in e.get('dates',{}).items():
            if not raw:continue
            due=date.fromisoformat(raw);delta=(due-today).days
            # First business-day run before weekend deadlines is covered on Friday.
            horizon=3 if today.weekday()==4 else 1
            mode='upcoming' if 0<=delta<=horizon else 'unconfirmed' if kind=='payment' and 2<=-delta<=14 else None
            if not mode:continue
            key_notice=kind+':'+raw+':'+mode
            if key_notice in e['notices']:continue
            if sent>=5:return
            if mode=='upcoming':
                text=f'📅 [{e["corp"]} · 다가오는 일정]\n{LABELS[kind]}: {raw}'
            else:
                text=f'🔎 [{e["corp"]} · 납입 결과 확인 필요]\n공시상 납입 예정일: {raw}\n{stamp} 조회한 DART 공시 중 이 건에 명시적으로 연결되는 결과 공시를 확인하지 못했습니다.'
            text+='\n'+URL+e['latest']
            if send({'rcept_no':e['latest']},text):
                e['notices'][key_notice]=stamp;sent+=1;save(state)
