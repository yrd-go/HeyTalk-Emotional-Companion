"""首屏推荐话题：动态、可回退、不把工具请求当聊天内容。"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC = PROJECT_ROOT / "src"
for _path in (str(PROJECT_ROOT), str(SRC)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from chat.opening_topics import (  # noqa: E402
    DEFAULT_OPENING_SUGGESTIONS,
    build_opening_suggestions,
)


def _session(*messages: str) -> SimpleNamespace:
    turns = [{"role": "user", "content": message} for message in messages]
    return SimpleNamespace(turns=turns)


class _History:
    def __init__(self, sessions):
        self._sessions = sessions

    def list_sessions(self):
        return list(self._sessions)


class TestOpeningTopics(unittest.TestCase):
    def test_no_history_returns_defaults(self):
        self.assertEqual(
            build_opening_suggestions(_History([])),
            list(DEFAULT_OPENING_SUGGESTIONS),
        )

    def test_recent_conversation_changes_topics(self):
        topics = build_opening_suggestions(_History([
            _session("最近准备考研压力很大，晚上总睡不着"),
            _session("今天和妈妈聊了聊，心里轻松一点"),
        ]))
        joined = "\n".join(topics)
        self.assertEqual(len(topics), 4)
        self.assertEqual(len(set(topics)), 4)
        self.assertIn("考研", joined)
        self.assertIn("妈妈", joined)

    def test_tool_requests_do_not_become_topics(self):
        topics = build_opening_suggestions(_History([
            _session("帮我查一下北京天气", "请翻译拉丁文"),
        ]))
        self.assertEqual(topics, list(DEFAULT_OPENING_SUGGESTIONS))

    def test_broken_history_falls_back_instead_of_raising(self):
        class Broken:
            def list_sessions(self):
                raise OSError("disk unavailable")

        self.assertEqual(
            build_opening_suggestions(Broken()),
            list(DEFAULT_OPENING_SUGGESTIONS),
        )


if __name__ == "__main__":
    unittest.main()
