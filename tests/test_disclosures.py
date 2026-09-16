import copy
import unittest
from datetime import datetime,timezone
from pathlib import Path
from unittest.mock import patch
from issue_terms import summarize_xml,needs_retry
from filing_threads import FamilyParser,send_filing
import summarize
import dart_watch

FIXTURES=Path(__file__).parent/'fixtures'
class IssuanceTests(unittest.TestCase):
    def test_gwangjin_includes_both_targets_and_exact_terms(self):
        text=summarize_xml((FIXTURES/'20260915000331.xml').read_text(encoding='utf-8'),'유상증자결정')
        for value in ['3,260원','613,496주','1,999,996,960원','09월 30일','디에스케이디','더블유에이치에너지조합']:
            self.assertIn(value,text)
        self.assertFalse(needs_retry(text))
    def test_amendment_uses_current_named_fields_not_old_plain_table(self):
        raw='<TD>정정 전 전환가액 999999</TD>'+(FIXTURES/'20260915000270.xml').read_text(encoding='utf-8')
        text=summarize_xml(raw,'[기재정정]전환사채권발행결정')
        self.assertIn('전환가액: 909원',text);self.assertIn('0% / 2%',text);self.assertNotIn('999999',text)
    def test_cb_and_eb_have_distinct_shares_and_labels(self):
        text=summarize_xml((FIXTURES/'20260915000382.xml').read_text(encoding='utf-8'),'전환사채권발행결정')
        self.assertIn('5,806원',text);self.assertIn('1,205,690주',text)
        raw=(FIXTURES/'20260911000411.xml').read_text(encoding='utf-8')+'<TE ACODE="OPT_FCT">조기상환청구권(Call Option) 발행회사는 조건에 따라 조기상환 가능</TE>'
        text=summarize_xml(raw,'교환사채권발행결정')
        self.assertIn('교환가액: 270,000원',text);self.assertIn('가온전선',text)
        self.assertIn('발행사 등 권리(콜)',text);self.assertNotIn('사채권자 조기상환(풋)',text)
    def test_no_other_receipt_substitution(self):
        self.assertIsNone(summarize._pick([{'rcept_no':'other'}],'wanted'))
    def test_target_only_pending_retries_without_pending_summary(self):
        state={'pending_tgt':{'20260915000331':{'title':'유상증자결정','code':'026910','corp':'광진실업'}}}
        with patch.object(dart_watch,'now_kst',return_value=datetime(2026,9,16,tzinfo=timezone.utc)),patch.object(dart_watch,'stock_snapshot',return_value=None),patch.object(dart_watch,'summarize',return_value='발행금액: 20억\n대상: A'),patch.object(dart_watch,'edit_existing_disclosure',return_value=True) as send,patch.object(dart_watch,'save_state'):
            dart_watch.retry_pending_summaries('fake',state)
        self.assertTrue(send.called);self.assertFalse(state['pending_sum']);self.assertNotIn('pending_tgt',state)

class ThreadTests(unittest.TestCase):
    def test_family_select_only(self):
        parser=FamilyParser();parser.feed('<select id="family"><option value="rcpNo=20260915000331">main</option></select><select id="att"><option value="rcpNo=19990101000000">attachment</option></select>')
        self.assertEqual(parser.receipts,['20260915000331'])
    def test_latest_reply_survives_restore_and_does_not_mix_issuances(self):
        state={};calls=[]
        def sender(text,**kwargs):calls.append(kwargs);return 100+len(calls)
        family=['20260728000487','20260908000396','20260915000237']
        with patch('filing_threads.related_receipts',return_value=family):
            for i,rn in enumerate(family):
                send_filing(state,{'rcept_no':rn},str(i),sender,lambda s:None,'bot','chat')
                state=copy.deepcopy(state)
        self.assertEqual([c.get('reply_to_message_id') for c in calls],[None,101,102])
        with patch('filing_threads.related_receipts',return_value=['other']):
            send_filing(state,{'rcept_no':'other'},'different',sender,lambda s:None,'bot','chat')
        self.assertIsNone(calls[-1]['reply_to_message_id'])
    def test_scoping_replay_and_uncertain(self):
        state={};calls=[]
        def sender(text,**kwargs):calls.append(kwargs);return True
        with patch('filing_threads.related_receipts',return_value=['one']):
            for _ in range(2):send_filing(state,{'rcept_no':'one'},'same',sender,lambda s:None,'bot','chat')
            self.assertEqual(len(calls),1)
            send_filing(state,{'rcept_no':'one'},'same',sender,lambda s:None,'bot','another')
        self.assertEqual(len(calls),2);self.assertIsNone(calls[-1]['reply_to_message_id'])
    def test_rejected_delivery_not_recorded(self):
        state={}
        with patch('filing_threads.related_receipts',return_value=['one']):
            self.assertFalse(send_filing(state,{'rcept_no':'one'},'x',lambda *a,**k:False,lambda s:None,'b','c'))
        book=next(iter(state['filing_replies'].values()))
        self.assertFalse(book['receipts']);self.assertFalse(book['deliveries'])

if __name__=='__main__':unittest.main()
