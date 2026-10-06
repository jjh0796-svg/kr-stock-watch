"""Resolve CB subscribers from explicit fund/manager tables in the same filing.

2026-10-06: 운용사(집합투자업자) 열 없이 '구분 | 집합투자기구' 두 열만 있는 공시(에코앤드림 CB 8회차, 펀드 40개)는
'본건 펀드 N · 연결 미확인'만 40줄 나왔다. 이제 펀드 이름은 공시 표로 정확히 연결하고, 운용사는 펀드 이름
앞부분(브랜드: 에이원·씨스퀘어·타임폴리오 …)으로 묶는다. 이 묶음은 '펀드명 기준'이라고 표시해 공시 기재와 구분한다.
"""
import html
import re
from decimal import Decimal


def company_name(value):
    return re.sub(r'주식회사|㈜|\(\s*주\s*\)', '', value).strip()


def fund_name(value):
    # Some issuers repeat the trustee wrapper even inside the fund-name column.
    match = re.search(r'\((.+)의\s*신탁업자\s*지위에서\)', value)
    name = match[1].strip() if match else value.strip()
    return re.sub(r'의\s*신탁업자$', '', name).strip()   # '…제3호의 신탁업자' 꼬리


def fund_ids(value):
    value = re.sub(r'[“”"‘’]', '', value)
    matches = re.findall(r'(?:본건\s*)?펀드\s*(\d+(?:\s*[,，·]\s*\d+)*)', value)
    if re.search(r'펀드\s*\d+\s*[~～\-]', value):
        return []  # A range is not a license to infer individual allocations.
    return [int(x) for group in matches for x in re.findall(r'\d+', group)]


def fund_map(raw):
    """{펀드 번호: (펀드 이름, 운용사)}. 운용사 열이 없는 표는 운용사를 ''로 둔다(추정하지 않음)."""
    from issue_terms import TableRows
    parser = TableRows(); parser.feed(raw)
    result = {}; conflicts = set(); columns = None
    for row in parser.rows:
        compact = [re.sub(r'\s+', '', c) for c in row]
        if '집합투자기구' in compact and '구분' in compact:
            manager_col = compact.index('집합투자업자') if '집합투자업자' in compact else None
            columns = (compact.index('구분'), compact.index('집합투자기구'), manager_col)
            continue
        if columns is None or max(c for c in columns if c is not None) >= len(row):
            continue
        label, name = row[columns[0]], row[columns[1]]
        manager = row[columns[2]] if columns[2] is not None else ''
        match = re.fullmatch(r'(?:본건\s*)?펀드\s*(\d+)', label.strip())
        if not match:
            continue
        ident = int(match[1]); pair = (fund_name(name), company_name(manager))
        if ident in result and result[ident] != pair:
            conflicts.add(ident)
        result[ident] = pair
    for ident in conflicts:
        result.pop(ident, None)
    return result


# 펀드 이름에서 브랜드(운용사 이름 앞부분) 뒤에 오는 상품명 단어 — 여기서 자른다.
_PRODUCT_WORDS = ('코스닥벤처', '코벤', '메자닌', '공모주', '하이일드', '벤처', '일반', '사모', '증권투자', '투자신탁',
                  '갤럭시', '밸류업', '컨버터블', '넥스처', '밸런스드', '플래티넘', '스마트', '국민성장', 'IPO', 'Reach',
                  '알파', '멀티', '플러스', '전문투자', '롱숏', '이벤트', '인컴', '채권', '주식', '혼합')


def fund_brand(name):
    """'에이원갤럭시코스닥벤처…' → '에이원', '타임폴리오 코스닥벤처 …' → '타임폴리오'. 못 찾으면 ''."""
    if not name:
        return ''
    head = name.split()[0] if ' ' in name.strip() else name.strip()
    cut = len(head)
    for word in _PRODUCT_WORDS:
        i = head.find(word)
        if 0 <= i < cut:
            cut = i                       # 상품명 단어로 시작하면(예: '일반사모…') 브랜드 없음
    brand = head[:cut]
    brand = re.sub(r'(?<=[가-힣]{2})[A-Z]{1,2}$', '', brand)      # '안다H' → '안다'
    brand = re.sub(r'(자산운용|투자자문|운용)$', '', brand)
    return brand if len(brand) >= 2 and not re.fullmatch(r'[\d\W_]+', brand) else ''


def partnership_operators(raw):
    from issue_terms import Fields
    result = {}; conflicts = set()
    for raw_table in re.findall(r'<TABLE\b[^>]*>(.*?)</TABLE>', raw, re.I | re.S):
        if 'BAS_NAME' not in raw_table or 'BAS_NAM2' not in raw_table:
            continue
        fields = Fields(); fields.feed(raw_table)
        names = [n for n in fields.fields.get('BAS_NAME', []) if '조합' in n]
        operators = {company_name(n) for n in fields.fields.get('BAS_NAM2', []) if n and n != '-'}
        # BAS_NAM2 is the executive member, not the representative or largest LP.
        if len(names) == 1 and len(operators) == 1:
            name = names[0]; operator = operators.pop()
            if name in result and result[name] != operator:
                conflicts.add(name)
            result[name] = operator
    return {k:v for k,v in result.items() if k not in conflicts}


ROLE_MANAGER, ROLE_BRAND, ROLE_GP = '운용사', '펀드명 기준', '업무집행조합원'



def format_investors(raw, fields, total):
    from issue_terms import number, eok_amount
    names = fields.get('ISSU_NM', []); amounts = fields.get('ISSU_AMT', [])
    if not names or len(names) != len(amounts):
        return None  # Never zip mismatched columns or silently truncate subscribers.
    funds = fund_map(raw); operators = partnership_operators(raw)
    if not funds and not operators and not any('신탁업자' in n or '펀드' in n for n in names):
        return None
    groups = {}; direct = []; unresolved = []; used = set(); known = Decimal(0)
    for name, raw_amount in zip(names, amounts):
        value = number(raw_amount)
        if value is not None:
            known += value
        ids = fund_ids(name)
        is_fund = bool(ids) or '신탁업자' in name or '펀드' in name
        mapped = ids and len(set(ids)) == len(ids) and all(i in funds and i not in used for i in ids)
        members = [funds[i] for i in ids] if mapped else []
        managers = {m for _,m in members if m and m != '-'}
        brands = {fund_brand(n) for n,_ in members}
        if mapped and len(managers) == 1 and all(m and m != '-' for _,m in members) and value is not None:
            manager = managers.pop(); role = ROLE_MANAGER
            details = [n for n,_ in members]
            used.update(ids)
        elif mapped and not managers and len(brands) == 1 and '' not in brands and value is not None:
            # 공시에 운용사 열이 없다 — 펀드 이름 앞부분으로 묶되 '펀드명 기준'으로 구분 표시
            manager = brands.pop(); role = ROLE_BRAND
            details = [n for n,_ in members]
            used.update(ids)
        elif not is_fund and name in operators and value is not None:
            manager = operators[name]; role = ROLE_GP; details = [name]
        elif is_fund:
            # Retain resolved fund names even when the manager/pooled allocation is ambiguous.
            details = [funds[i][0] for i in ids if i in funds]
            label = ' / '.join(details) if details else ('본건 펀드 '+', '.join(map(str,ids)) if ids else name)
            if len(managers) > 1:
                reason = '운용사별 배분 미공개'
            elif details:
                reason = '운용사 미기재'
            else:
                reason = '운용사·배정 연결 미확인'
            unresolved.append((label,value,reason)); continue
        else:
            direct.append((company_name(name),value)); continue
        group = groups.setdefault((manager,role), {'amount':Decimal(0),'allocations':[]})
        group['amount'] += value
        group['allocations'].append((details,value))
    lines = ['<b>투자자 · 운용사별 인수 예정액</b>']
    pooled = False; branded = False
    for (manager,role), group in sorted(groups.items(), key=lambda x:-x[1]['amount']):
        allocations = group['allocations']
        count = sum(len(d) for d,_ in allocations)
        if role == ROLE_BRAND:
            branded = True
            suffix = f' (펀드명 기준 · 펀드 {count}개)'
        elif role == ROLE_GP:
            suffix = ' (업무집행조합원)'
        else:
            suffix = ''
        lines.append('• '+html.escape(manager+suffix)+' '+eok_amount(group['amount']))
        if role == ROLE_BRAND:
            continue                      # 펀드명 기준 묶음은 이름 나열 대신 개수만(40줄 방지)
        for details,value in allocations:
            if len(details)==1:
                lines.append('  └ '+html.escape(details[0])+' · '+eok_amount(value))
            else:
                pooled=True
                lines.append('  묶음 배정 '+eok_amount(value))
                lines.extend('  └ '+html.escape(n) for n in details)
    if direct:
        lines.append('<b>직접 투자자 · 조합</b>')
        for name,value in direct:
            lines.append('• '+html.escape(name)+' '+eok_amount(value))
    if unresolved:
        lines.append('<b>운용사 합산 제외 · 연결 확인 필요</b>')
        for name,value,reason in unresolved:
            lines.append('• '+html.escape(name)+' · '+eok_amount(value)+' ('+reason+')')
    if any(number(v) is None for v in amounts) or known != number(total):
        lines.append('※ 배정액 합계와 발행금액 대조 미완료 · 합계 확인 필요')
    else:
        lines.append('배정 합계: '+eok_amount(known)+' (발행금액과 일치)')
    if branded:
        lines.append('※ 펀드명 기준 = 공시에 운용사 기재가 없어 펀드 이름 앞부분으로 묶음')
    if pooled:
        lines.append('※ 여러 펀드 묶음의 개별 배분액은 추정하지 않음')
    lines.append('※ 해당 공시의 인수 예정액 · 운용사 자기자금 투자 또는 납입 완료를 뜻하지 않음')
    return lines
