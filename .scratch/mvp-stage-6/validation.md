# 第六窗口最终复查与展示验收

- 日期：2026-10-03（Asia/Shanghai）
- 分支：`codex/mvp`。启动现场 HEAD 为 `f0e7b14`，与第 5 窗口记录中的 `6e4a1c5` 不同；现场版本以 Git 核验为准。启动工作区干净。
范围：最终复查、真实浏览器验收、故障规则回归、复现文档和必要的最小修复；未新增 MVP 功能。

## 当前结论

真实浏览器主闭环、同源 smoke、故障回归和工程检查均已通过。逻辑修复及回归测试已提交 `6b07e82`；README、验收记录和产品 / 技术进度文档已更新为“MVP 最终验收通过”。该结论不包含未实测的完整 20 分钟样本或实际账单回执。

## 预检与数据保护

| 项目 | 实际结果 |
| --- | --- |
| 工作区与分支 | 起始分支 `codex/mvp`，HEAD `f0e7b14`，工作区干净。HEAD 与第 5 窗口记录不同，未重置或切换历史。 |
| 运行环境 | Windows PowerShell；Python 3.13.12、Node 24.18.0、npm 11.16.0、Docker Compose 5.5.1。Docker Engine 已由用户启动。 |
| 浏览器端口 | 起始时 5173/5174 均无监听者。Playwright 按配置启动预览服务器至 5174，代理目标 Stage 2 API 58002；未改用 5173。 |
| API / 数据服务 | Stage 1 API `58001/health` 与 Stage 2 API `58002/health` 均返回 200。Stage 1 PostgreSQL、Stage 2 PostgreSQL 与 Redis 健康；Stage 2 恰有一个 Worker。E 盘 bind mount 指向各自 `data/mvp-stage-{1,2}`。 |
| 队列与执行 | 运行前及验收清理后，Stage 2 Redis `analysis` 队列为 0，未确认 execution 为 0。Celery remote control 在配置中关闭，因此 `inspect ping` 不作为 Worker 健康判据；Worker 日志确认已连接 Redis 并进入 ready。 |
| 私密配置 | 仅通过 Stage 2 Compose `--env-file` 与 Stage 0 本地私密配置加载；没有输出密钥、Cookie、CSRF、密码、转写或原始模型响应。配置显示模型价格为零且有本地确认标志；没有核对账户账单/余额，故不声称费用回执通过。 |
| 初始 Stage 2 业务基线 | 5 条视频、1 条已发布总结；任务为 2 条 `awaiting_summary/waiting`、1 条 `failed/failed`、1 条 `failed/not_started`、1 条 `succeeded/succeeded`；2 条历史来源失配任务原样保留。 |
| 授权样本 | 使用现有本地授权样本，1,116,803,236 字节、368.6 秒，哈希与第 0 窗口记录一致；没有复制到 Git。有效时长 20 分钟上限仍生效，但本样本不是 20 分钟实测。 |

首轮 Stage 2 启动、API/Worker 重启及测试只针对 `videoclip-mvp-stage2`；未停止、清理或迁移其他 Docker 项目。Stage 1/2 分别挂载 E 盘独立数据目录。

## 产品与技术验收对照

| 验收项 | 前序证据 | 第六窗口新证据 | 结论 |
| --- | --- | --- | --- |
| 授权课程视频原始体上传，覆盖大于 1 GiB 文件；不施加字节硬限额 | Stage 0/1/5 已验证授权样本和原始请求体。 | Chromium 上传 1,116,803,236 字节、368.6 秒授权样本返回 201；请求不是 multipart，详情字节数吻合。 | 通过 |
| 真实 ASR 全片持久化、冻结配置与成功检查点 | Stage 0/2/4 已验证 SenseVoice、13 个音频段和配置快照。 | 最终 task 完成 13/13 段；两次 summary-only 人工恢复均复用全部 13 段。成功结果经 API / Worker 重启后读回。 | 通过 |
| 真实免费模型总结、合法结果唯一发布、引用原文与区间校验 | Stage 0/4 已验证 Qwen、提示词 v4、结构和来源规则；未核对实际账单。 | `Qwen/Qwen3.5-4B`、`stage0-summary-v4`，task 级 3 次请求（前两次 `LLM_OUTPUT_TRUNCATED`、最后一次成功），151.241 秒；3 章、6 要点、9/9 引文和区间匹配。 | 通过；零价格来自本地配置，非账单回执 |
| 引用回看与播放继续、Range 206/416、内容与语音主题相符 | Stage 0 通过 FFplay 抽样播放零起点样本；Stage 5 已验证浏览器引用播放。 | 本窗口 9/9 quote 与 ASR 原文匹配、时间区间匹配；Chromium 点击两个相距超过 1 秒的引用，定位误差小于 2.5 秒并确认播放继续；HEAD 200、Range 206 / 16 字节、无效 Range 416。Stage 0 的 FFplay 在 0、30、360 秒确认实际 A/V 同步；不据此外推非零起点媒体。 | 通过（限已验证零起点样本） |
| 登录 / 退出、会话失效、跨账号隔离、删除后不可读 | Stage 1/5 与安全集成测试已有证据。 | Chromium 登出 204，会话与媒体请求 401；第二账户读取视频、task、转写、总结、媒体为 404；删除后媒体、转写、总结接口为 404/410。 | 通过 |
| API/Worker 重启后 task、转写和总结可读 | Stage 1/2/4/5 已验证数据库/API/Worker 持久化。 | 成功总结后仅重启 Stage 2 API/Worker，健康恢复；浏览器刷新后成功 task、13 段转写和总结仍可读。 | 通过 |
| pending/unknown/重复/迟到消息、停止未确认、超时及非法输出不发布 | Stage 1–5 隔离 PostgreSQL / Redis 测试。 | 本窗口完整后端 pytest 82 项通过；故障测试名称见下节。`recovery-fixture.spec.ts` 未运行：夹具直接修改 Stage 2 业务 task 行伪造前提，违反本规格不改业务表注入故障的约束；同源 smoke 与完整真实主流程通过。 | 通过（隔离回归 + 真实截断/恢复） |
| 清理失败保留合法结果、删除进度与重试 | Stage 3/5 隔离测试和页面证据。 | 全量隔离测试覆盖清理失败、停止、删除门禁与重试；最终浏览器删除走完，删除后内容均不可读。 | 通过 |
| 20 分钟媒体边界、单 Worker、Docker 7 GiB 新增预算 | Stage 0–5 已确认媒体时长策略和单 Worker。 | Stage 2 一个 Worker；镜像 / 全局缓存显示增量、容器层和日志预算见下方资源记录。真实样本为 368.6 秒，不代表 20 分钟实测。 | 通过；时长边界以隔离回归为证 |

## 故障与恢复回归

本窗口在 Stage 1 的 `videoclip_mvp_stage1_test` 隔离库运行完整后端测试，未清空 Stage 1 业务库，也未在 Stage 2 业务表伪造状态。

| 故障类别 | 隔离回归证据 |
| --- | --- |
| Broker 错误、unknown/pending 投递、确认丢失、重复提交 | `tests/test_analysis.py::test_broker_error_is_unknown_and_does_not_republish_on_duplicate`、`test_database_commit_before_publish_exit_leaves_pending_delivery`、`test_successful_publish_with_lost_confirmation_stays_pending`。 |
| 旧消息、重复恢复、并发领取和数据库锁丢失 | `test_manual_recovery_is_idempotent_and_old_attempt_messages_are_rejected`、`test_database_lock_loss_stops_local_process_and_blocks_another_claim`、`test_recovery_and_video_deletion_serialize_on_the_video_row`。 |
| 未确认停止阻止恢复/删除、停止子进程 | `test_stop_unconfirmed_blocks_recovery_and_deletion_until_confirmed`、`test_local_process_timeout_stops_and_reaps_the_child_group`、`tests/test_media.py::test_ffprobe_timeout_stops_child_process_with_bounded_diagnostics`。 |
| 总结截断、人工恢复复用检查点和 task 级三次上限 | 本窗口更新 `test_truncated_summary_requires_manual_recovery_and_reuses_transcript`：连续两次受控 `finish_reason=length` 均终止当前 attempt、停止确认后由人工创建 summary-only 子 attempt，第三次合法输出发布；只保留 3 条 task 级请求，不调用 ASR。定向测试与完整 pytest 均通过。 |
| 非法输出和来源 / 时间校验 | `tests/test_summarization.py` 的结构、quote、segment 和有限时间校验测试；`test_publish_database_failure_is_not_visible_and_recovery_reuses_transcript`、`test_delete_winning_summary_race_blocks_publication_and_later_requests`。 |
| 删除/清理失败与内容门禁 | `tests/test_upload_video.py::test_deletion_gate_and_retryable_storage_failure`、`test_cleanup_deadline_returns_pending_while_safe_cleanup_continues`、`tests/test_analysis.py::test_stopped_attempt_cleanup_preserves_transcript_and_video`。 |

不运行 `frontend/e2e/recovery-fixture.spec.ts`：其夹具脚本会直接修改业务表来构造 task 状态，和 Stage 6 不写业务表伪造故障的硬约束冲突。真实浏览器主流程覆盖正常删除与越权路径；恢复/停止/清理故障由全量隔离测试覆盖。

## 工程检查

| 命令 | 结果 |
| --- | --- |
| `cd frontend; npm ci` | 通过：198 个包，0 vulnerabilities。 |
| `npm run lint` | 通过，`--max-warnings=0`。 |
| `npm test` | 2 个文件、5 项通过。 |
| `npm run build` | TypeScript 检查与 Vite production build 通过。 |
| `npx playwright test e2e/stage5-flow.spec.ts --list` | 1 个真实主流程场景被收集。 |
| `npx playwright test e2e/stage5-flow.spec.ts --output=../data/mvp-stage-6/playwright-final-corrected` | 真实 Chromium 主流程 1 项通过，3.9 分钟；含注册、1.1 GiB 原始上传、真实 ASR / LLM、人工恢复、重启、播放、隔离、退出和删除。 |
| `npx playwright test e2e/same-origin-smoke.spec.ts --output=../data/mvp-stage-6/playwright-same-origin` | 真实 Chromium 同源 `/api` smoke 1 项通过。 |
| Stage 1 隔离库 `python -m pytest -q` | 82 项通过，1 条既有 Starlette/AnyIO `BlockingPortal` 弃用警告。 |
| Stage 0 回归 `python -m pytest probes/stage0/tests -q` | 32 项通过。 |
| Ruff | `ruff check` 全部通过；Ruff 0.11.4 对生产源码/脚本 38 个文件的 `format --check` 通过。 |
| Mypy | 使用 `--explicit-package-bases` 对源代码及脚本检查通过。最初误将未纳入项目 mypy 范围的测试文件也传入，报告既有测试注解问题；按仓库 source-only 范围重跑通过。 |
| `python -m pip check` | 通过：No broken requirements found。 |
| Stage 1/2 Compose `config --quiet` | 通过。 |
| Stage 2 API `alembic check` | 通过：No new upgrade operations detected。 |
| `git diff --check` | 已通过；文档最终 diff 与提交前复核待完成。 |

对 `ruff format --check` 的完整 `tests/` 范围还曾发现 `tests/test_analysis.py` 需格式化，已执行 Ruff formatter；生产源码及脚本格式检查已通过。前序 Stage 0 两个探针文件在 Ruff 0.11.4 下有既有格式差异，未修改；Ruff lint 通过。

## 真实浏览器审计

使用 Chromium 真实浏览器与 Stage 2 后端。Playwright 输出只保留计数、状态和稳定错误码，不保留账户口令、Cookie、转写或原始模型响应。

| 轮次 | 结果 |
| --- | --- |
| 前序第 5 窗口历史失败 | 一次真实总结请求曾遇到 `LLM_PHASE_TIMEOUT`，确认停止并清理隔离数据；该次不计通过，详见[第 5 窗口记录](../mvp-stage-5/validation.md)。 |
| 第 1 轮（旧验收脚本） | 13/13 ASR 成功；总结任务成功，2 次总结请求状态为 `failed,succeeded`，14 条引用均匹配原文和区间；测试在浏览器冷启动后要求显示“登录状态已失效”提示处失败。清除 Cookie 后冷启动只应显示登录页，因此脚本改为直接断言 `/auth/me` 和媒体请求 401；该轮隔离账户/视频已清理。 |
| 第 2 轮（冷启动断言修正、截断策略修复前） | 完整流程通过，13/13 ASR，2 次总结请求 `failed,succeeded`，5 章、5 个要点、10/10 引文和区间校验通过；API/Worker 重启后读回通过。审计没有收集首个失败错误码，故未把它描述成超时、截断或人工恢复。隔离数据已清理。 |
| 第 3 轮（最终逻辑、旧轮询已加载） | 已实际捕获第 1 次 LLM 请求 `LLM_OUTPUT_TRUNCATED`、`finish_reason=length`、66.211 秒；failed full attempt 与 execution 均确认停止。测试进程在脚本读入内存后才改入允许 `failed:failed:stopped` 的轮询分支，因此本轮仍用旧断言，20 分钟后因该状态未包含在内而失败；Playwright 清理后隔离账户 0、视频 5、task 5、未确认 execution 0、Redis 队列 0。此轮不计通过。 |
| 最终轮（修正轮询后） | 真实浏览器主流程 1 项通过（3.9 分钟）。授权样本 1,116,803,236 字节 / 368.6 秒；13 个成功 ASR 检查点。审计：`asrRequests=0`、`completedSegments=13`、`reusedSegments=13`（恢复子 attempt 无 ASR 重复请求）；3 次 task 级总结请求、合计 151.241 秒、状态 `failed,failed,succeeded`、前两次错误码均为 `LLM_OUTPUT_TRUNCATED`；两次恢复都创建了带正确 parent link 的 `summary_only` 子 attempt，最后一次成功发布。Qwen/Qwen3.5-4B + `stage0-summary-v4`；3 章、6 要点、9 条引用，quote/time 均 9/9；API/Worker 重启读回 true。浏览器另通过两处引用定位及继续播放、HEAD 200、206 / 416、logout/session 401、第二账户五资源 404、删除后内容 404/410。隔离账户清理后基线恢复（见下）。 |
| 同源 smoke | `e2e/same-origin-smoke.spec.ts` 1 项通过；无账户或业务记录副作用。 |

最终轮审计字段：授权视频字节/时长；ASR 请求数、用时和成功检查点数；LLM 每条请求状态/稳定错误码/finish reason、合计数和耗时；父子 attempt、停止确认、重用段数；章节/知识点/引用数和逐条原文/时间匹配；两个引用目标和播放继续；重启后读回；登录退出、第二账户 404、删除后 404/410；测试数据清理结果。

## Docker 与持久化占用

| 指标 | 基线 / 本窗口实测 |
| --- | --- |
| Stage 1/2 正式 API 镜像 | Stage 2 修复前 938,276,763 字节、重建后 938,276,726 字节；Stage 1 保持 938,276,760 字节。Stage 2 替换既有镜像，大小没有净增。 |
| Docker 全局汇总（含其他项目） | 重建前 Images 5.033 GB / Build Cache 7.682 GB，重建后 5.034 GB / 7.683 GB（Docker 显示精度下各约 +1 MB）；Containers 103.5 MB、Volumes 321.1 MB 不变。全局项包含其他项目，不能据此将增量全部归于 VideoClip。 |
| Compose 容器可写层 | 六个 Stage 1/2 容器重测合计 172,032 字节：Stage 1/2 API 各 16,384，Stage 1 PostgreSQL 24,576，Stage 2 PostgreSQL 86,016，Stage 2 Worker 24,576，Stage 2 Redis 4,096。 |
| 日志预算 | Compose 每服务 `json-file` 限制为 10 MiB × 2 个文件；Stage 1 两个运行服务与 Stage 2 四个服务的合计上限 120 MiB。 |
| 本窗口 Docker 新增预算 | 只重建 Stage 2 镜像，没有全局 prune 或操作其他项目。全局显示精度内镜像与缓存合计变化约 2 MB，可写层合计约 0.17 MB，日志预算 120 MiB；保守合计远低于 7 GiB。 |

## 文档、提交与结束状态

README 本地步骤已补充架构 / 数据流，README、产品、技术及前序验收记录的本地 Markdown 链接均已逐一解析通过；未记录密钥、Cookie、口令、账号、完整转写、原始模型响应或私密视频路径。最终验收后 Stage 2 基线为：`stage5fixture` 账户 0、视频 5、task 5、已发布总结 1、未确认 execution 0、Redis `analysis` 队列 0；该隔离账户和视频由 Playwright 清理钩子删除。逻辑提交：`6b07e82 fix: 截断总结改为人工恢复`。本窗口只重建 Stage 2 镜像，没有执行全局 Docker 清理。
