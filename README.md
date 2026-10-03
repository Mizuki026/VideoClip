# 课迹：课程视频转写与可追溯总结

课迹是一个本地运行的中文课程视频学习工作台。它把视频语音转成分段文本、章节和知识要点，并为总结提供可回到原视频核对的引用。当前 MVP 使用 FastAPI、PostgreSQL、Redis/Celery、FFmpeg、React 和 TypeScript；浏览器从 `http://127.0.0.1:5174` 同源访问 Stage 2 API（`127.0.0.1:58002`）。

分析只处理语音，不理解画面、屏幕代码或图表；工程校验能验证引用来源和时间定位，不能保证总结语义完整。项目用于本地演示，不提供公开部署。

## 环境与私密配置

- Windows PowerShell、已启动的 Docker Engine 与 Compose。
- Node.js 24+、npm 11+。前端依赖由 `frontend/package-lock.json` 锁定。
- Stage 2 私密配置在 `data/mvp-stage-2/.env`，PostgreSQL 密码在同目录的 `secrets/` 下；两者不应提交到 Git。若 Stage 2 配置尚不存在，需先按第 0 窗口流程准备本地 `data/mvp-stage-0/.env`，确认账户模型可用后运行 `scripts/stage2/prepare-stage2.ps1`。若配置已经存在，保留它，不要重新生成数据库凭据。
- 选择你有权使用的课程视频。媒体时长必须大于 0 且不超过 20 分钟；当前不限制上传字节数，也不承诺业务数据目录总量上限。上传中断需重新上传整个文件。

真实模型调用使用本机私密配置中的 SiliconFlow 凭据和模型。零价格以用户账户配置为依据；运行前请自行核对账户模型可用性，项目不会读取账单或账户余额。

Stage 1 与 Stage 2 使用独立数据库、端口和 E 盘数据目录。演示使用 Stage 2；仓库脚本只操作对应命名的 Compose 项目。

## 架构与数据流

React 页面由 Vite 提供服务，浏览器只访问同源 `/api`；FastAPI 负责 Cookie / CSRF 鉴权、原始视频上传、状态查询和手动恢复。视频文件保存在 E 盘目录，经鉴权媒体接口和 HTTP Range 提供播放；PostgreSQL 保存账户、task / attempt、冻结配置、转写检查点和唯一发布的总结。任务经 Redis / Celery 投递给一个并发为 1 的 Worker；Worker 调用 FFmpeg 提取并切分音频，调用 ASR 后先持久化每段检查点，再调用 LLM。总结只有通过结构、来源、原文和时间范围校验后才发布。失败后由用户按状态页确认停止并手动恢复；匹配的转写检查点会复用。

## 本地演示

从仓库根目录启动 Stage 2。该脚本构建/复用项目镜像、应用迁移、启动 PostgreSQL、Redis、API 和一个 Worker，并检查健康状态与 Worker 数量：

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
2. 在视频列表页上传授权课程视频，开始分析并查看处理进度。
3. 在视频详情页查看转写和已发布总结。点击至少两处不同引用，确认播放器跳到相应片段并继续播放。
4. 在详情页删除视频记录；删除完成后，媒体、转写和总结接口不再提供该记录内容。

总结会在 ASR 片段全部持久化后生成。若总结请求失败，只有 Worker 和执行已被确认停止且 task 仍符合恢复条件时，页面才提供人工恢复；恢复尝试使用 summary-only 模式并复用匹配的成功转写片段。每个 task 最多 3 次总结请求。若执行停止未确认，不要接管、重试或删除文件。

停止网页用 `Ctrl+C`。确认没有仍在运行的分析后，在仓库根目录运行：

```powershell
.\scripts\stage2\stop-stage2.ps1
```

该脚本先停止 Stage 2 Worker，并检查未确认 execution；如果发现未确认记录，会保留数据库和 Redis 并拒绝继续停止。只有状态页明确显示本次 Stage 2 execution 未确认停止时，才使用当前任务返回的真实 execution UUID 执行 `scripts/stage2/stop-and-confirm-worker.ps1 -ExecutionId <UUID>`，再按页面提示恢复。不要用 PID 存活状态代替停止确认。

## 检查与真实浏览器验收

前端检查（在 `frontend/` 下执行）：

```powershell
npm ci
npm run lint
npm test
npm run build
```

真实端到端场景需要授权样片、已运行且配置正确的 Stage 2、可用的真实模型凭据。Playwright 会自行启动构建预览服务器并占用 5174；运行前先停止 `npm run dev` 或其他占用该端口的本地服务器。以下命令会上传真实样片、调用 ASR/LLM、在成功后重启 Stage 2 API/Worker，并清理测试账户及视频：

```powershell
npm run build
$env:MVP_ACCEPTANCE_VIDEO = (Resolve-Path '..\video\如何选择适合自己的agent.mp4').Path
$env:MVP_STAGE6_RESTART_SERVICES = '1'
npm run e2e -- e2e/stage5-flow.spec.ts
```

若本机没有该样片，请把 `MVP_ACCEPTANCE_VIDEO` 指向你有权使用的本地视频。不要将视频或转写复制进 Git。

后端集成测试使用 Stage 1 PostgreSQL 上独立的 `videoclip_mvp_stage1_test` 测试库，不清空 Stage 1 业务库；运行它们前需先按本机私密配置启动 Stage 1。仓库根目录命令：

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

- 一个 Stage 2 Worker 同时执行一个分析任务；投递和执行异常需依据数据库中的事实人工恢复。
- 总结校验失败、输出截断或请求超时不会发布半成品。输出结构或引用校验可按冻结规则做有限修复；真实截断、网络超时等终态失败需停止确认后手动恢复。每个 task 的总结请求总数最多 3 次。
- 当前已实测的授权样本采用 H.264/AAC 且时间轴零起点；带延迟音轨或不能可靠映射的媒体会被拒绝。项目不提供通用转码、断点续传、自动接管、画面理解、移动端或线上部署。
- 20 分钟是媒体时长上限；完整 20 分钟视频尚未作为真实模型样本实测。上传字节数和业务数据目录总量均没有运行期硬上限。
- 本地配置中的零费率不是账单回执；实际费用和剩余额度需在服务商账户中核对。

第 0 至第 6 窗口的实际验收证据、历史故障和限制见[第六窗口验收记录](.scratch/mvp-stage-6/validation.md)。
