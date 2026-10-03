# 最终版第 3 窗口验收记录：可信停止确认

> 状态：第 3 窗口实施与故障验收通过；第 4 窗口尚未开始。
> 日期：2026-10-03（Asia/Shanghai）。

## 基线、环境与数据保护

- 开始时工作区干净；当前分支 `codex/mvp`，第 2 窗口交接提交 `eb5f8ed`。未重置或切换分支。
- 开始前 Stage 2 API、PostgreSQL、Redis 和单 analysis Worker 健康运行；未确认 execution、running attempt、analysis 队列均为 0。Beat 和 maintenance Worker 当时未运行，由第 2 窗口已验收的启动脚本管理。
- 历史基线：6 users、8 upload handoffs、8 videos、8 tasks（`awaiting_summary:2, failed:4, succeeded:2`）、14 attempts、14 executions、330 transcript segments、12 summary requests、2 published summaries；无未确认 execution。验收前后数据计数及 task 状态相同。
- 运行故障验收时仅创建三组随机 `stage3-*` 隔离账户，每组含一个受控 active task/attempt 和一个排队探针。验收前确认历史 analysis 队列为空，期间不发送真实分析消息，不调用外部 ASR/LLM 服务。模型客户端请求只连容器内本地挂起 HTTP 替身，使用占位 token；没有供应商费用、课程原文或模型响应进入证据。
- 隔离后端测试仅使用 `videoclip_mvp_final_stage3_test` 和 `data/mvp-stage-3/test-data`。测试期间只启动 Stage 1 PostgreSQL 服务，未启动 Stage 1 API/Worker；测试完成后将该 PostgreSQL 恢复为原先 stopped。Stage 2 使用专属 Compose 项目和 E 盘目录；没有清理历史业务记录或其他 Docker 项目。
- Stage 2 新增 identity 迁移 `f71a0d8c2e31` 仅增 nullable 列与约束；历史执行不回填、不伪造绑定、不追溯停止确认。
- 迁移后只读核对 14 条既有 execution 的新身份字段：`legacy_null_identity=14`、`bound_identity=0`、`unconfirmed=0`；与兼容策略一致。

## 实现与运行语义

- 每个新 execution 在领取事务中绑定 execution/task/attempt、一次性 Worker generation、容器 ID、Linux boot ID、PID/start ticks、session ID 和 `linux-session-v1` 边界。Worker 建立独立 session 后提交领取；事务提交后先向同容器监督器登记，得到登记回执才可开始任何分析副作用。
- Stage 2 分析容器入口为 `python -m videoclip.analysis_supervisor`。PID 1 启用 child subreaper 后，以单队列 `analysis`、并发 1、预取 1、每子进程一个任务启动 Celery Worker。实际 FFmpeg、`videoclip.asr_client`、`videoclip.llm_client` 均处于绑定 session 的独立 process group；监督器核验并扫描整个 session，而不是只检查父 PID 或一个进程组。
- 停止监督先向 session 成员发送 TERM，等待后对残留组发送 KILL，直到不再存在存活成员才生成停止证据。统一生命周期事务核对 execution、task/attempt、Worker generation 和当前绑定后写入证据。相同证据幂等；旧代次/错误 execution 不可确认新 execution。任务结果终态、停止确认和 attempt 清理状态分开；确认不改预算。
- 无证据时执行保持 `execution_state=stop_unconfirmed` / `STOP_CONFIRMATION_REQUIRED`，禁止恢复与工作目录清理。数据库确认写入失败后只对相同绑定和 evidence hash 有界重试；耗尽后转人工，不先恢复分析。清理只能在停止确认成功后进行。
- 人工入口 `scripts/stage2/stop-and-confirm-worker.ps1 -ExecutionId <UUID>` 先核对未确认 execution 的完整身份，再核对唯一 Worker 的 generation 与精确容器 ID，零宽限停止并检查同一容器 Docker 状态为 exited，最后调用相同业务操作确认。历史身份缺失时拒绝。

## 真实 Linux 进程故障验收

实际命令：

```powershell
& .\scripts\stage2\run-stage2.ps1
& .\data\mvp-stage-2\.venv\Scripts\python.exe scripts\stage2\stop-confirmation-acceptance.py
```

最终通过证据：`data/mvp-stage-3/acceptance/run-26a7ca93b24941df95fa2510125e5112/events.jsonl`。这是本窗口 E 盘运行输出，已被 Git 忽略，不纳入提交。事件只含随机 fixture 标识、进程身份、命令参数/退出码、状态和证据哈希，不保存 private env 内容、凭据、课程文本或响应体。

### 执行连接断开、子进程残留及危险清理阻断

- Runtime Worker generation `22cf93b4-32c9-44ef-b2df-eb1b05b5c74b`，容器 `11e1fd29bf0e`。受控 execution `616f38d5-0bf0-4034-a869-67f2b1754718` / task `b8ddcb17-795a-4d45-aac2-52c37cbd5921` / attempt `7a82c929-11c2-41eb-9da0-a6d489610c20` 绑定 boundary PID 24、start ticks 2223032、session 24；PostgreSQL advisory-lock 执行连接 backend PID 2412。
- 监督入口实际启动了 PID 26 本地模型 stub、27 ASR client、28 LLM client、29 FFmpeg。对 backend PID 2412 执行 `pg_terminate_backend` 后，记录显示 PID 24、26、27、28、29 均仍为 Linux `S` 状态，执行记录仍未确认；排队 attempt 的生产领取逻辑返回 `blocked_by_unconfirmed_execution`。
- 在相同活跃 execution 下调用现有 attempt 清理业务入口，返回 `STOP_CONFIRMATION_REQUIRED` / `cleanup_eligible=false`；本窗口随机 attempt workspace 内的 sentinel 保持存在。证明数据库锁连接释放不会导致接管或危险清理。
- 随后只对 boundary PID 24 发 SIGKILL。验收观察到 FFmpeg/ASR/LLM/stub 后代仍存活且数据库 execution 未确认；监督器回收并等待整个 session 后才写入 `worker_supervisor` 证据，hash `292beab478cf81655e2f01be344e2b75140432bfd122f6d1bce25f4d305a437c`。停止确认后的现有清理路径移除该 sentinel；生产验收脚本在退出码为 PASS 前断言该结果。

### 停止后数据库不可用、保留占位并转人工

- execution `cee12500-fa52-4161-92b9-4be352029c8c` / task `58e06c11-3cfa-4617-9b89-104b4045c8b9` / attempt `75e12e24-ea37-46b7-bab0-875b4489147f`，boundary PID 134/start ticks 2224728/session 134；受控进程 PID 136 stub、137 ASR、138 LLM、139 FFmpeg。
- 先断开该 execution 的数据库连接并验证新的领取仍被未确认记录阻断；再强杀 PID 134、仅停止 Stage 2 PostgreSQL。监督器让本地后代退出，但数据库不可用期间不能写入确认；记录观察到同一 evidence 的写入失败、`stop_confirmation_required`，执行占位仍为 1，未启动第二执行者。
- 恢复 Stage 2 PostgreSQL 后，执行位仍未确认。既有人工脚本校验精确 Worker generation `22cf93b4-32c9-44ef-b2df-eb1b05b5c74b` 和容器 `11e1fd29bf0e` 已退出，写入 `operator_container_exit` 证据，hash `68a541c37da6e7986f64900eaaaacd2946c7ac6fd0f23b46b364bce76b04f68c`。确认前没有恢复或重新领取。

### 监督器/容器强停与人工恢复入口

- execution `bc7b740d-c176-4f66-893d-4c0ea40990db` / task `a11564b4-d2d3-4e92-bc07-91c2318f6a9d` / attempt `0bdbf432-9b10-4b12-a137-e85f5ce94219`，Worker generation `88e194d7-ab9b-4138-b95f-4964f238aec4`，容器 `5357b73b1655`，boundary PID 17/start ticks 2232149/session 17；其 stub/ASR/LLM/FFmpeg 子进程 PID 为 19/20/21/22。
- 对精确 Worker 容器 PID 1 发送 SIGKILL，Docker 报告同一容器 `exited`、退出码 137。此时数据库 `stop_confirmation_written=false` 且未确认占位仍存在；容器/监督器死亡不会从心跳、PID 或重启推断停止。
- 运行既有 PowerShell 人工入口后，generation 与退出容器核验通过，才写入 `operator_container_exit` 证据，hash `b958f345ec2eff92eafd4ed8c0d65f3c8db930b4fc3394ecf0a5a1c9ae54d8fd`。随后 Stage 2 通过受保护的启动脚本恢复。停止确认之前没有第二 Worker 接管。

### 清理、历史和队列结果

- 故障验收三组随机账户、task、attempt、execution 只在确认对应停止后按精确所有权集合清理；三组临时进程文件均按受限路径删除。结束时随机 fixture 不留在数据库或 `/tmp`。
- 前后 Stage 2 快照一致：users 6、uploads 8、videos 8、tasks `awaiting_summary:2/failed:4/succeeded:2`、attempts 14、executions 14、unconfirmed 0、deliveries 14、transcript segments 330、summary requests 12、published summaries 2；analysis 队列为 0。maintenance 队列也为 0。没有修改历史任务、检查点、结果或失败行。
- 队列与周期回收回归：Stage 2 运行态恰好一个 `analysis_supervisor` Worker，PID limit 128；maintenance Worker 只订阅 `maintenance`、并发 1、PID limit 64；Beat 单实例 PID limit 64。三服务保持 JSON 日志轮转 `10m × 2`，restart policy 为 `no`，基础资源限制保留。Stage 2 Beat→Redis→maintenance Worker 在本次部署后实际运行周期回收，最新周期日志 `reclaim_cycle_finished status=completed reclaimed=0`；前一窗口真实清理/重试和分析队列隔离的故障证据见[第 2 窗口验收记录](../final-stage-2/validation.md)。当前故障验收期间没有到期上传被回收。
- 故障验收前后各运行受保护 Stage 2 启停脚本；Stage 2 API `/health` 返回 `ok`，PostgreSQL、Redis、Beat、maintenance Worker、analysis Worker 均运行。当前 Worker 是正常运行的单实例。Stage 1 PostgreSQL 测试服务已恢复为 stopped。

## 自动化验证

直接相关事务测试：

```powershell
$env:PYTHONPATH = (Resolve-Path 'src').Path
$env:VIDEOCLIP_TEST_DB_NAME = 'videoclip_mvp_final_stage3_test'
$env:VIDEOCLIP_TEST_DATA_DIR = (Join-Path (Resolve-Path '.').Path 'data\mvp-stage-3\test-data')
& .\data\mvp-stage-2\.venv\Scripts\python.exe -m pytest tests\test_analysis.py -k 'confirmation or legacy_execution' -q
```

结果：`7 passed, 33 deselected`。覆盖未登记证据、错误 hash / generation / execution、幂等重复、旧 execution 迟到确认、同 attempt successor/current binding、DB 写确认失败保持全局占位、已发布终态保持、请求数不重置、历史身份缺失拒绝确认。

最终验证命令和结果：

| 检查 | 结果 |
| --- | --- |
| 后端全量：`python -m pytest tests -q`（Stage 3 隔离 DB） | 107 passed，1 条既有 Starlette/AnyIO BlockingPortal 弃用警告，133.56s |
| Stage 0 回归：`python -m pytest probes/stage0/tests -q` | 32 passed，6.63s |
| 前端 `npm run lint` | 通过 |
| 前端 `npm test -- --run` | 4 files / 13 tests passed |
| 前端 `npm run build` | TypeScript 检查及 Vite build 通过 |
| Ruff：`src tests migrations scripts/stage1 scripts/stage2 scripts/stage4 probes/stage0` | 通过 |
| mypy：相同适用路径，`--explicit-package-bases` | 40 source files，0 errors |
| `pip check`、Python `compileall` | 通过，无 broken requirements |
| Stage 1 / Stage 2 Compose `config --quiet` | 均通过 |
| Stage 2 `alembic check` | `No new upgrade operations detected.` |
| `run-stage2.ps1`、`stop-stage2.ps1`、`stop-and-confirm-worker.ps1` PowerShell parse | 均通过 |
| `git diff --check` | 通过 |

本窗口最终回归命令（仓库根目录 PowerShell；前端 lint/test/build 在 `frontend/`）：

```powershell
$env:PYTHONPATH = (Resolve-Path 'src').Path
$env:VIDEOCLIP_TEST_DB_NAME = 'videoclip_mvp_final_stage3_test'
$env:VIDEOCLIP_TEST_DATA_DIR = (Join-Path (Resolve-Path '.').Path 'data\mvp-stage-3\test-data')
& .\data\mvp-stage-2\.venv\Scripts\python.exe -m pytest tests -q
& .\data\mvp-stage-0\.venv\Scripts\python.exe -m pytest probes\stage0\tests -q
& .\data\mvp-stage-2\.venv\Scripts\python.exe -m ruff check src tests migrations scripts\stage1 scripts\stage2 scripts\stage4 probes\stage0
& .\data\mvp-stage-2\.venv\Scripts\python.exe -m mypy --explicit-package-bases src migrations scripts\stage1 scripts\stage2 scripts\stage4 probes\stage0
& .\data\mvp-stage-2\.venv\Scripts\python.exe -m pip check
& .\data\mvp-stage-2\.venv\Scripts\python.exe -m compileall -q src migrations scripts\stage1 scripts\stage2 scripts\stage4 probes\stage0
docker compose --env-file data\mvp-stage-1\.env -p videoclip-mvp-stage1 -f compose.yaml config --quiet
docker compose --env-file data\mvp-stage-2\.env -p videoclip-mvp-stage2 -f compose.yaml config --quiet
docker compose --env-file data\mvp-stage-2\.env -p videoclip-mvp-stage2 -f compose.yaml exec -T api alembic check
```

```powershell
# frontend/
npm run lint
npm test -- --run
npm run build
```

PowerShell AST parser 对 `run-stage2.ps1`、`stop-stage2.ps1`、`stop-and-confirm-worker.ps1` 执行 `System.Management.Automation.Language.Parser.ParseFile` 且错误数为 0。

全量后端测试使用 Stage 3 独立测试数据库；没有运行第 4 窗口或真实付费模型。浏览器界面没有新增交互，因此没有另做 Playwright 页面验收。

## 支持范围、限制与未验证项

- 自动停止证明只覆盖本机 Linux Stage 2 Worker 容器中已绑定 execution session 的进程，以及当前实际的 FFmpeg、ASR client、LLM client 等同 session 后代。监督器须能读取完整 `/proc` 并核对 generation、boot ID、PID/start ticks 和 session 身份。
- 不覆盖主动 `setsid()` 逃逸的新 session 恶意本机进程、容器外进程或宿主机突然死亡后的内核级取证。监督器/容器/宿主机强停、历史 execution 无身份绑定、边界读取失败时转人工，保留未确认占位、恢复门禁和清理门禁。
- 停止证明不撤销供应商已收到的请求，也不返还或重置请求预算。故障验收仅访问本地替身，未验证真实供应商账单。
- 本窗口没有实现 Stage 4 排队协调、投递补偿、共享模型冷却、分析自动恢复或恢复扫描；重启后仍须通过原有未确认 execution 门禁。Beat 和 maintenance Worker 只执行临时上传回收。
- 第 0 窗口仍未验证：容器可写层处理中峰值、E 盘空间差额归因、完整 20 分钟真实模型处理、供应商账户计费/实际扣费。

## Git 交付

当前实现和文档按仓库规则拆分为已验证逻辑提交；最终 commit 哈希与提交后工作区状态由主任务最终汇报列出。运行证据位于 Git 忽略的 `data/mvp-stage-3/acceptance/`。
