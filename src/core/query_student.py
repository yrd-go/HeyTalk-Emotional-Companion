"""
query_student.py
使用 pymongo 从 MongoDB 的 agent_db.students 集合中查询指定 id 的学生，并打印其 name。

连接信息从环境变量 MONGO_URI 读取（默认 mongodb://localhost:27017/），严禁硬编码。

本地容错（重构后新增）：
    - 默认 STUDENT_ALLOW_MOCK=true：连不上 MongoDB 时不抛红色报错，改为返回一份
      结构化模拟数据，并在结尾附加 [mock] 标记，避免被误当成真实数据。
    - 设 STUDENT_ALLOW_MOCK=false 即进入「严格模式」：连不上就报错并以退出码 3 结束
      （未来配好真实 MongoDB Atlas 后建议切到严格模式）。

用法：
    python query_student.py                # 默认查询 id=2（人类可读输出）
    python query_student.py 3              # 查询 id=3
    python query_student.py 3 --json       # 只输出机器可解析的 JSON（供工具调用消费）
    python query_student.py 3 --no-mock    # 本次禁用 mock，连不上直接报错

退出码：
    0 = 查到真实数据（或确实没有该学生）
    1 = 使用了 mock 数据
    2 = 参数错误
    3 = 严格模式下连接/查询失败
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

from pymongo import MongoClient
from pymongo.errors import PyMongoError

# --------------------------------------------------------------------------
# 路径引导与配置加载：本文件位于 <root>/src/core/，先注入仓库根与 src/，
# 再由 paths.load_env() 加载「仓库根」.env / 云端 Streamlit Secrets，
# 从而保证 MONGO_URI 在任何工作目录、任何操作系统下都能被读到。
# --------------------------------------------------------------------------
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_SRC_DIR = Path(__file__).resolve().parents[1]
for _path in (str(_PROJECT_ROOT), str(_SRC_DIR)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from paths import load_env, mask_uri  # noqa: E402

load_env()

# 从环境变量读取连接串，缺省为本地 MongoDB
MONGO_URI = os.environ.get("MONGO_URI", "mongodb://localhost:27017/")
DB_NAME = os.environ.get("MONGO_DB", "agent_db")
COLLECTION_NAME = os.environ.get("MONGO_COLLECTION", "students")

# 未传参时默认查询的 id
DEFAULT_STUDENT_ID = 2

# 本地/云端都没有 MongoDB 时，是否允许降级为模拟数据（默认允许，保证开箱即用）
_TRUTHY = {"1", "true", "yes", "y", "on"}
_FALSY = {"0", "false", "no", "n", "off"}
STUDENT_ALLOW_MOCK = (
    os.environ.get("STUDENT_ALLOW_MOCK", "true").strip().lower() not in _FALSY
)

# 模拟数据：与真实数据保持同一份结构，便于上层（Agent 工具 / Streamlit）无差别消费
MOCK_STUDENTS = {
    1: "张三",
    2: "李四",
    3: "王五",
}
MOCK_NOTE = (
    "MongoDB 不可用，已返回模拟数据（status=mock）。"
    "如需真实数据：请配置 MONGO_URI（本地起 MongoDB 或在 Secrets 里填 Atlas 连接串）；"
    "如需严格模式、连不上就报错：设置 STUDENT_ALLOW_MOCK=false。"
)


def short_error(exc: Exception, limit: int = 120) -> str:
    """把冗长的 PyMongo 拓扑报错压成一行，避免淹没 [mock] 提示。"""
    text = " ".join(str(exc).split())                       # 合并换行与多余空格
    text = re.split(r"\s*\(configured timeouts", text)[0]    # 砍掉驱动内部细节
    text = re.split(r", Timeout: \d", text)[0]
    if not text:
        text = type(exc).__name__
    return text if len(text) <= limit else text[:limit] + " ..."


def mock_payload(student_id: int) -> dict:
    """构造与真实查询同结构的模拟结果（status=mock 便于上层区分）。"""
    return {
        "status": "mock",
        "id": student_id,
        "result": MOCK_STUDENTS.get(student_id, f"模拟学生{student_id}"),
        "name": MOCK_STUDENTS.get(student_id, f"模拟学生{student_id}"),
        "source": "mock",
        "note": MOCK_NOTE,
    }


def error_payload(student_id: int, message: str) -> dict:
    """严格模式下的失败结果（机器可解析）。"""
    return {
        "status": "error",
        "id": student_id,
        "result": None,
        "name": None,
        "source": "mongodb",
        "error": message,
    }


def allow_mock(force_off: bool = False) -> bool:
    """本次是否允许 mock：命令行 --no-mock 优先级高于环境变量。"""
    return False if force_off else STUDENT_ALLOW_MOCK


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="按 id 查询 MongoDB 中的学生姓名（支持连接失败时返回模拟数据）",
    )
    parser.add_argument("student_id", nargs="?", type=int, default=DEFAULT_STUDENT_ID,
                        help=f"学生 id，默认 {DEFAULT_STUDENT_ID}")
    parser.add_argument("--json", action="store_true",
                        help="只输出 JSON（供 Agent 工具等程序消费）")
    parser.add_argument("--no-mock", action="store_true",
                        help="本次禁用模拟数据：连不上 MongoDB 直接报错")
    return parser.parse_args(argv)


def query_student(student_id: int) -> dict:
    """查询真实数据。

    返回 {"status": "ok"|"not_found", "id":.., "result":..}；
    连接或查询失败则抛出 PyMongoError，由调用方决定是否降级为 mock。
    """
    client = None
    try:
        # serverSelectionTimeoutMS 让连不上时快速失败，而不是长时间挂起
        client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000)
        client.admin.command("ping")

        collection = client[DB_NAME][COLLECTION_NAME]
        # 使用文档查询（字典条件），由驱动负责转义，避免注入
        doc = collection.find_one({"id": student_id}, {"_id": 0, "name": 1})

        if doc is None:
            return {
                "status": "not_found",
                "id": student_id,
                "result": None,
                "name": None,
                "source": f"{DB_NAME}.{COLLECTION_NAME}",
                "message": f"未找到 id 为 {student_id} 的学生。",
            }
        name = doc.get("name")
        return {
            "status": "ok",
            "id": student_id,
            "result": name,
            "name": name,
            "source": f"{DB_NAME}.{COLLECTION_NAME}",
        }
    finally:
        # 确保连接被关闭，释放资源
        if client is not None:
            client.close()


def main(argv=None) -> int:
    args = parse_args(argv)
    student_id = args.student_id

    try:
        payload = query_student(student_id)
    except PyMongoError as exc:
        if allow_mock(args.no_mock):
            payload = mock_payload(student_id)
            payload["error"] = f"MongoDB 不可用：{short_error(exc)}"
        else:
            payload = error_payload(student_id, f"MongoDB 操作出错：{short_error(exc)}")
            if args.json:
                print(json.dumps(payload, ensure_ascii=False))
            else:
                print(f"MongoDB 操作出错：{short_error(exc)}")
                print("提示：如需在无 MongoDB 的环境下继续演示，"
                      "请设置 STUDENT_ALLOW_MOCK=true（或不加 --no-mock）。")
            return 3
    except Exception as exc:  # noqa: BLE001  兜底捕获其他未知异常
        if allow_mock(args.no_mock):
            payload = mock_payload(student_id)
            payload["error"] = f"未知异常：{short_error(exc)}"
        else:
            payload = error_payload(student_id, f"发生未知错误：{short_error(exc)}")
            if args.json:
                print(json.dumps(payload, ensure_ascii=False))
            else:
                print(f"发生未知错误：{short_error(exc)}")
            return 3

    # ---- 输出 ----
    if args.json:
        print(json.dumps(payload, ensure_ascii=False))
        return 1 if payload["status"] == "mock" else 0

    if payload["status"] == "mock":
        # 只打印打码后的连接串：Atlas 连接串里带账号密码，而本脚本的输出会被
        # Streamlit 页面原样渲染到公网。裸打印 MONGO_URI 等于把密码贴到网页上。
        print(f"[mock] 未能连接 MongoDB（{mask_uri(MONGO_URI)}），已返回模拟数据。")
        print(f"[mock] 数据来源：内置模拟表（status=mock），不是真实数据库结果。")
        print(f"[mock] 修复方式：配置 MONGO_URI，或设置 STUDENT_ALLOW_MOCK=false 走严格模式。")
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 1

    if payload["status"] == "not_found":
        print(f"未找到 id 为 {student_id} 的学生。")
        print(json.dumps(payload, ensure_ascii=False))
        return 0

    print(f"id 为 {student_id} 的学生名字是：{payload['result']}")
    print(json.dumps(payload, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
