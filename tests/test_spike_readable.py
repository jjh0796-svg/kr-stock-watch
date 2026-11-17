"""급등락 글의 한자를 한글로(2026-11-17) — 뉴스 제목에 섞여 온 '남북경협株'·'리딩證'이 그대로 나갔다.

한자 정리 장치는 CODEX 저장소(stock_alert_bot.hanja)에 있고, 이 봇은 서버에서 그것을 빌려 쓴다.
CODEX가 없는 곳(깃허브 실행)에서는 아무 일도 하지 않고 글을 그대로 보낸다.
"""
import sys
import types

import spike_watch as sw


def fake_codex(monkeypatch, clean):
    package = types.ModuleType("stock_alert_bot")
    module = types.ModuleType("stock_alert_bot.hanja")
    module.clean = clean
    package.hanja = module
    monkeypatch.setitem(sys.modules, "stock_alert_bot", package)
    monkeypatch.setitem(sys.modules, "stock_alert_bot.hanja", module)


def test_uses_the_shared_cleaner_when_it_is_there(monkeypatch, tmp_path):
    monkeypatch.setenv("CODEX_DIR", str(tmp_path))
    fake_codex(monkeypatch, lambda text: text.replace("株", "주").replace("證", "증권"))
    assert sw._readable("└ 📰 리딩證 \"남북경협株, 급등\"") == "└ 📰 리딩증권 \"남북경협주, 급등\""


def test_without_codex_the_text_goes_out_unchanged(monkeypatch, tmp_path):
    monkeypatch.setenv("CODEX_DIR", str(tmp_path / "없는폴더"))
    monkeypatch.delitem(sys.modules, "stock_alert_bot", raising=False)
    monkeypatch.delitem(sys.modules, "stock_alert_bot.hanja", raising=False)
    assert sw._readable("남북경협株, 급등") == "남북경협株, 급등"


def test_cleaner_failure_or_overlong_result_keeps_the_original(monkeypatch, tmp_path):
    monkeypatch.setenv("CODEX_DIR", str(tmp_path))

    def boom(text):
        raise RuntimeError("고장")

    fake_codex(monkeypatch, boom)
    assert sw._readable("AI 카메라株") == "AI 카메라株"
    fake_codex(monkeypatch, lambda text: text + "가" * 5000)
    assert sw._readable("AI 카메라株") == "AI 카메라株"


def test_send_cleans_before_posting(monkeypatch, tmp_path):
    monkeypatch.setenv("CODEX_DIR", str(tmp_path))
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "1:x")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "-100")
    fake_codex(monkeypatch, lambda text: text.replace("株", "주"))
    posted = {}

    class Resp:
        status_code = 200

        def json(self):
            return {"result": {"message_id": 7}}

    def post(url, json=None, timeout=None):
        posted.update(json)
        return Resp()

    monkeypatch.setattr(sw.requests, "post", post)
    monkeypatch.setattr(sw, "_ops_record", lambda *a: posted.setdefault("recorded", a[2]))
    assert sw.send(["📡 내 종목 스파이크 10:18", "└ 📰 AI 카메라株 너무 올랐나"]) == 7
    assert posted["text"] == "📡 내 종목 스파이크 10:18\n└ 📰 AI 카메라주 너무 올랐나"
    assert posted["recorded"] == posted["text"]            # 발송 기록부에도 바뀐 글이 남는다
