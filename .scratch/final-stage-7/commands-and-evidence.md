# Stage 7 命令与证据索引

## 部署、迁移与状态

```powershell
Set-Location E:\VideoClip
.\scripts\stage2\run-stage2.ps1
docker compose --env-file data/mvp-stage-2/.env -p videoclip-mvp-stage2 -f compose.yaml config --quiet
docker compose --env-file data/mvp-stage-2/.env -p videoclip-mvp-stage2 -f compose.yaml exec -T api alembic check
docker compose --env-file data/mvp-stage-2/.env -p videoclip-mvp-stage2 -f compose.yaml ps
```

结果：当前 API/worker/maintenance-worker/beat/PostgreSQL/Redis 均运行，API health 成功；migration `1a7e3c9d5b42`，`alembic check` 无差异。Stage 2 migration 前备份在 `data/mvp-stage-2/backups/final-stage7-pre-migration-20261005-152918-2aba2940.dump`，大小/SHA-256 见[验证记录](validation.md)。结束时 DB 业务基线恢复为 6/8/8/14/2，事件 0，未确认 execution 0。

复核表计数、停止门禁与队列长度：

```powershell
$envLines = Get-Content 'data/mvp-stage-2/.env'
$dbUser = (($envLines | Where-Object { $_ -match '^POSTGRES_USER=' } | Select-Object -First 1) -split '=', 2)[1]
$dbName = (($envLines | Where-Object { $_ -match '^POSTGRES_DB=' } | Select-Object -First 1) -split '=', 2)[1]
$compose = @('compose','--env-file','data/mvp-stage-2/.env','-p','videoclip-mvp-stage2','-f','compose.yaml')
$sql = "SELECT 'users='||(SELECT count(*) FROM users)||' videos='||(SELECT count(*) FROM videos)||' tasks='||(SELECT count(*) FROM analysis_tasks)||' attempts='||(SELECT count(*) FROM analysis_attempts)||' summaries='||(SELECT count(*) FROM analysis_summaries)||' events='||(SELECT count(*) FROM analysis_events)||' unconfirmed='||(SELECT count(*) FROM analysis_executions WHERE stop_confirmed_at IS NULL);"
docker @compose exec -T postgres psql -X -v ON_ERROR_STOP=1 -qAt -U $dbUser -d $dbName -c $sql
docker @compose exec -T redis redis-cli LLEN analysis
docker @compose exec -T redis redis-cli LLEN maintenance
```

本次复核结果：`users=6 videos=8 tasks=8 attempts=14 summaries=2 events=0 unconfirmed=0`；analysis 与 maintenance 队列均为 0。两条旧 `awaiting_summary/waiting` 任务仍在且停止已确认。

## 代码与自动化检查

当前媒体边界 fixture（无需 PostgreSQL）：

```powershell
$env:PYTHONPATH = (Resolve-Path 'src').Path
& .\data\mvp-stage-2\.venv\Scripts\python.exe -m pytest --noconftest tests\test_media.py -q
```

结果：17 passed，包含 1200.0 秒接受和 1200.01 秒 `VIDEO_DURATION_LIMIT` 拒绝。

Stage 0 回归：

```powershell
& .\data\mvp-stage-0\.venv\Scripts\python.exe -m pytest probes\stage0\tests -q
```

结果：32 passed。

后端静态与依赖检查：

```powershell
$env:PYTHONPATH = (Resolve-Path 'src').Path
& .\data\mvp-stage-2\.venv\Scripts\python.exe -m ruff check src tests migrations scripts\stage1 scripts\stage2 scripts\stage4 probes\stage0
& .\data\mvp-stage-2\.venv\Scripts\python.exe -m mypy --explicit-package-bases src migrations scripts\stage1 scripts\stage2 scripts\stage4 probes\stage0
& .\data\mvp-stage-2\.venv\Scripts\python.exe -m pip check
```

结果：Ruff passed；mypy 30 source files passed；pip check passed。独立隔离 Stage 5、6 runner 中后端全量测试各 157 passed。Stage 4 队列协调验收通过。

前端：

```powershell
Set-Location E:\VideoClip\frontend
npm run lint
npm test
npm run build
```

结果：lint 无 warning；Vitest 5 files / 14 tests passed；`tsc --noEmit` 与 Vite build passed。

本窗口用默认 pytest 测试库再次启动 4 个后端测试文件时，`tests/conftest.py` 在 Alembic session startup 尝试连接未运行的 `127.0.0.1:55442` 并超时，测试未开始。该失败已在[验证记录](validation.md)明确列为“未运行”，不改动测试库。

## Playwright 和故障演示

真实 Stage 2 17 分钟样片命令模板（会发起真实模型请求；先核对账户免费额度）：

```powershell
Set-Location E:\VideoClip\frontend
$env:MVP_WEB_MODE = 'preview'
$env:VIDEOCLIP_WEB_PORT = '5174'
$env:MVP_WEB_URL = 'http://127.0.0.1:5174'
$env:MVP_ACCEPTANCE_VIDEO = (Resolve-Path '..\video\agent-skill介绍.mp4').Path
$env:MVP_STAGE6_RESTART_SERVICES = '1'
$env:MVP_PLAYWRIGHT_OUTPUT_DIR = '..\data\mvp-stage-2\acceptance\playwright-' + [guid]::NewGuid().ToString('N')
npx playwright test e2e/stage5-flow.spec.ts --workers=1
```

本次产生的数据只放入 Git 忽略的 `data/mvp-stage-2/acceptance/final-stage7-20261005T075452Z-a49c7d45/`。原长流程测试因 query 列假设失败；修复后的后续浏览器运行串联完成引用播放、媒体删除、整条删除和删除后读回，详细结果见[验证记录](validation.md)。不要从 failure 的 `error-context.md` 导出课程内容。

隔离替身命令（不会连接供应商）：

```powershell
Set-Location E:\VideoClip
.\scripts\stage2\run-queue-coordination-acceptance.ps1
.\scripts\stage2\run-final-stage5-acceptance.ps1
.\scripts\stage2\run-final-stage6-acceptance.ps1
```

本次 run-id 路径：

- Stage 4：`data/mvp-stage-4/acceptance/20261005t065823z-3685f2a7/`
- Stage 5：`data/mvp-stage-5/acceptance/20261005t070512z-2906b317/`
- Stage 6：`data/mvp-stage-6/acceptance/20261005t072135z-cda9ae0e/`

## 资源与账户材料

- 37 次资源采样命令由 [`resource-sampler.ps1`](resource-sampler.ps1) 执行；输出 `data/mvp-stage-2/acceptance/final-stage7-20261005T075452Z-a49c7d45/resource-samples.csv`。采样进程已正常退出；其停止标记及本次隔离测试生成的合成媒体/租约文件已清理。
- 真实模型聚合审计（模型名、请求数、状态、elapsed/tokens、配置/来源哈希、无模型正文）：`data/mvp-stage-2/acceptance/final-stage7-20261005T075452Z-a49c7d45/real-model-audit.json`。
- Stage 2 preflight 与运行日志、Playwright error context、生成的视频摘要和课程原文都在 Git 忽略的 `data/mvp-stage-2/acceptance/`。它们不属于公开展示材料。
- 费用来源：用户在真实请求前提供的本地账户配置和 2026-10-05 费用截图；供应商公开资料为[价格中心](https://siliconflow.cn/pricing)及[Qwen3.5 上线公告](https://www.siliconflow.cn/news/yg6n19y2g6frnp4koxu8ye48)。调用后的账户额度/费用记录仍缺。

## 当前提交

```text
18133c8 fix: 声明共享冷却索引元数据
81ec79f test: 为 Stage 5 验收选择空闲网络
763dc49 feat: 增加持久化分析时间线
804b93e test: 修复 Stage 7 浏览器验收续测
```

文档提交可在仓库 `git log --oneline` 中查阅。
