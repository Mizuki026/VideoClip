# 课迹：课程视频转写与可追溯总结

课迹是一个本地运行的中文课程视频学习工作台。它把视频语音转成分段文本、章节和知识要点，并为总结提供可回到原视频核对的引用。当前 MVP 使用 FastAPI、PostgreSQL、Redis/Celery、FFmpeg、React 和 TypeScript；浏览器从 `http://127.0.0.1:5174` 同源访问 Stage 2 API（`127.0.0.1:58002`）。

分析只处理语音，不理解画面、屏幕代码或图表；工程校验能验证引用来源和时间定位，不能保证总结语义完整。项目用于本地演示，不提供公开部署。

## 环境与私密配置

- Windows PowerShell、已启动的 Docker Engine 与 Compose。
- Node.js 24+、npm 11+。前端依赖由 `frontend/package-lock.json` 锁定。
- Stage 2 私密配置在 `data/mvp-stage-2/.env`，PostgreSQL 密码在同目录的 `secrets/` 下；两者不应提交到 Git。若 Stage 2 配置尚不存在，需先按 Stage 0 流程准备本地 `data/mvp-stage-0/.env`，确认账户模型可用后运行 `scripts/stage2/prepare-stage2.ps1`。若配置已经存在，保留它，不要重新生成数据库凭据。
- 选择你有权使用的课程视频。媒体时长必须大于 0 且不超过 20 分钟；当前不限制上传字节数，也不承诺业务数据目录总量上限。浏览器默认使用分片续传：中断后刷新页面并重新选择原文件，页面会重新计算摘要并从服务端确认偏移续传；普通上传接口仍保留供旧调用方使用。

真实模型调用使用本机私密配置中的 SiliconFlow 凭据和模型。2026-10-05 官方价格中心将 SenseVoiceSmall 标为免费，Qwen3.5-4B 的官方上线公告称免费；本机账户配置和调用前账单截图也显示免费/¥0.00。本次真实调用后账户账单与剩余额度尚未取得证据；任何后续真实调用前仍需核对账户页面。项目不会读取账单或账户余额。

Stage 1 与 Stage 2 使用独立数据库、端口和 E 盘数据目录。演示使用 Stage 2；仓库脚本只操作对应命名的 Compose 项目。

## 架构与数据流

React 页面由 Vite 提供服务，浏览器只访问同源 `/api`；FastAPI 负责 Cookie / CSRF 鉴权、普通及分片视频上传、状态查询和人工恢复。视频文件保存在 E 盘目录，经鉴权媒体接口和 HTTP Range 提供播放；PostgreSQL 保存账户、task / attempt、冻结配置、转写检查点、队列资格、投递代次和唯一发布的总结。Celery Beat 在单实例中周期投递临时上传回收和分析队列协调任务到 `maintenance` 队列。维护 Worker 只扫描 PostgreSQL 并发起投递，不领取分析任务、不调用模型；Redis 消息不是业务事实源。分析 Worker 保持并发 1、prefetch 1，在领取时按 PostgreSQL 队列顺序检查视频、task、当前 attempt、全局未确认 execution 及可信停止门禁。每次新 execution 绑定 Worker 实例代次、容器及 Linux 受控 session；FFmpeg、ASR 和 LLM 本地请求子进程留在此 session 内。监督器在确认 session 的全部本地进程退出后才提交停止证据。分析 Worker 调用 FFmpeg 提取并切分音频，调用 ASR 后先持久化每段检查点，再调用 LLM。总结只有通过结构、来源、原文和时间范围校验后才发布。页面区分排队、投递待确认、执行位占用、停止确认阻断和投递预算耗尽；只有符合原有执行失败恢复条件的任务才提供人工恢复，并复用匹配的转写检查点。

## 本地演示

从仓库根目录启动 Stage 2。该脚本构建/复用项目镜像、应用迁移、启动 PostgreSQL、Redis、API、一个分析 Worker、一个维护 Worker 和单实例 Beat；替换 Worker 前会检查未确认 execution、运行中任务和 analysis 队列为空，再检查健康状态、监督入口、队列和并发配置：

```powershell
.\scripts\stage2\run-stage2.ps1
```

在第二个 PowerShell 窗口启动网页：

```powershell
Set-Location frontend
npm ci
npm run dev
```

打开 [http://127.0.0.1:5174](http://127.0.0.1:5174)，按以下顺序演示：

1. 在注册 / 登录页创建本地账户并登录。
2. 在视频列表页上传授权课程视频。中断后刷新并重新选择原文件即可从服务端已确认的分片续传；确认上传后再开始分析并查看处理进度。
3. 在视频详情页查看转写和已发布总结。点击至少两处不同引用，确认播放器跳到相应片段并继续播放。
4. 若要保留笔记，在详情页选择“删除原视频，保留笔记”并确认。处理中会显示等待停止确认或清理状态；媒体不可用后播放器与回看入口关闭，但转写、总结引文与时间信息继续保留。清理失败只能重试清理。
5. 若要删除全部内容，选择“删除整条记录”并确认；删除完成后，该记录的视频、转写和总结接口均不可访问。

Stage 7 交付后的启动/迁移/停止、正常流程和隔离故障演示步骤见[本地演示指南](.scratch/final-stage-7/demo-guide.md)。本次演示创建的账户和视频已经清理；Stage 2 保留其既有 6 个本地账户、8 条视频/笔记。真实样片、账户凭据、原文和模型响应只保存在 Git 忽略的 `data/mvp-stage-2/acceptance/`，不作为公开演示材料。

总结会在 ASR 片段全部持久化后生成。新 execution 在领取提交后、开始任何分析副作用前，必须完成监督登记。受支持的停止边界为 Linux Worker 容器中的 execution session 及其子进程组，包括当前使用的 FFmpeg、ASR 和 LLM 子进程；监督器先回收并观察整个 session 退出，再按绑定身份提交证据。心跳超时、父进程退出、数据库锁释放、PID 不存在或服务重启本身都不能确认停止。监督器、容器或宿主机强停、边界身份不匹配时执行位继续保留，状态显示“停止尚未确认” / `STOP_CONFIRMATION_REQUIRED`，恢复和工作目录清理均被阻断。只有 Worker 容器已退出且 execution 的代次和容器 ID 精确匹配时，人工入口才登记停止确认。

若总结请求失败，只有执行停止已确认且 task 仍符合恢复条件时，页面才提供人工恢复；恢复尝试使用 summary-only 模式并复用匹配的成功转写片段。每个 task 最多 3 次总结请求。Stage 4 已实现不同视频任务持久排队、发送结果不确定时的补偿，以及忙时消息被 ACK 后的周期续接；补投不会自动重跑已开始的 execution，也不改变总结请求额度。Stage 5 已实现共享模型冷却与受控自动恢复：默认本地退避为 60–900 秒，单次等待上限 24 小时、task 累计等待上限 72 小时，自动恢复最多 2 次；供应商给出的较长合法 `Retry-After` 不会被本地截短，超出等待上限则转人工。总结请求预算仍跨全部 attempt 共 3 次；Stage 4 的 8 次补投与 24 小时可推进期限保持独立。冷却仅按供应商、非秘密账户引用和模型共享；密钥轮换不应更换账户引用。迁移不自动纳入历史 `failed` / `awaiting_summary`，只有新任务及部署后明确接受的人工操作加入 Stage 5。供应商已收到的请求不能由本地停止证明撤销，HTTP 请求也不承诺恰好一次。参数、错误白名单、历史策略和验证证据见[Stage 5 规格](.scratch/final-stage-5/spec.md)及[验收记录](.scratch/final-stage-5/validation.md)；停止证明边界见[Stage 3 验收记录](.scratch/final-stage-3/validation.md)，队列和补偿语义见[Stage 4 规格及验收](.scratch/final-stage-4/spec.md)与[验收记录](.scratch/final-stage-4/validation.md)。Stage 6 已实现独立原媒体删除、笔记保留及失败重试；接口状态和验证证据见[规格](.scratch/final-stage-6/spec.md)与[验收记录](.scratch/final-stage-6/validation.md)。这些阶段的本地替身验收不代表真实供应商限额或账单。

停止网页用 `Ctrl+C`。确认没有仍在运行的分析后，在仓库根目录运行：

```powershell
.\scripts\stage2\stop-stage2.ps1
```

该脚本先停止 Beat 和维护 Worker，再停止 Stage 2 分析 Worker并检查未确认 execution；如果发现未确认记录，会保留数据库和 Redis 并拒绝继续停止。只有状态页明确显示本次 Stage 2 execution 未确认停止时，才使用当前任务返回的真实 execution UUID 执行 `scripts/stage2/stop-and-confirm-worker.ps1 -ExecutionId <UUID>`。该入口校验绑定的 Worker 实例代次、精确容器 ID，并要求 Docker 报告同一容器已退出；历史 execution 缺少新身份时不会补造确认。之后按页面资格执行人工恢复。不要用 PID、心跳或数据库锁状态代替停止确认。

## 检查与真实浏览器验收

前端检查（在 `frontend/` 下执行）：

```powershell
npm ci
npm run lint
npm test
npm run build
```

浏览器续传验收不调用模型。使用有权访问的授权视频、已运行且配置正确的 Stage 2；Playwright 会启动构建预览服务器并占用 5174，运行前先停止 `npm run dev` 或其他占用该端口的本地服务器。每次运行指定新的输出目录：

```powershell
Set-Location frontend
npm run build
$env:MVP_RESUMABLE_VIDEO = (Resolve-Path '..\video\agent-skill介绍.mp4').Path
$env:MVP_PLAYWRIGHT_OUTPUT_DIR = Join-Path '..\data\mvp-stage-2\acceptance' ('playwright-' + [guid]::NewGuid().ToString('N'))
npm run e2e -- e2e/resumable-upload.spec.ts
Remove-Item Env:MVP_RESUMABLE_VIDEO, Env:MVP_PLAYWRIGHT_OUTPUT_DIR
```

模型端到端场景另需授权样片、可用的真实模型凭据。以下命令会调用 ASR/LLM、在成功后重启 Stage 2 API/Worker，并清理测试账户及视频：

```powershell
npm run build
$env:MVP_ACCEPTANCE_VIDEO = (Resolve-Path '..\video\如何选择适合自己的agent.mp4').Path
$env:MVP_STAGE6_RESTART_SERVICES = '1'
npm run e2e -- e2e/stage5-flow.spec.ts
```

若本机没有该样片，请把 `MVP_ACCEPTANCE_VIDEO` 指向你有权使用的本地视频。不要将视频或转写复制进 Git。

后端集成测试默认使用 Stage 1 PostgreSQL 上独立的 `videoclip_mvp_stage1_test` 测试库，不清空 Stage 1 业务库；运行默认测试前需按本机私密配置启动 Stage 1。最终版 Stage 3 使用独立的 `videoclip_mvp_final_stage3_test` 与 `data/mvp-stage-3/test-data`，可按以下变量将测试指向该隔离环境：

```powershell
$env:VIDEOCLIP_TEST_DB_NAME = 'videoclip_mvp_final_stage3_test'
$env:VIDEOCLIP_TEST_DATA_DIR = (Join-Path (Resolve-Path '.').Path 'data\mvp-stage-3\test-data')
```

Stage 4 后端全量测试使用独立 PostgreSQL 数据库 `videoclip_mvp_final_stage4_concurrency_test` 和 `data/mvp-stage-4/test-data`。测试前按本机 Stage 2 私密配置提供 `DB_HOST`、`DB_PORT`、`DB_USER`、`DB_PASSWORD_FILE` 等连接变量并启动其 PostgreSQL；确认测试库已单独创建，再设置：

```powershell
$env:VIDEOCLIP_TEST_DB_NAME = 'videoclip_mvp_final_stage4_concurrency_test'
$env:VIDEOCLIP_TEST_DATA_DIR = (Join-Path (Resolve-Path '.').Path 'data\mvp-stage-4\test-data')
```

真实 Beat→Redis→maintenance Worker→analysis Worker 队列验收使用全新隔离 Compose 项目、数据库、Redis、账户和本地 HTTPS 模型替身，不连接真实模型供应商，也不触碰 Stage 2 业务库。需要 Docker、Node/npm 和仓库根目录 `.venv`；每次 Playwright 运行使用独立输出目录，验收数据和日志保存在 Git 忽略的 `data/mvp-stage-4/acceptance/<run-id>/`：

```powershell
.\scripts\stage2\run-queue-coordination-acceptance.ps1
```

该脚本在成功时只对已空闲的隔离服务执行正常停止；失败时保留隔离栈和输出供检查。可直接运行，不需要先部署或清空 Stage 2。

生产入口真实进程故障验收使用 Stage 2 和本地模型替身，不会调用供应商模型；每次输出单独写入被 Git 忽略的目录：

```powershell
& .\data\mvp-stage-2\.venv\Scripts\python.exe scripts\stage2\stop-confirmation-acceptance.py
```

只在可接受 Stage 2 Worker、PostgreSQL 及 Redis 按验收流程被强停/恢复时运行。脚本先验证业务库没有未确认 execution、运行 attempt 或 analysis 队列项，并只创建随机隔离 fixture；结束后恢复 Stage 2 并核对前后数据库快照。

通用仓库根目录检查命令：

```powershell
$env:PYTHONPATH = (Resolve-Path 'src').Path
& .\data\mvp-stage-2\.venv\Scripts\python.exe -m pytest -q
& .\data\mvp-stage-0\.venv\Scripts\python.exe -m pytest probes\stage0\tests -q
& .\data\mvp-stage-2\.venv\Scripts\python.exe -m ruff check src tests migrations scripts\stage1 scripts\stage2 scripts\stage4 probes\stage0
& .\data\mvp-stage-2\.venv\Scripts\python.exe -m mypy --explicit-package-bases src migrations scripts\stage1 scripts\stage2 scripts\stage4 probes\stage0
& .\data\mvp-stage-2\.venv\Scripts\python.exe -m pip check
```

运行中的 Stage 2 还可用以下命令检查 Compose 配置和迁移模型是否一致：

```powershell
docker compose --env-file data/mvp-stage-2/.env -p videoclip-mvp-stage2 -f compose.yaml config --quiet
docker compose --env-file data/mvp-stage-2/.env -p videoclip-mvp-stage2 -f compose.yaml exec -T api alembic check
```

## 当前边界

- 一个 Stage 2 分析 Worker 同时执行一个分析任务。Beat 与维护 Worker 周期执行临时上传回收、投递协调和有界恢复扫描，不领取分析任务或调用模型；Stage 4 补投次数默认 8 次（首次投递不计入），可推进的投递等待期限默认 24 小时，执行占位、停止确认阻断和队列公平等待期间暂停期限。Stage 5 的共享模型冷却按供应商/账户引用/模型持久化，默认退避 60–900 秒，单次等待 24 小时、累计 72 小时；执行失败自动恢复最多 2 次，总结请求仍最多 3 次。三类额度独立计数，历史失败任务不会由迁移自动启用恢复。停止确认由生产监督器和精确容器人工入口管理；等待状态本身不代替停止证据。
- 总结校验失败、输出截断或请求超时不会发布半成品。输出结构或引用校验可按冻结规则做有限修复；真实截断、网络超时等终态失败需停止确认后手动恢复。每个 task 的总结请求总数最多 3 次。
- 当前已实测的授权样本采用 H.264/AAC 且时间轴零起点；带延迟音轨或不能可靠映射的媒体会被拒绝。浏览器分片续传与 24 小时临时会话回收已按 Stage 2 验收；不提供通用转码、分析自动接管、画面理解、移动端或线上部署。
- 停止证明覆盖 Linux Worker 容器中绑定的 execution session 与当前受支持子进程组，不覆盖主动逃逸到新 session 的恶意本机进程。监督器、容器或宿主机突然终止时需人工核对精确容器退出；本地证据不撤销已送达供应商的请求。旧 execution 身份字段为 NULL 时保持阻断，不伪造回填或追溯确认。
- 20 分钟媒体边界 fixture 已验证：1200.0 秒接受，1200.01 秒拒绝；完整 20 分钟课程真实模型处理尚未运行。上传字节数和业务数据目录总量均没有运行期硬上限。
- Stage 7 本次真实浏览器新账户上传约 17 分 41 秒授权样片，36 段 ASR、1 次 LLM、合法引用、API/Worker 重启读回、媒体删除保留笔记和整条删除已按分段运行证据核验。完整 Stage 7 Playwright 单测没有一次全绿运行，账户调用后账单/额度也未取得，详见[验收矩阵](.scratch/final-stage-7/acceptance-matrix.md)及[验证记录](.scratch/final-stage-7/validation.md)。
- 37 个资源点覆盖真实处理期间；媒体目录删除后恢复到基线，未发现分析/维护容器持有被删媒体 FD。Docker 日志精确文件长度及 WSL VHDX 物理差额仍未知；本次实测 Docker 分类净变化为 MB 级。
- 本地零费率配置、官方免费模型说明和调用前 ¥0.00 截图不等同于调用后账单回执；实际账户费用和剩余额度仍需在服务商账户中核对。

MVP 的实际验收证据、历史故障和限制见[Stage 6 验收记录](.scratch/mvp-stage-6/validation.md)。最终版 Stage 1 至 Stage 6 的规格与验收记录见[Stage 1](.scratch/final-stage-1/validation.md)、[Stage 2](.scratch/final-stage-2/validation.md)、[Stage 3](.scratch/final-stage-3/validation.md)、[Stage 4](.scratch/final-stage-4/validation.md)、[Stage 5](.scratch/final-stage-5/validation.md)和[Stage 6](.scratch/final-stage-6/validation.md)。Stage 7 部分完成，逐项结论、命令和证据见[验收矩阵](.scratch/final-stage-7/acceptance-matrix.md)、[命令与证据索引](.scratch/final-stage-7/commands-and-evidence.md)、[验证记录](.scratch/final-stage-7/validation.md)及[公开脱敏展示材料](.scratch/final-stage-7/public-showcase.md)。
