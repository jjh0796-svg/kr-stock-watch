"""내 종목(💼보유·⭐관심) ±10% 단계 돌파 시 '왜 움직였나' AI 요약 (2026-11-11).

재료: 네이버 종목 뉴스(당일 제목+본문 앞부분)·네이버 종목 공시(당일 제목). 재료에 있는 사실만 쓰게 하고,
재료가 없으면 AI를 부르지 않고 '뚜렷한 재료 미확인'으로 답한 뒤 10분 뒤 한 번 더 확인한다(급등 시점엔 기사가 아직 없는 경우가 많다).
모델: Gemini 무료(가벼운 모델) → 실패하면 Groq(Qwen → gpt-oss). 키는 서버 ~/bots/codex.env의 GEMINI_API_KEY·GROQ_API_KEY만 읽는다.
끄기: SPIKE_REASON=0. 어떤 실패도 스파이크 알림 자체에는 영향 없음(호출부에서 try/except).
"""
from __future__ import annotations

import datetime
import html
import json
import os
import re
from pathlib import Path

import requests

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
KST = datetime.timezone(datetime.timedelta(hours=9))
MIN_LEVEL = 10            # 이 단계(%) 이상을 새로 넘었을 때만
RECHECK_MIN = 10          # 재료 없을 때 재확인까지(분)
GEMINI_MODELS = ("gemini-3.1-flash-lite", "gemini-3.6-flash")
GROQ_MODELS = ("qwen/qwen3.8-27b", "openai/gpt-oss-120b")
_MACRO = re.compile(r"코스피|코스닥|증시|마감|시황|개장|특징주 모음|오늘의|\[표\]|장중 수급")

PROMPT = """너는 한국 주식 개인투자자의 보조다. 아래는 {name}({code})가 오늘 {move} 시점에 모은 당일 뉴스·공시다.
이 재료만 근거로 '{dir_word} 사유'를 한국어로 정리해 JSON 객체로만 답하라.
형식: {{"found": true|false, "lines": ["사유 한 줄(45자 이내, 숫자·상대방 등 구체 사실 포함)", ...최대 3줄], "basis": [근거로 쓴 재료 번호들]}}
규칙:
- 재료에 적힌 사실만 쓴다. 추측·일반론(수급, 기대감, 업황 호조 등 근거 없는 말) 금지. 재료에 없는 숫자·회사명 금지.
- 재료가 이 종목의 오늘 {dir_word}을 설명하지 못하면(시황 기사, 무관한 기사, 단순 주가 중계뿐이면) found=false, lines=[].
- 주가가 올랐다/내렸다는 사실 자체는 사유가 아니다.
- 하루 ±10% 움직임을 설명할 만한 재료만 사유로 인정한다: 실적·가이던스, 수주·공급계약, 증설·투자, M&A·지분 변동, 증자·CB,
  임상·허가, 규제·정책, 증권사 목표가·투자의견, 고객사·경쟁사 뉴스, 같은 업종 동반 급등락의 원인, 기사에 적힌 급등락 이유.
  회의 소집·수상·인증·사회공헌·인사·일반 홍보처럼 주가를 크게 움직이기 어려운 소식뿐이면 found=false.

{shown_rule}
재료:
{items}"""

SHOWN_RULE = """- 스파이크 알림에는 이미 이 기사 제목이 붙어 나갔다: "{shown}"
  이 제목만 읽어도 알 수 있는 내용을 되풀이하지 마라. 그 제목 말고 다른 재료에서 나온 추가 사유가 없으면 found=false.
"""


def _norm(t: str) -> str:
    return re.sub(r"[^0-9A-Za-z가-힣]", "", t or "")


def same_story(a: str, b: str) -> bool:
    """두 제목이 같은 기사(또는 받아쓴 기사)인지 — 앞 30자 일치 또는 두 글자 조각 겹침 60% 이상."""
    a, b = _norm(a), _norm(b.rstrip("…"))
    if not a or not b:
        return False
    n = min(len(a), len(b), 30)
    if n >= 12 and a[:n] == b[:n]:
        return True
    ga = {a[i:i + 2] for i in range(len(a) - 1)}
    gb = {b[i:i + 2] for i in range(len(b) - 1)}
    return bool(ga and gb) and len(ga & gb) / min(len(ga), len(gb)) >= 0.6


def enabled() -> bool:
    return os.environ.get("SPIKE_REASON", "1") != "0"


def _keys() -> dict:
    """codex.env에서 두 키만 읽는다(환경변수에 이미 있으면 그 값). 값은 어디에도 출력하지 않는다."""
    out = {k: os.environ.get(k, "").strip() for k in ("GEMINI_API_KEY", "GROQ_API_KEY")}
    p = Path.home() / "bots" / "codex.env"
    if p.exists() and not all(out.values()):
        for line in p.read_text(encoding="utf-8-sig", errors="replace").splitlines():
            k, _, v = line.strip().partition("=")
            if k in out and not out[k]:
                out[k] = v.strip().strip("'\"")
    return out


NEW_LISTING_DAYS = 20     # 상장 후 이 거래일 수 안이면 사유 분석 생략(11/12) — 신규상장주는 재료 없이도 크게 움직인다


def recently_listed(code: str) -> bool:
    """최근 두 달 일봉이 NEW_LISTING_DAYS개 미만이면 신규상장주로 본다. 조회 실패는 False(평소대로 분석)."""
    try:
        end = datetime.datetime.now(KST).date()
        start = end - datetime.timedelta(days=60)
        r = requests.get("https://api.finance.naver.com/siseJson.naver",
                         params={"symbol": code, "requestType": 1, "startTime": start.strftime("%Y%m%d"),
                                 "endTime": end.strftime("%Y%m%d"), "timeframe": "day"}, headers=UA, timeout=8)
        rows = json.loads(r.text.replace("'", '"').replace("\n", ""))[1:]
        today = end.strftime("%Y%m%d")
        days = [row for row in rows if str(row[0]) != today]
        if not rows:   # 빈 응답은 조회 문제일 수 있어 신규상장으로 보지 않는다
            return False
        return len(days) < NEW_LISTING_DAYS
    except Exception:
        return False


def level_of(note: str) -> int:
    """신호 note('+10% 돌파', '상한가' 등) → 단계 숫자. 해당 없으면 0."""
    if note in ("상한가", "하한가"):
        return 30
    m = re.match(r"[+-](\d+)% 돌파", note or "")
    return int(m.group(1)) if m else 0


def gather(code: str, name: str, now: datetime.datetime | None = None) -> list[dict]:
    """당일 뉴스·공시 → [{'kind','time','title','body'}] (최신순, 최대 10개)."""
    now = now or datetime.datetime.now(KST)
    day = now.strftime("%Y%m%d")
    items: list[dict] = []
    try:
        r = requests.get(f"https://m.stock.naver.com/api/stock/{code}/disclosure",
                         params={"pageSize": 8, "page": 1}, headers=UA, timeout=6)
        for d in r.json() or []:
            ts = str(d.get("datetime", ""))
            if ts[:10].replace("-", "") == day:
                items.append({"kind": "공시", "time": ts[11:16], "title": html.unescape(str(d.get("title", ""))).strip(),
                              "body": ""})
    except Exception:
        pass
    try:
        r = requests.get(f"https://m.stock.naver.com/api/news/stock/{code}",
                         params={"pageSize": 12, "page": 1}, headers=UA, timeout=6)
        seen = set()
        for group in r.json() or []:
            for it in group.get("items", []):
                ts = str(it.get("datetime", ""))
                title = html.unescape(str(it.get("title", ""))).strip()
                if ts[:8] != day or not title or title in seen:
                    continue
                seen.add(title)
                body = " ".join(html.unescape(str(it.get("body", ""))).split())[:220]
                # 종목명이 없는 시황 기사는 재료에서 뺀다
                if _MACRO.search(title) and name not in title:
                    continue
                items.append({"kind": "뉴스", "time": f"{ts[8:10]}:{ts[10:12]}", "title": title, "body": body,
                              "office": str(it.get("officeName", ""))})
    except Exception:
        pass
    # 네이버 종목 뉴스에 안 잡히는 중소형주 보강: 구글 뉴스 검색(제목에 종목명이 있고 오늘 나온 기사만) — 11/11 에프에스티 0건
    if sum(1 for it in items if it["kind"] == "뉴스") < 3:
        try:
            import email.utils
            import xml.etree.ElementTree as ET
            r = requests.get("https://news.google.com/rss/search",
                             params={"q": f'"{name}" when:1d', "hl": "ko", "gl": "KR", "ceid": "KR:ko"},
                             headers=UA, timeout=8)
            have = {it["title"][:20] for it in items}
            for node in ET.fromstring(r.content).iter("item"):
                title = html.unescape(node.findtext("title") or "").strip()
                src = node.findtext("source") or ""
                if src and title.endswith(" - " + src):
                    title = title[: -len(src) - 3]
                try:
                    pub = email.utils.parsedate_to_datetime(node.findtext("pubDate") or "").astimezone(KST)
                except Exception:
                    continue
                if pub.strftime("%Y%m%d") != day or name not in title or title[:20] in have:
                    continue
                if _MACRO.search(title):
                    continue
                have.add(title[:20])
                items.append({"kind": "뉴스", "time": pub.strftime("%H:%M"), "title": title, "body": "", "office": src})
        except Exception:
            pass
    return items[:10]


def _parse(text: str):
    text = re.sub(r"<think>.*?</think>", "", text or "", flags=re.S)
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except ValueError:
        return None


def _ask(prompt: str) -> tuple[dict | None, str]:
    keys = _keys()
    if keys["GEMINI_API_KEY"]:
        body = {"contents": [{"parts": [{"text": prompt}]}],
                "generationConfig": {"responseMimeType": "application/json", "temperature": 0.1}}
        for model in GEMINI_MODELS:
            try:
                r = requests.post(f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                                  headers={"x-goog-api-key": keys["GEMINI_API_KEY"]}, json=body, timeout=25)
                if r.status_code != 200:
                    print(f"[reason] {model} 응답 {r.status_code}")
                    continue
                data = _parse(r.json()["candidates"][0]["content"]["parts"][0]["text"])
                if isinstance(data, dict):
                    return data, model
            except Exception as e:
                print(f"[reason] {model} 실패 {type(e).__name__}")
    if keys["GROQ_API_KEY"]:
        for model in GROQ_MODELS:
            payload = {"model": model, "temperature": 0.1, "max_completion_tokens": 1500,
                       "messages": [{"role": "user", "content": prompt}]}
            if "gpt-oss" in model:
                payload["reasoning_effort"] = "low"
            try:
                r = requests.post("https://api.groq.com/openai/v1/chat/completions",
                                  headers={"Authorization": "Bearer " + keys["GROQ_API_KEY"]}, json=payload, timeout=25)
                if r.status_code != 200:
                    print(f"[reason] {model} 응답 {r.status_code}")
                    continue
                data = _parse(r.json()["choices"][0]["message"]["content"])
                if isinstance(data, dict):
                    return data, model
            except Exception as e:
                print(f"[reason] {model} 실패 {type(e).__name__}")
    return None, ""


def _grounded(line: str, blob: str) -> bool:
    """두 자리 이상 숫자가 재료에 없으면 지어낸 것으로 보고 버린다."""
    for num in re.findall(r"\d[\d,.]*\d", line):
        if num.replace(",", "") not in blob.replace(",", ""):
            return False
    return True


def analyze(code: str, name: str, direction: str, move: str, now: datetime.datetime | None = None,
            shown: str = "") -> dict:
    """→ {'found': bool, 'text': 발송할 글 또는 '', 'model': 모델명, 'dup': 알림에 붙은 제목과 겹쳐 생략했는지}
    shown = 스파이크 알림에 이미 붙여 보낸 기사 제목(11/12 중복 방지). 재료가 그 기사뿐이면 AI를 부르지 않는다."""
    items = gather(code, name, now)
    if shown and items and all(same_story(it["title"], shown) for it in items):
        return {"found": False, "model": "", "text": "", "dup": True}
    # 붙어 나간 제목이 이 종목을 직접 다룬 기사(종목명 포함·시황 아님)면 사유는 이미 전달된 것 → AI 생략.
    # 단, 당일 공시가 있으면 제목에 없는 재료일 수 있어 분석한다.
    if shown and name in shown and not _MACRO.search(shown) and not any(it["kind"] == "공시" for it in items):
        return {"found": False, "model": "", "text": "", "dup": True}
    dir_word = "상승" if direction == "up" else "하락"
    icon = "🧠"
    head = f"{icon} {name} {dir_word} 사유 (AI 추정 · 당일 뉴스·공시 기준)"
    if not items:
        return {"found": False, "model": "", "text": ""}
    listing = "\n".join(f"{n}. [{it['kind']} {it['time']}] {it['title']}" + (f" — {it['body']}" if it["body"] else "")
                        for n, it in enumerate(items, 1))
    rule = SHOWN_RULE.format(shown=shown.replace('"', "'")) if shown else ""
    data, model = _ask(PROMPT.format(name=name, code=code, move=move, dir_word=dir_word, items=listing, shown_rule=rule))
    if not data or not data.get("found"):
        return {"found": False, "model": model, "text": ""}
    blob = listing
    lines = [" ".join(str(x).split())[:70] for x in (data.get("lines") or []) if str(x).strip()]
    lines = [x for x in lines if _grounded(x, blob)][:3]
    if not lines:
        return {"found": False, "model": model, "text": ""}
    basis = []
    used = []
    for b in data.get("basis") or []:
        try:
            it = items[int(b) - 1]
        except (TypeError, ValueError, IndexError):
            continue
        used.append(it)
        t = it["title"] if len(it["title"]) <= 44 else it["title"][:44] + "…"
        basis.append(f"   └ {'📄' if it['kind'] == '공시' else '📰'} {it['time']} {t}")
    if shown and used and all(same_story(it["title"], shown) for it in used):   # 근거가 이미 붙은 그 기사뿐 → 되풀이
        return {"found": False, "model": model, "text": "", "dup": True}
    out = [head] + [f"• {x}" for x in lines] + basis[:2]
    return {"found": True, "model": model, "text": "\n".join(out)}


def not_found_text(name: str, direction: str) -> str:
    dir_word = "상승" if direction == "up" else "하락"
    return f"🧠 {name} {dir_word} 사유: 아직 뚜렷한 재료 미확인 (당일 뉴스·공시 기준, {RECHECK_MIN}분 뒤 한 번 더 확인)"
