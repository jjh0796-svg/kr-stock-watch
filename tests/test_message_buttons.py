import copy,json
from datetime import date
from unittest.mock import Mock,patch
import pytest
import common,dart_watch
from message_buttons import prepare,register_message,apply_callback,keyboard,handle_callback
from followup_watch import refresh,scope_for,observe
from filing_threads import send_filing

def setup():
    scope=scope_for('token','42');state={}
    item={'rcept_no':'20260916000001','corp_code':'00000001','corp_name':'A','report_nm':'유상증자결정'}
    ref=prepare(state,scope,item,{'payment':'2026-09-18'});register_message(state,scope,ref,123)
    return state,scope,ref,item

def query(ref,action,actor=42,chat=42,mid=123):
    return {'id':'callback','data':f'fw:{ref}:{action}','from':{'id':actor},
            'message':{'message_id':mid,'chat':{'id':chat,'type':'private'}}}

def test_selection_survives_reload_and_same_click_is_idempotent():
    state,scope,ref,item=setup()
    apply_callback(state,scope,query(ref,'track'),'42')
    state=json.loads(json.dumps(state))
    apply_callback(state,scope,query(ref,'track'),'42')
    event=state['followup_events'][scope][item['rcept_no']]
    assert event['user_tracking'] is True
    apply_callback(state,scope,query(ref,'stop'),'42')
    assert event['user_tracking'] is False
    # Corrected dates must not re-enable user-disabled tracking.
    observe(state,scope,{**item,'rcept_no':'20260917000001'},[item['rcept_no'],'20260917000001'],{'payment':'2026-10-18'})
    assert event['user_tracking'] is False

@pytest.mark.parametrize('actor,chat,mid',[(99,42,123),(42,99,123),(42,42,999)])
def test_forged_or_other_user_callback_changes_nothing(actor,chat,mid):
    state,scope,ref,item=setup();before=copy.deepcopy(state)
    apply_callback(state,scope,query(ref,'track',actor,chat,mid),'42')
    assert state==before

def test_vote_replaces_previous_vote_without_suppressing_filings():
    state,scope,ref,item=setup()
    apply_callback(state,scope,query(ref,'up'),'42')
    apply_callback(state,scope,query(ref,'down'),'42')
    assert state['message_buttons'][scope][ref]['vote']=='down'
    assert not state.get('followup_events')
    assert '✓' in keyboard(state,scope,ref)['inline_keyboard'][1][1]['text']

def test_mute_suppresses_reminders_but_keeps_result_checks_and_can_be_reversed():
    state,scope,ref,item=setup()
    with patch('message_buttons.day',return_value=date(2026,9,16)):
        apply_callback(state,scope,query(ref,'mute'),'42')
    send=Mock(return_value=True);fetch=Mock(return_value=[])
    refresh(state,scope,'key',send,lambda s:None,today=date(2026,9,17),fetch=fetch)
    assert fetch.called;send.assert_not_called()
    apply_callback(state,scope,query(ref,'unmute'),'42')
    refresh(state,scope,'key',send,lambda s:None,today=date(2026,9,17),fetch=fetch)
    assert send.call_count==1

def test_stop_prevents_followup_queries():
    state,scope,ref,item=setup()
    apply_callback(state,scope,query(ref,'stop'),'42');fetch=Mock();send=Mock()
    refresh(state,scope,'key',send,lambda s:None,fetch=fetch)
    fetch.assert_not_called();send.assert_not_called()

def test_failed_persistence_never_acknowledges_success():
    state,scope,ref,item=setup()
    with patch('message_buttons.requests.post') as post,pytest.raises(OSError):
        handle_callback(state,query(ref,'up'),'token','42',Mock(side_effect=OSError()))
    post.assert_not_called()

def test_callback_storage_failure_does_not_advance_offset():
    state,scope,ref,item=setup();cfg={'tg_offset':7,'tg_token_tail':'token'}
    response=Mock();response.json.return_value={'ok':True,'result':[{'update_id':8,'callback_query':query(ref,'up')}]}
    with patch.dict('os.environ',{'TELEGRAM_BOT_TOKEN':'token','TELEGRAM_CHAT_ID':'42'}),patch.object(dart_watch,'DRY_RUN',False),patch.object(dart_watch.requests,'get',return_value=response),patch.object(dart_watch,'handle_callback',side_effect=OSError()):
        assert not dart_watch.process_commands(cfg,state=state)
    assert cfg['tg_offset']==7

def test_keyboard_on_last_chunk_only():
    sender=Mock(side_effect=[1,2,3]);markup={'inline_keyboard':[]}
    with patch('filing_threads.related_receipts',return_value=['r']):
        send_filing({}, {'rcept_no':'r'},'x'*4000,sender,lambda s:None,'b','c',reply_markup=markup)
    assert 'reply_markup' not in sender.call_args_list[0].kwargs
    assert sender.call_args_list[-1].kwargs['reply_markup']==markup

def test_live_sender_serializes_markup():
    response=Mock(status_code=200);response.json.return_value={'ok':True,'result':{'message_id':123}}
    with patch.dict('os.environ',{'TELEGRAM_BOT_TOKEN':'token','TELEGRAM_CHAT_ID':'42'}),patch.object(common,'DRY_RUN',False),patch.object(common.requests,'post',return_value=response) as post:
        assert common.tg_send('body',reply_markup={'inline_keyboard':[]})==123
    assert post.call_args.kwargs['json']['reply_markup']=={'inline_keyboard':[]}
