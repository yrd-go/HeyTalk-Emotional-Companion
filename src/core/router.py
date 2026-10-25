"""
router.py
多模型路由脚本：根据自然语言指令中的关键词，把任务路由到不同的后端，以降低企业成本
（简单任务走本地/免费接口，复杂任务才走大模型）。

用法：
    非翻译路由：
        python router.py "帮我查学生2"            # 本地查询脚本（零成本）
        python router.py "帮我查学生3"            # id 从指令中提取
        python router.py "帮我写一段代码"          # DeepSeek 大模型（有成本）

    翻译路由（支持多目标语言，未指定时自动判定方向）：
        python router.py "帮我翻译 你好"           # 未指定目标语言 -> 自动中译英 (zh -> en)
        python router.py "帮我翻译 apple"          # 未指定目标语言 -> 自动英译中 (en -> zh)
        python router.py "帮我翻译 你好 到日语"     # 中译日 (zh -> ja)
        python router.py "帮我翻译 你好 到俄语"     # 中译俄 (zh -> ru)
        python router.py "帮我翻译 你好 到韩语"     # 中译韩 (zh -> ko)
        python router.py "帮我翻译 你好 到英语"     # 中译英 (zh -> en)
        python router.py "帮我翻译 你好 to Japanese"  # 英文后缀写法同样支持

    目标语言后缀支持：「到/成/为 + 语言名」或「to + Language」，语言名可用
    日语/日文/俄语/俄文/韩语/韩文/英语/英文/中文/汉语 及对应英文单词。
"""
import json
import os
import re
import socket
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

# --------------------------------------------------------------------------
# 路径引导：本文件位于 <root>/src/core/，先动态把仓库根与 src/ 注入 sys.path，
# 再从 src/paths.py 取被调脚本的绝对路径 + 加载根目录 .env / 云端 Secrets。
# --------------------------------------------------------------------------
PROJECT_ROOT = Path(__file__).resolve().parents[2]   # 仓库根目录
SRC_DIR = Path(__file__).resolve().parents[1]        # src/ 目录
for _path in (str(PROJECT_ROOT), str(SRC_DIR)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from paths import QUERY_STUDENT_SCRIPT, load_env, subprocess_env  # noqa: E402

load_env()

# ---------------- 全局配置 ----------------
# 被调用的学生查询脚本绝对路径（原写法是「与本脚本同级」，重构后必须指向 src/core/）
STUDENT_SCRIPT = str(QUERY_STUDENT_SCRIPT)
DEFAULT_STUDENT_ID = 2
TIMEOUT = 15  # 秒：统一网络超时，防止请求挂死

TRANSLATE_URL = "https://api.mymemory.translated.net/get"
TRANSLATE_PREFIX = "帮我翻译"
CN_CHAR_RE = re.compile(r"[\u4e00-\u9fa5]")  # 中文字符检测

# 语言名 -> MyMemory 语言代码（英文名匹配时统一转小写）
LANG_NAME_TO_CODE = {
    "日语": "ja", "日文": "ja", "japanese": "ja",
    "俄语": "ru", "俄文": "ru", "russian": "ru",
    "韩语": "ko", "韩文": "ko", "korean": "ko",
    "英语": "en", "英文": "en", "english": "en",
    "中文": "zh", "汉语": "zh", "chinese": "zh",
}

# 目标语言后缀：「到日语」/「到俄语」/ ... 或「to Japanese」/「to Russian」/ ...
TRANSLATE_SUFFIX_RE = re.compile(
    r"(?:到|成|为)\s*(?P<cn>[\u4e00-\u9fa5]{2})|to\s+(?P<en>[A-Za-z]+)",
    re.IGNORECASE,
)

DEEPSEEK_URL = "https://api.deepseek.com/chat/completions"
DEEPSEEK_MODEL = "deepseek-chat"
# 仅用于"是否还是占位符"的判断，绝不作为真实 Key 使用
DEEPSEEK_PLACEHOLDER = "[你的API Key]"


def print_usage():
    print('用法：python router.py "<自然语言指令>"')
    print("示例：")
    print('  python router.py "帮我查学生2"     -> 本地查询脚本')
    print('  python router.py "帮我翻译 hello"  -> 免费翻译接口')
    print('  python router.py "帮我写一段代码"  -> DeepSeek 大模型')


# ---------------- 路由一：查学生（本地子进程，零成本） ----------------
def extract_student_id(text):
    """从指令中提取第一个数字（如「帮我查学生2」-> 2），没有数字时回退到默认值 2。"""
    match = re.search(r"\d+", text)
    return int(match.group()) if match else DEFAULT_STUDENT_ID


def route_student(instruction):
    student_id = extract_student_id(instruction)
    print(f"[路由] 学生查询 -> query_student.py {student_id}")

    cmd = [sys.executable, STUDENT_SCRIPT, str(student_id)]

    # 构造子进程环境：由 src/paths.py 统一注入 UTF-8 相关变量，并把 src/ 追加到
    # PYTHONPATH。Windows 中文环境下，子进程输出一旦被重定向到管道，默认按本地
    # GBK 编码写出，而父进程按 UTF-8 解码就会出现乱码；显式注入 PYTHONIOENCODING
    # 可让父子进程编码对齐，从根因上修复乱码（云端 Linux 本就是 UTF-8，无害）。
    child_env = subprocess_env()

    try:
        # 优先捕获子进程输出；受限沙箱下若禁止创建管道，则退化为继承 stdio
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=30,
                env=child_env,
            )
            stdout, stderr, returncode = result.stdout, result.stderr, result.returncode
        except OSError:
            result = subprocess.run(cmd, timeout=30, env=child_env)
            stdout, stderr, returncode = None, None, result.returncode

        if stdout:
            print(stdout.strip())
        if stderr:
            print(stderr.strip())
        if returncode != 0:
            print(f"（子脚本退出码：{returncode}）")

    except FileNotFoundError:
        print("错误：找不到 query_student.py 或 Python 解释器。")
    except subprocess.TimeoutExpired:
        print("错误：查询学生超时（30 秒）。")
    except subprocess.SubprocessError as e:
        print(f"错误：子进程执行失败：{e}")
    except Exception as e:
        print(f"错误：查询学生时发生未知异常：{e}")


# ---------------- 路由二：翻译（免费接口，零成本） ----------------
def parse_translate_instruction(instruction):
    """把「帮我翻译 你好 到日语」解析为 (query, target_lang)。

    target_lang 为 None 表示用户没有显式指定目标语言（走自适应模式）。
    """
    text = instruction.strip()

    # 1) 移除「帮我翻译」前缀
    if text.startswith(TRANSLATE_PREFIX):
        text = text[len(TRANSLATE_PREFIX):]

    # 2) 识别并移除目标语言后缀（取最后一个合法匹配，避免误判）
    target_lang, cut = None, None
    for m in TRANSLATE_SUFFIX_RE.finditer(text):
        name = (m.group("cn") or m.group("en") or "").lower()
        if name in LANG_NAME_TO_CODE:
            target_lang, cut = LANG_NAME_TO_CODE[name], m.start()
    if cut is not None:
        text = text[:cut]

    # 3) 剩下的部分就是待翻译文本
    return text.strip(" \t\r\n，,。.；;：:"), target_lang


def detect_source_lang(query):
    """含中文字符 -> zh，否则 -> en。"""
    return "zh" if CN_CHAR_RE.search(query) else "en"


def resolve_langpair(query, target_lang):
    """源语言按中文检测；未显式指定目标语言时走自适应模式。"""
    source_lang = detect_source_lang(query)
    if target_lang is None:
        target_lang = "en" if source_lang == "zh" else "zh"
    return source_lang, target_lang


def route_translate(instruction):
    print("[路由] 翻译 -> MyMemory 免费接口")

    # 1) 解析待翻译文本与目标语言
    query, target_lang = parse_translate_instruction(instruction)

    # 2) 边界处理：只有「帮我翻译」而没有内容
    if not query:
        print('提示：没有检测到待翻译内容。用法：python router.py "帮我翻译 你好 到日语"')
        return

    # 3) 源语言 + 目标语言 -> langpair
    source_lang, target_lang = resolve_langpair(query, target_lang)

    # 4) 同语言保护：源语言与目标语言一致时无需翻译
    if source_lang == target_lang:
        print(f"提示：源语言与目标语言都是 {source_lang}，无需翻译。")
        return

    langpair = source_lang + "|" + target_lang
    print(f"[解析] 待翻译：{query}  langpair={langpair}")

    params = urllib.parse.urlencode({"q": query, "langpair": langpair})
    url = f"{TRANSLATE_URL}?{params}"

    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            raw = resp.read().decode("utf-8", errors="replace")

        data = json.loads(raw)
        translated = (data.get("responseData") or {}).get("translatedText")
        if translated:
            # 5) 输出优化：显示具体翻译方向
            print(f"翻译结果 ({source_lang} -> {target_lang})：{translated}")
        else:
            print(f"未获取到翻译结果，原始响应片段：{raw[:300]}")

    except urllib.error.HTTPError as e:
        print(f"错误：翻译接口返回 HTTP {e.code} {e.reason}")
    except (socket.timeout, TimeoutError):
        print("错误：翻译请求超时，请稍后重试。")
    except urllib.error.URLError as e:
        print(f"错误：无法连接翻译接口（{e.reason}），请检查网络。")
    except json.JSONDecodeError as e:
        print(f"错误：翻译接口返回的不是合法 JSON：{e}")
    except Exception as e:
        print(f"错误：翻译时发生未知异常：{e}")


# ---------------- 路由三：代码生成（DeepSeek 大模型，有成本，按需调用） ----------------
def route_code(instruction):
    print("[路由] 代码生成 -> DeepSeek API")

    api_key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if not api_key or api_key == DEEPSEEK_PLACEHOLDER:
        print("提示：未检测到有效的 DEEPSEEK_API_KEY 环境变量，已优雅跳过 DeepSeek 调用。")
        print("      设置方式（PowerShell）：$env:DEEPSEEK_API_KEY = 'sk-xxxx'")
        return

    payload = {
        "model": DEEPSEEK_MODEL,
        "messages": [
            {"role": "user", "content": "请写一段冒泡排序的 Python 代码。"}
        ],
        "stream": False,
    }
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        DEEPSEEK_URL,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
    )

    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            raw = resp.read().decode("utf-8", errors="replace")

        result = json.loads(raw)
        content = result["choices"][0]["message"]["content"]
        print("DeepSeek 返回的代码：")
        print(content)

    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", errors="replace")[:300]
        except Exception:
            pass
        print(f"错误：DeepSeek 接口返回 HTTP {e.code} {e.reason}；响应：{detail}")
    except (socket.timeout, TimeoutError):
        print("错误：DeepSeek 请求超时，请稍后重试。")
    except urllib.error.URLError as e:
        print(f"错误：无法连接 DeepSeek（{e.reason}），请检查网络。")
    except json.JSONDecodeError as e:
        print(f"错误：DeepSeek 返回的不是合法 JSON：{e}")
    except (KeyError, IndexError, TypeError) as e:
        print(f"错误：DeepSeek 响应结构异常：{e}")
    except Exception as e:
        print(f"错误：调用 DeepSeek 时发生未知异常：{e}")


# ---------------- 关键词路由表（顺序即优先级） ----------------
ROUTES = (
    ("学生查询", ("查", "学生"), route_student),
    ("翻译", ("翻译",), route_translate),
    ("代码生成", ("代码",), route_code),
)


def main(argv):
    if len(argv) < 2 or not argv[1].strip():
        print("参数错误：缺少自然语言指令。")
        print_usage()
        return 2

    instruction = argv[1].strip()
    print(f"[指令] {instruction}")

    for name, keywords, handler in ROUTES:
        if any(kw in instruction for kw in keywords):
            print(f"[匹配] 命中「{name}」路由")
            handler(instruction)
            return 0

    print("未匹配到任何路由。支持的关键词：查/学生、翻译、代码。")
    print_usage()
    return 1


if __name__ == "__main__":
    # 管道 / 后台运行时 stdout 默认是块缓冲，会导致本脚本日志与子进程输出顺序错乱；
    # 这里改为行缓冲，保证输出顺序与执行顺序一致。
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except Exception:
        pass

    try:
        sys.exit(main(sys.argv))
    except KeyboardInterrupt:
        print("\n已中断。")
        sys.exit(130)
    except Exception as e:
        # 兜底：任何未预料的异常都不会让脚本直接崩掉
        print(f"致命错误：{e}")
        sys.exit(1)
