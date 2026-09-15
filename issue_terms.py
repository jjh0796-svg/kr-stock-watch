"""Extract the current issuance form's named XML fields, not amendment-before columns."""
import html
import io
import re
import zipfile
from decimal import Decimal,InvalidOperation
from html.parser import HTMLParser
import requests

class Fields(HTMLParser):
    def __init__(self):
        super().__init__();self.fields={};self.active=None;self.parts=[];self.tag=None
    def handle_starttag(self,tag,attrs):
        attrs=dict(attrs)
        if tag in ('te','tu','td') and ('acode' in attrs or 'aunit' in attrs):
            self.active=attrs.get('acode') or attrs['aunit'];self.tag=tag;self.parts=[]
    def handle_data(self,data):
        if self.active:self.parts.append(data)
    def handle_endtag(self,tag):
        if self.active and tag==self.tag:
            self.fields.setdefault(self.active,[]).append(re.sub(r'\s+',' ',''.join(self.parts)).strip())
            self.active=None

def fetch_xml(key,receipt):
    response=requests.get('https://opendart.fss.or.kr/api/document.xml',params={'crtfc_key':key,'rcept_no':receipt},timeout=(10,30))
    response.raise_for_status()
    archive=zipfile.ZipFile(io.BytesIO(response.content))
    names=[n for n in archive.namelist() if n.lower().endswith(('.xml','.html','.htm'))]
    exact=[n for n in names if n.rsplit('/',1)[-1].split('.')[0]==receipt]
    if not names:return ''
    raw=archive.read(exact[0] if exact else max(names,key=lambda n:archive.getinfo(n).file_size))
    for encoding in ('utf-8','euc-kr','cp949'):
        try:return raw.decode(encoding)
        except UnicodeDecodeError:pass
    return ''

def number(value):
    if not re.fullmatch(r'-?\d[\d,]*(?:\.\d+)?',value or ''):return None
    try:return Decimal(value.replace(',',''))
    except InvalidOperation:return None

def summarize_xml(raw,title):
    parser=Fields();parser.feed(raw);f=parser.fields
    def get(*keys):
        for key in keys:
            values=[v for v in f.get(key,[]) if v and v!='-']
            if values:return values[-1]
        return ''
    def show(value,suffix=''):return html.escape(value+suffix) if value else '미확인'
    def amount(value):
        n=number(value)
        return f'{n/Decimal(100000000):,.2f}억원 ({n:,.0f}원)' if n is not None else '미확인'
    bond=any(s in title for s in ('전환사채','교환사채','신주인수권부사채'))
    funds=[(label,get(key)) for label,key in [('시설','FND_USE1'),('영업양수','FND_USE_SQ'),('운영','FND_USE2'),('채무상환','FND_USE_RD'),('타법인 증권취득','ANC_ACQ_PRC' if bond else 'ANC_ACQ_AMT'),('기타','FND_USE3')]]
    purposes=[(label,number(v)) for label,v in funds if number(v) is not None and number(v)>0]
    targets=list(dict.fromkeys(v for v in f.get('ISSU_NM' if bond else 'PART',[]) if v and v!='-'))
    if not get('DNM_SUM' if bond else 'CST_CNT','PST_CNT'):return None
    lines=['<b>발행조건 · 해당 접수번호 원문 기준</b>']
    if bond:
        kind='EB' if '교환사채' in title else 'BW' if '신주인수권부' in title else 'CB'
        label={'EB':'교환','BW':'행사','CB':'전환'}[kind]
        lines += [f"{kind} {show(get('SEQ_NO'))}회차 · {show(get('ISSU_MTH'))}",f"발행금액: {amount(get('DNM_SUM'))}",
                  f"{label}가액: {show(get('EXE_PRC'),'원')}",f"{label}대상: {show(get('STK_KND'))} · {show(get('STK_CNT'),'주')}",
                  f"주식총수 대비: {show(get('STK_RT'),'%')} (공시 기재 기준)",
                  f"표면/만기 이자율: {show(get('PRFT_RATE'),'%')} / {show(get('LST_RTN_RT'),'%')}",
                  f"납입일: {show(get('PYM_DT'))} · 만기일: {show(get('EXP_DT'))}",
                  f"{label}청구: {show(get('SB_BGN_DT'))} ~ {show(get('SB_END_DT'))}"]
        minimum=get('MIN_PRC')
        if minimum:lines.append(f'가격조정 최저가: {show(minimum,"원")}')
        elif get('EXE_REG'):lines.append('가격조정: 조항 있음 · 최저가 숫자는 미확인, 원문 확인')
        option=get('OPT_FCT')
        for label,pattern in [('사채권자 조기상환(풋)',r'Put\s*Option'),('발행사 등 권리(콜)',r'Call\s*Option')]:
            match=re.search(pattern,option,re.I)
            if match:
                excerpt=option[match.start():match.start()+170]
                lines.append(label+': '+html.escape(excerpt)+'… [원문 일부]')
        if not option:lines.append('풋·콜옵션: 미확인 · 원문 확인')
        elif not re.search(r'(Put|Call)\s*Option',option,re.I):lines.append('옵션 기타사항: '+html.escape(option[:200])+'… [원문 일부]')
    else:
        total=sum((n for _,n in purposes),Decimal(0))
        lines += [f"방식: {show(get('CI_MTH'))}",f'조달금액(자금용도 합계): {amount(str(total)) if purposes else "미확인"}',
                  f"신주: 보통주 {show(get('CST_CNT'),'주')} · 기타주 {show(get('PST_CNT') or ('해당 없음' if f.get('PST_CNT')==['-'] else ''))}",
                  f"발행가액: 보통주 {show(get('CST_ISS_VAL'),'원')} · 기타주 {show(get('PST_ISS_VAL') or ('해당 없음' if f.get('PST_ISS_VAL')==['-'] else ''))}",
                  f"할인·할증률: {show(get('DC_RATE'),'%')} (원문 부호 유지)",
                  f"납입일: {show(get('PYM_DT'))} · 상장예정: {show(get('LST_PLN_DT'))}"]
        if get('ETC'):lines.append('배정 비고: '+show(get('ETC')[:160]))
    lines.append('대상: '+(' / '.join(html.escape(v) for v in targets[:6])+(f' 외 {len(targets)-6}곳' if len(targets)>6 else '') if targets else '미확인'))
    lines.append('자금용도: '+(' · '.join(label+' '+amount(str(n)) for label,n in purposes) if purposes else '미확인'))
    return '\n'.join(lines)

def issuance_summary(item,key):
    try:return summarize_xml(fetch_xml(key,item['rcept_no']),item.get('report_nm',''))
    except Exception as e:
        print('[발행조건 원문 대기]',item.get('rcept_no'),type(e).__name__);return None

def needs_retry(summary):
    if not summary:return True
    core=[line for line in summary.splitlines() if line.startswith(('발행금액:','조달금액','발행가액:','전환가액:','행사가액:','교환가액:','납입일:','대상:','표면/만기'))]
    return not core or any('미확인' in line for line in core)
