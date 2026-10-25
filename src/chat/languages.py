# -*- coding: utf-8 -*-
"""
src/chat/languages.py —— 语言注册表（全球覆盖）
================================================================

为什么单独一个文件
--------------------------------------------------------------------------
之前只有 16 个语言代码（`LANG_NAME_TO_CODE`），这就是"只局限在某几个语言"
的根因。要做到"世界上有多少种语言就翻多少种"，前提是**先有一份像样的
语言清单**：代码、中文名、英文名、当地写法都要有，否则用户写 "Nederlands"
或 "فارسی" 我们就认不出来。

覆盖范围
--------------------------------------------------------------------------
Google Cloud Translation 支持 130+ 种语言；这里收录了**全球主要语言与
常用语言约 90 种**（ISO 639-1/639-2/BCP-47 代码），足以覆盖现实中会遇到
的绝大多数请求。没有收录全部 7000+ 种语言，原因很实在：
    - 翻译引擎本身也不支持那么多；
    - 语言名写错比"不认识"更糟，宁可明确说不认识。

用法
--------------------------------------------------------------------------
    resolve("日语")        -> "ja"
    resolve("Japanese")    -> "ja"
    resolve("日本語")      -> "ja"
    resolve("JA")          -> "ja"
    resolve("Nederlands")  -> "nl"
    resolve("克林贡语")     -> None   （不认识就明确返回 None，不猜）
"""
from __future__ import annotations

import re

__all__ = [
    "LANGUAGES",
    "COMMON_ALIASES",
    "language_count",
    "resolve",
    "name_of",
    "supported_names",
    "detect_script",
    "guess_source_language",
]

# 代码 -> (中文名, 英文名, 当地写法或别名)
# 说明：别名里放常见的自称呼写法，让用户可以直接贴当地语言的名字。
LANGUAGES: dict[str, tuple[str, str, str]] = {
    # ---- 东亚 ----
    "zh": ("中文", "Chinese", "中文/汉语/普通话/简体/繁體"),
    "zh-TW": ("繁体中文", "Chinese (Traditional)", "繁體中文/繁体"),
    "ja": ("日语", "Japanese", "日本語/にほんご"),
    "ko": ("韩语", "Korean", "한국어/조선말"),
    "yue": ("粤语", "Cantonese", "粵語/广东话"),
    "mn": ("蒙古语", "Mongolian", "Монгол/蒙古文"),
    "bo": ("藏语", "Tibetan", "བོད་སྐད་"),
    "ug": ("维吾尔语", "Uyghur", "ئۇيغۇرچە"),
    # ---- 东南亚 ----
    "th": ("泰语", "Thai", "ไทย"),
    "vi": ("越南语", "Vietnamese", "Tiếng Việt"),
    "id": ("印尼语", "Indonesian", "Bahasa Indonesia"),
    "ms": ("马来语", "Malay", "Bahasa Melayu"),
    "tl": ("菲律宾语", "Filipino", "Tagalog/Filipino"),
    "km": ("高棉语", "Khmer", "ខ្មែរ"),
    "lo": ("老挝语", "Lao", "ລາວ"),
    "my": ("缅甸语", "Burmese", "မြန်မာ"),
    "jv": ("爪哇语", "Javanese", "Basa Jawa"),
    "su": ("巽他语", "Sundanese", "Basa Sunda"),
    # ---- 南亚 ----
    "hi": ("印地语", "Hindi", "हिन्दी"),
    "bn": ("孟加拉语", "Bengali", "বাংলা"),
    "ur": ("乌尔都语", "Urdu", "اردو"),
    "ta": ("泰米尔语", "Tamil", "தமிழ்"),
    "te": ("泰卢固语", "Telugu", "తెలుగు"),
    "mr": ("马拉地语", "Marathi", "मराठी"),
    "gu": ("古吉拉特语", "Gujarati", "ગુજરાતી"),
    "kn": ("卡纳达语", "Kannada", "ಕನ್ನಡ"),
    "ml": ("马拉雅拉姆语", "Malayalam", "മലയാളം"),
    "pa": ("旁遮普语", "Punjabi", "ਪੰਜਾਬੀ"),
    "si": ("僧伽罗语", "Sinhala", "සිංහල"),
    "ne": ("尼泊尔语", "Nepali", "नेपाली"),
    "sd": ("信德语", "Sindhi", "سنڌي"),
    # ---- 中亚 / 西亚 ----
    "fa": ("波斯语", "Persian", "فارسی"),
    "tr": ("土耳其语", "Turkish", "Türkçe"),
    "az": ("阿塞拜疆语", "Azerbaijani", "Azərbaycan dili"),
    "kk": ("哈萨克语", "Kazakh", "Қазақ тілі"),
    "ky": ("吉尔吉斯语", "Kyrgyz", "Кыргызча"),
    "uz": ("乌兹别克语", "Uzbek", "Oʻzbekcha"),
    "tk": ("土库曼语", "Turkmen", "Türkmençe"),
    "tg": ("塔吉克语", "Tajik", "Тоҷикӣ"),
    "hy": ("亚美尼亚语", "Armenian", "Հայերեն"),
    "ka": ("格鲁吉亚语", "Georgian", "ქართული"),
    "he": ("希伯来语", "Hebrew", "עברית"),
    "ar": ("阿拉伯语", "Arabic", "العربية"),
    "ku": ("库尔德语", "Kurdish", "Kurdî"),
    "ps": ("普什图语", "Pashto", "پښتو"),
    # ---- 欧洲 ----
    "en": ("英语", "English", "English"),
    "fr": ("法语", "French", "Français"),
    "de": ("德语", "German", "Deutsch"),
    "es": ("西班牙语", "Spanish", "Español"),
    "pt": ("葡萄牙语", "Portuguese", "Português"),
    "it": ("意大利语", "Italian", "Italiano"),
    "nl": ("荷兰语", "Dutch", "Nederlands"),
    "sv": ("瑞典语", "Swedish", "Svenska"),
    "no": ("挪威语", "Norwegian", "Norsk"),
    "da": ("丹麦语", "Danish", "Dansk"),
    "fi": ("芬兰语", "Finnish", "Suomi"),
    "is": ("冰岛语", "Icelandic", "Íslenska"),
    "pl": ("波兰语", "Polish", "Polski"),
    "cs": ("捷克语", "Czech", "Čeština"),
    "sk": ("斯洛伐克语", "Slovak", "Slovenčina"),
    "hu": ("匈牙利语", "Hungarian", "Magyar"),
    "ro": ("罗马尼亚语", "Romanian", "Română"),
    "bg": ("保加利亚语", "Bulgarian", "Български"),
    "el": ("希腊语", "Greek", "Ελληνικά"),
    "ru": ("俄语", "Russian", "Русский"),
    "uk": ("乌克兰语", "Ukrainian", "Українська"),
    "be": ("白俄罗斯语", "Belarusian", "Беларуская"),
    "sr": ("塞尔维亚语", "Serbian", "Српски"),
    "hr": ("克罗地亚语", "Croatian", "Hrvatski"),
    "bs": ("波斯尼亚语", "Bosnian", "Bosanski"),
    "sl": ("斯洛文尼亚语", "Slovenian", "Slovenščina"),
    "mk": ("马其顿语", "Macedonian", "Македонски"),
    "sq": ("阿尔巴尼亚语", "Albanian", "Shqip"),
    "lt": ("立陶宛语", "Lithuanian", "Lietuvių"),
    "lv": ("拉脱维亚语", "Latvian", "Latviešu"),
    "et": ("爱沙尼亚语", "Estonian", "Eesti"),
    "ga": ("爱尔兰语", "Irish", "Gaeilge"),
    "cy": ("威尔士语", "Welsh", "Cymraeg"),
    "gd": ("苏格兰盖尔语", "Scots Gaelic", "Gàidhlig"),
    "ca": ("加泰罗尼亚语", "Catalan", "Català"),
    "gl": ("加利西亚语", "Galician", "Galego"),
    "eu": ("巴斯克语", "Basque", "Euskara"),
    "mt": ("马耳他语", "Maltese", "Malti"),
    "yi": ("意第绪语", "Yiddish", "ייִדיש"),
    # ---- 非洲 ----
    "sw": ("斯瓦希里语", "Swahili", "Kiswahili"),
    "am": ("阿姆哈拉语", "Amharic", "አማርኛ"),
    "so": ("索马里语", "Somali", "Soomaali"),
    "yo": ("约鲁巴语", "Yoruba", "Yorùbá"),
    "ig": ("伊博语", "Igbo", "Igbo"),
    "ha": ("豪萨语", "Hausa", "Hausa"),
    "zu": ("祖鲁语", "Zulu", "isiZulu"),
    "xh": ("科萨语", "Xhosa", "isiXhosa"),
    "af": ("南非荷兰语", "Afrikaans", "Afrikaans"),
    "st": ("塞索托语", "Sesotho", "Sesotho"),
    "sn": ("绍纳语", "Shona", "chiShona"),
    "rw": ("卢旺达语", "Kinyarwanda", "Ikinyarwanda"),
    "mg": ("马达加斯加语", "Malagasy", "Malagasy"),
    # ---- 美洲原住民 / 其他 ----
    "qu": ("克丘亚语", "Quechua", "Runa Simi"),
    "gn": ("瓜拉尼语", "Guarani", "Avañe'ẽ"),
    "ht": ("海地克里奥尔语", "Haitian Creole", "Kreyòl ayisyen"),
    "la": ("拉丁语", "Latin", "Latina"),
    "eo": ("世界语", "Esperanto", "Esperanto"),
    "haw": ("夏威夷语", "Hawaiian", "ʻŌlelo Hawaiʻi"),
    "mi": ("毛利语", "Maori", "Te Reo Māori"),
    "sm": ("萨摩亚语", "Samoan", "Gagana Sāmoa"),
    "fj": ("斐济语", "Fijian", "Na Vosa Vakaviti"),
    "to": ("汤加语", "Tongan", "Lea faka-Tonga"),
}

# 常见口语/别称 -> 标准代码（用户怎么写都能认出来）
COMMON_ALIASES: dict[str, str] = {
    "中文": "zh", "汉语": "zh", "普通话": "zh", "简体": "zh", "简体中文": "zh",
    "繁体": "zh-TW", "繁体中文": "zh-TW", "台湾": "zh-TW", "香港": "zh-TW",
    "chinese": "zh", "mandarin": "zh", "simplified chinese": "zh",
    "traditional chinese": "zh-TW", "cantonese": "yue", "广东话": "yue",
    "粤语": "yue", "粵語": "yue",
    "日文": "ja", "日語": "ja", "霓虹语": "ja", "jp": "ja", "jpn": "ja",
    "韩文": "ko", "朝鲜语": "ko", "kr": "ko", "kor": "ko",
    "英文": "en", "英语": "en", "english": "en", "us": "en", "uk": "en",
    "美式英语": "en", "英式英语": "en",
    "俄文": "ru", "russian": "ru", "русский": "ru",
    "法文": "fr", "français": "fr", "francais": "fr",
    "德文": "de", "deutsch": "de",
    "西班牙文": "es", "español": "es", "espanol": "es", "castellano": "es",
    "葡萄牙文": "pt", "português": "pt", "portugues": "pt", "巴西": "pt",
    "意大利文": "it", "italiano": "it",
    "阿拉伯文": "ar", "arabic": "ar", "العربية": "ar",
    "希伯来文": "he", "hebrew": "he", "עברית": "he",
    "波斯文": "fa", "farsi": "fa", "فارسی": "fa",
    "土耳其文": "tr", "türkçe": "tr", "turkce": "tr",
    "泰文": "th", "ไทย": "th",
    "越南文": "vi", "tiếng việt": "vi", "tieng viet": "vi",
    "印尼文": "id", "bahasa": "id", "bahasa indonesia": "id",
    "马来文": "ms", "melayu": "ms",
    "印地文": "hi", "हिन्दी": "hi", "hindi": "hi",
    "孟加拉文": "bn", "bengali": "bn",
    "乌尔都文": "ur", "urdu": "ur",
    "荷兰文": "nl", "dutch": "nl", "nederlands": "nl",
    "瑞典文": "sv", "svenska": "sv",
    "挪威文": "no", "norsk": "no",
    "丹麦文": "da", "dansk": "da",
    "芬兰文": "fi", "suomi": "fi",
    "波兰文": "pl", "polski": "pl",
    "捷克文": "cs", "čeština": "cs", "cestina": "cs",
    "希腊文": "el", "ελληνικά": "el", "greek": "el",
    "乌克兰文": "uk", "українська": "uk", "ukrainian": "uk",
    "塞尔维亚文": "sr", "српски": "sr",
    "克罗地亚文": "hr", "hrvatski": "hr",
    "罗马尼亚文": "ro", "română": "ro", "romana": "ro",
    "匈牙利文": "hu", "magyar": "hu",
    "保加利亚文": "bg", "български": "bg",
    "斯瓦希里文": "sw", "kiswahili": "sw",
    "南非荷兰文": "af", "afrikaans": "af",
    "拉丁文": "la", "latin": "la",
    "世界语": "eo", "esperanto": "eo",
    "粤語": "yue", "蒙古文": "mn", "藏文": "bo", "维吾尔文": "ug",
}


# 合法语言代码（ISO 639 / BCP-47 形态）：2–3 位语言码，可选 2–4 位地区码。
# 注册表只是**提示**：没有收录的代码（如 mai / bho）也放行给引擎去试，
# 否则等于在入口替引擎做了它没做的判断。
_LANGUAGE_CODE_RE = re.compile(r"^[A-Za-z]{2,3}(?:-[A-Za-z]{2,4})?$")


def language_count() -> int:
    """收录的语言数量（含中文简繁两种写法）。"""
    return len(LANGUAGES)


def _normalize_key(name: str) -> str:
    """统一查表用的键：去空白、转小写、去常见分隔符。"""
    return " ".join(str(name or "").strip().lower().replace("_", " ").split())


def resolve(name: object) -> str | None:
    """把任意写法解析成语言代码；不认识返回 None（**不猜**）。

    识别顺序：
        1. 标准代码本身（ja / zh-TW / yue）
        2. 常见别称表（中文名、英文名、当地写法）
        3. LANGUAGES 里的中文名 / 英文名 / 当地写法（含斜杠分隔的多个别名）
    """
    if name is None:
        return None
    raw = str(name).strip()
    if not raw:
        return None
    key = _normalize_key(raw)

    # 1) 代码本身（保留大小写敏感的部分，如 zh-TW）
    for code in LANGUAGES:
        if _normalize_key(code) == key:
            return code

    # 2) 别称表
    if key in COMMON_ALIASES:
        return COMMON_ALIASES[key]

    # 3) 中文名 / 英文名 / 别名（别名可能是 "A/B/C" 形式）
    for code, (cn, en, alias) in LANGUAGES.items():
        if key == _normalize_key(cn) or key == _normalize_key(en):
            return code
        for piece in str(alias).split("/"):
            if key == _normalize_key(piece):
                return code

    # 4) 合法代码放行：注册表没收录不等于引擎不支持
    if _LANGUAGE_CODE_RE.fullmatch(raw):
        head, _, tail = raw.partition("-")
        return f"{head.lower()}-{tail.upper()}" if tail else head.lower()
    return None


def name_of(code: str, *, style: str = "cn") -> str:
    """取语言名字；未知代码原样返回（便于告诉用户"你给的是这个"）。"""
    entry = LANGUAGES.get(str(code or ""))
    if not entry:
        return str(code or "")
    index = 0 if style == "cn" else 1
    return entry[index]


def supported_names(*, limit: int = 0, style: str = "cn") -> list[str]:
    """列出支持的语言名（给界面/提示用）。"""
    names = [entry[0 if style == "cn" else 1] for entry in LANGUAGES.values()]
    return names[:limit] if limit else names


# 常用文字区间的粗略范围，用于判断源语言（不追求精确，够用即可）
_SCRIPT_RANGES: tuple[tuple[str, tuple[tuple[int, int], ...]], ...] = (
    ("Han", ((0x4E00, 0x9FFF), (0x3400, 0x4DBF))),
    ("Hiragana", ((0x3040, 0x309F),)),
    ("Katakana", ((0x30A0, 0x30FF),)),
    ("Hangul", ((0xAC00, 0xD7AF), (0x1100, 0x11FF))),
    ("Arabic", ((0x0600, 0x06FF), (0x0750, 0x077F), (0xFB50, 0xFDFF))),
    ("Hebrew", ((0x0590, 0x05FF),)),
    ("Cyrillic", ((0x0400, 0x04FF),)),
    ("Greek", ((0x0370, 0x03FF),)),
    ("Thai", ((0x0E00, 0x0E7F),)),
    ("Devanagari", ((0x0900, 0x097F),)),
    ("Bengali", ((0x0980, 0x09FF),)),
    ("Tamil", ((0x0B80, 0x0BFF),)),
    ("Telugu", ((0x0C00, 0x0C7F),)),
    ("Kannada", ((0x0C80, 0x0CFF),)),
    ("Malayalam", ((0x0D00, 0x0D7F),)),
    ("Gujarati", ((0x0A80, 0x0AFF),)),
    ("Gurmukhi", ((0x0A00, 0x0A7F),)),
    ("Sinhala", ((0x0D80, 0x0DFF),)),
    ("Myanmar", ((0x1000, 0x109F),)),
    ("Khmer", ((0x1780, 0x17FF),)),
    ("Lao", ((0x0E80, 0x0EFF),)),
    ("Ethiopic", ((0x1200, 0x137F),)),
    ("Georgian", ((0x10A0, 0x10FF),)),
    ("Armenian", ((0x0530, 0x058F),)),
    ("Latin", ((0x0041, 0x005A), (0x0061, 0x007A),
               (0x00C0, 0x024F), (0x1E00, 0x1EFF))),
)


def detect_script(text: str) -> str | None:
    """判断文本主要用什么书写系统（返回区间名，如 "Han"/"Cyrillic"）。"""
    counts: dict[str, int] = {}
    for char in str(text or ""):
        point = ord(char)
        for name, ranges in _SCRIPT_RANGES:
            if any(lo <= point <= hi for lo, hi in ranges):
                counts[name] = counts.get(name, 0) + 1
                break
    if not counts:
        return None
    return max(counts.items(), key=lambda item: item[1])[0]


# 书写系统 -> 最可能的源语言（日文里混着汉字，所以要先查假名）
_SCRIPT_TO_LANG: dict[str, str] = {
    "Hiragana": "ja", "Katakana": "ja",
    "Hangul": "ko", "Han": "zh", "Cyrillic": "ru", "Greek": "el",
    "Thai": "th", "Arabic": "ar", "Hebrew": "he", "Devanagari": "hi",
    "Bengali": "bn", "Tamil": "ta", "Telugu": "te", "Kannada": "kn",
    "Malayalam": "ml", "Gujarati": "gu", "Gurmukhi": "pa", "Sinhala": "si",
    "Myanmar": "my", "Khmer": "km", "Lao": "lo", "Ethiopic": "am",
    "Georgian": "ka", "Armenian": "hy", "Latin": "en",
}

# 平假名 / 片假名：日语专用的"决定性证据"
_KANA_SCRIPTS = ("Hiragana", "Katakana")


def guess_source_language(text: str, *, default: str = "en") -> str:
    """猜源语言。

    比原来的"含中文就是 zh，否则 en"准确得多：韩文/日文/阿拉伯文/泰文
    都能认出来，不会一律当成英语。

    关键规则（实跑踩过才加的）：**只要出现假名，就是日语**。
    原因：日语文本常以汉字为主、只夹少量假名，而"按主导书写系统判断"
    会把「私は学生です」这种句子判成中文（汉字 3 个 > 假名 3 个里取到的多数）。
    假名是日语独有的，中文文本不会出现 —— 所以它是比"数量占比"更可靠的证据。
    """
    body = str(text or "")
    scripts = _script_counts(body)
    if not scripts:
        return default

    for kana in _KANA_SCRIPTS:
        if scripts.get(kana):
            return "ja"

    dominant = max(scripts.items(), key=lambda item: item[1])[0]
    return _SCRIPT_TO_LANG.get(dominant, default)


def _script_counts(text: str) -> dict[str, int]:
    """统计各书写系统的字符数。"""
    counts: dict[str, int] = {}
    for char in str(text or ""):
        point = ord(char)
        for name, ranges in _SCRIPT_RANGES:
            if any(lo <= point <= hi for lo, hi in ranges):
                counts[name] = counts.get(name, 0) + 1
                break
    return counts
