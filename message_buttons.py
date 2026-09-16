"""Owner-only callbacks handled by the existing DART poller and durable state cache."""
import hashlib
from datetime import datetime,timezone
import requests
from followup_watch import day,scope_for

def contexts(state,scope):return state.setdefault('message_buttons',{}).setdefault(scope,{})

def prepare(state,scope,item,dates):
    ref=hashlib.sha256(item['rcept_no'].encode()).hexdigest()[:16]
    entry=contexts(state,scope).setdefault(ref,{'item':dict(item),'dates':dates or {},'messages':[]})
    if item.get('corp_code'):entry['item']=dict(item)
    if dates:entry['dates']=dates
    return ref

def event_for(state,scope,entry):
    rn=entry['item']['rcept_no']
    family=state.get('filing_replies',{}).get(scope,{}).get('receipts',{}).get(rn,{}).get('family',[rn])
    matches=[e for e in state.get('followup_events',{}).get(scope,{}).values() if set(e['family']) & set(family)]
    return matches[0] if len(matches)==1 else None,family

def keyboard(state,scope,ref):
    entry=contexts(state,scope)[ref];event,_=event_for(state,scope,entry)
    tracking=bool(event and event.get('active') and event.get('user_tracking',True))
    muted=bool(event and event.get('mute_reminders'))
    vote=entry.get('vote')
    def button(text,action):return {'text':text,'callback_data':f'fw:{ref}:{action}'}
    return {'inline_keyboard':[
        [button('추적 중 · 끄기' if tracking else '이 사건 추적','stop' if tracking else 'track'),
         button('일정 알림 켜기' if muted else '일정 알림 끄기','unmute' if muted else 'mute')],
        [button(('✓ ' if vote=='up' else '')+'👍 유용함','up'),button(('✓ ' if vote=='down' else '')+'👎 불필요함','down')]]}

def register_message(state,scope,ref,mid):
    if type(mid) is not int:return
    entry=contexts(state,scope)[ref]
    if mid not in entry['messages']:entry['messages'].append(mid)

def apply_callback(state,scope,query,allowed_chat):
    """Validate actor, destination, registered message and a fixed action. No network."""
    msg=query.get('message') or {};chat=msg.get('chat') or {}
    if chat.get('type')!='private' or str(chat.get('id'))!=str(allowed_chat) or str(query.get('from',{}).get('id'))!=str(allowed_chat):
        return '이 버튼은 수신자 본인만 사용할 수 있습니다.',None
    parts=str(query.get('data','')).split(':')
    if len(parts)!=3 or parts[0]!='fw' or parts[2] not in {'track','stop','mute','unmute','up','down'}:
        return '지원하지 않는 버튼입니다.',None
    _,ref,action=parts
    entry=state.get('message_buttons',{}).get(scope,{}).get(ref)
    if not entry or msg.get('message_id') not in entry['messages']:
        return '이 버튼의 저장 기록을 찾지 못했습니다. 새 메시지의 버튼을 이용해 주세요.',None
    if action in ('up','down'):
        entry['vote']=action;answer='평가를 저장했습니다. 반대 버튼을 누르면 수정됩니다. 평가만으로 공시를 차단하지 않습니다.'
    else:
        item=entry['item'];event,family=event_for(state,scope,entry)
        if not item.get('corp_code'):
            return '회사 식별정보가 없어 추적을 시작할 수 없습니다.',ref
        if event is None:
            # A manual selection may track filings without a scheduled date too.
            event={'family':family,'observed':[item['rcept_no']],'dates':entry['dates'],
                   'notices':{},'created':day().isoformat(),'latest':item['rcept_no'],
                   'corp':item.get('corp_name',''),'corp_code':item['corp_code'],
                   'title':item.get('report_nm',''),'active':True,'user_tracking':action!='stop'}
            state.setdefault('followup_events',{}).setdefault(scope,{})[item['rcept_no']]=event
        if action in ('track','stop'):
            if action=='track' and not event.get('active'):
                return '종료되었거나 추적 기간이 지난 사건입니다. 최신 공시에서 추적해 주세요.',ref
            event['user_tracking']=action=='track'
            answer='후속 공시 추적을 켰습니다.' if action=='track' else '추가 후속 확인을 껐습니다. 원래 구독 중인 공시는 계속 받습니다.'
        else:
            event['mute_reminders']=action=='mute'
            answer='이 사건의 예정일·결과 미확인 안내를 껐습니다. 정정·철회·결과 공시는 유지합니다.' if action=='mute' else '이 사건의 일정 안내를 켰습니다. 이미 보낸 안내는 반복하지 않습니다.'
    entry['updated']=datetime.now(timezone.utc).isoformat()
    return answer,ref

def handle_callback(state,query,token,chat,save):
    scope=scope_for(token,chat)
    answer,ref=apply_callback(state,scope,query,chat)
    save(state)  # Persist BEFORE acknowledging success or advancing getUpdates offset.
    for method,payload in [('answerCallbackQuery',{'callback_query_id':query['id'],'text':answer,'show_alert':False}),
                          ('editMessageReplyMarkup',{'chat_id':chat,'message_id':(query.get('message') or {}).get('message_id'),
                                                    'reply_markup':keyboard(state,scope,ref) if ref else None})]:
        if method=='editMessageReplyMarkup' and not ref:continue
        try:
            response=requests.post(f'https://api.telegram.org/bot{token}/{method}',json=payload,timeout=(5,10))
            if not response.json().get('ok'):print('[buttons UI update unavailable]',method)
        except Exception as exc:print('[buttons UI update unavailable]',method,type(exc).__name__)
