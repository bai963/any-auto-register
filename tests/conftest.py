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
import shutil
import tempfile

# pytest 会在解析参数前加载 conftest。若参数非法，pytest_sessionfinish 不会执行；
# TemporaryDirectory 的 weakref 清理会在仍持有 SQLite 连接时抛 WinError 32。
# 这里显式 dispose 并 best-effort 清理，测试临时目录删不掉也不让解释器退出时报错。
_TMP_DB_DIR = tempfile.mkdtemp(prefix="any-auto-register-pytest-")
os.environ["DATABASE_URL"] = f"sqlite:///{os.path.join(_TMP_DB_DIR, 'test.db')}"

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
        shutil.rmtree(_TMP_DB_DIR, ignore_errors=True)
    except Exception:
        pass


atexit.register(_cleanup_test_database)


def pytest_sessionfinish(session, exitstatus):
    _cleanup_test_database()
