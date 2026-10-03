# 最终版第 2 窗口验收记录

> 状态：第 2 窗口范围已实现并完成验证；可进入第 3 窗口“可信停止确认”。本窗口没有开始第 3 窗口工作。
> 日期：2026-10-03。

## 基线与运行环境

- 开始时工作区干净，当前分支 codex/mvp，HEAD 为第 1 窗口提交 84cd5bb。没有重置或切换分支。
- Stage 2 原有 PostgreSQL、Redis、API 和单并发分析 Worker 按既有 E 盘数据目录运行。窗口实现后由 scripts/stage2/run-stage2.ps1 部署 Beat 与独立维护 Worker，API 健康检查通过。
- 当前地址：浏览器 http://127.0.0.1:5174，Stage 2 API 同源代理至 http://127.0.0.1:58002。维护任务每 60 秒调度，单次默认最多扫描 4 个会话，配置硬上限为 8；常规会话有效期仍为 24 小时。
- 第 0 窗口的容器可写层处理中峰值、E 盘空闲差额归因、账户计费及完整 20 分钟模型处理保持未验证。本窗口未调用 ASR/LLM、未提交分析任务。

## 浏览器上传实现

默认页面通过第 1 窗口分片接口上传，普通 /videos 上传兼容接口保留。@noble/hashes 精确锁定 1.8.0；Web Worker 逐片增量计算完整 SHA-256 与分片 SHA-256。按账户隔离的恢复元数据只含文件名、长度、摘要、幂等键、会话 ID 和时间，不含文件/凭据。

每次创建、分片或完成响应不确定时先读取服务端状态；仅用 confirmed_offset 推进。页面分开显示摘要、当前请求传输、服务端已确认偏移及完成校验。暂停保留会话，放弃调用 DELETE；202 保留待清理状态，204 才清除。错误摘要不接续旧会话；完成响应丢失后按状态确认并幂等得到同一视频。

自动化测试覆盖摘要与分片边界、创建响应丢失后的幂等键复用、服务端确认偏移/分片响应丢失恢复、同名同长错误文件、账户隔离、暂停/放弃、202/204、重复完成和完成响应丢失。增加媒体流客户端断开后关闭文件句柄的回归测试；真实浏览器删除视频后，API 无已删除媒体文件句柄。

## 周期回收真实链路

部署包含单 Beat、仅订阅 maintenance 的维护 Worker 和仅订阅 analysis 的单并发分析 Worker。Beat 文件绑定至 E 盘 data/mvp-stage-2/app/maintenance-beat-schedule。Beat→Redis→维护 Worker 的专用隔离验收通过：

- Beat 发布 2 次，维护 Worker 确认接收；首个到期周期只清理无活跃租约会话，跳过持有文件租约的会话，并保留注入的清理失败状态。
- 维护 Worker 在中间重启后，下一次周期把待清理失败记录重新扫描并清除。失败意图为 cleanup_failed 时保留到后续周期；验收核对会话与 .part 文件最终移除。
- 三个会话均为本验收新建的随机账户 1 字节 fixture；只对这些 fixture 注入到期时间，并将一个 fixture 路径临时改为目录以制造清理失败。没有改写历史账户、视频、task、attempt、转写或总结数据。
- 稳定租约标记文件仍为 1 字节且未删除。分析队列前后深度均为 0，分析未确认 execution 为 0；视频/task/attempt/转写/总结计数在本验收前后不变。测试账户在会话清理后通过“无视频且无上传”条件删除。
- 实际运行输出：{"result":"PASS","beat_schedule_sends_observed":2,"first_cycle_reclaimed":1,"retry_cycle_reclaimed":2,"active_lease_skipped_then_reclaimed":true,"cleanup_failure_retained_then_retried":true,"lease_markers_preserved":true,"analysis_queue_depth":0,"unconfirmed_analysis_executions":0,"test_account_removed":true}。维护 Worker 收到任务计数包含调度基线日志窗口。

## 测试、构建与配置检查

已运行并通过：

- 后端直接测试：`pytest tests/test_maintenance_worker.py tests/test_resumable_upload.py tests/test_upload_video.py -q`，32 passed，含 1 条既有 Starlette/AnyIO 弃用警告。
- 后端全量 pytest：`pytest -q`，102 passed，含同一既有弃用警告。
- Stage 0 回归：32 passed。
- Ruff 全仓适用路径检查、mypy（35 个源文件）、pip check 通过。
- 前端 ESLint、Vitest（13 tests/4 files）、Vite 生产构建通过。
- Stage 1 与 Stage 2 Compose config --quiet 通过；Stage 2 alembic check 返回 No new upgrade operations detected；部署后 API health 为 ok。
- scripts/stage2/run-stage2.ps1 检查恰好一个 Beat、分析 Worker、维护 Worker，且两个 Worker 分别只订阅各自队列、并发为 1。

主要命令（仓库根目录 PowerShell；前端命令在 `frontend`）：

```powershell
$env:PYTHONPATH = 'src'
& .\data\mvp-stage-2\.venv\Scripts\python.exe -m pytest tests\test_maintenance_worker.py tests\test_resumable_upload.py tests\test_upload_video.py -q
& .\data\mvp-stage-2\.venv\Scripts\python.exe -m pytest -q
& .\data\mvp-stage-2\.venv\Scripts\python.exe -m pytest probes\stage0\tests -q
& .\data\mvp-stage-2\.venv\Scripts\python.exe -m ruff check src tests migrations scripts\stage1 scripts\stage2 scripts\stage4 probes\stage0
& .\data\mvp-stage-2\.venv\Scripts\python.exe -m mypy --explicit-package-bases src migrations scripts\stage1 scripts\stage2 scripts\stage4 probes\stage0
& .\data\mvp-stage-2\.venv\Scripts\python.exe -m pip check
docker compose --env-file data\mvp-stage-1\.env -p videoclip-mvp-stage1 -f compose.yaml config --quiet
docker compose --env-file data\mvp-stage-2\.env -p videoclip-mvp-stage2 -f compose.yaml config --quiet
docker compose --env-file data\mvp-stage-2\.env -p videoclip-mvp-stage2 -f compose.yaml exec -T api alembic check
```

## 浏览器续传验收

测试输入：有权使用的 `video/agent-skill介绍.mp4`，3,221,243,133 字节；Chromium 真实浏览器通过 Playwright 验收。最终通过命令从 `frontend` 运行：

```powershell
$env:MVP_RESUMABLE_VIDEO = (Resolve-Path '..\video\agent-skill介绍.mp4').Path
$env:MVP_PLAYWRIGHT_OUTPUT_DIR = '../data/mvp-stage-2/acceptance/playwright-window2-final-959d646256e14924836d53908b9c76cc'
npm run e2e -- e2e/resumable-upload.spec.ts
```

结果：`1 passed (6.1m)`。在服务端 `confirmed_offset=8,388,608` 时暂停并刷新；重新选择同名、同长度但首字节不同的文件后，客户端拒绝接续旧会话并放弃独立的新会话；重新选择原文件返回原 `upload_id` 且 `replayed=true`。服务端最终确认偏移为总长，原会话 chunk 0 只发送一次；一个账户视频列表中只登记一个目标视频。鉴权播放器加载元数据并播放时间前进，三个 Range（起始、8 MiB 边界、文件尾）均返回 206，返回字节与原文件对应位置一致。最后通过页面“删除记录”完成删除，播放器卸载且列表为空。

最终运行证据目录为 `data/mvp-stage-2/acceptance/playwright-window2-final-959d646256e14924836d53908b9c76cc/`，保留 Playwright 最近运行信息和 `test-account.txt`（仅随机测试用户名，无凭据）。验收后按“无视频且无 upload_handoff”条件删除了该测试账户。Stage 2 视频/task/attempt/转写/总结计数保持 `8/8/14/330/2`，未确认 analysis execution 为 0，analysis 与 maintenance 队列深度均为 0。

验收曾发现媒体播放器断开后服务端可能保留已 unlink 的文件句柄；已在响应取消/异常/结束路径显式关闭文件，并用单测及最终 `/proc/1/fd` 检查确认修复。最终运行前 E 盘剩余 140,684,238,848 字节；上传错误文件副本和原始 `.part` 同时存在时观测峰值约占用 6 GiB，E 盘剩余仍约 134.2 GB。删除后可用空间回到 140,683,960,320 字节（与运行前差约 0.27 MiB），没有遗留已删除媒体句柄。

验收迭代中本机 Vite 代理曾对个别请求报告 TCP connect timeout；客户端先查服务端确认偏移后继续，最终整次上传和媒体读取验收通过。最终验收代码的 Range 取证仅对连接错误/5xx 做最多 4 次、每次最多 15 秒的短重试；确定性 4xx 仍直接失败。

## 资源与运行方式

Beat 空闲实测约 69 MiB，因此容器上限为 192 MiB；维护 Worker 上限 384 MiB。验收后空闲样本：API 68.69 MiB / 1 GiB、analysis Worker 86.31 MiB / 1 GiB、maintenance Worker 102.3 MiB / 384 MiB、Beat 69.15 MiB / 192 MiB。维护 Worker 和 Beat 的 PID 上限各 64、JSON 日志轮转各 10 MiB × 2；analysis Worker 仍为并发 1 / PID 上限 128 / 日志 10 MiB × 2。四个 Stage 2 容器共享一个约 938,461,484 字节后端镜像；新服务没有单独复制后端镜像。Beat 调度文件在 E 盘项目数据目录。运行期间未清理其他 Docker 项目，也未用全局 Docker 磁盘占用推算项目归属。

复现方式：先在仓库根目录运行 `scripts/stage2/run-stage2.ps1`，再在 `frontend` 运行 `npm run dev`；地址为 http://127.0.0.1:5174，API 为 http://127.0.0.1:58002。停止前端开发服务器用 Ctrl+C；根目录运行 `scripts/stage2/stop-stage2.ps1` 停止 Stage 2。实际停止脚本按 Beat、维护 Worker、analysis Worker、API、Redis、PostgreSQL 顺序成功执行，并在未确认 execution 数为 0 时通过既有安全门禁；随后再次运行启动脚本，最终健康检查 `/health` 为 `ok`，API、两个 Worker、Beat、Redis 与 PostgreSQL 均保持运行。

## 范围边界与后续

可以进入第 3 窗口“可信停止确认”。窗口 2 没有实现监督器或可信停止证明、分析任务补投/排队协调、共享模型冷却、分析自动恢复、原视频与笔记分离删除或最终版执行时间线。第 0 窗口的容器可写层处理中峰值、E 盘空闲差额归因、账户计费及完整 20 分钟模型处理仍未验证；这些不阻塞可信停止确认窗口。本窗口没有提交 analysis 任务或调用 ASR/LLM。
