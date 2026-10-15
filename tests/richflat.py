"""리치 메시지(표·접기) 카드를 예전 줄 형식으로 되돌려 기존 테스트의 문자열 검사를 그대로 쓰기 위한 도우미 (10/16).

표 한 줄 → '• 운용사 금액' (업무집행조합원/펀드명 기준/미연결 사유는 예전 괄호 표기로),
접기 블록의 '운용사 · 펀드 · 금액' → '  └ 펀드 · 금액', 묶음 배정은 '  묶음 배정 금액' + '  └ 펀드' 줄들."""
import re

_REASONS = ('운용사별 배분 미공개', '운용사 미기재', '운용사·배정 연결 미확인')


def _table(block):
    out = []
    for tr in re.findall(r'<tr>(.*?)</tr>', block):
        tds = re.findall(r'<td>(.*?)</td>', tr)
        if len(tds) != 4:
            continue
        name, amount, count, tag = tds
        if tag == '펀드명 기준':
            out.append(f'• {name} (펀드명 기준 · 펀드 {count}개) {amount}')
        elif tag == '업무집행조합원':
            out.append(f'• {name} (업무집행조합원) {amount}')
        elif tag in _REASONS:
            out.append(f'• {name} · {amount} ({tag})')
        else:
            out.append(f'• {name} {amount}')
    return '\n'.join(out)


def _details(body):
    out = []
    for d in body.split('\n'):
        if not d.strip():
            continue
        if ' · 묶음 배정 ' in d:
            head, _, funds = d.partition(' — ')
            out.append('  ' + head.split(' · ', 1)[1])
            out.extend('  └ ' + f for f in funds.split(' / '))
        elif ' · ' in d:
            out.append('  └ ' + d.split(' · ', 1)[1])
        else:
            out.append('  ' + d)
    return '\n'.join(out)


def flatten(text):
    def _block(m):
        # 펀드 세부 접기만 '└' 줄로 되돌린다. 조정·풋·콜 조항 접기는 안의 줄을 그대로 둔다.
        return _details(m.group(2)) if m.group(1).startswith('펀드별 배정 세부') else m.group(2)
    text = re.sub(r'<details><summary>(.*?)</summary>(.*?)</details>', _block, text, flags=re.S)
    text = re.sub(r'<table>(.*?)</table>', lambda m: _table(m.group(1)), text, flags=re.S)
    return text
