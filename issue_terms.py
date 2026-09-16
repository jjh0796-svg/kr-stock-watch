"""Extract the current issuance form's named XML fields, not amendment-before columns."""
import html
import io
import re
import zipfile
from decimal import Decimal,InvalidOperation,ROUND_HALF_UP
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

class TableRows(HTMLParser):
    def __init__(self):
        super().__init__();self.rows=[];self.row=[];self.cell=None
    def handle_starttag(self,tag,attrs):
        if tag=='tr':self.row=[]
        if tag in ('td','th'):self.cell=[]
    def handle_data(self,data):
        if self.cell is not None:self.cell.append(data)
    def handle_endtag(self,tag):
        if tag in ('td','th') and self.cell is not None:
            self.row.append(re.sub(r'\s+',' ',''.join(self.cell)).strip());self.cell=None
        if tag=='tr' and self.row:self.rows.append(self.row)

def subsidiary_summary(raw):
    """KRX subsidiary capital contributions use plain tables, often no per-share amount."""
    parser=TableRows();parser.feed(raw)
    starts=[i for i,row in enumerate(parser.rows) if row[0]=='종속회사인' and len(row)>=2]
    if not starts:return None
    rows=parser.rows[starts[-1]:]  # exclude the preceding before/after amendment table
    company=rows[0][1]
    def field(label):
        for row in rows:
            if re.sub(r'^\d+\.\s*','',row[0])==label and len(row)>1:return row[-1]
        return ''
    method=field('증자방식');due=field('납입일');note=field('기타 투자판단과 관련한 중요사항')
    if not note:
        note=next((r[-1] for r in rows if re.match(r'^22\.',r[0])), '')
    # These two templates explicitly describe a non-share-based overseas contribution.
    # Do not use this branch for ordinary third-party share allotments or unknown layouts.
    if method!='주주배정증자' or not all(w in note for w in ('해외','주식수','기재하지 않습니다')):return None
    purposes=[]
    for label in ['시설자금','영업양수자금','운영자금','채무상환자금','타법인 증권 취득자금','기타자금']:
        match=next((row[-1] for row in rows if any(cell==label+'(원)' for cell in row[:-1])), '')
        value=number(match)
        if value is not None and value>0:purposes.append((label,value))
    if not purposes or not re.fullmatch(r'20\d{2}-\d{2}-\d{2}',due):return None
    total=sum(v for _,v in purposes)
    total_eok=(total/Decimal(100000000)).quantize(Decimal('.01'),rounding=ROUND_HALF_UP)
    lines=['<b>발행조건 · 종속회사 원문 기준</b>',f'출자 대상 회사: {html.escape(company)}',
           f'방식: {method} · 해외 종속회사 지분출자',f'조달금액(원화 환산): {total_eok:,.2f}억원',
           '신주·주당 발행가액: 원문 미기재(주식으로 나누지 않는 자본금 구조)',
           '대상: 기존 주주(공시상 주주배정 방식)',f'납입일: {due} (최종 예정일)',
           '자금용도: '+' · '.join(f'{k} {v/Decimal(100000000):,.2f}억원' for k,v in purposes)]
    currency=re.search(r'유상증자 금액\(([^)]+)\)',note)
    if currency:lines.append('원통화 금액: '+html.escape(currency[1])+' · 실제 원화액은 납입 시 환율에 따라 달라질 수 있음')
    installments=re.findall(r'\d+차\)\s*CNY\s*[\d.]+억\s*\(20\d{2}년\s*\d+월\s*\d+일\)',note)
    if installments:lines.append('분납 일정(공시 기재):\n'+'\n'.join(html.escape(x) for x in installments))
    return '\n'.join(lines)

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
    if not get('DNM_SUM' if bond else 'CST_CNT','PST_CNT'):
        return subsidiary_summary(raw) if '종속회사' in title and not bond else None
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
