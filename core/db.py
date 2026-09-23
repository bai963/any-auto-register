"""数据库模型 - SQLModel（支持 SQLite 与 PostgreSQL）"""
from datetime import datetime, timezone
import os
import secrets
from typing import Optional
from sqlmodel import Field, SQLModel, create_engine, Session, select
from sqlalchemy import event, Index
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError
from sqlalchemy.pool import QueuePool
from contextlib import contextmanager
import threading
import json


def _utcnow():
    return datetime.now(timezone.utc)


def new_alias_share_token() -> str:
    """128 位随机串，够长到不能枚举，又短到能塞进一行链接里。"""
    return secrets.token_urlsafe(16)

# SQLite 保持零配置默认值；生产环境可使用：
# postgresql+psycopg://USER:PASSWORD@HOST:5432/DBNAME
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///account_manager.db").strip()

try:
    _database_url = make_url(DATABASE_URL)
except ArgumentError as exc:
    raise RuntimeError("DATABASE_URL 格式无效，请使用 SQLite 或 PostgreSQL SQLAlchemy URL") from exc

_database_backend = _database_url.get_backend_name()
# 注册 worker 的硬上限随数据库的并发写入能力而定。不要仅依赖调用方的
# concurrency 参数，否则 SQLite 会被误配成 200 个并发写入者。
DATABASE_BACKEND = _database_backend
POSTGRES_REGISTER_CONCURRENCY = 200
SQLITE_REGISTER_CONCURRENCY = 40
DATABASE_REGISTER_CONCURRENCY_CAP = (
    POSTGRES_REGISTER_CONCURRENCY if _database_backend == "postgresql" else SQLITE_REGISTER_CONCURRENCY
)
if _database_backend not in {"sqlite", "postgresql"}:
    raise RuntimeError(
        f"不支持的数据库类型: {_database_backend}。当前仅支持 SQLite 和 PostgreSQL。"
    )

def _read_positive_int_env(name: str, default: int) -> int:
    try:
        return max(1, int(os.getenv(name, str(default))))
    except ValueError:
        return default


# PostgreSQL 是多连接服务，连接断开（数据库重启、网络抖动）后应自动探活重连。
# 200 个网络 worker 不需要 200 个数据库连接：写入时间远小于外部 HTTP 等待时间。
# SQLite 使用 WAL 允许读写并行，busy timeout 让短暂的写锁竞争自动等待。
_engine_options = (
    {
        "pool_pre_ping": True,
        "pool_size": _read_positive_int_env("DB_POOL_SIZE", 30),
        "max_overflow": _read_positive_int_env("DB_MAX_OVERFLOW", 30),
        "pool_timeout": _read_positive_int_env("DB_POOL_TIMEOUT", 10),
    }
    if _database_backend == "postgresql"
    else {
        "poolclass": QueuePool,
        "pool_size": _read_positive_int_env("SQLITE_DB_POOL_SIZE", 8),
        "max_overflow": 0,
        "pool_timeout": _read_positive_int_env("SQLITE_DB_POOL_TIMEOUT", 10),
        "connect_args": {"check_same_thread": False, "timeout": 30},
    }
)
engine = create_engine(DATABASE_URL, **_engine_options)


if _database_backend == "sqlite":
    @event.listens_for(engine, "connect")
    def _configure_sqlite_connection(dbapi_connection, _connection_record) -> None:
        """Reduce lock errors for the supported 40-worker SQLite mode."""
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA synchronous=NORMAL")
            cursor.execute("PRAGMA busy_timeout=30000")
            # WAL 文件过大时 checkpoint 会拖慢所有写者；该阈值适合本地 40 worker。
            cursor.execute("PRAGMA wal_autocheckpoint=1000")
            cursor.execute("PRAGMA journal_size_limit=67108864")
            cursor.execute("PRAGMA foreign_keys=ON")
        finally:
            cursor.close()


# SQLite allows concurrent readers in WAL mode but has exactly one writer.  This
# gate serializes *short* local write transactions only; never do network I/O
# while holding it. PostgreSQL keeps normal row-level write concurrency.
_sqlite_write_lock = threading.RLock()

@contextmanager
def database_write_session():
    if DATABASE_BACKEND == "sqlite":
        with _sqlite_write_lock:
            with Session(engine) as session:
                yield session
    else:
        with Session(engine) as session:
            yield session


class SmsActivationModel(SQLModel, table=True):
    """Durable SMS activation state shared by workers, watchdogs and children."""
    __tablename__ = "sms_activations"

    id: Optional[int] = Field(default=None, primary_key=True)
    provider_identity: str = Field(index=True, max_length=128)
    activation_id: str = Field(max_length=128)
    phone_number: str = Field(default="", max_length=64)
    country: str = Field(default="", max_length=32)
    state: str = Field(default="rented", index=True, max_length=32)
    rented_at: datetime = Field(default_factory=_utcnow, index=True)
    activation_deadline_at: datetime = Field(index=True)
    send_request_started_at: Optional[datetime] = Field(default=None)
    sms_sent_at: Optional[datetime] = Field(default=None)
    code_deadline_at: Optional[datetime] = Field(default=None, index=True)
    otp_received_at: Optional[datetime] = Field(default=None)
    otp_submitted_at: Optional[datetime] = Field(default=None)
    refund_requested_at: Optional[datetime] = Field(default=None)
    refunded_at: Optional[datetime] = Field(default=None)
    finished_at: Optional[datetime] = Field(default=None)
    version: int = Field(default=1, nullable=False)
    created_at: datetime = Field(default_factory=_utcnow, index=True)
    updated_at: datetime = Field(default_factory=_utcnow, index=True)


class SmsCleanupJobModel(SQLModel, table=True):
    __tablename__ = "sms_cleanup_jobs"

    id: Optional[int] = Field(default=None, primary_key=True)
    activation_db_id: int = Field(index=True)
    action: str = Field(max_length=16)
    status: str = Field(default="pending", index=True, max_length=16)
    attempts: int = Field(default=0)
    lease_owner: str = Field(default="", max_length=128)
    lease_until: Optional[datetime] = Field(default=None, index=True)
    next_retry_at: datetime = Field(default_factory=_utcnow, index=True)
    reason: str = Field(default="", max_length=300)
    last_error: str = Field(default="", max_length=500)
    created_at: datetime = Field(default_factory=_utcnow, index=True)
    updated_at: datetime = Field(default_factory=_utcnow, index=True)


Index("uq_sms_activation_provider_activation", SmsActivationModel.provider_identity, SmsActivationModel.activation_id, unique=True)
Index("ix_sms_activation_refund_scan", SmsActivationModel.state, SmsActivationModel.activation_deadline_at)
Index("ix_sms_activation_code_refund_scan", SmsActivationModel.state, SmsActivationModel.code_deadline_at)
Index("uq_sms_cleanup_job_activation_action", SmsCleanupJobModel.activation_db_id, SmsCleanupJobModel.action, unique=True)
Index("ix_sms_cleanup_job_claim", SmsCleanupJobModel.status, SmsCleanupJobModel.next_retry_at, SmsCleanupJobModel.lease_until)

class AccountModel(SQLModel, table=True):
    __tablename__ = "accounts"

    id: Optional[int] = Field(default=None, primary_key=True)
    platform: str = Field(index=True)
    email: str = Field(index=True)
    password: str
    user_id: str = ""
    region: str = ""
    token: str = ""
    status: str = "registered"
    trial_end_time: int = 0
    cashier_url: str = ""
    extra_json: str = "{}"   # JSON 存储平台自定义字段
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)

    def get_extra(self) -> dict:
        return json.loads(self.extra_json or "{}")

    def set_extra(self, d: dict):
        self.extra_json = json.dumps(d, ensure_ascii=False)


class TaskLog(SQLModel, table=True):
    __tablename__ = "task_logs"

    id: Optional[int] = Field(default=None, primary_key=True)
    platform: str
    email: str
    status: str        # success | failed
    error: str = ""
    detail_json: str = "{}"
    created_at: datetime = Field(default_factory=_utcnow)


class TaskRunModel(SQLModel, table=True):
    __tablename__ = "task_runs"

    id: str = Field(primary_key=True)
    platform: str = Field(index=True)
    source: str = Field(default="manual", index=True)
    status: str = Field(default="pending", index=True)
    total: int = 0
    progress: str = "0/0"
    success: int = 0
    registered: int = 0
    skipped: int = 0
    error: str = ""
    meta_json: str = "{}"
    logs_json: str = "[]"
    errors_json: str = "[]"
    cashier_urls_json: str = "[]"
    control_json: str = "{}"
    created_at: datetime = Field(default_factory=_utcnow, index=True)
    updated_at: datetime = Field(default_factory=_utcnow, index=True)


class OutlookAccountModel(SQLModel, table=True):
    __tablename__ = "outlook_accounts"

    id: Optional[int] = Field(default=None, primary_key=True)
    email: str = Field(index=True, sa_column_kwargs={"unique": True})
    password: str
    client_id: str = ""
    refresh_token: str = ""
    account_type: str = "microsoft_oauth"
    mailapi_url: str = ""
    enabled: bool = True
    status: str = Field(default="available", index=True)
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)
    last_used: Optional[datetime] = None


class ICloudAccountModel(SQLModel, table=True):
    """iCloud 主号。Web Session 与 IMAP 凭据统一以 AES-256-GCM 密文保存。"""

    __tablename__ = "icloud_accounts"

    id: Optional[int] = Field(default=None, primary_key=True)
    email: str = Field(index=True, sa_column_kwargs={"unique": True})
    display_name: str = ""
    region: str = "global"
    status: str = "active"
    enabled: bool = True
    credentials_cipher: str = ""
    sync_error: str = ""
    last_sync_at: Optional[datetime] = None
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)


class ICloudAliasModel(SQLModel, table=True):
    """从主号生成或同步得到的 Hide My Email 隐私邮箱。"""

    __tablename__ = "icloud_aliases"

    id: Optional[int] = Field(default=None, primary_key=True)
    account_id: int = Field(index=True, foreign_key="icloud_accounts.id")
    address: str = Field(index=True, sa_column_kwargs={"unique": True})
    label: str = ""
    note: str = ""
    status: str = "active"
    provider_id: str = ""
    # 免登录查看最新邮件的凭证，链接本身就是权限，所以必须是猜不出来的随机串
    share_token: str = Field(default_factory=lambda: new_alias_share_token(), index=True)
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)


class ProxyModel(SQLModel, table=True):
    __tablename__ = "proxies"

    id: Optional[int] = Field(default=None, primary_key=True)
    url: str = Field(unique=True)
    region: str = ""
    success_count: int = 0
    fail_count: int = 0
    is_active: bool = True
    last_checked: Optional[datetime] = None


def save_account(account) -> 'AccountModel':
    """从 base_platform.Account 存入数据库（同平台同邮箱则更新）"""
    with Session(engine) as session:
        existing = session.exec(
            select(AccountModel)
            .where(AccountModel.platform == account.platform)
            .where(AccountModel.email == account.email)
        ).first()
        if existing:
            existing.password = account.password
            existing.user_id = account.user_id or ""
            existing.region = account.region or ""
            existing.token = account.token or ""
            existing.status = account.status.value
            existing.extra_json = json.dumps(account.extra or {}, ensure_ascii=False)
            existing.cashier_url = (account.extra or {}).get("cashier_url", "")
            existing.updated_at = _utcnow()
            session.add(existing)
            session.commit()
            session.refresh(existing)
            return existing
        m = AccountModel(
            platform=account.platform,
            email=account.email,
            password=account.password,
            user_id=account.user_id or "",
            region=account.region or "",
            token=account.token or "",
            status=account.status.value,
            extra_json=json.dumps(account.extra or {}, ensure_ascii=False),
            cashier_url=(account.extra or {}).get("cashier_url", ""),
        )
        session.add(m)
        session.commit()
        session.refresh(m)
        return m


def _migrate_outlook_accounts_schema() -> None:
    if engine.url.get_backend_name() != "sqlite":
        return
    with engine.begin() as conn:
        rows = conn.exec_driver_sql("PRAGMA table_info('outlook_accounts')").fetchall()
        if not rows:
            return
        existing_columns = {str(row[1]) for row in rows}
        if "account_type" not in existing_columns:
            conn.exec_driver_sql(
                "ALTER TABLE outlook_accounts ADD COLUMN account_type TEXT DEFAULT 'microsoft_oauth'"
            )
        if "mailapi_url" not in existing_columns:
            conn.exec_driver_sql(
                "ALTER TABLE outlook_accounts ADD COLUMN mailapi_url TEXT DEFAULT ''"
            )
        if "status" not in existing_columns:
            conn.exec_driver_sql(
                "ALTER TABLE outlook_accounts ADD COLUMN status TEXT DEFAULT 'available'"
            )
        conn.exec_driver_sql(
            "UPDATE outlook_accounts SET account_type = 'microsoft_oauth' WHERE account_type IS NULL OR TRIM(account_type) = ''"
        )
        conn.exec_driver_sql(
            "UPDATE outlook_accounts SET mailapi_url = '' WHERE mailapi_url IS NULL"
        )
        conn.exec_driver_sql(
            "UPDATE outlook_accounts SET status = 'available' WHERE status IS NULL OR TRIM(status) = ''"
        )
        conn.exec_driver_sql(
            "UPDATE outlook_accounts SET status = 'used' "
            "WHERE EXISTS (SELECT 1 FROM accounts "
            "WHERE lower(accounts.email) = lower(outlook_accounts.email)) "
            "AND status = 'available'"
        )


def _migrate_icloud_aliases_schema() -> None:
    if engine.url.get_backend_name() == "sqlite":
        with engine.begin() as conn:
            rows = conn.exec_driver_sql("PRAGMA table_info('icloud_aliases')").fetchall()
            if not rows:
                return
            if "share_token" not in {str(row[1]) for row in rows}:
                conn.exec_driver_sql(
                    "ALTER TABLE icloud_aliases ADD COLUMN share_token TEXT DEFAULT ''"
                )

    # 建表之前就存在的隐私邮箱没有 token，逐行补一个（不能用一条 UPDATE，
    # 每行得是不同的随机值）
    with Session(engine) as session:
        pending = session.exec(
            select(ICloudAliasModel).where(
                (ICloudAliasModel.share_token == None)  # noqa: E711 - SQL 里要 IS NULL
                | (ICloudAliasModel.share_token == "")
            )
        ).all()
        for row in pending:
            row.share_token = new_alias_share_token()
            session.add(row)
        if pending:
            session.commit()


def init_db():
    SQLModel.metadata.create_all(engine)
    _migrate_outlook_accounts_schema()
    _migrate_icloud_aliases_schema()


def get_session():
    with Session(engine) as session:
        yield session
