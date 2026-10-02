# 第 1 窗口：账户、上传与视频管理验收记录

日期：2026-10-02（Asia/Shanghai）
分支：`codex/mvp`
状态：第 1 窗口验收通过；未实现下一窗口的任务分析功能。

## 目标与边界

交付并验证账户注册 / 登录、账号隔离、原始请求体流式上传、视频查询与鉴权读取，以及未提交分析的视频删除和文件清理。上传不设单文件字节上限或业务目录运行期容量门槛；有效媒体时长须满足 `0 < duration <= 1200` 秒。

本窗口没有实现 React 页面、分析任务提交与 attempt、Celery / Redis Worker、ASR / LLM、转写检查点、总结发布、手动恢复或已执行分析视频删除。`analysis_task_created_at` 是后续分析创建必须在事务中写入的删除门闩。

## 实现

- FastAPI、SQLAlchemy、PostgreSQL 和 Alembic；业务库 `videoclip_mvp_stage1`，独立测试库 `videoclip_mvp_stage1_test`。
- 账号密码使用 Argon2id；数据库只存会话令牌哈希；Cookie、Origin 与双提交 CSRF 校验；资源查询由所有者 ID 限定。
- `POST /videos` 读取原始 ASGI 请求流，先写入持久化交接和受控 `.part` 路径，再按块写文件并计算实际字节数 / SHA-256；不按 `Content-Length`、原文件扩展名、MIME 或字节数拒绝上传。
- ffprobe 限时校验第 0 窗口已验证的媒体白名单：MP4 家族、单路 H.264 `yuv420p` 视频、单路 AAC 音轨、容器与音视频流均零起点、时长大于 0 且不超过 1200 秒。
- 上传交接、正式路径改名、视频记录与清理意图均有数据库事实记录。使用本机文件租约防止恢复 / 删除误碰活跃写入；启动恢复只清理由租约证明已停止的写入者所拥有的路径。
- 列表、详情、内容 `GET` / `HEAD`、Range `206` / 无效 Range `416` 和删除均有会话及所有者校验。删除先持久化阻止新读取，再执行清理；失败保留清理状态与受控路径供重试。

## 私密本地运行

首次准备私密配置与独立 E 盘目录，然后启动：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/stage1/prepare-private-data.ps1
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/stage1/run-stage1.ps1
```

配置和 PostgreSQL 密码位于被 Git 忽略的 `data/mvp-stage-1/`。Stage 1 Compose 项目名为 `videoclip-mvp-stage1`，API 与 PostgreSQL 仅绑定回环地址端口 `58001` / `55442`；两者数据分别 bind mount 到 `data/mvp-stage-1/app` 和 `data/mvp-stage-1/postgres`。测试不清空或复用业务库。

检查服务：

```powershell
docker compose --env-file data/mvp-stage-1/.env -f compose.yaml ps
Invoke-RestMethod http://127.0.0.1:58001/health
```

只停止 Stage 1 服务并保留数据库与文件：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/stage1/stop-stage1.ps1
```

运行脚本按顺序构建 API、启动并等待 PostgreSQL 健康、执行 `alembic upgrade head`、启动 API。当前验收库已保持运行，主库迁移检查为 head 且 `alembic check` 无待迁移操作。若要回滚数据库迁移，先备份 Stage 1 E 盘数据并确认数据丢弃影响；初始迁移的 downgrade 会删除本窗口业务表，不要将其当作保留数据的回滚方法。

## 自动化与配置验证

命令均从仓库根目录执行，Stage 1 使用隔离虚拟环境：

```powershell
py -3.13 -m venv data/mvp-stage-1/.venv
data/mvp-stage-1/.venv/Scripts/python.exe -m pip install --require-hashes -r requirements-dev.lock
$env:PYTHONPATH = 'src'
data/mvp-stage-1/.venv/Scripts/python.exe -m pytest -q
data/mvp-stage-1/.venv/Scripts/ruff.exe check src tests scripts/stage1
data/mvp-stage-1/.venv/Scripts/mypy.exe src scripts/stage1/acceptance.py
data/mvp-stage-1/.venv/Scripts/python.exe -m pip check
data/mvp-stage-1/.venv/Scripts/python.exe -m alembic check
docker compose --env-file data/mvp-stage-1/.env -f compose.yaml config --quiet
```

结果：`37 passed`（14.37 秒；仅有 Starlette 上游 AnyIO deprecation warning）；Ruff 全通过；mypy 12 个源文件无问题；`pip check` 无依赖冲突；Alembic 输出 `No new upgrade operations detected.`；Compose 配置解析成功。上述 pytest 对真实隔离 PostgreSQL 执行，覆盖两个账号、鉴权与 CSRF、流式上传、媒体拒绝矩阵、重启恢复、所有者隔离、Range、清理失败 / 重试、活跃写入租约和 ffprobe 超时等行为。

第 0 窗口回归：`python -m pytest -q probes/stage0` 为 `32 passed`；`ruff check probes/stage0` 全通过；`mypy --strict probes/stage0/probe.py` 无问题。第 0 窗口样本、数据和容器配置未改动。

## 一次性真实 HTTP 大文件验收

命令：

```powershell
data/mvp-stage-1/.venv/Scripts/python.exe scripts/stage1/acceptance.py "video\如何选择适合自己的agent.mp4"
```

结果：`passed`；实际大小 `1,116,803,236` 字节；SHA-256 `2b4230d3277d8f0fd3370db11dce78532d20ae51f2dc1149f1d0cdba932eec7a`；上传用时 `14.11` 秒。客户端未声明 `Content-Length`，上传完毕后服务端记录的大小与流式计算摘要和源文件一致。验收随后只重启 Stage 1 API，验证视频仍可从列表 / 详情读取，再验证本人 `HEAD`、Range `206`、无效 Range `416`、删除与删除后不可读取；注册用户正常退出。样本只上传一次，未复制或改写 `video/` 源文件；已通过 API 删除该视频，并在验收后清除临时用户及会话记录。

## Docker 预算与隔离

基线在 2026-10-02 11:03（+08:00）记录，当时 Stage 1 PostgreSQL 已创建并运行、API 镜像尚未构建。构建及真实 HTTP 验收后：

| 资源 | 基线 | Stage 1 验收后 / 增量 |
| --- | ---: | ---: |
| Docker 镜像 | 263 个，4.094 GB | 264 个，约 5 GB；新增 API 镜像 `905,761,264` 字节 |
| 构建缓存 | 187 项，6.649 GB | 206 项，7.555 GB；新增 19 项、净增 `906,770,836` 字节 |
| 容器 | 22 个，1 个运行 | 23 个，2 个运行；新增 Stage 1 API，Stage 1 PostgreSQL 已计入基线 |
| API 可写层 | — | `16,384` 字节 |
| API 日志采样 | — | `915` UTF-8 字节；配置最多保留 2 × 10 MiB |
| Docker 卷 | 15 个，6 个活动 | 数量与占用未增加 |

新增镜像与缓存合计约 `1.69 GiB`，连同 API 可写层和日志采样仍明显低于 7 GiB。每个容器均配置最多 2 × 10 MiB 的 JSON 日志；API 限制 1 GiB / 128 PID，PostgreSQL 限制 768 MiB / 128 PID。对照基线，原有 263 个镜像 ID / 标签完全一致，其他项目容器名称、镜像与状态完全一致；未执行其他 Compose 项目的启停、删除、清理或全局 prune。原有 MuseFlow、Stage 0 容器及 E 盘数据均保留。

## 交付状态与下一步

第 1 窗口目标全部通过，可提交并进入技术方案中的第 2 阶段“可恢复分析”。该阶段需要设计和实现任务提交、attempt 生命周期、执行停止确认、Redis 投递事实与 Worker 安全边界；这些能力仍未在本窗口创建。当前不限制单文件上传字节数，也不对业务数据目录实行总量硬限额、容量准入或预留。
