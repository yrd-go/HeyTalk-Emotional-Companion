# -*- coding: utf-8 -*-
"""
src/server/transcribe.py —— 语音转文字（**本地识别，录音不出本机**）
================================================================

为什么选本地识别（faster-whisper）而不是浏览器自带的语音识别：
    浏览器的 Web Speech API 在 Chrome/Edge 上会把**音频送到 Google**。
    这个项目唯一的护城河是"你的话不出这台机器" —— 用浏览器识别等于
    把这条承诺打折（文字没出本机，但**你说的话的音频出了**）。
    faster-whisper 完全在本机跑，录音只在内存与临时文件里出现。

★ 设计原则：**每一种失败都给可操作的中文提示**，不抛堆栈给用户看。
   语音这条链上能坏的地方比文字多（没装库、模型没下、音频解不开、
   网络下不动模型），而用户看到的必须是一句"该怎么办"，不是 traceback。

隐私细节（这里刻意做得比较细）：
    · 音频写进**临时文件**，识别完**立刻删除**（finally 里删）；
    · 不落盘到 data/，不进历史，不记日志；
    · 只把**识别出来的文字**交回上层，音频本身不留痕。

★ 为什么我们自己解码，而不是让 faster-whisper 解（这是一个上游不兼容）：
    实测下来 faster_whisper 1.2.1 的 audio.py 会调用
        av.open(input_file, mode="r", metadata_errors="ignore")
    而 av 19.0.1（**查过了，已经是最新版**）的 av.open **不接受**
    metadata_errors 这个参数：
        TypeError: open() got an unexpected keyword argument 'metadata_errors'
    两个包**都已经是最新**，所以"升级"这条路是死的（我先试了升级，
    卡在等待里，查了版本才发现无版可升 —— 诊断方向一开始就错了）。

    正确的做法不是修依赖，而是**不走它那条路**：
    WhisperModel.transcribe() 接受 np.ndarray，此时它会**跳过 decode_audio**。
    于是我们用 av 自己解（只使用 19.0.1 确实支持的 API），
    解成 16kHz 单声道 float32 再交给模型。

    附带好处：解码这一步在我们手里，出错时能给出准确的中文提示
    （"这个格式解不开" vs "没听清"），而不是一个 TypeError。
"""
from __future__ import annotations

import os
import socket
import sys
import tempfile
import threading
from pathlib import Path

__all__ = ["TranscribeError", "available", "model_name", "transcribe",
           "warm_up", "pick_endpoint", "HF_MIRROR"]

# 模型要求的采样率（whisper 的训练采样率，必须一致）
_TARGET_SR = 16000

# 默认模型：small。为什么不用 base/tiny：
#   中文识别对模型大小很敏感，tiny/base 经常把整句听成同音字，
#   那种错误比"识别不出来"更糟 —— 用户会以为系统听错了他的意思。
#   small 约 460MB，首次使用会从 HuggingFace 下载（**需要联网一次**），
#   之后完全离线。想更准可以把 HEYTALK_WHISPER_MODEL 设成 medium。
_DEFAULT_MODEL = "small"

# 单次音频上限（秒）。太长既慢又容易失败，而且陪伴对话本来就没有长独白。
MAX_AUDIO_SECONDS = 120

# ---------------------------------------------------------------------------
# 模型源（这一段是被真实网络环境逼出来的）
#
# 实测：在中国大陆 `huggingface.co` **连不上**，下载直接失败并抛
# `LocalEntryNotFoundError` —— 那个错误名完全看不出是网络问题，
# 我为此查了一轮。`hf-mirror.com` 可达，是社区常用的镜像。
#
# 做法是**先探测再决定**，而不是把镜像写死：
#   · 能连上官方源 → 用官方源（国外的用户不该被迫走第三方镜像）
#   · 官方源不通、镜像通 → 自动改走镜像，并在消息里说明
#   · 都不通 → 明确告诉用户"是网络问题"，而不是抛一个看不懂的错误名
#
# ⚠️ 时序很关键：`HF_ENDPOINT` 是 **huggingface_hub 在 import 时读取**的，
#    所以必须在**导入 faster_whisper 之前**设好，之后再改环境变量没用。
# ---------------------------------------------------------------------------
_HF_HOST = "huggingface.co"
_MIRROR_HOST = "hf-mirror.com"
HF_MIRROR = f"https://{_MIRROR_HOST}"

# ---------------------------------------------------------------------------
# 模型缓存放哪：**项目内**，不放全局用户目录
#
# 这是被一个 PermissionError 逼出来的决定，但它本来就更合理：
#   · 全局缓存（C:\Users\X\.cache\huggingface）在**受限环境里写不进去**，
#     下载直接失败 —— 我实际撞到过（WinError 5）；
#   · 放项目里，模型与项目同生共死：**好找、好删、好备份**，
#     卸载就是删一个目录，不用去翻用户目录；
#   · 刻意**不放在 DATA_DIR 下面**：因为 DATA_DIR 可以被 HEYTALK_DATA_DIR
#     改到临时目录（做隔离验证用），模型跟着走会导致每起一个探针实例
#     就重下 460MB。模型是"项目级资源"，不是"某次运行的数据"。
# ---------------------------------------------------------------------------
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
MODELS_DIR = _PROJECT_ROOT / "models" / "hf"


def _prepare_cache_dir() -> str:
    """把 HF 的缓存目录指到项目内（必须在 import faster_whisper 之前）。"""
    try:
        MODELS_DIR.mkdir(parents=True, exist_ok=True)
    except Exception as exc:  # noqa: BLE001
        print(f"[heytalk] 模型目录创建失败：{exc}", file=sys.stderr)
        return ""
    # HF_HOME 会同时覆盖 hub 缓存与临时目录，比单独设 HUGGINGFACE_HUB_CACHE 更全
    os.environ.setdefault("HF_HOME", str(MODELS_DIR))
    # Windows 默认不支持符号链接，huggingface_hub 会刷一大段警告。
    # 那个警告对用户没有信息量，而且会淹没真正的错误 —— 关掉。
    os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
    return os.environ["HF_HOME"]


def _prepare_transport(endpoint: str) -> None:
    """配置下载通道。**必须在 import faster_whisper 之前调用。**

    ★ 为什么要关掉 Xet（这是我查了三轮才找到的真因）：
        huggingface_hub 现在默认走 **Xet 存储**，文件数据从
        `cas-server.xethub.hf.co` 取。而 `hf-mirror.com` **只代理了
        huggingface.co，没有代理 Xet 的 CAS 服务器** —— 于是元数据能下、
        文件重建时 401，报出来是一句：
            "File reconstruction error: CAS Client Error: HTTP 401
             ... cas-server.xethub.hf.co/v2/reconstructions/..."
        前面几层表象（LocalEntryNotFoundError、RuntimeError）都看不出
        是这个问题，因为它们只是外层包装。
        关掉 Xet 后走传统 HTTP 下载，镜像就正常支持了。
    """
    if endpoint:
        # 只有在走镜像时才关：官方源下 Xet 是好的，没必要降级
        os.environ.setdefault("HF_HUB_DISABLE_XET", "1")


def _tcp_ok(host: str, port: int = 443, timeout: float = 4.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def pick_endpoint(force: bool = False) -> str:
    """决定用哪个模型源；返回实际使用的 endpoint（空串=官方源）。"""
    existing = (os.environ.get("HF_ENDPOINT", "") or "").strip()
    if existing and not force:
        return existing
    if _tcp_ok(_HF_HOST):
        return ""
    if _tcp_ok(_MIRROR_HOST):
        os.environ["HF_ENDPOINT"] = HF_MIRROR
        return HF_MIRROR
    return ""


class TranscribeError(Exception):
    """给用户看的中文错误（调用方直接显示 message）。"""


_model = None
_model_lock = threading.Lock()
_model_error: str = ""


def model_name() -> str:
    return (os.environ.get("HEYTALK_WHISPER_MODEL", "").strip()
            or _DEFAULT_MODEL)


def available() -> bool:
    """本机是否具备本地识别的能力（装了库）。"""
    try:
        import faster_whisper  # noqa: F401
        return True
    except Exception:  # noqa: BLE001
        return False


def _load_model():
    """惰性加载模型（首次调用会下载，可能几十秒到几分钟）。"""
    global _model, _model_error
    if _model is not None:
        return _model
    with _model_lock:
        if _model is not None:
            return _model
        if not available():
            raise TranscribeError(
                "本机还没装本地语音识别。在项目目录里执行："
                ".\\.venv-rag\\Scripts\\python.exe -m pip install faster-whisper")
        # ★ 顺序很重要，必须在 import faster_whisper **之前** 做完这三件事：
        #   1) 把缓存目录指到项目内（否则受限环境写不进去）
        #   2) 决定模型源（huggingface_hub 在 import 时读 HF_ENDPOINT）
        _prepare_cache_dir()
        endpoint = pick_endpoint()
        if endpoint:
            print(f"[heytalk] huggingface.co 连不上，已自动改用镜像 {endpoint}"
                  f"（并关闭 Xet，因为它不在镜像范围内）", file=sys.stderr)
        _prepare_transport(endpoint)
        try:
            from faster_whisper import WhisperModel
        except Exception as exc:  # noqa: BLE001
            raise TranscribeError(f"语音识别库加载失败：{type(exc).__name__}") from exc

        name = model_name()
        try:
            # device=cpu：不假设用户有 NVIDIA 显卡。
            # compute_type=int8：CPU 上快得多，中文精度损失很小。
            _model = WhisperModel(name, device="cpu", compute_type="int8")
        except Exception as exc:  # noqa: BLE001
            _model_error = f"{type(exc).__name__}: {exc}"
            # 错误名（LocalEntryNotFoundError）看不出是网络问题，
            # 也可能只是"官方源不通"，所以这里把**两种**可能都说清楚。
            hint = ("首次下载模型需要联网。当前官方源 "
                    f"{_HF_HOST} 与镜像 {_MIRROR_HOST} 都没连上，"
                    "请检查网络后再试一次。"
                    if not endpoint else
                    f"首次下载模型失败（已尝试镜像 {endpoint}），请检查网络。")
            raise TranscribeError(
                f"语音模型（{name}）没能就绪：{hint}"
                f"（{type(exc).__name__}）") from exc
        return _model


def warm_up() -> str:
    """预热：把模型提前载入（可选）。返回一句状态说明，不抛异常。

    为什么提供它：首次识别要下载模型，用户会等很久且不知道在等什么。
    服务启动时预热可以把这个等待挪到"没人用的时候"。
    """
    try:
        _load_model()
        return f"语音模型 {model_name()} 已就绪"
    except TranscribeError as exc:
        return f"语音模型未就绪：{exc}"
    except Exception as exc:  # noqa: BLE001
        return f"语音模型未就绪：{type(exc).__name__}"


def _decode_to_pcm16k(path: Path):
    """把音频文件解成 16kHz 单声道 float32 数组。

    ★ 自己解而不是交给 faster-whisper：见模块顶部那段（上游不兼容）。
    这里只使用 av 19.0.1 确实支持的 API —— **不传 metadata_errors**。
    """
    try:
        import av
        import numpy as np
    except Exception as exc:  # noqa: BLE001
        raise TranscribeError(
            f"音频解码库不可用（{type(exc).__name__}）。"
            f"可以试试重新安装：pip install av numpy") from exc

    chunks = []
    try:
        with av.open(str(path)) as container:
            if not container.streams.audio:
                raise TranscribeError("这段文件里没有音频轨道，可能录的时候没采到声音。")
            stream = container.streams.audio[0]
            resampler = av.AudioResampler(format="fltp", layout="mono",
                                          rate=_TARGET_SR)
            for frame in container.decode(stream):
                for rf in resampler.resample(frame):
                    chunks.append(rf.to_ndarray())
            # 冲掉重采样器里剩下的缓冲，否则末尾会丢一小段
            for rf in resampler.resample(None):
                chunks.append(rf.to_ndarray())
    except TranscribeError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise TranscribeError(
            f"这段录音解不开（{type(exc).__name__}）。"
            f"换一种录制方式或直接打字都可以。") from exc

    if not chunks:
        raise TranscribeError("这段录音里没有声音。")
    # fltp 是 planar 格式（形状 通道数×采样点），展平成一维
    return np.concatenate([c.reshape(-1) for c in chunks]).astype(np.float32)


def transcribe(audio_bytes: bytes, *, language: str = "zh") -> str:
    """把一段音频识别成文字。

    audio_bytes：浏览器 MediaRecorder 产出的容器（通常是 webm/opus）。
                 解码由本模块用 av 完成，见 _decode_to_pcm16k 的说明。
    language：默认中文。传 None 让它自动判语言（会慢一点）。

    失败一律抛 TranscribeError（中文、可操作）。
    """
    if not audio_bytes:
        raise TranscribeError("没有收到音频")

    model = _load_model()

    # 写临时文件：av 从文件路径解最稳（内存流要自己处理 seek）
    tmp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=".webm") as fh:
            fh.write(audio_bytes)
            tmp_path = Path(fh.name)

        # ★ 先自己解码再交给模型：这样就不会走到 faster-whisper 里那个
        #   与 av 19 不兼容的 decode_audio（见模块顶部说明）。
        samples = _decode_to_pcm16k(tmp_path)

        seconds = len(samples) / float(_TARGET_SR)
        if seconds < 0.3:
            raise TranscribeError("这段太短了，没听清。可以再录一次。")
        if seconds > MAX_AUDIO_SECONDS:
            raise TranscribeError(
                f"这段有点长（约 {seconds:.0f} 秒，上限 {MAX_AUDIO_SECONDS} 秒），"
                f"可以分几次说。")

        try:
            segments, info = model.transcribe(
                samples,               # ← 传数组，模型会跳过它自己的解码器
                language=language or None,
                beam_size=5,
                vad_filter=True,       # 去掉静音，避免把长静音"听"成幻听
            )
            text = "".join(seg.text for seg in segments).strip()
        except Exception as exc:  # noqa: BLE001
            raise TranscribeError(
                f"这段音频没能识别出来（{type(exc).__name__}）。"
                f"可以再录一次，或者直接打字。") from exc

        if not text:
            raise TranscribeError("没听清，可以再说一次或者直接打字")
        return text
    finally:
        # ★ 无论成功失败都删掉音频 —— 这是"录音不留痕"的关键一步
        if tmp_path is not None:
            try:
                tmp_path.unlink(missing_ok=True)
            except Exception:  # noqa: BLE001
                print(f"[heytalk] 临时音频删除失败：{tmp_path}", file=sys.stderr)
