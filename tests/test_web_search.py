# -*- coding: utf-8 -*-
"""Web search parser tests: no live network is used."""
from __future__ import annotations

import unittest
from unittest.mock import patch

from chat.web_search import build_search_query, format_hits, search_web


_HTML = """
<html><body>
  <li class="b_algo">
    <h2><a href="https://example.com/a">Example result A</a></h2>
    <div class="b_caption"><p>Short summary A.</p></div>
  </li>
  <li class="b_algo">
    <h2><a href="https://example.com/a">Duplicate</a></h2>
    <div class="b_caption"><p>Duplicate should be dropped.</p></div>
  </li>
  <li class="b_algo">
    <h2><a href="https://example.com/b">Example result B</a></h2>
    <div class="b_snippet">Summary B.</div>
  </li>
</body></html>
"""


class _Response:
    text = _HTML

    @staticmethod
    def raise_for_status() -> None:
        return None


class WebSearchTests(unittest.TestCase):
    def test_parses_deduplicates_and_limits_results(self):
        with patch("chat.web_search.requests.get", return_value=_Response()):
            hits = search_web("anything", max_results=5)
        self.assertEqual([hit.url for hit in hits], [
            "https://example.com/a",
            "https://example.com/b",
        ])
        self.assertEqual(hits[0].snippet, "Short summary A.")
        self.assertIn("[2] Example result B", format_hits(hits))

    def test_network_failure_degrades_to_empty(self):
        with patch("chat.web_search.requests.get", side_effect=OSError("offline")):
            self.assertEqual(search_web("anything"), [])

    def test_empty_query_does_not_hit_network(self):
        with patch("chat.web_search.requests.get") as request:
            self.assertEqual(search_web("   "), [])
            request.assert_not_called()

    def test_flash_query_builder_returns_one_clean_line(self):
        class _Model:
            @staticmethod
            def invoke(messages):
                assert messages[0]["role"] == "system"
                return type("R", (), {"content": "查询：北京 天气\n不要解释"})()

        self.assertEqual(build_search_query(_Model(), "北京今天天气怎么样"), "北京 天气")

    def test_query_builder_falls_back_to_user_text(self):
        class _Broken:
            @staticmethod
            def invoke(messages):
                raise OSError("offline")

        self.assertEqual(build_search_query(_Broken(), "东京天气"), "东京天气")


if __name__ == "__main__":
    unittest.main()
