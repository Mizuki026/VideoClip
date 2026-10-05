# 第 6 窗口验收记录：原视频与笔记分离

> 状态：实现、隔离验收及部署前历史数据复核通过；Stage 2 迁移/部署结果及提交哈希在本次交付结束前补记。第 7 窗口未开始。
> 日期：2026-10-05（Asia/Shanghai）。

## 基线、分支与数据保护

- 开始时分支 `codex/mvp`、HEAD `4c1554722427ce1eef88e265582d37f42e81a9fd`，工作区干净；第 5 窗口交接提交 `4c15547`，当前基础迁移 `7c2a91e5b04d`。未切换/重置分支。
- 已读取 `AGENTS.md`、领域文档入口 `docs/agents/domain.md`、产品/技术最终方案、README、第 3、4、5 窗口规格及验收。未发现适用于本领域的额外 CONTEXT 或 ADR。
- 第 5 窗口 Compose 已停止，不能代表 Stage 2 最新代码。部署前 Stage 2 只启动其专属 PostgreSQL 与 Redis；API、分析 Worker、维护 Worker 和 Beat 均未启动。历史查询全为只读。
- Stage 2 迁移前数据：6 users、8 个 complete uploads、8 videos（全部 `ready`）、8 tasks（`awaiting_summary:2, failed:4, succeeded:2`）、14 attempts（`awaiting_summary:2, failed:10, succeeded:2`）、14 executions、14 deliveries、330 transcript segments、12 summary requests、2 published summaries。`deletion_error` 非空 0、旧 cleanup intents 0、running tasks 0、未确认 executions/attempts 0、analysis 队列长度 0。逐一按存储名核对 8 个受控 `.media` 文件均存在。
- 隔离测试只使用 `data/mvp-stage-6/acceptance/<唯一 run id>/` 下新建的数据目录、唯一 Compose 项目、专属 Postgres/Redis、随机测试账户和本地 TLS 模型替身；没有真实模型供应商调用。Playwright 每次使用新的 `output/playwright-<随机 id>` 目录。仅清理本窗口创建的 Compose 项目/网络和容器；测试数据及日志保留供检查。没有删除 Stage 1、Stage 2 业务数据或其他 Docker 项目。

## 实现验收证据

### 数据库、任务取消和预算

- `tests/test_media_deletion.py` 覆盖已发布总结与转写保留、等待总结取消、排队/共享冷却等待/自动恢复待执行资格失效、未确认 execution 不伪造停止且占用全局位、清理失败和重试不创建 attempt/请求/总结或改变预算、CSRF/Origin/跨账户隔离、媒体删除提交与分析提交/领取竞争、媒体打开与删除的锁串行化、媒体文件删除后状态提交回滚及幂等重试、失败清理状态下兼容整条删除。
- `tests/test_analysis.py::test_delete_winning_summary_race_blocks_publication_and_later_requests` 对整条删除与媒体删除均用在途本地替身响应复现迟到总结；媒体删除分支确认 task 请求被取消、迟到结果不发布、停止后只重试清理、合法转写仍可读，随后整条删除仍完成。
- 已发布 `AnalysisSummary` 的 attempt 与载荷不改写。等待总结中的完整转写仍标记为完整转写，但没有已发布总结；删除后总结 API 不伪造成功。
- 媒体待处理及清理失败态下 GET、HEAD 和 Range 均返回 410；跨账户返回 404，缺少 CSRF/错误 Origin 返回 403；笔记在媒体待处理、清理失败、媒体已删除时仍可读，整条删除后视频、转写和总结接口均不可访问。

### 真实受控进程停止门禁

- 隔离 Linux API 容器内真实启动独立 session 的存活子进程，并把 PID/start ticks/session/boot ID 绑定到执行记录。调用媒体删除后观察到 `deletion_pending`、execution 与 attempt 停止时间仍为空、全局未确认 execution 查询仍命中该执行、原媒体路径仍存在。
- 发送终止信号并等待真实进程退出，扫描 `/proc` 确认该 session 已空；随后通过 `worker_stop_evidence` 和 `confirm_stopped_execution` 确认同一绑定，再重试清理。观察到媒体路径删除、attempt 清理完成、整条记录删除完成。脚本输出：`held_live_execution=1 stop_confirmed_before_cleanup=1 media_deleted=1 whole_record_deleted=1`。
- 此证据证明的是受控进程存活期间不会危险清理及可信停止后的恢复；不扩大 Stage 3 已支持的 Linux session 停止证明边界。

### 真实浏览器与 API 重启

- Playwright `frontend/e2e/stage6-media-deletion.spec.ts` 使用隔离账户读取合成已发布笔记；确认删除原视频后播放器卸载，回看按钮消失，引用文本和时间仍显示；GET/HEAD/Range 均为 410；刷新及隔离 API 容器重启后笔记仍读回；整条删除后视频、转写、总结和媒体接口均为 404。
- 最终隔离运行输出目录：`data/mvp-stage-6/acceptance/20261005t061010z-564da765/output/`，对应最新代码和完整后端套件。前两次 `060526z`、`060653z` 运行定位并修正了数据库回滚故障注入测试的测试客户端/注入时点；最终运行已通过。此前 `20261005t060824z-c9602e01/output/` 用于单独确认 13 项定向测试、真实进程 helper 和浏览器；该次不包含全量后端测试。浏览器可见产物、trace 与日志只在各自隔离目录。

## 自动化验证命令和结果

Stage 6 专用运行器（含独立 Compose、迁移、质量检查、直接/完整后端测试、Linux 真实进程和 Playwright）：

```powershell
.\scripts\stage2\run-final-stage6-acceptance.ps1
```

最近一次完整运行 `20261005t061010z-564da765`：

| 检查 | 结果 |
| --- | --- |
| Stage 6 直接后端事务测试 | 13 passed，1 条既有 Starlette/AnyIO `BlockingPortal` 弃用警告 |
| 总结迟到响应专项回归 | 2 passed，同一既有弃用警告 |
| 后端全量 `pytest -q` | 155 passed，1 条既有弃用警告，216.25s |
| Ruff（`src tests migrations` 与 Stage 6 两个夹具脚本） | 通过 |
| 项目配置 mypy | 29 source files，0 errors；进程夹具单独 mypy 亦通过 |
| `pip check` | 无 broken requirements |
| Compose build、隔离 Postgres/Redis/模型替身健康、Alembic 从空库升级到 head | 通过，head `d42f6c8a901e` |
| Stage 0 回归 `pytest probes/stage0/tests -q` | 32 passed，6.27s |
| 前端 lint | 通过 |
| 前端 Vitest | 5 files / 14 tests passed |
| 前端 TypeScript / Vite build | 通过 |
| 隔离真实进程停止门禁 | 通过，进程仍存活时 `deletion_pending` 且执行占位及文件保留；可信停止后清理成功 |
| 隔离真实浏览器流程 | 1 passed；API restart 期间 Vite `/health` 有预期连接重置日志，重启后健康检查、读回及断言通过 |
| `git diff --check` | 通过；Git 提示若干既有工作区文件将来可能 LF→CRLF，没有 whitespace error |

Stage 2 部署前复核（只读）：6 users、8 `complete` upload handoffs、8/8 `ready` videos、tasks `awaiting_summary:2/failed:4/succeeded:2`、attempts `awaiting_summary:2/failed:10/succeeded:2`（14 总数）、14 executions（未确认 0）、attempt 未确认停止 0、14 deliveries、330 transcript segments、12 summary requests、2 published summaries、旧 `deletion_error` 0、cleanup intents 0、running tasks 0、analysis 队列长度 0；8 个由受控 `storage_name` 定位的历史 `.media` 文件均存在。Stage 2 PostgreSQL/Redis 健康运行；API、分析 Worker、维护 Worker、Beat 均停止。执行 `scripts/stage2/run-stage2.ps1` 前仍会再次通过脚本门禁确认未确认 execution、running task 和队列为空。迁移/部署结果、迁移后历史快照、运行地址和服务形态将在此处补记。不得把 `data/mvp-stage-6/acceptance` 的隔离结果当作 Stage 2 部署证据。

## 未验证事项、限制和第 7 窗口交接

- 已验媒体路径不存在；没有测量磁盘可用空间差值，也不据文件 unlink 声称物理介质块立即回收。开放响应不会被追回；行锁只覆盖打开前检查与文件打开，流式响应期间不持有数据库锁，断开连接关闭句柄。
- Stage 6 隔离媒体替身/笔记是合成数据，不是用户课程或真实模型响应。真实付费供应商限额和账单未验证。
- 第 0 窗口未验证项继续保留：20 分钟真实模型样本、供应商账户计费/扣费证据、容器可写层处理中峰值、E 盘占用差额归因。未扩大到最终展示材料或整体验收。
- 第 7 窗口应先读取本记录、产品/技术文档和第 0 窗口遗留项，完成最终时间线、完整最终版验收及展示材料；当前窗口未启动第 7 窗口。

## Git 交付

逻辑提交、提交前历史快照与最终工作区状态将在本次提交后补记。当前改动不得包含 `.env`、密码、课程原文、模型响应或 `data/mvp-stage-6` 运行产物。
