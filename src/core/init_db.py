"""
init_db.py
使用 pymongo 连接本地 MongoDB，创建数据库 agent_db 与集合 students，
并插入 3 条学生数据。

连接信息从环境变量 MONGO_URI 读取（默认 mongodb://localhost:27017/），
严禁硬编码。

用法：
    python init_db.py
    MONGO_URI="mongodb://user:pass@host:27017/" python init_db.py
"""
import os
import sys
from pathlib import Path

from pymongo import MongoClient
from pymongo.errors import PyMongoError

# --------------------------------------------------------------------------
# 路径引导与配置加载：本文件位于 <root>/src/core/，先注入仓库根与 src/，
# 再由 paths.load_env() 加载「仓库根」.env / 云端 Streamlit Secrets。
# 这样 MONGO_URI 等键无论从哪个工作目录启动、无论 Windows 还是 Linux 都能读到。
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

# 要写入的学生数据
STUDENTS = [
    {"id": 1, "name": "张三"},
    {"id": 2, "name": "李四"},
    {"id": 3, "name": "王五"},
]


def main():
    client = None
    try:
        # serverSelectionTimeoutMS 让连不上时快速失败，而不是长时间挂起
        client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000)
        # 主动探活，尽早暴露连接问题
        client.admin.command("ping")

        collection = client[DB_NAME][COLLECTION_NAME]

        # 保证可重复执行：先清空旧数据，再重新插入
        collection.delete_many({})

        # 为 id 建唯一索引，重复执行或并发写入都不会产生重复文档
        collection.create_index("id", unique=True)

        result = collection.insert_many(STUDENTS)

        count = collection.count_documents({})
        print(
            # Atlas 连接串含账号密码，且该输出会被 Streamlit 页面渲染到公网，必须打码
            f"已连接 MongoDB（{mask_uri(MONGO_URI)}），"
            f"数据库 {DB_NAME} 集合 {COLLECTION_NAME} "
            f"插入 {len(result.inserted_ids)} 条数据，当前共 {count} 条："
            "1 张三，2 李四，3 王五"
        )

    except PyMongoError as e:
        print(f"MongoDB 操作出错：{e}")
        sys.exit(1)
    except Exception as e:
        # 兜底捕获其他未知异常
        print(f"发生未知错误：{e}")
        sys.exit(1)
    finally:
        # 无论成功失败，都关闭连接释放资源
        if client is not None:
            client.close()


if __name__ == "__main__":
    main()
