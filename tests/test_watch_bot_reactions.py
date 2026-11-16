# -*- coding: utf-8 -*-
"""종목모니터링 대화봇 — 채널 글의 반응 수를 장부에 적는다(2026-11-16)."""
import json
import os
from unittest import mock

import watch_bot as wb


def test_reaction_row_and_ledger(tmp_path):
    rc = {"chat": {"id": -1003927447215}, "message_id": 700, "date": 1800000000,
          "reactions": [{"type": {"type": "emoji", "emoji": "\U0001F44D"}, "total_count": 2},
                        {"type": {"type": "custom_emoji", "custom_emoji_id": "555"}, "total_count": 1}]}
    assert wb.reaction_row(rc) == {"chat": -1003927447215, "message_id": 700, "date": 1800000000,
                                   "reactions": {"\U0001F44D": 2, "555": 1}}
    path = tmp_path / "r.jsonl"
    with mock.patch.dict(os.environ, {"REACTION_LEDGER": str(path)}):
        wb.record_reaction(rc)
        wb.record_reaction(rc)
    rows = [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 2 and rows[0]["message_id"] == 700
