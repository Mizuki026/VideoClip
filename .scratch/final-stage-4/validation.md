# 最终版第 4 窗口验收记录：排队协调与投递补偿

> 状态：第 4 窗口实现、隔离真实链路验收和 Stage 2 部署均已完成；可以进入第 5 窗口，但本次没有开始第 5 窗口。
> 日期：2026-10-03（Asia/Shanghai）。

## 基线和保护范围

- 开始前工作区干净，分支为 `codex/mvp`，HEAD 为第 3 窗口交接提交 `c7ac8c0`，其前一逻辑提交为 `9d7deb3`。没有重置或切换分支。
- Stage 2 历史基线由只读 SQL 和 Redis 查询保存于 `data/mvp-stage-4/test-output/20261003T-stage2-predeploy/stage2-before.json`：6 users、8 uploads、8 videos、8 tasks（2 `succeeded`、4 `failed`、2 `awaiting_summary`）、14 attempts、14 executions、14 deliveries、330 transcript segments、12 summary requests、2 published summaries。未确认 execution、running task 和 analysis 队列项均为 0。
- 本窗口故障注入只在新建的 Stage 4 Compose 项目中进行。该项目使用独立 PostgreSQL、Redis、E 盘数据/密钥目录、随机隔离账户和随机输出目录；只生成受控 8 秒媒体 fixture。ASR/LLM 请求经隔离 Compose 网络与本地 HTTPS 替身交互，不访问供应商模型。Stage 2 不用于故障注入，也没有清空队列、强杀业务 Worker 或修改历史业务行。
- Stage 3 停止证明的适用范围不变：只覆盖已绑定的 Stage 2 Linux execution session。监督器/容器强停、身份缺失、边界不受支持或证据不可验证仍保持未确认占位，需人工处理；本窗口不扩大证明范围。

## 实施语义

- 不同视频任务可在分析执行位占用时登记资格；同视频重复提交返回既有任务。PostgreSQL 按 `(queue_entered_at, task.id)` 决定领取先后，消息发送先后不证明公平性。人工恢复或重新取得资格写入新的队列时间，但保留同 task 已有补投计数、操作计数和 deadline；旧 attempt 因失去当前绑定资格不能被迟到消息重新领取。
- 首次投递、人工恢复和 Beat 扫描统一调用 `coordinate_analysis_delivery`。业务入口短事务锁住业务行并以同一 PostgreSQL execution advisory key 的事务锁和 Worker claim 门禁争用，登记唯一 reservation / generation；随后在事务外发布 Celery 消息，再以 generation 和当前资格 CAS 写回。消息 `sent` 不是已领取证明；pending、unknown、发送成功但确认丢失及已发送未领取均留待 PostgreSQL 后续协调。
- 实际领取时再次核验用户视频可用性、当前 task / attempt 绑定、`claim_count=0`、队列次序和全局无未确认 execution。analysis Worker 维持并发 1、prefetch 1 和原有 ACK 策略；忙时或顺序不合格的消息可以被消费并 ACK，数据库检查时间仍使任务保持可协调。已开始 execution 的 attempt 不由本窗口补投或自动重试。
- maintenance Worker 只扫描、判定和发布，不领取分析、不调用 ASR/LLM。单实例 Beat 同时保留临时上传回收并调度分析协调。两类维护操作使用各自 Redis owner-token TTL 锁和 PostgreSQL advisory lock；Redis 锁 180 秒过期，owner 核验释放，advisory lock 随连接释放；分析协调任务软/硬超时为 140/150 秒，扫描每轮最多 4 项。

## 参数和预算

| 参数 | 默认值 | 语义与上界 |
| --- | ---: | --- |
| `ANALYSIS_DELIVERY_RETRY_LIMIT` | 8 次 | 首次发送之外最多接受 8 个补投 reservation；允许范围 1–32。 |
| `ANALYSIS_DELIVERY_DEADLINE_SECONDS` | 86,400 秒 | 首次 reservation 开始的可推进投递期限；允许 60–604,800 秒。 |
| `ANALYSIS_DELIVERY_SCAN_LIMIT` | 4 项 | 每轮最多扫描 4 项；允许 1–8 项。 |
| `ANALYSIS_DELIVERY_INTERVAL_SECONDS` | 60 秒 | Beat 协调节拍；允许 60–3,600 秒。 |
| `ANALYSIS_DELIVERY_OPERATION_TIMEOUT_SECONDS` | 60 秒 | 无回执的 pending reservation 至少等待时间；允许 30–600 秒。 |
| `ANALYSIS_DELIVERY_BACKOFF_BASE_SECONDS` / `MAX` | 60 / 900 秒 | 退避基数和上限；范围 1–3,600 / 60–86,400 秒，max 不小于 base。 |

首次 reservation 增加 task 级 `delivery_operation_count`，不消耗 retry。每个之后被接受的唯一补投 reservation 在同一事务原子增加 `delivery_retry_count`，即使发布前后进程退出或结果未知也不退费；默认最多 9 次发送 reservation（1 次首次 + 8 次补投）。扫描、重放、重复请求、正常排队以及只读取已有发送状态不扣次数。`TaskDelivery.operation_generation` 按 attempt 递增，不替代 task 级补投计数。

活跃期限只计算能够推进投递的时间。可信 execution 占位、停止确认阻断和 `queue_order_wait` 均暂停并顺延 deadline，因此长视频正常占位不会误耗投递期限。退避为 `min(max, base × 2^retry_count)`，默认依次 60、120、240、480、900 秒并封顶；判定显式传入时间，不用 Redis ETA/countdown 保存长期状态。retry 次数或未阻断期限耗尽后，task / attempt / delivery 进入 failed，记录 `ANALYSIS_DELIVERY_RETRIES_EXHAUSTED` 或 `ANALYSIS_DELIVERY_DEADLINE_EXPIRED` 并撤销领取资格。预算与执行失败自动恢复、总结请求额度独立；人工恢复、新 attempt 和 attempt 序号不重置预算。

扫描默认 60 秒、单轮最多 4 项与已有上传回收规模及维护 Worker 并发 1 相配；pending 超时与扫描间隔相同，避免可能仍在正常确认的操作马上重复发送。8 次补投给短暂 broker/Worker 故障保留有限恢复机会；退避最高 15 分钟控制持续故障时的消息频率；24 小时活跃期限为投递异常设置明确人工边界，同时暂停正常阻断等待。所有参数可配置且有硬范围。

## 自动化验证

最终后端全量测试在专用数据库 `videoclip_mvp_final_stage4_concurrency_test` 运行，数据库凭据来自本机私密 Stage 2 配置，不记录在本文件。直接分析与维护相关测试在最终代码上单独复跑。

| 检查 | 结果 | 证据/说明 |
| --- | --- | --- |
| 直接测试：`pytest tests/test_analysis.py tests/test_maintenance_worker.py -q` | 54 passed；1 条既有 Starlette / AnyIO 弃用警告 | `data/mvp-stage-4/test-output/20261003T131925Z-direct-final/pytest.log`。覆盖投递代次、reservation 提交后退出、确认写回丢失、HTTP/扫描竞争、删除/投递竞争、稳定排序、预算与期限、人工恢复、execution advisory 门禁和维护锁。 |
| 后端全量：`pytest -q` | 117 passed；同一弃用警告 | `data/mvp-stage-4/test-output/20261003T132121Z-full-backend-final-gate/pytest.log`，使用专用 Stage 4 隔离数据库。 |
| Stage 0 回归：`pytest probes/stage0/tests -q` | 32 passed | `data/mvp-stage-4/test-output/20261003T132843Z-stage0-final-gate/pytest.log`。 |
| Ruff | 通过 | 覆盖 `src tests migrations scripts/stage1 scripts/stage2 scripts/stage4 probes/stage0`。 |
| mypy | 45 source files，0 errors | 使用 `--explicit-package-bases`。 |
| `pip check` / `compileall` | 均通过 | 无 broken requirements；Python 源码与迁移/脚本可编译。 |
| 前端 ESLint / Vitest / build | 均通过；4 files、13 tests；TypeScript 和 Vite build 通过 | `data/mvp-stage-4/test-output/20261003T125540Z-frontend-final/`。 |
| Playwright 队列状态页面 | 1 passed | 真实链路运行内触发；页面看到排队原因及下次协调时间，未显示不可执行的人工恢复入口。每轮使用独立输出目录。 |
| Compose / Alembic | Stage 2 `config --quiet` 通过；迁移部署后 `alembic check` 为 `No new upgrade operations detected.` | 当前 revision `b4c91e7a2d60 (head)`。 |

直接和全量后端测试命令（仓库 PowerShell 根目录；运行前应设置本地测试数据库连接变量，不能指向业务库）：

```powershell
$env:PYTHONPATH = (Resolve-Path 'src').Path
$env:VIDEOCLIP_TEST_DB_NAME = 'videoclip_mvp_final_stage4_concurrency_test'
$env:VIDEOCLIP_TEST_DATA_DIR = (Join-Path (Resolve-Path '.').Path 'data\mvp-stage-4\test-data')
& .\data\mvp-stage-2\.venv\Scripts\python.exe -m pytest tests\test_analysis.py tests\test_maintenance_worker.py -q
& .\data\mvp-stage-2\.venv\Scripts\python.exe -m pytest -q
& .\data\mvp-stage-0\.venv\Scripts\python.exe -m pytest probes\stage0\tests -q
& .\data\mvp-stage-2\.venv\Scripts\python.exe -m ruff check src tests migrations scripts\stage1 scripts\stage2 scripts\stage4 probes\stage0
& .\data\mvp-stage-2\.venv\Scripts\python.exe -m mypy --explicit-package-bases src migrations scripts\stage1 scripts\stage2 scripts\stage4 probes\stage0
& .\data\mvp-stage-2\.venv\Scripts\python.exe -m pip check
& .\data\mvp-stage-2\.venv\Scripts\python.exe -m compileall -q src migrations scripts\stage1 scripts\stage2 scripts\stage4 probes\stage0
```

前端最终检查在 `frontend/` 执行：

```powershell
npm run lint
npm test -- --run
npm run build
```

## 隔离真实 Beat → Redis → maintenance Worker → analysis Worker 验收

复现命令：

```powershell
.\scripts\stage2\run-queue-coordination-acceptance.ps1
```

该入口创建新 Compose 项目与空数据库、Redis、随机账户、随机密钥和受控 HTTPS ASR/LLM 替身；Playwright 每轮创建单独输出目录。最终通过运行 ID `20261003t132424z-22d025a4`，事件和 JSON 位于 `data/mvp-stage-4/acceptance/20261003t132424z-22d025a4/output/`，结果文件为 `acceptance.json`。该目录被 Git 忽略，不进入提交。

真实链路观察结果：

1. task A 经分析 Worker 领取并到达本地 ASR 替身，PostgreSQL 有且只有一个未确认、绑定 Linux session 的 execution。A 占位期间，B、C 仍通过 API 创建并显示 `execution_slot_occupied`，有持久 `delivery_next_check_at`；两者补投数为 0。再次提交 B 返回同一 task。
2. 故意先发送 C 消息。analysis Worker 将它消费并 ACK 为 `ignored_not_eligible`；C 仍有 `claim_count=0`，数据库保留其后续协调时间。浏览器核验排队原因、下次检查和不可用恢复操作。
3. 释放本地 stub 后 A 成功完成并写入可信停止确认。随后停止已空闲的隔离 analysis Worker，等待 Beat 和 maintenance Worker 将队列最早的 B 发布到 Redis 且保持未领取；此时 PostgreSQL 仍将 C 保持在 `queue_order_wait`。重启 analysis Worker 后 B、C 均在无用户二次提交的情况下完成，领取顺序与 PostgreSQL `(queue_entered_at, task.id)` 排序一致。
4. 三个 attempt 均 `claim_count=1`、最终成功，三条 delivery 各只有 generation 1，retry 数均为 0。ASR 和 LLM 各有 3 次本地替身请求；全程最大未确认 execution 为 1，结束时 unconfirmed/running 为 0；`history_rows_modified=0`。

同一验收 JSON 保存 task、attempt、queue / claim 时间、delivery generation、最终状态和 stub 请求计数。事件日志含 `tasks_queued_behind_a`、`out_of_order_message_injected`、`out_of_order_message_acknowledged`、`analysis_worker_stopped_idle`、`sent_but_unclaimed`、`analysis_worker_restarted_for_delivery` 及 `acceptance_passed`。

以下发送不确定与竞争故障由 PostgreSQL 集成测试覆盖，而非用 Celery publisher 成功冒充最终领取：

- `test_database_commit_before_publish_exit_leaves_pending_delivery`：reservation 提交后在发送前退出，待 pending 超时后重新协调。
- `test_successful_publish_with_lost_confirmation_stays_pending` 与 `test_stale_delivery_result_cannot_overwrite_a_newer_generation`：发送已成功但确认未写回、旧 generation 迟到回执均可安全补偿且不能覆盖当前状态。
- `test_http_submission_and_due_scanner_race_is_generation_safe`：HTTP 与 due scanner 并发，操作代次核验阻止旧发布结果覆盖新状态。
- `test_delivery_reservation_contends_on_the_worker_execution_gate`：reservation 和 Worker claim 争用同一 PostgreSQL advisory key；锁占用时不发消息、不扣 retry，释放后可完成首次投递。
- `test_delivery_retries_are_idempotent_bounded_and_independent_of_execution_budget`、deadline 暂停/耗尽测试：只接受唯一 reservation 时计数；重复扫描、幂等重放、正常执行位等待不扣费；耗尽关闭资格且不改总结请求额度。
- `test_deletion_invalidates_an_inflight_delivery_and_its_late_message`、`test_submit_and_delete_race_has_one_winner`、`test_attempt_can_be_claimed_only_once_even_after_lock_release`：删除/发送竞争、迟到消息和重复领取均不能重激活失效 attempt。
- `test_delivery_scan_uses_stable_queue_order_not_message_order` 和 maintenance 的独立 Redis TTL / PostgreSQL advisory lock 测试：消息顺序不取代公平性；过期锁可恢复，重复扫描不会并行发起冲突操作。
- `test_manual_recovery_is_idempotent_and_old_attempt_messages_are_rejected`：当前 attempt 改变后旧消息被拒绝；预先持有的任务级补投预算和 deadline 保留。

第 3 窗口既有真实 Linux session 故障验收继续证明数据库连接丢失、监督器/容器异常、身份缺失或不可验证证据时全局占位及危险清理门禁保持；第 4 窗口未改变其受支持边界。证据见[第 3 窗口验收记录](../final-stage-3/validation.md)。第 2 窗口真实 Beat→Redis→maintenance Worker 临时上传回收及失败重试证据见[第 2 窗口验收记录](../final-stage-2/validation.md)。

## Stage 2 迁移和线上维护链路

部署前只读状态证据为 `data/mvp-stage-4/test-output/20261003T-stage2-predeploy/stage2-before.json`。预检确认 Stage 2 API 健康、迁移 `f71a0d8c2e31`，未确认 execution/running task/analysis 队列均为 0。随后只执行现有保护入口：

```powershell
.\scripts\stage2\run-stage2.ps1
```

脚本成功从 `f71a0d8c2e31` 迁移到 `b4c91e7a2d60`，重建并启动恰好一个 API、`analysis_supervisor` Worker、仅订阅 `maintenance` 且并发 1 的 maintenance Worker 和一个 Beat。API `/health` 为 `ok`，PostgreSQL/Redis healthy，Compose 配置和 `alembic check` 通过。

部署后快照：`data/mvp-stage-4/test-output/20261003T-stage2-postdeploy-final/stage2-after.json`。历史行保持 users 6、uploads 8、videos 8、tasks `succeeded:2 / failed:4 / awaiting_summary:2`、attempts 14、executions 14、deliveries 14、transcript segments 330、summary requests 12、published summaries 2；unconfirmed/running/analysis queue 均为 0。8 条历史 task 的全部 Stage 4 协调列仍为 NULL，14 条历史 delivery 的 `operation_generation` 仍为 NULL。因此旧 `failed` / `awaiting_summary` 没有自动进入队列、没有按 attempt 序号回填补投数，旧身份和结果没有改写。

部署后实测 Beat→Redis→maintenance Worker 周期正常轮转：Beat 分别投递 `reclaim-expired-resumable-uploads` 与 `coordinate-analysis-deliveries`；维护 Worker 记录 `reclaim_cycle_finished status=completed reclaimed=0` 和 `analysis_delivery_cycle_finished status=completed scanned=0 published=0 blocked=0 exhausted=0`。Stage 2 没有可回收到期上传或显式入队的新 task，所以扫描为 0；第 2 窗口的真实隔离上传回收/失败重试仍是周期回收的功能验收证据。服务保持运行，未做停机清理。

## 限制和下一步

- 投递达到 8 次补投或 24 小时可推进期限后进入投递失败，需要人工处理；本窗口没有对耗尽任务提供执行恢复按钮，也不创建 execution retry attempt。
- Redis/Celery 外部发布可能重复；本设计保证 PostgreSQL 领取门禁下最多一次当前 attempt claim，不承诺 broker exactly-once。发送 `sent` 仍只表示 publisher 接受，不表示已领取。
- 停止证明不能覆盖没有 Stage 2 Linux session 绑定、监督器/容器/宿主机强停或不可验证证据；这些仍须人工处置。窗口 4 没有将上述等待绕过来推进队列。
- Stage 0 尚未验证事项继续保留：完整 20 分钟真实模型处理、账户计费、容器可写层处理中峰值和 E 盘空闲差额归因。它们不是第 5 窗口的前置安全门禁。
- Stage 5“共享限流与受控自动恢复”可在独立范围/spec/验收约束下开始；本次没有启动该窗口。

## Git 交付

实现与测试的独立逻辑提交为 `99340d5a91a3aac56128f4d5480ffb20fbcb1f02`（`feat: 实现持久化排队与投递补偿`）。产品、技术和运行文档单独按已验证结果提交；运行证据、随机凭据、课程媒体和模型响应均不纳入 Git。
