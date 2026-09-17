from pathlib import Path
from datetime import datetime,timezone,timedelta
from unittest.mock import Mock,patch
import summarize as s
ROOT=Path(__file__).parent/'fixtures'

def test_multiple_exercise_dates_and_current_amendment():
    for rn,price,listing,count in [('20260917900508','2,323','2026-10-23',2),('20260917900467','5,552','2026-10-02',1)]:
        with patch('issue_terms.fetch_xml',return_value=(ROOT/(rn+'.xml')).read_text(encoding='utf-8')),patch.object(s,'latest_close',return_value='최근 종가: 3,000원 (2026-09-17)'):
            text=s._sum_exercise('',rn,{'code':'x'})
        assert f'전환청구 단가: {price}원' in text
        assert text.count('청구 2026-')==count
        assert listing in text and '최근 종가: 3,000원 (2026-09-17)' in text
        assert '상장예정 2026-09-28' not in text

def test_close_excludes_current_session_until_closed():
    rows=[{'localTradedAt':'2026-09-18','closePrice':'3,100'},{'localTradedAt':'2026-09-17','closePrice':'3,000'}]
    for status,hour,want in [('OPEN',14,'3,000'),('PREOPEN',8,'3,000'),('CLOSE',16,'3,100')]:
        basic={'marketStatus':status,'localTradedAt':f'2026-09-18T{hour:02d}:00:00+09:00'}
        with patch.object(s.requests,'get',side_effect=[Mock(json=lambda:basic),Mock(json=lambda:rows)]):
            assert want in s.latest_close('x',datetime(2026,9,18,hour,tzinfo=timezone(timedelta(hours=9))))
