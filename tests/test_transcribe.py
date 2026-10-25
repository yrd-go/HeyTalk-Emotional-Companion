# -*- coding: utf-8 -*-
"""
tests/test_transcribe.py —— 语音转文字（本地识别）
================================================================

这组测试守两类东西：

1. **接口行为**：要登录、限大小、限频率，失败要说人话。
   语音这条链上能坏的地方比文字多（没装库、模型没下、音频解不开、
   网络下不动模型），而用户看到的必须是一句"该怎么办"。

2. ★ **隐私属性**：录音**不留痕**。
   这个项目的护城河是"你的话不出这台机器"，而语音是唯一会把
   **原始音频**带进来的入口 —— 所以"用完即删"必须被测试钉住，
   不能只写在注释里。
"""
from __future__ import annotations

import importlib
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
for _p in (str(PROJECT_ROOT), str(PROJECT_ROOT / "src")):
    if _p not in sys.path:
        sys.path.insert(0, _p)


class TestEndpointOnServer(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="heytalk-stt-")
        base = Path(self.tmp.name)
        from server import api
        importlib.reload(api)
        self.api = api
        self._orig = (api.USERS_FILE, api.SESSIONS_FILE, api.CHAT_HISTORY_DIR,
                      api._history)
        from chat.history import HistoryStore
        api.USERS_FILE = base / "users.json"
        api.SESSIONS_FILE = base / "sessions.json"
        api.CHAT_HISTORY_DIR = base / "chat_history"
        api._history = HistoryStore(api.CHAT_HISTORY_DIR)
        api._CONVERSATIONS.clear()
        api._HISTORY_BY_USER.clear()

        from server import auth
        auth.set_user(api.USERS_FILE, "jia", "pw-jia-123", name="甲")
        api._rl.LOGIN_LIMITER._hits.clear()
        api._rl.LOGIN_IP_LIMITER._hits.clear()
        api._rl.TRANSCRIBE_LIMITER._hits.clear()

        from starlette.testclient import TestClient
        self.client = TestClient(api.app)
        self.token = self.client.post(
            "/api/login", json={"user_id": "jia", "password": "pw-jia-123"}
        ).json()["token"]

    def tearDown(self) -> None:
        api = self.api
        (api.USERS_FILE, api.SESSIONS_FILE, api.CHAT_HISTORY_DIR,
         api._history) = self._orig
        api._CONVERSATIONS.clear()
        api._HISTORY_BY_USER.clear()
        api._rl.TRANSCRIBE_LIMITER._hits.clear()
        self.tmp.cleanup()

    def _hdr(self) -> dict:
        return {"Authorization": f"Bearer {self.token}"}

    def _fake_available(self, value: bool = True):
        """把 transcribe.available 换成固定值（避免依赖真的装了库）。"""
        api = self.api
        original = api._stt.available
        api._stt.available = lambda: value
        self.addCleanup(lambda: setattr(api._stt, "available", original))

    def _fake_transcribe(self, result=None, error=None):
        api = self.api
        original = api._stt.transcribe

        def fake(audio, **kw):
            if error is not None:
                raise error
            return result if result is not None else "识别出来的话"

        api._stt.transcribe = fake
        self.addCleanup(lambda: setattr(api._stt, "transcribe", original))

    # ---- 权限 ----
    def test_requires_login(self):
        # 必须用**全新 client**：TestClient 会保留 cookie，而 setUp 里
        # 已经登录过，复用同一个 client 时其实是"带着登录态"的
        # （我第一版就吃了这个亏：期望 401，实际拿到 422）。
        from starlette.testclient import TestClient
        fresh = TestClient(self.api.app)
        res = fresh.post("/api/transcribe", content=b"fake-audio")
        self.assertEqual(res.status_code, 401)

    # ---- 环境未就绪 ----
    def test_503_when_library_missing(self):
        self._fake_available(False)
        res = self.client.post("/api/transcribe", content=b"fake-audio",
                               headers=self._hdr())
        self.assertEqual(res.status_code, 503)
        # 提示必须包含"怎么办"，不能只说"不支持"
        self.assertIn("how_to_fix", res.json())

    # ---- 输入校验 ----
    def test_empty_body_is_rejected(self):
        self._fake_available(True)
        res = self.client.post("/api/transcribe", content=b"", headers=self._hdr())
        self.assertEqual(res.status_code, 400)

    def test_oversized_audio_is_rejected(self):
        self._fake_available(True)
        big = b"x" * (self.api.MAX_AUDIO_BYTES + 10)
        res = self.client.post("/api/transcribe", content=big, headers=self._hdr())
        self.assertEqual(res.status_code, 413)

    # ---- 正常路径 ----
    def test_success_returns_text(self):
        self._fake_available(True)
        self._fake_transcribe(result="我今天有点累")
        res = self.client.post("/api/transcribe", content=b"fake-audio",
                               headers=self._hdr())
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["text"], "我今天有点累")

    # ---- 失败要说人话 ----
    def test_transcribe_error_message_reaches_the_user(self):
        """识别失败时，用户看到的必须是**可操作的中文**，不是错误码。"""
        self._fake_available(True)
        self._fake_transcribe(error=self.api._stt.TranscribeError(
            "没听清，可以再说一次或者直接打字"))
        res = self.client.post("/api/transcribe", content=b"fake-audio",
                               headers=self._hdr())
        self.assertEqual(res.status_code, 422)
        self.assertIn("再说一次", res.json()["error"])

    def test_rate_limited(self):
        self._fake_available(True)
        self._fake_transcribe()
        limit = self.api._rl.TRANSCRIBE_LIMITER.limit
        for _ in range(limit):
            self.client.post("/api/transcribe", content=b"a", headers=self._hdr())
        res = self.client.post("/api/transcribe", content=b"a", headers=self._hdr())
        self.assertEqual(res.status_code, 429)


class TestAudioLeavesNoTrace(unittest.TestCase):
    """★ 隐私属性：录音用完即删。

    为什么单独测这一条：
        语音是唯一会把**原始音频**带进系统的入口。项目的护城河是
        "你的话不出这台机器"，所以"音频不落盘、用完立刻删"是**承诺**，
        必须被钉住 —— 只写在注释里不算。

    注意断言挂在哪一层：
        临时文件的生死归**解码器**管（`_decode_to_pcm16k` 接收路径），
        因为 transcribe 现在是"先自己解码成数组、再把数组交给模型"。
        所以"处理期间文件存在"要在解码器里检查
        （第一版把假模型写成接收路径，改架构后这两条就红了 ——
        测试跟着架构走，不是反过来）。
    """

    def test_temp_audio_file_is_removed(self):
        from server import transcribe as stt

        captured: list[str] = []
        existed_during: list[bool] = []

        class FakeModel:
            def transcribe(self, samples, **kw):
                # 模型收到的是**数组**，不是路径（这正是绕过坏解码器的关键）
                self.assert_is_array = hasattr(samples, "dtype")
                return [type("S", (), {"text": "测试"})()], None

        def fake_decode(path):
            captured.append(str(path))
            existed_during.append(Path(path).is_file())
            import numpy as np
            return np.zeros(16000, dtype=np.float32)

        original_decode = stt._decode_to_pcm16k
        original_load = stt._load_model
        stt._decode_to_pcm16k = fake_decode
        stt._load_model = lambda: FakeModel()
        try:
            text = stt.transcribe(b"\x00" * 4096)
        finally:
            stt._decode_to_pcm16k = original_decode
            stt._load_model = original_load

        self.assertEqual(text, "测试")
        self.assertTrue(captured, "解码器应当被调用到")
        self.assertTrue(all(existed_during),
                        "解码时临时文件应当存在（否则测的不是同一回事）")
        for path in captured:
            self.assertFalse(Path(path).exists(),
                             "识别完成后临时音频必须被删掉（录音不留痕）")

    def test_temp_audio_file_is_removed_even_on_failure(self):
        """解码/识别**失败**时也必须删 —— 否则失败的录音会留在磁盘上。"""
        from server import transcribe as stt

        captured: list[str] = []

        def boom_decode(path):
            captured.append(str(path))
            raise stt.TranscribeError("模拟解码失败")

        original_decode = stt._decode_to_pcm16k
        original_load = stt._load_model
        stt._decode_to_pcm16k = boom_decode
        stt._load_model = lambda: None      # 失败点在解码，模型不会被用到
        try:
            with self.assertRaises(stt.TranscribeError):
                stt.transcribe(b"\x00" * 4096)
        finally:
            stt._decode_to_pcm16k = original_decode
            stt._load_model = original_load

        self.assertTrue(captured)
        for path in captured:
            self.assertFalse(Path(path).exists(),
                             "失败路径也必须删掉临时音频")

    def test_model_receives_array_not_path(self):
        """★ 模型必须收到**数组**，不能是路径。

        这是绕过 `av.open(metadata_errors=...)` 不兼容的关键：
        faster_whisper 只在收到**非数组**时才会调用它自己那个坏解码器。
        哪天有人"顺手"改回传路径，这条测试会红 —— 那正是它存在的意义。
        """
        from server import transcribe as stt

        seen: list[object] = []

        class FakeModel:
            def transcribe(self, samples, **kw):
                seen.append(samples)
                return [type("S", (), {"text": "好"})()], None

        import numpy as np

        original_decode = stt._decode_to_pcm16k
        original_load = stt._load_model
        stt._decode_to_pcm16k = lambda p: np.zeros(16000, dtype=np.float32)
        stt._load_model = lambda: FakeModel()
        try:
            stt.transcribe(b"\x00" * 1024)
        finally:
            stt._decode_to_pcm16k = original_decode
            stt._load_model = original_load

        self.assertEqual(len(seen), 1)
        self.assertIsInstance(seen[0], np.ndarray,
                              "传给模型的必须是数组，否则会走到与 av 19 不兼容的"
                              "内部解码器（TypeError: metadata_errors）")

    def test_short_audio_is_rejected_before_the_model(self):
        """太短的录音不该浪费一次识别。"""
        from server import transcribe as stt
        import numpy as np

        called: list[int] = []

        class FakeModel:
            def transcribe(self, samples, **kw):
                called.append(1)
                return [], None

        original_decode = stt._decode_to_pcm16k
        original_load = stt._load_model
        stt._decode_to_pcm16k = lambda p: np.zeros(100, dtype=np.float32)
        stt._load_model = lambda: FakeModel()
        try:
            with self.assertRaises(stt.TranscribeError) as ctx:
                stt.transcribe(b"\x00" * 512)
        finally:
            stt._decode_to_pcm16k = original_decode
            stt._load_model = original_load

        self.assertIn("太短", str(ctx.exception))
        self.assertEqual(called, [], "太短的音频不该调用模型")


class TestModelEndpointSelection(unittest.TestCase):
    """模型源的选择规则（这一段是被"大陆连不上 huggingface"逼出来的）。"""

    def setUp(self) -> None:
        import os
        from server import transcribe as stt
        self.stt = stt
        self._saved = os.environ.get("HF_ENDPOINT")
        os.environ.pop("HF_ENDPOINT", None)

    def tearDown(self) -> None:
        import os
        if self._saved is None:
            os.environ.pop("HF_ENDPOINT", None)
        else:
            os.environ["HF_ENDPOINT"] = self._saved

    def test_existing_env_var_wins(self):
        """用户/环境已经指定了源 → 不要自作主张改掉它。"""
        import os
        os.environ["HF_ENDPOINT"] = "https://example.invalid"
        self.assertEqual(self.stt.pick_endpoint(), "https://example.invalid")

    def test_falls_back_to_mirror_when_official_unreachable(self):
        original = self.stt._tcp_ok
        self.stt._tcp_ok = lambda host, *a, **k: host == "hf-mirror.com"
        try:
            self.assertEqual(self.stt.pick_endpoint(force=True),
                             self.stt.HF_MIRROR)
        finally:
            self.stt._tcp_ok = original

    def test_prefers_official_when_reachable(self):
        """官方源能连上就用官方源 —— 不该强迫所有人走第三方镜像。"""
        original = self.stt._tcp_ok
        self.stt._tcp_ok = lambda host, *a, **k: True
        try:
            self.assertEqual(self.stt.pick_endpoint(force=True), "")
        finally:
            self.stt._tcp_ok = original

    def test_returns_empty_when_nothing_reachable(self):
        original = self.stt._tcp_ok
        self.stt._tcp_ok = lambda host, *a, **k: False
        try:
            self.assertEqual(self.stt.pick_endpoint(force=True), "")
        finally:
            self.stt._tcp_ok = original

    def test_cache_dir_lives_inside_the_project(self):
        """模型缓存必须在**项目内**，不能落在全局用户目录。

        两个理由（都是我踩出来的）：
          · 受限环境写不进 C:\\Users\\X\\.cache\\huggingface（WinError 5）；
          · 放项目里好找、好删、好备份，卸载就是删一个目录。
        """
        root = self.stt.MODELS_DIR.resolve()
        self.assertTrue(str(root).startswith(str(PROJECT_ROOT.resolve())),
                        f"模型目录跑到项目外了：{root}")


if __name__ == "__main__":
    unittest.main()
