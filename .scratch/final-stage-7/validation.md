# 第 7 窗口验收记录

日期：2026-10-05（Asia/Shanghai）
工作区：`E:\VideoClip`，分支 `codex/mvp`；未切换/重置分支。

## 结论

- **第 7 窗口：部分完成。** 主要真实用户动作、迁移/时间线、隔离故障回归和资源观测均已验证；完整 Stage 7 Playwright 单次全绿结果、调用后账户账单/额度及真实 20 分钟模型处理仍缺失。
- **最终版既定完成标准：部分满足。** 必需外部账单/用量证据缺失；20 分钟真实课程处理没有运行；本次完整长流程 Playwright 由于验收脚本两处错误分段完成而非一次全绿。不可称最终版已通过。
- **当前可演示：有条件可以。** 前端 `http://127.0.0.1:5174` 与 Stage 2 API `http://127.0.0.1:58002` 均已实测返回 HTTP 200。登录、授权视频上传、状态/事件查看和已有笔记路径可演示。重新进行真实模型处理前，先核对账户剩余额度和免费条件；当前没有保留本窗口的真实样片结果。

命令模板与证据路径索引见[commands-and-evidence.md](commands-and-evidence.md)；逐项要求见[验收矩阵](acceptance-matrix.md)，演示步骤见[demo-guide.md](demo-guide.md)。

## 当前部署与历史数据

- 后端产品源码版本：`763dc49373d5c57fa9a7e8b146753768ad94cb12`；本窗口仅变更前端 E2E 与文档，未改动已部署后端代码。
- Stage 2 migration：`1a7e3c9d5b42_analysis_events.py`，从 `d42f6c8a901e` 前进。部署前私密备份：`data/mvp-stage-2/backups/final-stage7-pre-migration-20261005-152918-2aba2940.dump`，129,792 bytes，SHA-256 `ea7fe7da30a9ce1c3637043a500e0ed7a8e3bd576875d306462bfc184e363887`。
- 当前 Stage 2 六个服务均运行，API health 成功；Compose config 与 `alembic check` 通过；Redis analysis/maintenance 队列为 0，未确认 execution 为 0。本窗口末端执行受门禁保护的 `stop-stage2.ps1` 与 `run-stage2.ps1` 启停验证；停止只停服务容器、不删除卷，启动再次检查迁移/运行 task/队列并通过健康检查。之后 API、前端仍返回 HTTP 200。
- Stage 2 业务表回到处理前基线：users 6、videos 8、tasks 8、attempts 14、summaries 2、analysis_events 0。两条旧 task 仍为 `awaiting_summary/waiting`，均没有未确认 execution；历史用户、笔记和媒体未删除。Stage 7 临时账户和记录已清理。
- 上传前原样片在本次重新核验为 3,221,243,133 bytes，SHA-256 `e08fdf8772a508ce8811f46f28f330ec493ba50d000977c8f10e273fb54ff8b3`；H.264 3200×1792/30fps、AAC 48 kHz stereo；容器 1061.333 秒，音轨 1061.291 秒；Stage 0 映射区间 0–1061.290646 秒。原文件未更改。

## 真实长视频闭环及验收脚本修正

真实运行数据、摘要 payload 哈希、配置/来源指纹、模型请求状态与 token usage 仅在 Git 忽略目录保存：

`data/mvp-stage-2/acceptance/final-stage7-20261005T075452Z-a49c7d45/real-model-audit.json`

运行顺序及事实：

1. 第一次浏览器运行 `20261005T073615Z-3ed188fc` 暂停后重选续传通过，但其摘要解析器把 PostgreSQL `true::text` 当作 `t`，在模型请求前失败；无模型调用。新建记录通过业务删除服务清理，历史表计数恢复。
2. 第二次使用新的随机账户/记录 `20261005T075452Z-a49c7d45`。3.22 GB 样片分片上传在 8 MiB 中断；刷新、重选同一原文件并从服务端确认偏移续传；视频大小/SHA 一致且唯一。完整真实 ASR 36/36 段成功，真实 LLM 1 次成功，发布 1 条唯一总结。
3. 原 E2E 审计 SQL 假设 `analysis_tasks` 存在 `summary_requests_used/limit` 列，真实执行后查询失败；改为查询 `summary_requests` 记录数，再与 API 预算字段核对。业务处理本身成功。
4. 在同一记录上的续测确认停止后重启 API/Worker，摘要、转写、47 条事件读回；全部摘要引用的原文及时间校验通过；两处相异引用在浏览器定位并继续播放。媒体删除后笔记仍可读，媒体 GET/HEAD/Range 为 410。
5. 第一次删除整条记录的续测用了错误 accessible name `删除记录`，而 UI 实际为 `删除整条记录`。修正后整条删除的 UI 动作和 8 个接口 404 断言均执行；测试最后仅在输出摘要处引用已删除分支中不存在的局部变量退出。随后单独 Playwright 读回通过：视频列表不再含该记录，视频/task/media GET/HEAD/Range/transcript/summary/events 均为 404。账户清理脚本确认空账户删除。

因此真实用户动作已按顺序完成，并有分段浏览器/API/数据库证据；但这不是单次全绿的完整 Playwright 记录。不得隐藏上述脚本失败，也没有重跑一遍并重复 36 次 ASR 与 LLM 调用。

## 费用和模型

- 调用前，用户提供 Stage 2 私密配置并明确确认两模型在其账户免费。配置宣告两模型零价格，未显示或复制密钥。
- 当天 SiliconFlow 官方价格中心将 SenseVoiceSmall 列为免费；官方 2026-03-09 公告称 Qwen3.5-4B 免费使用。真实接口本次两模型成功响应。
- 用户调用前提供的账户费用页面截图筛选日期为 2026-10-05，顶部金额均显示 ¥0.00，明细列表为空。这是调用前证据，不是本次之后的使用/账单回执。
- 缺少账户剩余免费额度截图及本次调用后账单/用量页。36 个 ASR 请求和 1 个 LLM 请求、LLM token usage 保存在上面的私密审计 JSON；本地配置价格 0 不是扣费证明。
- 真实运行仅用用户确认免费模型，未切换付费模型。故障回归使用本地模型替身。

## 故障与跨窗口回归

本次隔离验收结果在各窗口私密 `data/mvp-stage-{4,5,6}/acceptance/` run-id 目录；这些不是当前真实供应商执行的证据。

| 链路 | 本次结果 | 范围边界 |
| --- | --- | --- |
| Stage 4 真实队列协调 | 通过：繁忙时 B/C 公平顺序、ACK 后续推进、迟到消息/结果拒绝、最大未确认执行 1 | 独立 Compose；3 个 ASR + 3 个 LLM 替身请求 |
| Stage 5 冷却和预算 | 通过：共享 429 冷却、非受限阶段继续、窗口到期续接、自动恢复、永久认证错误不重试、summary-only 重试和三类预算独立耗尽 | `20261005t070512z-2906b317`；8 ASR + 12 LLM 替身请求，供应商真实请求 0 |
| Stage 6 媒体删除竞争 | 通过：execution 保持期间不清理；可信停止后删除；失败清理保持媒体关闭、笔记可读且重试不调用模型；整条删除与迟到响应拒绝 | `20261005t072135z-cda9ae0e`；独立真实进程边界 + 本地替身，不是 Stage 2 真实供应商中注入 |

Stage 4 本次目录：`data/mvp-stage-4/acceptance/20261005t065823z-3685f2a7/`，其队列协调 acceptance 通过；Stage 5 与 Stage 6 隔离全量后端各 157 passed。Stage 6 另有媒体清理直接测试 14 passed、迟到总结竞争测试 2 passed、Playwright 1 passed。

## 测试、配置和迁移

| 检查 | 结果 |
| --- | --- |
| 本窗口当前 `tests/test_media.py`，`pytest --noconftest` | 17 passed；包括 1200.0 秒接受与 1200.01 秒拒绝 |
| Stage 5/6 隔离后端全量 | 各 157 passed |
| Stage 0 回归 | 32 passed |
| Ruff | passed |
| 配置范围 mypy | 30 source files passed |
| pip check | passed |
| 前端 ESLint | passed，0 warning |
| 前端 Vitest | 5 files / 14 tests passed |
| 前端生产构建 | `tsc --noEmit` 与 Vite build passed |
| Compose config | passed |
| Stage 2 Alembic check | `No new upgrade operations detected` |
| Playwright 删除后读回 | 1 passed；验证列表/8 个接口 404。Stage 7 主流程分段执行，未得一次全绿运行 |

本窗口另一次尝试把 4 个后端文件集直接指向默认 pytest 测试库；`tests/conftest.py` 在 session startup 连接 `127.0.0.1:55442` 超时，测试尚未开始。该端口当时无监听；没有创建/清空数据库，也没有测试断言被误记为通过。后端 157 项通过来自 Stage 6 隔离 PostgreSQL 链路。测试库启动/连接变量见 README 的后端测试说明。

## 资源观测

私密采样文件：`data/mvp-stage-2/acceptance/final-stage7-20261005T075452Z-a49c7d45/resource-samples.csv`。总计 37 点，覆盖约 25 分 53 秒，实测间隔 41.2–45.1 秒、平均 43.1 秒；每次目录扫描和 Docker 查询使实际间隔长于配置的 30 秒。

- E 盘空闲采样低/高：135,475,982,336 / 138,765,664,256 bytes；最低点与样片上传 + 分析中间文件同现。删除后 `app/media` 恢复到原基线 15,247,745,579 bytes。当前 8 个数据库视频的 storage-name 集合与 8 个本地 `.media` 文件精确匹配；四个分析/维护容器没有指向已删除媒体文件的 `/proc` FD。
- 上传 `.part` 最大 3,221,243,133 bytes；音频/分析目录峰值 67,924,186 bytes。PostgreSQL 目录约 94.5 MB，Redis 目录约 2.34 MB。采样期间 Host 可用内存最低约 9.02 GB；容器内存观测峰值：API 77.8 MiB、analysis worker 185.3 MiB、maintenance worker 105.2 MiB、Beat 69.8 MiB、PostgreSQL 87 MiB、Redis 13.2 MiB。
- 容器 SizeRw 峰值：API 16 KiB、analysis worker 24 KiB、maintenance worker 24 KiB、Beat 8 KiB、PostgreSQL 200 KiB、Redis 4 KiB。RootFs 是镜像/基础文件系统观测，不当作容器可写增量。
- Docker 对照早期分类样本与结束样本：images 约 5.038→5.037 GB、containers 约 106→106.1 MB、volumes 321.1→321.1 MB、build cache 7.719→7.723 GB。没有 prune 或调整全局 Docker 存储；净变化为 MB 级，低于 7 GiB。各镜像共享层无法分摊独占字节。
- `docker logs --timestamps` 的输出字节代理已随采样记录；精确 Docker `LogPath` 文件长度及 Windows 主机上 Docker Desktop WSL VHDX 实际物理变化不可访问，保留为未知。采样前后 E 盘空闲差只有约 0.5 MiB，不能再精确拆分给具体文件/卷。

## 提交

- `18133c8` `fix: 声明共享冷却索引元数据`
- `81ec79f` `test: 为 Stage 5 验收选择空闲网络`
- `763dc49` `feat: 增加持久化分析时间线`
- `804b93e` `test: 修复 Stage 7 浏览器验收续测`
- Stage 7 文档已作为单独 `docs` commit 提交，可在仓库 `git log --oneline` 中查阅。

## 补验与决策

1. 用户补充 SiliconFlow 调用后账户费用/用量页及剩余免费额度证据，确认本次 36 ASR + 1 LLM 请求的账户侧计费。
2. 若“真实 20 分钟模型处理”仍是必需项，提供授权的完整 20 分钟样片并再次先确认账户剩余免费额度；不得用静音或重复拼接样本替代。
3. 对完整 Stage 7 E2E 取得一次全绿记录，需要在账户额度证据确认后用新记录重新运行完整流程；当前记录已依删除要求清理，不重用历史样片结果。
4. 若需要精确解释 Docker Desktop WSL 虚拟盘物理增长，应提供可读取该 VHDX/宿主 Docker 数据目录的账户/系统证据；当前不调整全局存储或清理其他项目。
