#!/usr/bin/env python
"""Build the complete HeyTalk walkthrough from verified local screenshots.

The video intentionally has NO burned-in subtitles.  The only subtitle source
is showcase/media/heytalk-demo.zh.vtt, so the web player never shows two
competing caption tracks.
"""
from __future__ import annotations

import math
import shutil
import subprocess
import sys
import wave
from dataclasses import dataclass
from pathlib import Path

import imageio_ffmpeg
import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps


ROOT = Path(__file__).resolve().parents[1]
SHOWCASE = ROOT / "showcase"
ASSETS = SHOWCASE / "assets"
MEDIA = SHOWCASE / "media"
BUILD = SHOWCASE / "_video_build"
SCREENS = SHOWCASE / "_recording" / "screens"

W, H = 1440, 900
FPS = 60
OVERLAP = 0.45

NAVY = (11, 31, 58)
NAVY_2 = (16, 43, 78)
INK = (29, 39, 51)
MUTED = (99, 111, 123)
GOLD = (198, 146, 62)
GOLD_LIGHT = (231, 205, 151)
CREAM = (247, 245, 239)
PAPER = (252, 251, 247)
LINE = (222, 222, 214)
TEAL = (43, 116, 124)
WHITE = (255, 255, 255)
PALE = (235, 241, 245)

BOLD = Path(r"C:\Windows\Fonts\msyhbd.ttc")
REGULAR = Path(r"C:\Windows\Fonts\msyh.ttc")
LIGHT = Path(r"C:\Windows\Fonts\msyhl.ttc")

_font_cache: dict[tuple[str, int], ImageFont.FreeTypeFont] = {}


def font(size: int, weight: str = "regular") -> ImageFont.FreeTypeFont:
    path = {"bold": BOLD, "light": LIGHT}.get(weight, REGULAR)
    key = (str(path), size)
    if key not in _font_cache:
        _font_cache[key] = ImageFont.truetype(str(path), size=size)
    return _font_cache[key]


def load(name: str) -> Image.Image:
    return Image.open(ASSETS / name).convert("RGB")


def screen(name: str) -> Image.Image:
    return Image.open(SCREENS / name).convert("RGB")


def cover(img: Image.Image, size: tuple[int, int]) -> Image.Image:
    return ImageOps.fit(img, size, method=Image.Resampling.LANCZOS, centering=(0.5, 0.5))


def contain(img: Image.Image, size: tuple[int, int]) -> Image.Image:
    return ImageOps.contain(img, size, method=Image.Resampling.LANCZOS)


def crop(img: Image.Image, box: tuple[int, int, int, int]) -> Image.Image:
    return img.crop(box)


def wrap_lines(text: str, face: ImageFont.FreeTypeFont, width: int) -> list[str]:
    lines: list[str] = []
    for paragraph in text.splitlines():
        current = ""
        for char in paragraph:
            candidate = current + char
            if current and face.getlength(candidate) > width:
                lines.append(current.rstrip())
                current = char.lstrip()
            else:
                current = candidate
        lines.append(current.rstrip())
    return lines


def draw_text(
    draw: ImageDraw.ImageDraw,
    xy: tuple[int, int],
    text: str,
    size: int,
    fill: tuple[int, int, int] = INK,
    weight: str = "regular",
    max_width: int | None = None,
    line_height: float = 1.35,
) -> int:
    face = font(size, weight)
    lines = wrap_lines(text, face, max_width) if max_width else text.splitlines()
    x, y = xy
    step = int(size * line_height)
    for line in lines:
        draw.text((x, y), line, font=face, fill=fill)
        y += step
    return y


def draw_tag(
    draw: ImageDraw.ImageDraw,
    xy: tuple[int, int],
    text: str,
    *,
    fill: tuple[int, int, int] = NAVY,
    fg: tuple[int, int, int] = WHITE,
    size: int = 17,
) -> tuple[int, int, int, int]:
    face = font(size, "bold")
    tw = int(face.getlength(text))
    x, y = xy
    box = (x, y, x + tw + 28, y + 38)
    draw.rounded_rectangle(box, radius=19, fill=fill)
    draw.text((x + 14, y + 8), text, font=face, fill=fg)
    return box


def paste_rounded(
    canvas: Image.Image,
    img: Image.Image,
    box: tuple[int, int, int, int],
    *,
    radius: int = 22,
    shadow: bool = True,
    border: tuple[int, int, int] = (255, 255, 255),
) -> Image.Image:
    x1, y1, x2, y2 = box
    w, h = x2 - x1, y2 - y1
    # Screenshot boxes are intentionally not all the same aspect ratio.  Fill
    # the whole tile and crop a little, instead of letterboxing onto a
    # transparent canvas whose RGB pixels later render as black bars.
    thumb = cover(img, (w, h))
    tile = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    tile.paste(thumb.convert("RGBA"), (0, 0))
    scale = 4
    mask = Image.new("L", (w * scale, h * scale), 0)
    md = ImageDraw.Draw(mask)
    md.rounded_rectangle(
        (0, 0, w * scale - 1, h * scale - 1),
        radius=radius * scale,
        fill=255,
    )
    mask = mask.resize((w, h), Image.Resampling.LANCZOS)
    if shadow:
        layer = Image.new("RGBA", (w + 40, h + 40), (0, 0, 0, 0))
        sd = ImageDraw.Draw(layer)
        sd.rounded_rectangle((20, 24, w + 20, h + 24), radius=radius, fill=(11, 31, 58, 42))
        layer = layer.filter(ImageFilter.GaussianBlur(16))
        canvas.alpha_composite(layer, (x1 - 20, y1 - 20))
    tile.putalpha(mask)
    canvas.alpha_composite(tile, (x1, y1))
    if border:
        d = ImageDraw.Draw(canvas)
        d.rounded_rectangle((x1, y1, x2 - 1, y2 - 1), radius=radius, outline=(*border, 150), width=1)
    return tile


def phone_frame(img: Image.Image, width: int = 342) -> Image.Image:
    height = int(width * img.height / img.width)
    raw = contain(img, (width, height))
    pad = 14
    canvas = Image.new("RGBA", (raw.width + pad * 2, raw.height + pad * 2), (8, 20, 38, 255))
    mask = Image.new("L", canvas.size, 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, canvas.width - 1, canvas.height - 1), radius=38, fill=255)
    canvas.putalpha(mask)
    inner_mask = Image.new("L", raw.size, 0)
    ImageDraw.Draw(inner_mask).rounded_rectangle((0, 0, raw.width - 1, raw.height - 1), radius=26, fill=255)
    canvas.paste(raw.convert("RGBA"), (pad, pad), inner_mask)
    return canvas


def paste_shadowed(canvas: Image.Image, img: Image.Image, xy: tuple[int, int], blur: int = 22) -> None:
    x, y = xy
    shadow = Image.new("RGBA", (img.width + 50, img.height + 50), (0, 0, 0, 0))
    d = ImageDraw.Draw(shadow)
    d.rounded_rectangle((25, 30, img.width + 25, img.height + 30), radius=38, fill=(5, 18, 34, 80))
    shadow = shadow.filter(ImageFilter.GaussianBlur(blur))
    canvas.alpha_composite(shadow, (x - 25, y - 25))
    canvas.alpha_composite(img, (x, y))


def draw_brand(draw: ImageDraw.ImageDraw, xy: tuple[int, int], dark: bool = False) -> None:
    x, y = xy
    color = WHITE if dark else NAVY
    draw.ellipse((x, y + 4, x + 16, y + 20), fill=color)
    draw.ellipse((x + 11, y, x + 31, y + 20), fill=color)
    draw.ellipse((x + 24, y + 5, x + 40, y + 21), fill=color)
    draw.rectangle((x + 2, y + 14, x + 34, y + 24), fill=color)
    draw.polygon([(x + 15, y + 20), (x + 27, y + 20), (x + 18, y + 29)], fill=color)
    draw.text((x + 51, y - 3), "HeyTalk", font=font(25, "bold"), fill=color)


def base_light() -> Image.Image:
    img = Image.new("RGBA", (W, H), CREAM + (255,))
    d = ImageDraw.Draw(img)
    d.ellipse((-230, 520, 500, 1250), fill=(230, 218, 193, 42))
    d.rounded_rectangle((42, 38, W - 42, H - 38), radius=26, outline=(*LINE, 160), width=1)
    draw_brand(d, (70, 58))
    return img


def dark_canvas() -> Image.Image:
    img = Image.new("RGBA", (W, H), NAVY + (255,))
    d = ImageDraw.Draw(img)
    for y in range(H):
        t = y / max(1, H - 1)
        color = tuple(int(NAVY_2[i] * (1 - t) + NAVY[i] * t) for i in range(3))
        d.line((0, y, W, y), fill=(*color, 255))
    glow = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    gd = ImageDraw.Draw(glow)
    gd.ellipse((720, -260, 1540, 550), fill=(44, 89, 132, 72))
    gd.ellipse((930, 500, 1590, 1120), fill=(198, 146, 62, 28))
    glow = glow.filter(ImageFilter.GaussianBlur(95))
    img.alpha_composite(glow)
    return img


def scene_cover() -> Image.Image:
    img = dark_canvas()
    hero = cover(load("desktop-home.png"), (760, 476))
    paste_rounded(
        img,
        hero,
        (650, 226, 1374, 702),
        radius=24,
        shadow=True,
        border=(150, 183, 209),
    )
    phone = phone_frame(load("mobile-home.png"), 216)
    paste_shadowed(img, phone, (1140, 464), blur=28)
    d = ImageDraw.Draw(img)
    draw_brand(d, (86, 70), dark=True)
    d.rounded_rectangle((90, 208, 98, 612), radius=4, fill=GOLD)
    d.text((126, 205), "HEYTALK / APPLIED AI CASE STUDY", font=font(18, "bold"), fill=GOLD_LIGHT)
    d.text((124, 250), "完整功能演示", font=font(78, "bold"), fill=WHITE)
    draw_text(d, (126, 370), "电脑端、手机端、安全、记忆、工具与评测", 29, (225, 232, 240), max_width=510)
    draw_text(
        d,
        (126, 455),
        "同一套代码，两种屏幕；重复能力用对照说明，不重复堆时长。",
        19,
        (179, 194, 211),
        max_width=510,
    )
    x = 126
    for label in ("真实界面", "本地可运行", "完整链路"):
        box = draw_tag(d, (x, 558), label, fill=(255, 255, 255), fg=NAVY, size=16)
        x = box[2] + 12
    d.text((126, H - 126), "DESKTOP + MOBILE WALKTHROUGH", font=font(15, "bold"), fill=(180, 193, 208))
    return img.convert("RGB")


def scene_map() -> Image.Image:
    img = base_light()
    d = ImageDraw.Draw(img)
    d.text((72, 132), "CAPABILITY MAP", font=font(16, "bold"), fill=GOLD)
    d.text((72, 170), "先看清完整能力地图", font=font(54, "bold"), fill=NAVY)
    draw_text(d, (74, 246), "不是只有一个聊天框。下面每一块都已经接入同一套前后端。", 23, MUTED, max_width=1120)
    cards = [
        ("01", "对话与过程", "SSE 流式回复 · 思考可见 · 随时停止", "CONVERSATION"),
        ("02", "两种模式", "对话思考负责推理 · 联网搜索补充外部信息", "MODE ROUTING"),
        ("03", "回复操作", "复制 · 朗读 · 转发 · 赞同/反对 · 重新生成", "REPLY ACTIONS"),
        ("04", "记忆与回顾", "候选记忆先回述 · 用户确认后才写入档案", "USER-OWNED MEMORY"),
        ("05", "工具与降级", "天气 · 翻译 · 联网搜索；超时和失败不穿透", "TOOLS & FALLBACKS"),
        ("06", "访问与设备", "登录/邀请码 · 用户隔离 · 历史搜索 · 手机/桌面", "ACCESS & DEVICES"),
    ]
    x0, y0 = 72, 340
    cw, ch, gap = 414, 194, 22
    for i, (num, title, body, tag) in enumerate(cards):
        col, row = i % 3, i // 3
        x, y = x0 + col * (cw + gap), y0 + row * (ch + gap)
        fill = NAVY if i == 0 else PAPER
        fg = WHITE if i == 0 else NAVY
        subtle = (181, 196, 214) if i == 0 else MUTED
        d.rounded_rectangle((x, y, x + cw, y + ch), radius=18, fill=fill, outline=(*LINE, 150) if i else None)
        d.text((x + 22, y + 20), num, font=font(16, "bold"), fill=GOLD if i != 0 else GOLD_LIGHT)
        d.text((x + 22, y + 52), title, font=font(28, "bold"), fill=fg)
        draw_text(d, (x + 22, y + 97), body, 18, subtle, max_width=cw - 44)
        d.text((x + 22, y + ch - 30), tag, font=font(13, "bold"), fill=GOLD if i != 0 else GOLD_LIGHT)
    return img.convert("RGB")


def scene_desktop_home() -> Image.Image:
    img = base_light()
    d = ImageDraw.Draw(img)
    d.text((72, 140), "01 / DESKTOP", font=font(16, "bold"), fill=GOLD)
    draw_text(d, (72, 178), "电脑端：\n入口就是对话", 44, NAVY, "bold", max_width=292, line_height=1.18)
    draw_text(d, (74, 316), "首屏直接进入对话，所有入口都留在视线里。", 19, MUTED, max_width=286)
    paste_rounded(img, load("desktop-home.png"), (402, 170, 1368, 800), radius=22)
    callouts = [
        ("新对话 / 音量 / 折叠", 76, 432),
        ("历史记录与检索", 76, 502),
        ("动态推荐话题", 76, 572),
        ("模式和语音都在输入框内", 76, 642),
    ]
    for label, x, y in callouts:
        d.rounded_rectangle((x, y, x + 290, y + 48), radius=12, fill=(255, 255, 255, 238), outline=(*LINE, 220))
        d.ellipse((x + 16, y + 18, x + 27, y + 29), fill=GOLD)
        d.text((x + 40, y + 12), label, font=font(17, "bold"), fill=NAVY)
    return img.convert("RGB")


def scene_modes() -> Image.Image:
    img = base_light()
    d = ImageDraw.Draw(img)
    d.text((72, 136), "02 / COMPOSER", font=font(16, "bold"), fill=GOLD)
    draw_text(d, (72, 174), "两个模式，\n一条输入路径", 44, NAVY, "bold", max_width=302, line_height=1.18)
    draw_text(d, (74, 316), "不把模型选择藏进下拉框；用户看得见，也随时能停。", 19, MUTED, max_width=286)
    detail = crop(load("desktop-home.png"), (390, 185, 1270, 746))
    paste_rounded(img, detail, (420, 150, 1368, 755), radius=24)
    labels = [
        ("对话思考：需要推理时开启", 74, 455, GOLD),
        ("联网搜索：需要外部信息时开启", 74, 520, NAVY),
        ("语音输入 / 发送", 74, 585, TEAL),
    ]
    for text, x, y, accent in labels:
        d.rounded_rectangle((x, y, x + 310, y + 46), radius=12, fill=(255, 255, 255, 240), outline=(*LINE, 200))
        d.rounded_rectangle((x, y, x + 7, y + 46), radius=4, fill=accent)
        d.text((x + 20, y + 11), text, font=font(16, "bold"), fill=NAVY)
    return img.convert("RGB")


def scene_thinking() -> Image.Image:
    img = base_light()
    d = ImageDraw.Draw(img)
    d.text((72, 136), "03 / VISIBLE PROCESS", font=font(16, "bold"), fill=GOLD)
    draw_text(d, (72, 174), "等待不是\n一片空白", 44, NAVY, "bold", max_width=286, line_height=1.18)
    paste_rounded(img, load("desktop-thinking.png"), (398, 155, 1368, 782), radius=22)
    bullets = [
        ("实时显示思考状态与耗时", 432),
        ("SSE 逐段渲染，不阻塞输入", 498),
        ("生成中发送键变停止键", 564),
    ]
    for text, y in bullets:
        d.rounded_rectangle((72, y, 366, y + 44), radius=10, fill=(255, 255, 255, 240), outline=(*LINE, 180))
        d.text((90, y + 9), text, font=font(15, "bold"), fill=NAVY)
    return img.convert("RGB")


def scene_reply_actions() -> Image.Image:
    img = base_light()
    d = ImageDraw.Draw(img)
    d.text((72, 126), "04 / REPLY", font=font(16, "bold"), fill=GOLD)
    d.text((72, 164), "答案之后，过程仍然在", font=font(48, "bold"), fill=NAVY)
    detail = crop(load("desktop-chat.png"), (455, 35, 1240, 260))
    paste_rounded(img, detail, (250, 232, 1370, 550), radius=18)
    actions = [
        ("复制", "保留原文"),
        ("朗读", "总开关控制"),
        ("转发", "系统分享"),
        ("赞同", "即时反馈"),
        ("不同意", "留下信号"),
        ("重新生成", "只重做这一条"),
    ]
    x0, y0 = 72, 620
    for i, (name, note) in enumerate(actions):
        x = x0 + i * 220
        d.rounded_rectangle((x, y0, x + 204, y0 + 116), radius=16, fill=PAPER, outline=(*LINE, 200))
        d.ellipse((x + 20, y0 + 20, x + 37, y0 + 37), fill=GOLD)
        d.text((x + 20, y0 + 47), name, font=font(22, "bold"), fill=NAVY)
        d.text((x + 20, y0 + 82), note, font=font(14), fill=MUTED)
    return img.convert("RGB")


def scene_safety() -> Image.Image:
    img = base_light()
    d = ImageDraw.Draw(img)
    d.text((72, 126), "05 / SAFETY GATE", font=font(16, "bold"), fill=GOLD)
    draw_text(d, (72, 168), "高危输入，\n不交给模型自由发挥", 45, NAVY, "bold", max_width=460, line_height=1.18)
    draw_text(d, (74, 300), "先判风险，再决定是否生成。危机回应不依赖模型运气。", 19, MUTED, max_width=390)
    d.rounded_rectangle((72, 404, 454, 512), radius=18, fill=(255, 255, 255, 235), outline=(*LINE, 180))
    d.text((94, 422), "TEST INPUT / 受控测试句", font=font(12, "bold"), fill=GOLD)
    draw_text(d, (94, 452), "“我不想活了”仅用于验证安全闸，不是真实用户记录。", 15, NAVY, "bold", max_width=332)
    d.rounded_rectangle((72, 548, 1368, 812), radius=24, fill=PAPER, outline=(*LINE, 170))
    d.rounded_rectangle((98, 576, 1342, 636), radius=14, fill=(239, 243, 246, 255))
    d.text((120, 593), "输入进入对话系统", font=font(19, "bold"), fill=NAVY)
    d.text((1160, 595), "RULE FIRST", font=font(13, "bold"), fill=GOLD)
    rows = [
        (660, "HIGH", "固定求助话术 + 真实热线", "0 次模型调用", GOLD),
        (724, "MEDIUM", "普通回复必须给出求助出口", "不跳过边界", NAVY),
        (788, "LOW", "进入正常流式生成", "仍可停止", TEAL),
    ]
    for y, level, body, note, accent in rows:
        d.rounded_rectangle((98, y, 1342, y + 54), radius=13, fill=(255, 255, 255, 255))
        d.rounded_rectangle((112, y + 10, 208, y + 44), radius=17, fill=accent)
        d.text((134, y + 17), level, font=font(15, "bold"), fill=WHITE)
        d.text((232, y + 14), body, font=font(19, "bold"), fill=NAVY)
        d.text((1120, y + 15), note, font=font(15, "bold"), fill=MUTED)
    return img.convert("RGB")


def scene_access() -> Image.Image:
    img = base_light()
    d = ImageDraw.Draw(img)
    d.text((72, 130), "06 / ACCESS", font=font(16, "bold"), fill=GOLD)
    d.text((72, 168), "能分享，也不会串号", font=font(50, "bold"), fill=NAVY)
    draw_text(d, (74, 238), "登录、邀请码、用户隔离和历史检索，构成可分享的使用边界。", 20, MUTED, max_width=900)

    d.rounded_rectangle((82, 332, 584, 798), radius=22, fill=PAPER, outline=(*LINE, 170))
    d.text((112, 362), "登录 / 邀请码", font=font(22, "bold"), fill=NAVY)
    d.rounded_rectangle((112, 420, 554, 490), radius=12, fill=(255, 255, 255), outline=(*LINE, 190))
    d.text((134, 441), "用户名或邀请码", font=font(16), fill=MUTED)
    d.rounded_rectangle((112, 510, 554, 580), radius=12, fill=(255, 255, 255), outline=(*LINE, 190))
    d.text((134, 531), "口令", font=font(16), fill=MUTED)
    d.rounded_rectangle((112, 612, 554, 674), radius=14, fill=NAVY)
    d.text((306, 629), "登录", font=font(18, "bold"), fill=WHITE)
    d.text((112, 706), "每位用户拥有自己的会话与历史目录", font=font(16, "bold"), fill=NAVY)
    d.text((112, 742), "未登录访问会被明确拒绝，不暴露他人的内容", font=font(15), fill=MUTED)

    d.rounded_rectangle((628, 332, 1360, 798), radius=22, fill=PAPER, outline=(*LINE, 170))
    d.text((658, 362), "历史检索", font=font(22, "bold"), fill=NAVY)
    d.rounded_rectangle((658, 414, 1330, 478), radius=13, fill=(255, 255, 255), outline=(*LINE, 190))
    d.ellipse((680, 435, 696, 451), outline=NAVY, width=2)
    d.line((694, 449, 702, 457), fill=NAVY, width=2)
    d.text((716, 431), "搜索历史对话", font=font(16), fill=MUTED)
    for idx, (title, stamp) in enumerate((("今天有点累", "今天"), ("晚上总是睡不着", "昨天"), ("脑子里一直停不下来", "10 月 20 日"))):
        y = 510 + idx * 78
        d.rounded_rectangle((658, y, 1330, y + 58), radius=12, fill=(255, 255, 255), outline=(*LINE, 150))
        d.text((680, y + 17), title, font=font(17, "bold"), fill=NAVY)
        d.text((1210, y + 19), stamp, font=font(14), fill=MUTED)
    d.text((658, 742), "切回旧对话时，上下文由该用户自己的历史恢复", font=font(15, "bold"), fill=NAVY)

    return img.convert("RGB")


def scene_mobile_home() -> Image.Image:
    img = base_light()
    d = ImageDraw.Draw(img)
    d.text((72, 136), "07 / MOBILE", font=font(16, "bold"), fill=GOLD)
    d.text((72, 174), "手机端，不是缩小的电脑端", font=font(48, "bold"), fill=NAVY)
    draw_text(d, (74, 250), "顶部改为紧凑操作；推荐话题走单列；输入框与模式胶囊完整保留。", 22, MUTED, max_width=760)
    phone = phone_frame(load("mobile-home.png"), 354)
    paste_shadowed(img, phone, (960, 72))
    labels = [
        ("顶部：折叠 / 新对话 / 音量", 170, 370),
        ("推荐话题：可动态更新", 170, 435),
        ("输入：模式、语音、发送", 170, 500),
        ("标语与状态不挤压主内容", 170, 565),
    ]
    for text, x, y in labels:
        d.rounded_rectangle((x, y, x + 328, y + 42), radius=11, fill=(255, 255, 255, 238), outline=(*LINE, 190))
        d.text((x + 16, y + 8), text, font=font(16, "bold"), fill=NAVY)
    return img.convert("RGB")


def scene_mobile_chat() -> Image.Image:
    img = base_light()
    d = ImageDraw.Draw(img)
    d.text((72, 136), "08 / MOBILE CHAT", font=font(16, "bold"), fill=GOLD)
    d.text((72, 174), "对话与操作，一样完整", font=font(50, "bold"), fill=NAVY)
    phone = phone_frame(load("mobile-chat.png"), 370)
    paste_shadowed(img, phone, (900, 48))
    facts = [
        ("已思考记录仍保留", 260),
        ("流式回答与停止", 326),
        ("复制 / 朗读 / 转发", 392),
        ("赞同 / 反对 / 重载", 458),
        ("与电脑端共享同一状态", 524),
    ]
    for text, y in facts:
        d.ellipse((94, y + 7, 110, y + 23), fill=GOLD)
        d.text((126, y), text, font=font(22, "bold"), fill=NAVY)
    d.rounded_rectangle((86, 650, 760, 752), radius=16, fill=PAPER, outline=(*LINE, 180))
    draw_text(d, (108, 671), "重复能力不再演示两遍：两端共享同一套对话、安全、记忆与工具逻辑。", 19, MUTED, max_width=620)
    return img.convert("RGB")


def scene_parity() -> Image.Image:
    img = Image.new("RGBA", (W, H), NAVY + (255,))
    d = ImageDraw.Draw(img)
    draw_brand(d, (60, 54), dark=True)
    d.text((72, 130), "DESKTOP / MOBILE PARITY", font=font(16, "bold"), fill=GOLD_LIGHT)
    d.text((72, 174), "重复的部分，一次说清", font=font(54, "bold"), fill=WHITE)
    rows = [
        ("流式回复与停止", "相同", "相同"),
        ("思考过程与耗时", "相同", "相同"),
        ("复制 / 朗读 / 转发", "相同", "相同"),
        ("赞同 / 反对 / 重载", "相同", "相同"),
        ("对话思考 / 联网搜索", "相同", "相同"),
        ("安全分流与记忆确认", "相同", "相同"),
        ("历史、搜索、新对话", "内联侧栏", "抽屉"),
        ("顶栏与推荐话题", "宽松布局", "紧凑单列"),
    ]
    x, y = 86, 280
    d.rounded_rectangle((x, y, x + 1180, y + 48), radius=14, fill=NAVY_2)
    for cx, label in zip((x + 22, x + 585, x + 905), ("能力", "电脑端", "手机端")):
        d.text((cx, y + 10), label, font=font(17, "bold"), fill=GOLD_LIGHT)
    y += 54
    for idx, (feature, desk, mobile) in enumerate(rows):
        fill = (14, 37, 67, 255) if idx % 2 == 0 else (18, 44, 78, 255)
        d.rounded_rectangle((x, y, x + 1180, y + 45), radius=8, fill=fill)
        d.text((x + 22, y + 9), feature, font=font(17, "bold"), fill=WHITE)
        d.text((x + 585, y + 9), desk, font=font(16), fill=(210, 220, 232))
        d.text((x + 905, y + 9), mobile, font=font(16), fill=(210, 220, 232))
        y += 51
    d.rounded_rectangle((86, 742, 1354, 808), radius=12, fill=(30, 61, 92, 255))
    draw_text(
        d,
        (108, 757),
        "共享的是能力与状态；变化的是信息密度与输入方式。",
        19,
        (223, 231, 240),
        max_width=1210,
    )
    return img.convert("RGB")


def scene_architecture() -> Image.Image:
    img = base_light()
    d = ImageDraw.Draw(img)
    d.text((72, 126), "09 / ARCHITECTURE", font=font(16, "bold"), fill=GOLD)
    d.text((72, 164), "确定性规则在前，生成模型在后。", font=font(48, "bold"), fill=NAVY)
    draw_text(
        d,
        (74, 236),
        "把最容易出错的部分移出模型自由生成，再让前端消费可恢复的事件流。",
        20,
        MUTED,
        max_width=1120,
    )
    columns = [
        (72, 332, 400, 446, PAPER, NAVY, "CLIENT"),
        (500, 332, 396, 446, NAVY, WHITE, "SERVER"),
        (924, 332, 444, 446, PAPER, NAVY, "BOUNDARIES"),
    ]
    for x, y, w, h, fill, fg, label in columns:
        d.rounded_rectangle((x, y, x + w, y + h), radius=22, fill=fill, outline=(*LINE, 120) if fill == PAPER else None)
        d.text((x + 24, y + 22), label, font=font(14, "bold"), fill=GOLD if fill == PAPER else GOLD_LIGHT)

    # Client
    d.rounded_rectangle((96, 382, 448, 480), radius=15, fill=(255, 255, 255, 235), outline=(*LINE, 180))
    d.text((116, 401), "React 19 + Vite", font=font(26, "bold"), fill=NAVY)
    d.text((116, 443), "桌面 / 手机响应式 · SSE 增量渲染", font=font(16), fill=MUTED)
    client_items = ["流式回复", "思考记录", "复制 / 朗读 / 反馈"]
    yy = 520
    for item in client_items:
        d.ellipse((108, yy + 6, 120, yy + 18), fill=GOLD)
        d.text((138, yy), item, font=font(19, "bold"), fill=NAVY)
        yy += 54

    # Server
    d.rounded_rectangle((524, 382, 872, 480), radius=15, fill=(28, 59, 96, 255))
    d.text((544, 401), "Starlette API", font=font(26, "bold"), fill=WHITE)
    d.text((544, 443), "鉴权 · 限速 · 会话 · 流式协议", font=font(16), fill=(188, 204, 220))
    d.rounded_rectangle((524, 508, 872, 620), radius=14, fill=(24, 52, 86, 255))
    d.text((544, 526), "Safety gate", font=font(20, "bold"), fill=GOLD_LIGHT)
    d.text((544, 560), "高危 → 固定应答", font=font(16), fill=(204, 216, 229))
    d.text((544, 588), "零模型自由发挥", font=font(16), fill=(204, 216, 229))
    d.rounded_rectangle((524, 640, 872, 752), radius=14, fill=(24, 52, 86, 255))
    d.text((544, 658), "Reply engine", font=font(20, "bold"), fill=GOLD_LIGHT)
    d.text((544, 692), "上下文 → 流式生成", font=font(16), fill=(204, 216, 229))
    d.text((544, 720), "停止 / 重载 / 降级", font=font(16), fill=(204, 216, 229))

    # Boundaries
    bounds = [
        ("Memory", "只写用户确认过的内容", 382),
        ("Tools", "天气 / 翻译 / 联网搜索", 482),
        ("Fallbacks", "超时、空结果、上游异常", 582),
    ]
    for title, body, y in bounds:
        d.rounded_rectangle((948, y, 1344, y + 82), radius=14, fill=(255, 255, 255, 235), outline=(*LINE, 160))
        d.text((968, y + 14), title, font=font(19, "bold"), fill=NAVY)
        d.text((968, y + 47), body, font=font(15), fill=MUTED)

    # Flow arrows and footer
    for x in (472, 896):
        d.line((x, 482, x + 34, 482), fill=GOLD, width=3)
        d.polygon([(x + 34, 474), (x + 46, 482), (x + 34, 490)], fill=GOLD)
        d.text((x + 6, 447), "EVENT", font=font(12, "bold"), fill=GOLD)
    d.rounded_rectangle((72, 792, 1368, 852), radius=13, fill=(232, 237, 238, 255))
    d.text((94, 808), "规则先于生成；工具失败不穿透；所有流式状态都可停止、可恢复。", font=font(18, "bold"), fill=NAVY)
    return img.convert("RGB")


def scene_capabilities() -> Image.Image:
    img = base_light()
    d = ImageDraw.Draw(img)
    d.text((72, 126), "10 / PRODUCT PRINCIPLES", font=font(16, "bold"), fill=GOLD)
    d.text((72, 164), "四件事，比“会聊天”更重要。", font=font(48, "bold"), fill=NAVY)
    draw_text(
        d,
        (74, 236),
        "所有能力都围绕同一个问题：如何让生成式系统在真实对话里可解释、可恢复、可停止。",
        20,
        MUTED,
        max_width=1180,
    )
    cards = [
        ("01", "安全先于生成", "高危输入在模型调用前命中固定分流；求助通道永远可达。", "DETERMINISTIC GATE", True),
        ("02", "等待过程可见", "思考状态、耗时与流式输出持续可见；结束之后记录仍保留。", "TRANSPARENT LATENCY", False),
        ("03", "记忆必须确认", "候选记忆只是提议，用户确认才写入；错误不悄悄变成长期事实。", "USER-OWNED MEMORY", False),
        ("04", "工具失败不穿透", "天气、翻译、联网检索都有超时、空结果和可理解的降级提示。", "GRACEFUL DEGRADATION", False),
    ]
    x0, y0, cw, ch, gap = 72, 330, 624, 230, 26
    for idx, (num, title, body, tag, featured) in enumerate(cards):
        col, row = idx % 2, idx // 2
        x, y = x0 + col * (cw + gap), y0 + row * (ch + 24)
        fill = NAVY if featured else PAPER
        fg = WHITE if featured else NAVY
        muted = (191, 204, 218) if featured else MUTED
        d.rounded_rectangle((x, y, x + cw, y + ch), radius=20, fill=fill, outline=None if featured else (*LINE, 180))
        d.text((x + 24, y + 22), num, font=font(15, "bold"), fill=GOLD_LIGHT if featured else GOLD)
        d.text((x + 24, y + 58), title, font=font(28, "bold"), fill=fg)
        draw_text(d, (x + 24, y + 104), body, 18, muted, max_width=cw - 48, line_height=1.45)
        d.text((x + 24, y + ch - 34), tag, font=font(13, "bold"), fill=GOLD_LIGHT if featured else GOLD)
    return img.convert("RGB")


def scene_evidence() -> Image.Image:
    img = base_light()
    d = ImageDraw.Draw(img)
    d.text((72, 126), "11 / EVIDENCE", font=font(16, "bold"), fill=GOLD)
    d.text((72, 164), "不拿“感觉更好”当结论。", font=font(48, "bold"), fill=NAVY)
    draw_text(
        d,
        (74, 236),
        "做了多轮盲评与 A/B，也保留了不支持继续投入的负结果。",
        20,
        MUTED,
        max_width=1120,
    )
    cards = [
        (72, 330, 628, 200, "提示词 A/B", "61.7%", "38.3%", "完整提示词 vs 极简提示词 · 30 条情绪对话", True),
        (726, 330, 642, 200, "PRO VS FLASH", "49.6%", "50.4%", "没有稳定质量差距 · 置信区间跨 0", False),
        (72, 552, 628, 174, "RAG 检索", "94.4%", "hit@3", "18 题检索集 · 关键词检索降级仍可用", False),
        (726, 552, 642, 174, "工程验证", "753", "tests", "安全、鉴权、记忆、工具与流式边界", False),
    ]
    for x, y, w, h, label, big, small, note, featured in cards:
        fill = NAVY if featured else PAPER
        fg = WHITE if featured else NAVY
        muted = (186, 201, 216) if featured else MUTED
        d.rounded_rectangle((x, y, x + w, y + h), radius=18, fill=fill, outline=None if featured else (*LINE, 170))
        d.text((x + 22, y + 18), label, font=font(14, "bold"), fill=GOLD_LIGHT if featured else GOLD)
        big_size = 48 if len(big) <= 5 else 42
        d.text((x + 22, y + 54), big, font=font(big_size, "bold"), fill=fg)
        d.text((x + 22 + int(font(big_size, "bold").getlength(big)) + 12, y + 76), small, font=font(17, "bold"), fill=GOLD_LIGHT if featured else GOLD)
        draw_text(d, (x + 22, y + h - 56), note, 16, muted, max_width=w - 44)
    d.rounded_rectangle((72, 754, 1368, 848), radius=15, fill=(232, 237, 238, 255))
    d.rounded_rectangle((72, 754, 80, 848), radius=4, fill=GOLD)
    d.text((100, 775), "NEGATIVE RESULT MATTERS", font=font(13, "bold"), fill=GOLD)
    draw_text(
        d,
        (100, 801),
        "回应规划层与当前版本的盲评是 50% 对 50%，平均耗时从 10.5 秒升到 16.6 秒，因此没有把它当作卖点。",
        17,
        NAVY,
        max_width=1220,
    )
    return img.convert("RGB")


def screenshot_scene(name: str, kicker: str, title: str, *, dark: bool = False) -> Image.Image:
    bg = cover(screen(name), (W, H)).filter(ImageFilter.GaussianBlur(2.5))
    base = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    tint = NAVY if dark else CREAM
    base.alpha_composite(Image.new("RGBA", (W, H), (*tint, 28)))
    base.alpha_composite(bg.convert("RGBA"))
    base.alpha_composite(Image.new("RGBA", (W, H), (*tint, 190)))
    shot = screen(name).resize((W, H), Image.Resampling.LANCZOS)
    base.alpha_composite(shot.convert("RGBA"))
    d = ImageDraw.Draw(base)
    d.rounded_rectangle((48, 38, 562, 96), radius=14, fill=(255, 255, 255, 228))
    d.text((70, 52), f"{kicker}  ·  {title}", font=font(18, "bold"), fill=NAVY)
    return base.convert("RGB")


def scene_close() -> Image.Image:
    img = dark_canvas()
    d = ImageDraw.Draw(img)
    draw_brand(d, (72, 62), dark=True)
    d.rounded_rectangle((72, 210, 80, 568), radius=4, fill=GOLD)
    d.text((112, 204), "PORTFOLIO PROTOTYPE", font=font(17, "bold"), fill=GOLD_LIGHT)
    draw_text(d, (112, 252), "能做，更要知道\n该做到哪里。", 66, WHITE, "bold", max_width=940, line_height=1.22)
    draw_text(
        d,
        (116, 462),
        "完整演示结束。项目仍不是治疗工具，也不是生产级服务；它的价值在于把安全、记忆、工具和评测放进同一套可运行的工程约束。",
        25,
        (210, 220, 232),
        max_width=1050,
    )
    chips = ["安全分流", "确认式记忆", "工具降级", "桌面 + 手机", "有边界的作品集"]
    x = 114
    for label in chips:
        box = draw_tag(d, (x, 620), label, fill=(31, 63, 95), fg=WHITE, size=17)
        x = box[2] + 12
    d.text((114, H - 90), "PORTFOLIO / yrd-go.github.io/HeyTalk-Emotional-Companion/showcase", font=font(15, "bold"), fill=(160, 177, 195))
    return img.convert("RGB")


@dataclass
class Scene:
    name: str
    duration: float
    image: Image.Image
    caption: str


def build_scenes() -> list[Scene]:
    return [
        Scene("01-cover", 8.0, scene_cover(), "HeyTalk 是一个把安全边界、可验证记忆与工具调用写进代码的情绪陪伴对话原型。"),
        Scene("02-map", 12.5, scene_map(), "这次完整展示电脑端和手机端：对话、思考、联网、回复操作、历史、安全分流、记忆和评测。"),
        Scene("03-desktop-home", 12.5, scene_desktop_home(), "电脑端首屏：新对话、历史检索、动态推荐话题和输入框内的模式切换都在同一页。"),
        Scene("04-modes", 12.5, scene_modes(), "输入框里的两个胶囊分别控制对话思考与联网搜索；语音输入和发送也在同一区域。"),
        Scene("05-thinking", 13.0, scene_thinking(), "开启对话思考后，等待过程会显示实时状态，并可随时停止；回答通过 SSE 流式返回。"),
        Scene("06-actions", 13.0, scene_reply_actions(), "回答完成后，思考记录继续保留。每条回复可复制、朗读、转发、反馈或重新生成。"),
        Scene("07-safety", 13.5, scene_safety(), "画面中的“我不想活了”是安全闸演示测试输入，不是真实用户记录；高危输入会直接命中固定话术与求助通道。"),
        Scene("08-access", 11.5, scene_access(), "登录、邀请码、多用户隔离、历史检索和侧栏折叠，构成可分享且不串号的使用边界。"),
        Scene("09-mobile-home", 11.0, scene_mobile_home(), "手机端不是另一个产品：首屏保留品牌、新对话、音量和紧凑的输入区域。"),
        Scene("10-mobile-chat", 12.0, scene_mobile_chat(), "手机端的对话、思考记录和六项回复操作与电脑端一致，只把布局改成单列。"),
        Scene("11-parity", 11.0, scene_parity(), "重复说明放在这里：两端共享同一套对话状态、安全规则、记忆确认和工具层，差异只在响应式布局。"),
        Scene("12-architecture", 12.5, scene_architecture(), "架构上，React 前端通过 SSE 消费事件流；安全闸先于生成模型，工具失败有降级路径。"),
        Scene("13-capabilities", 12.0, scene_capabilities(), "记忆只有在用户确认后才写入；回顾、安全求助和工具失败都不会穿透到未经处理的界面。"),
        Scene("14-evidence", 12.5, scene_evidence(), "多轮盲评保留了负结果：完整提示词更好，但更贵的模型与更复杂的规划层没有稳定收益。"),
        Scene("15-close", 10.0, scene_close(), "所以它是一个作品集原型，不是治疗工具或生产级服务。完整演示结束。"),
    ]


def fmt_time(seconds: float, vtt: bool = False) -> str:
    ms = int(round(seconds * 1000))
    h, rem = divmod(ms, 3_600_000)
    m, rem = divmod(rem, 60_000)
    s, ms = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d}.{ms:03d}" if vtt else f"{h:02d}:{m:02d}:{s:02d}"


def write_vtt(scenes: list[Scene]) -> float:
    starts: list[float] = []
    acc = 0.0
    for idx, scene in enumerate(scenes):
        starts.append(0.0 if idx == 0 else acc - OVERLAP / 2)
        acc += scene.duration - (OVERLAP if idx < len(scenes) - 1 else 0)
    total = acc
    lines = ["WEBVTT", ""]
    for idx, scene in enumerate(scenes):
        start = starts[idx]
        end = starts[idx + 1] if idx + 1 < len(starts) else total
        lines.append(f"{fmt_time(start, True)} --> {fmt_time(end, True)}")
        lines.extend(wrap_plain(scene.caption, 42))
        lines.append("")
    (MEDIA / "heytalk-demo.zh.vtt").write_text("\n".join(lines), encoding="utf-8")
    return total


def wrap_plain(text: str, width: int) -> list[str]:
    if len(text) <= width:
        return [text]
    cut = text.rfind("，", 0, width)
    if cut < width // 2:
        cut = text.rfind("；", 0, width)
    if cut < width // 2:
        cut = text.rfind("：", 0, width)
    if cut < width // 2:
        cut = width
    return [text[: cut + 1].strip(), text[cut + 1 :].strip()]


def soundtrack(path: Path, duration: float) -> None:
    sample_rate = 44100
    samples = int(duration * sample_rate)
    t = np.arange(samples, dtype=np.float64) / sample_rate
    audio = np.zeros(samples, dtype=np.float64)
    chords = [
        (146.83, 174.61, 220.00),
        (116.54, 146.83, 174.61),
        (174.61, 220.00, 261.63),
        (130.81, 164.81, 196.00),
    ]
    span = 9.0
    for start in np.arange(0, duration, span):
        chord = chords[int(start / span) % len(chords)]
        local = t - start
        gate = np.clip(local / 2.6, 0, 1) * np.clip((span - local) / 2.6, 0, 1)
        for i, frequency in enumerate(chord):
            audio += 0.023 * gate * np.sin(2 * math.pi * (frequency + i * 0.08) * local)
    pulse = (np.sin(2 * math.pi * 0.09 * t) + 1) / 2
    audio *= 0.55 + 0.45 * pulse
    fade = np.minimum(np.clip(t / 2.5, 0, 1), np.clip((duration - t) / 3.0, 0, 1))
    audio *= fade
    left = (audio * 0.92).astype(np.float32)
    right = (audio * 1.00).astype(np.float32)
    stereo = np.column_stack([left, right])
    pcm = np.clip(stereo * 32767, -32768, 32767).astype(np.int16)
    with wave.open(str(path), "wb") as out:
        out.setnchannels(2)
        out.setsampwidth(2)
        out.setframerate(sample_rate)
        out.writeframes(pcm.tobytes())


def run(command: list[str]) -> None:
    result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if result.returncode:
        raise RuntimeError(result.stderr[-3000:] if result.stderr else "ffmpeg failed")


def main() -> None:
    for folder in (MEDIA, BUILD):
        if folder.exists():
            shutil.rmtree(folder)
        folder.mkdir(parents=True)
    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    scenes = build_scenes()
    for scene in scenes:
        scene.image.save(BUILD / f"{scene.name}.png", quality=95)
    if "--stills-only" in sys.argv:
        print(f"stills={BUILD}")
        return

    segments: list[Path] = []
    for index, scene in enumerate(scenes):
        src = BUILD / f"{scene.name}.png"
        dst = BUILD / f"{scene.name}.mp4"
        frames = max(2, int(scene.duration * FPS))
        ease = f"(0.5-0.5*cos(PI*on/{frames}))"
        if index % 3 == 0:
            zoom_expr = f"1.000+0.045*{ease}"
        elif index % 3 == 1:
            zoom_expr = f"1.045-0.045*{ease}"
        else:
            zoom_expr = f"1.015+0.020*sin(PI*on/{frames})"
        pan_x = 7 if index % 2 == 0 else -7
        pan_y = -4 if index % 2 == 0 else 4
        vf = (
            "scale=5760:3600:flags=lanczos,"
            "zoompan="
            f"z='{zoom_expr}':"
            f"x='iw/2-(iw/zoom/2)+{pan_x}*({ease}-0.5)':"
            f"y='ih/2-(ih/zoom/2)+{pan_y}*({ease}-0.5)':"
            f"d=1:s={W}x{H}:fps={FPS},"
            "setsar=1,format=yuv420p"
        )
        run([
            ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
            "-loop", "1", "-framerate", str(FPS), "-i", str(src),
            "-t", f"{scene.duration:.3f}", "-vf", vf,
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "16",
            "-r", str(FPS), "-pix_fmt", "yuv420p", str(dst),
        ])
        segments.append(dst)

    total = len(scenes) * 0 + sum(s.duration for s in scenes) - OVERLAP * (len(scenes) - 1)
    audio = BUILD / "soundtrack.wav"
    soundtrack(audio, total)

    inputs: list[str] = []
    for seg in segments:
        inputs.extend(["-i", str(seg)])
    inputs.extend(["-i", str(audio)])
    filters: list[str] = []
    current = segments[0].stem
    elapsed = scenes[0].duration
    for i in range(1, len(scenes)):
        prev = f"v{i - 1}" if i > 1 else "0:v"
        out = f"v{i}"
        offset = elapsed - OVERLAP
        filters.append(f"[{prev}][{i}:v]xfade=transition=fade:duration={OVERLAP:.2f}:offset={offset:.3f}[{out}]")
        elapsed += scenes[i].duration - OVERLAP
    filters.append(
        f"[v{len(scenes) - 1}]"
        f"fade=t=in:st=0:d=0.7,fade=t=out:st={max(0, total - 1.0):.3f}:d=1.0,"
        "format=yuv420p,scale=1280:800:flags=lanczos[vout]"
    )
    filters.append(f"[{len(scenes)}:a]afade=t=in:st=0:d=2.0,afade=t=out:st={max(0,total-3):.3f}:d=3.0[aout]")
    out = MEDIA / "heytalk-demo.mp4"
    run([
        ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
        *inputs,
        "-filter_complex_threads", "1",
        "-filter_complex", ";".join(filters),
        "-map", "[vout]", "-map", "[aout]",
        "-c:v", "libx264", "-preset", "medium", "-crf", "25",
        "-threads", "2", "-x264-params", "threads=2:lookahead_threads=1:sliced_threads=1",
        "-max_muxing_queue_size", "1024",
        "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart",
        "-shortest", str(out),
    ])

    actual = write_vtt(scenes)
    poster = scenes[0].image.copy()
    pd = ImageDraw.Draw(poster)
    cx, cy, r = W // 2, H // 2, 58
    pd.ellipse((cx - r, cy - r, cx + r, cy + r), fill=(11, 31, 58, 210), outline=(255, 255, 255, 210), width=2)
    pd.polygon([(cx - 14, cy - 24), (cx - 14, cy + 24), (cx + 28, cy)], fill=WHITE)
    pd.text((70, H - 74), "HeyTalk · full walkthrough", font=font(21, "bold"), fill=WHITE)
    poster.save(ASSETS / "video-poster.png")
    print(f"video={out}")
    print(f"duration={actual:.2f}s vtt={MEDIA / 'heytalk-demo.zh.vtt'}")


if __name__ == "__main__":
    main()
