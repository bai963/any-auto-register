"""测试期的数据库隔离。

``core.db`` 在 import 时就按 ``DATABASE_URL`` 建好 engine，所以这里必须在任何
测试模块 import 之前改环境变量 —— conftest 是 pytest 唯一保证先于测试模块执行
的入口。跑测试不再往仓库根目录写 ``account_manager.db``，也不会读到上一次跑
留下的脏数据。

建表同样放在这里：生产环境由 ``main.py`` 启动时调 ``init_db()``，测试里没人调，
不建表的话所有落库路径都会撞上 ``no such table``。``configs`` 表定义在
``core.config_store`` 里，不 import 它就不会进 ``SQLModel.metadata``。
"""

import atexit
import os

# pytest 会在解析参数前加载 conftest。Windows 受控环境中，临时目录下新建
# 子目录可能没有 SQLite 的可写 ACL；测试库因此直接放在项目 data/（该目录已
# 被项目用于 SQLite WAL），文件名带 PID，避免并发 pytest 互相覆盖。
_TEST_WORK_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "data"))
os.makedirs(_TEST_WORK_DIR, exist_ok=True)
_TEST_DB_FILE = os.path.join(_TEST_WORK_DIR, f".pytest-{os.getpid()}.db")
for suffix in ("", "-wal", "-shm"):
    try:
        os.remove(_TEST_DB_FILE + suffix)
    except FileNotFoundError:
        pass
# SQLAlchemy 的 sqlite URL 需要正斜杠路径。
os.environ["DATABASE_URL"] = f"sqlite:///{_TEST_DB_FILE.replace(chr(92), '/')}"

import core.config_store  # noqa: E402,F401  注册 configs 表
from core.db import init_db  # noqa: E402  必须在 DATABASE_URL 设好之后再 import

init_db()


def _cleanup_test_database() -> None:
    # dispose 必须发生在删文件之前；Windows 不允许删除仍被 SQLite 打开的文件。
    try:
        from core.db import engine
        engine.dispose()
    except Exception:
        pass
    try:
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(_TEST_DB_FILE + suffix)
            except FileNotFoundError:
                pass
    except Exception:
        pass


atexit.register(_cleanup_test_database)


def pytest_sessionfinish(session, exitstatus):
    _cleanup_test_database()
