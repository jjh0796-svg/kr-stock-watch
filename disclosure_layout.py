"""Presentation only: add paragraph breaks without rewriting disclosure facts."""
import re


def space_summary(title, summary):
    title=re.sub(r'\s+','',title or '')
    # Break at the start of a section, keeping related rows and fund lists together.
    starts=[r'변경\s*(?:사항|내용)|정정\s*(?:사항|내용|전후)|주요\s*변경']
    if re.search(r'유무상증자|유상증자|사채권발행',title):
        starts += [r'발행금액:|조달금액',r'납입일:',r'가격조정',r'자금용도:',r'대상:|투자 펀드|분납 일정']
    elif '단일판매' in title or '공급계약' in title:
        starts += [r'금액:']
    elif '자기주식' in title:
        starts += [r'목적:|계약기간:']
    elif '무상증자' in title:
        starts += [r'기준일:|상장예정']
    elif '감자' in title:
        starts += [r'방법:|기준일:']
    elif '배당' in title:
        starts += [r'기준일\b|지급\b']
    elif '대량보유' in title:
        starts += [r'보유',r'보고구분:|사유:']
    elif '기업설명회' in title:
        starts += [r'목적:']
    elif re.search(r'가액의?조정',title):
        starts += [r'사유:']
    elif re.search(r'유형자산',title):
        starts += [r'자산:']
    elif re.search(r'(전환청구권|신주인수권|교환청구권)행사',title):
        starts += [r'미전환 잔액']
    elif '실적' in title:
        starts += [r'최근 추이|최근 실적|컨센서스|실적 추이']
    boundary=re.compile(r'^(?:'+'|'.join(starts)+')')
    lines=[]
    for raw in summary.splitlines():
        line=raw.rstrip()
        if not line:
            if lines and lines[-1]:lines.append('')
            continue
        visible=re.sub(r'<[^>]+>','',line).strip()
        if boundary.match(visible) and lines and lines[-1]:
            lines.append('')
        lines.append(line)
    return '\n'.join(lines).strip()
