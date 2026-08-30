# 异步服务骨架

当前仓库采用 FastAPI + SQLAlchemy async + PostgreSQL + Redis/ARQ 的标准异步架构。
生产运行时只允许访问 PostgreSQL；SQLite 仅由一次性迁移脚本读取，不能作为应用后端。

```text
                     ┌──────────────┐
Browser / API client │ FastAPI API  │ :8765
                     └──────┬───────┘
                            │ claim + create run (one transaction)
                    ┌───────▼────────┐
                    │   PostgreSQL    │ accounts / runs / credentials
                    └───────┬────────┘
                            │ enqueue + durable event stream
                    ┌───────▼────────┐
                    │      Redis      │ ARQ + events + Team seat cache/leases
                    └───────┬────────┘
                            │
                    ┌───────▼────────┐
                    │  Worker x N     │ asyncio + to_thread bridge
                    │ protocol integrations │
                    └─────────────────┘
```

## 边界

- `api/routes`: 只做 HTTP 参数和状态码转换。
- `repositories`: 只做 SQL，所有 claim 都使用 PostgreSQL 行锁和
  `skip_locked`，不再依赖 Python 全局锁。
- `services`: 编排一个用例，负责事务、入队、状态转换和事件发布。
- `infrastructure`: 只管理 SQLAlchemy async engine、Redis、ARQ 等外部依赖。
- `worker.py`: ARQ 进程入口。现有 AuthFlow/mail/SMS/Team provider 通过
  `asyncio.to_thread` 隔离阻塞协议；数据库写入仍通过异步 session 完成。
- 当前凭证字段仍按旧系统以明文存储。生产环境接入前应使用 KMS/应用层加密，
  并限制数据库、日志和 Redis 的访问权限。

## 状态流

```text
available -> in_use -> done
                    └-> failed
```

API 在同一个 PostgreSQL 事务中 claim 账号并创建 `queued` run；Redis 入队失败
会把账号释放回 `available`，并把 run 标记为失败。Worker 先把 run 改为 `running`，最终同时
更新账号、凭证和 run。SSE 从 Redis Stream 读取，断线后通过 `Last-Event-ID`
继续消费，不受 API 进程重启或多副本影响。

## 本地启动

```bash
cp .env.example .env
uv venv --python 3.11
uv sync --extra dev
docker compose -f deploy/docker-compose.yml up --build
```

仓库根目录的 `.python-version` 固定为 `3.11`；后续直接执行 `uv run` 会复用该版本。

访问 `http://127.0.0.1:18765/` 打开 frontend；接口位于 `/api/v1`，健康检查是
`/healthz`。账号、凭证、导出、设置和 Team 母号都有对应的异步路由；详情以
`/docs` OpenAPI 为准。

注册、SMS 清理、Team 监控、SSO 同步均由 PostgreSQL 状态和 Redis/ARQ worker
驱动，不再存在 legacy API、legacy registrar 或 SQLite 运行时开关。

### Team 轮转

Team 轮转是按母号拆分的动态调度：ARQ 每 5 秒只扫描 PostgreSQL 中到期的
`next_rotation_at`，然后为每个母号投递一个幂等任务。母号的席位和健康快照放在
Redis（带 TTL），PostgreSQL 保存同一份可恢复游标；Redis 锁避免多个 worker 同时
操作同一个母号。

子号阶段持久化为 `joining -> hub_push -> active -> removing -> done`。每个远程副作用
之前先写入 `stage/lease_until`，成功后再提交结果；任务被杀死后，过期租约会重新排队，
加入阶段先拉一次成员快照对账，推送和删除接口按已有 account/member id 重试，避免重复
创建。席位缓存命中时不会请求母号，只有缓存过期、加入/移出后的对账或恢复未完成加入时
才调用母号接口。额度查询走子号凭证和 Hub，不额外消耗母号请求。

母号之间由 `mother_concurrency` 控制；同一个母号内，席位拉入由 `join_concurrency`
控制，Hub 推送由 `hub_concurrency` 控制，三者都是独立并发闸门。也就是说一批席位
不会再逐个排队，单个账号失败只影响自己的租约和重试，不阻塞同母号其他账号。

可调参数以 `team_rotation_` 开头：`interval_seconds`（轮转周期，最低 5 秒）、
`seat_cache_ttl`（席位缓存）、`member_refresh_interval`（成员复核）、
`quota_threshold`、`quota_concurrency`、`mother_concurrency`、`join_concurrency`、
`hub_concurrency`、`operation_lease_seconds` 和
`retry_max_seconds`。Redis 丢失时可从 PostgreSQL 继续；Redis 不可用期间任务会等待，
避免多 worker 并发修改 Team。

导入账号并创建任务：

```bash
curl -X POST http://127.0.0.1:18765/api/v1/accounts/import \
  -H 'content-type: application/json' \
  -d '{"accounts":[{"email":"a@example.com","kind":"outlook","password":"p","client_id":"c","refresh_token":"r"}]}'

curl -X POST http://127.0.0.1:18765/api/v1/runs \
  -H 'content-type: application/json' \
  -d '{"email":"a@example.com","options":{"want_access_token":true}}'
```

## 旧库迁移

迁移脚本只读 SQLite，不会修改源文件，可重复执行。账号、凭证、runs、settings、
Team rotation 四张表和 SSO/SMS 两个队列都会搬迁：

```bash
uv run alembic -c deploy/alembic.ini upgrade head
uv run python scripts/migrate_sqlite_to_postgres.py \
  --sqlite backend/data/webui.db
```

生产环境用 `uv run alembic -c deploy/alembic.ini upgrade head` 管理 schema。`AUTO_CREATE_SCHEMA` 只适合临时
开发环境，生产保持关闭。

## 运行时边界

1. API、worker、migration 使用同一个 `DATABASE_URL`，必须是 PostgreSQL/asyncpg。
2. Redis 只承载 ARQ 任务和 SSE stream，不保存业务真相。
3. `scripts/migrate_sqlite_to_postgres.py` 是一次性只读迁移工具；迁移完成后可将
   本地 SQLite 文件归档或删除，应用不会读取它。
