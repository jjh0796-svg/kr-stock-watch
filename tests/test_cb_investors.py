from pathlib import Path
import re
from issue_terms import summarize_xml, fund_investors, Fields
from cb_investors import fund_map

FIXTURES=Path(__file__).parent/'fixtures'

def render(receipt):
    return summarize_xml((FIXTURES/(receipt+'.xml')).read_text(encoding='utf-8'),'주요사항보고서(전환사채권발행결정)')

def test_atlas_real_funds_and_direct_subscribers():
    text=render('20260921000354')
    for part in ['한국채권투자운용 90.0억원','한국채권 스마트 메자닌 일반사모투자신탁2호[전문] · 55.0억원','한국채권 스마트 코스닥벤처 일반사모투자신탁1호 · 35.0억원','비에프에이 6.0억원','대호피엔씨 3.0억원','박지호 16.0억원','엄혜정 1.0억원','배정 합계: 135.0억원 (발행금액과 일치)']:
        assert part in text
    assert '한국투자증권' not in text and '외 4곳' not in text
    assert len(text.encode('utf-16-le'))//2<3000

def test_special_construction_managers_and_explicit_executive_member():
    text=render('20260921000401')
    for part in ['라이노스자산운용 45.0억원','제26호 · 18.0억원','코스닥벤처메자닌 일반 사모증권투자신탁 제6호 · 15.0억원','코스닥벤처공모주 일반 사모증권투자신탁 제6호 · 12.0억원','에이피캐피탈 (업무집행조합원) 55.0억원','에이피기술투자조합 2호 · 55.0억원','배정 합계: 100.0억원 (발행금액과 일치)']:
        assert part in text
    assert '삼성증권' not in text and '신한투자증권' not in text
    assert '아시아프라퍼티' not in text  # LP is not the executive member.
    assert '인수 예정액' in text

def tables(rows):
    data=[['구분','집합투자기구','집합투자업자','신탁업자']]+rows
    return '<TABLE>'+''.join('<TR>'+''.join('<TD>'+c+'</TD>' for c in row)+'</TR>' for row in data)+'</TABLE>'

def test_partial_mapping_keeps_good_fund_and_direct_investor():
    raw=tables([['펀드 1','실제 A펀드','A운용','X증권']])
    f={'ISSU_NM':['X증권(“본건 펀드1”의 신탁업자 지위에서)','Y증권(본건 펀드2의 신탁업자 지위에서)','직접법인'],'ISSU_AMT':['100000000','200000000','300000000']}
    text='\n'.join(fund_investors(raw,f,'600000000'))
    assert 'A운용 1.0억원' in text and '직접법인 3.0억원' in text
    assert '본건 펀드 2 · 2.0억원 (운용사·배정 연결 미확인)' in text
    assert 'Y증권 2.0억원' not in text

def test_cross_manager_pool_not_split_or_double_counted():
    raw=tables([['본건 펀드 1','A펀드','A운용','X'],['본건 펀드 2','B펀드','B운용','Y']])
    text='\n'.join(fund_investors(raw,{'ISSU_NM':['X(본건 펀드 1, 2의 신탁업자 지위에서)'],'ISSU_AMT':['100000000']},'100000000'))
    assert '운용사별 배분 미공개' in text
    assert 'A운용 1.0억원' not in text and 'B운용 1.0억원' not in text
    assert 'A펀드 / B펀드 · 1.0억원' in text

def test_header_columns_and_escaping():
    raw='<TABLE><TR><TD>구분</TD><TD>신탁업자</TD><TD>집합투자업자</TD><TD>집합투자기구</TD></TR><TR><TD>펀드1</TD><TD>TRUSTEE</TD><TD>A &amp; B운용</TD><TD>성장펀드</TD></TR></TABLE>'
    assert fund_map(raw)=={1:('성장펀드','A & B운용')}
    text='\n'.join(fund_investors(raw,{'ISSU_NM':['TRUSTEE(본건 펀드 1의 신탁업자 지위에서)'],'ISSU_AMT':['100000000']},'100000000'))
    assert 'A &amp; B운용' in text and 'TRUSTEE' not in text

def test_conflicting_fund_definition_not_guessed():
    raw=tables([['펀드 1','A펀드','A운용','X'],['펀드 1','B펀드','B운용','Y']])
    assert fund_map(raw)=={}

def test_mismatched_allocation_columns_not_zipped():
    assert fund_investors('',{'ISSU_NM':['펀드1','개인'],'ISSU_AMT':['100']},'100') is None

def test_fund_names_without_manager_column_grouped_by_brand():
    """10/6 에코앤드림 CB: '구분 | 집합투자기구' 표만 있고 운용사 열 없음 → 40줄 '연결 미확인'이던 것."""
    text=render('20261006000446')
    for part in ['에이원 (펀드명 기준 · 펀드 9개) 70.0억원','씨스퀘어 (펀드명 기준 · 펀드 5개) 50.0억원',
                 '타임폴리오 (펀드명 기준 · 펀드 3개) 30.0억원','안다 (펀드명 기준 · 펀드 1개) 15.0억원',
                 '비엔케이투자증권 10.0억원','배정 합계: 350.0억원 (발행금액과 일치)','※ 펀드명 기준 =']:
        assert part in text
    assert '연결 미확인' not in text and '본건 펀드' not in text
    assert '케이비증권' not in text            # 신탁업자는 투자자가 아니다
    assert len(text.encode('utf-16-le'))//2<2500

def test_fund_brand():
    from cb_investors import fund_brand
    cases={'에이원갤럭시코스닥벤처일반사모증권투자신탁2호':'에이원','타임폴리오 코스닥벤처 Mezzanine S 5호':'타임폴리오',
           '안다H 코스닥벤처 일반사모투자신탁 제6호(전문)':'안다','NH헤지 코벤 메자닌 일반 사모투자신탁 제4호':'NH헤지',
           '라이프IPO코스닥벤처일반사모투자신탁제13호':'라이프','수성국민성장코스닥벤처 일반 사모투자신탁':'수성',
           '일반사모투자신탁':'', '':''}
    for name,want in cases.items():
        assert fund_brand(name)==want, name

def test_manager_column_missing_still_keeps_fund_name_when_brand_unknown():
    raw='<TABLE><TR><TD>구분</TD><TD>집합투자기구</TD></TR><TR><TD>본건 펀드 1</TD><TD>일반사모투자신탁 1호</TD></TR></TABLE>'
    text='\n'.join(fund_investors(raw,{'ISSU_NM':['X증권(본건 펀드1의 신탁업자 지위에서)'],'ISSU_AMT':['100000000']},'100000000'))
    assert '일반사모투자신탁 1호 · 1.0억원 (운용사 미기재)' in text
