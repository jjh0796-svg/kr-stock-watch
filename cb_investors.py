"""Resolve CB subscribers from explicit fund/manager tables in the same filing."""
import html
import re
from decimal import Decimal


def company_name(value):
    return re.sub(r'주식회사|㈜|\(\s*주\s*\)', '', value).strip()


def fund_name(value):
    # Some issuers repeat the trustee wrapper even inside the fund-name column.
    match = re.search(r'\((.+)의\s*신탁업자\s*지위에서\)', value)
    return match[1].strip() if match else value.strip()


def fund_ids(value):
    value = re.sub(r'[“”"‘’]', '', value)
    matches = re.findall(r'(?:본건\s*)?펀드\s*(\d+(?:\s*[,，·]\s*\d+)*)', value)
    if re.search(r'펀드\s*\d+\s*[~～\-]', value):
        return []  # A range is not a license to infer individual allocations.
    return [int(x) for group in matches for x in re.findall(r'\d+', group)]


def fund_map(raw):
    from issue_terms import TableRows
    parser = TableRows(); parser.feed(raw)
    result = {}; conflicts = set(); columns = None
    for row in parser.rows:
        compact = [re.sub(r'\s+', '', c) for c in row]
        if '집합투자기구' in compact and '집합투자업자' in compact and '구분' in compact:
            columns = (compact.index('구분'), compact.index('집합투자기구'), compact.index('집합투자업자'))
            continue
        if columns is None or max(columns) >= len(row):
            continue
        label, name, manager = (row[i] for i in columns)
        match = re.fullmatch(r'(?:본건\s*)?펀드\s*(\d+)', label)
        if not match:
            continue
        ident = int(match[1]); pair = (fund_name(name), company_name(manager))
        if ident in result and result[ident] != pair:
            conflicts.add(ident)
        result[ident] = pair
    for ident in conflicts:
        result.pop(ident, None)
    return result


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
        if mapped and len(managers) == 1 and all(m and m != '-' for _,m in members) and value is not None:
            manager = managers.pop(); role = '운용사'
            details = [n for n,_ in members]
            used.update(ids)
        elif not is_fund and name in operators and value is not None:
            manager = operators[name]; role = '업무집행조합원'; details = [name]
        elif is_fund:
            # Retain resolved fund names even when the manager/pooled allocation is ambiguous.
            details = [funds[i][0] for i in ids if i in funds]
            label = ' / '.join(details) if details else ('본건 펀드 '+', '.join(map(str,ids)) if ids else name)
            reason = '운용사별 배분 미공개' if len(managers) > 1 else '운용사·배정 연결 미확인'
            unresolved.append((label,value,reason)); continue
        else:
            direct.append((company_name(name),value)); continue
        group = groups.setdefault((manager,role), {'amount':Decimal(0),'allocations':[]})
        group['amount'] += value
        group['allocations'].append((details,value))
    lines = ['<b>투자자 · 운용사별 인수 예정액</b>']
    pooled = False
    for (manager,role), group in sorted(groups.items(), key=lambda x:-x[1]['amount']):
        suffix = ' (업무집행조합원)' if role != '운용사' else ''
        lines.append('• '+html.escape(manager+suffix)+' '+eok_amount(group['amount']))
        for details,value in group['allocations']:
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
    if pooled:
        lines.append('※ 여러 펀드 묶음의 개별 배분액은 추정하지 않음')
    lines.append('※ 해당 공시의 인수 예정액 · 운용사 자기자금 투자 또는 납입 완료를 뜻하지 않음')
    return lines
