# PostgreSQL 支持

应用默认仍使用 SQLite，无需额外配置。将 `DATABASE_URL` 改为 PostgreSQL SQLAlchemy URL 后，所有 SQLModel 数据模型、账号、任务、配置、邮箱、代理和 iCloud 数据都会使用 PostgreSQL。

## 连接串

推荐使用 psycopg 3 驱动：

```dotenv
DATABASE_URL=postgresql+psycopg://any_auto_register:请替换强密码@127.0.0.1:5432/any_auto_register
```

如果用户名或密码包含 `@`、`:`、`/`、`?` 等保留字符，请先做 URL 编码。服务启动时会自动建表；项目当前不依赖 SQLite 专用 SQL，因此同一套 API 可直接切换数据库。

## 200 并发任务配置

注册、补 RT 和绑 2FA 的单任务上限为 200；同一应用进程内所有这类任务共享 `TASK_GLOBAL_MAX_CONCURRENCY`（默认 200）的总预算。SQLite 最多允许 40 并发，高于此值必须使用 PostgreSQL。

PostgreSQL 默认连接池为 `DB_POOL_SIZE=30`、`DB_MAX_OVERFLOW=50`、`DB_POOL_TIMEOUT=30`。网络任务绝大部分时间都在等待外部服务，不需要把数据库连接数设为 200。

高并发任务会对代理实施进程内租约：默认 `PROXY_MAX_CONCURRENT_PER_IP=1`，同一个代理不会同时分配给多个账号。代理数量少于目标并发时任务会在日志中提示并等待空闲代理；未配置代理时会提示直连高并发风险。

外部服务也有独立的进程内并发预算：`OPENAI_MAX_CONCURRENCY=200`、`MAIL_API_MAX_CONCURRENCY=100`、`SMS_API_MAX_CONCURRENCY=100`。限制覆盖一个账号完整的外部链路，避免 200 个 worker 同时轮询同一个邮箱/接码服务或冲击 OpenAI。任务日志会输出实际生效的预算。

## Docker Compose（随应用启动 PostgreSQL）

仓库提供 `docker-compose.postgres.yml` 覆盖文件，会启动 PostgreSQL 18 并让应用等待数据库健康后再启动。项目根目录创建 `.env`：

```dotenv
POSTGRES_PASSWORD=请替换为高强度密码
# 可选：POSTGRES_DB=any_auto_register
# 可选：POSTGRES_USER=any_auto_register
```

```bash
docker compose -f docker-compose.yml -f docker-compose.postgres.yml up -d --build
```

PostgreSQL 数据保存在 Docker 命名卷 `postgres_data`；删除容器不会删除数据，执行 `docker compose ... down -v` 才会删除该卷。

## Docker Compose（外部 PostgreSQL）

在项目根目录 `.env` 中配置连接串，再启动应用：

```dotenv
DATABASE_URL=postgresql+psycopg://any_auto_register:请替换强密码@postgres.example.com:5432/any_auto_register
```

```bash
docker compose up -d --build
# 无头服务器镜像：
docker compose -f docker-compose.server.yml up -d --build
```

数据库不在应用容器内时，应用的 `data/` 挂载卷仍用于日志和凭据加密密钥，**不要删除**。

## 从 SQLite 迁移

切换连接串只会创建 PostgreSQL 的空表，不会自动复制旧 SQLite 数据。请在停机窗口使用适合你环境的迁移工具（例如 `pgloader`）迁移 `account_manager.db`，并在新库上核对账号数、配置和任务记录后再切换 `DATABASE_URL`。迁移前务必备份 SQLite 文件和 PostgreSQL 数据库。

## 本地开发

安装依赖后直接设置环境变量：

```powershell
$env:DATABASE_URL = 'postgresql+psycopg://any_auto_register:请替换强密码@127.0.0.1:5432/any_auto_register'
python main.py
```

连接失败或 URL 不是 SQLite/PostgreSQL 时，应用会在启动阶段报出明确错误，不会悄悄回退到其他数据库。
