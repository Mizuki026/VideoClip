# 最终版第 0 窗口：长视频与环境基线

日期：2026-10-03（UTC）

样片：`video/agent-skill介绍.mp4`

## 结论

约 17 分钟授权样片在现有 Stage 2 普通上传、异步分析和真实模型链路上完成了真实浏览器验收。最终通过的任务持久化了 36/36 个 ASR 片段，生成并发布一个结构合法的总结；页面校验 6/6 条引用的原文和时间范围，点击两处不同引用后播放器均定位并继续播放。确认执行停止后，验收脚本只重启 Stage 2 API/Worker，并读回任务、转写和总结。

本窗口结果只覆盖约 17 分 41 秒样片，不代表完整 20 分钟模型能力或最终版增量功能已通过。Stage 2 本机配置含零费率字段，但未取得账户账单、余额或账户侧价格证据；实际费用状态未验证。资源记录对 E 盘可用空间、业务目录、主机/容器内存、Docker 全局用量和物理日志有实测；容器可写层只在处理前后测量，处理中峰值没有单独采样，E 盘空闲空间的运行前后差额也无法完全归因，详见“资源测量”。

**阶段状态：**约 17 分钟真实模型/浏览器主流程通过；容器可写层峰值这一资源子项尚未完整验证，因此不宣称第 0 窗口所有资源测量条目均已闭环。可验证的后续补测方式是在下一次获准的 Stage 0 资源运行中，把 `docker inspect --size` 的 `SizeRw` 加入 10 秒采样轨迹，并在同一处理窗口重测 E 盘可用空间；无需修改业务表、清理其他 E 盘数据或重置历史 task。

## 范围与保护检查

- 只使用新增授权样片，未处理或清理其他历史任务。Stage 2 开始时为 5 个视频、5 个 task、7 个 attempt/execution、1 个已发布总结；Redis 队列及未确认 execution 均为 0。两条等待总结任务、既有失败任务和成功任务均保留。
- Stage 1 与 Stage 2 使用各自 E 盘目录、PostgreSQL 端口和 API 端口：Stage 1 为 `data/mvp-stage-1`、55442/58001；Stage 2 为 `data/mvp-stage-2`、55443/58002。Stage 1 业务库保持空白；后端测试期间只启动 Stage 1 PostgreSQL，测试结束后停止。
- Stage 2 始终只有一个 Worker。真实处理和重启前后都检查了未确认 execution；每次 API/Worker 重启前计数为 0。最终状态为 API healthy、一个 Worker、PostgreSQL/Redis healthy、待投递为 0、未确认 execution 为 0。
- 初始 E 盘可用空间记录为 150,344,454,144 字节（约 140.019 GiB）。初始 Stage 2 `app` 目录为 5,584,016,180 字节、5 个文件。Docker 起始全局统计为镜像 5.034 GB、容器 103.5 MB、卷 321.1 MB、Build Cache 7.683 GB。既有 MuseFlow 等其他 Docker 项目未启动、停止、迁移或清理。
- 全程没有重置请求预算、改写业务表伪造状态、切换付费模型、截断转写、做全局 Docker 清理或删除原始样片目录。

## 样片属性

| 项目 | 实测 |
| --- | --- |
| 字节数 | 3,221,243,133 |
| SHA-256 | `e08fdf8772a508ce8811f46f28f330ec493ba50d000977c8f10e273fb54ff8b3` |
| 容器 | MP4 |
| 视频流 | H.264 Main、yuv420p、3200×1792、30 fps；起始时间 0 秒，时长 1061.333300 秒 |
| 音频流 | AAC-LC、48 kHz、双声道；起始时间 0 秒，时长 1061.290646 秒 |
| 有效音频时长 | 1061.290646 秒，约 17 分 41.29 秒 |
| 时间轴映射 | Stage 0 `map_supported_mp4` 返回零偏移 `(0.0, 1061.290646)` |
| 浏览器播放 | 最终 Playwright Chromium 流程成功播放；两次引用跳转和继续播放断言通过 |

视频流比音频流长约 0.043 秒。样片符合当前支持的 MP4/H.264/AAC 零偏移路径，未做转码或画面理解。

## 冻结配置、模型结果与恢复记录

三种配置都按 task 冻结；失败任务保留原 attempt 快照，新的设置只用于新 task。task 最多 3 次总结请求的约束始终有效。

| 任务批次 | 冻结配置 | 请求与结果 | 处理结论 |
| --- | --- | --- | --- |
| A，05:04 UTC | `stage0-summary-v4`；`Qwen/Qwen3.5-4B`；context 256000；output 3072；配置 SHA-256 `e4239e1bd0549de2a5ade9240dea80ae6017e7951b8a4b3971251abed2173afe`；每 task 最多 3 次 | 36/36 ASR 成功。3 次总结输出分别在 3072 token 处以 `finish_reason=length` 截断，耗时 78.223、129.920、108.549 秒；各次提示约 4182 token、completion 3072 token、响应 10709 字节。 | 1 次 full + 2 次现有 summary-only 人工恢复；达到 3 次上限后安全停止。无总结发布，转写及错误/attempt 记录保留。 |
| B，05:31 UTC | `stage0-summary-v4`；同模型/context；output 6144；配置 SHA-256 `b07488c2a22c3094e58f728329fcb71fe409c26257b92b6642061ae53d2772c1`；每 task 最多 3 次 | 36/36 ASR 成功。总结请求 1 收到 `LLM_MODEL_OVERLOADED`，55.611 秒；请求 2 和 3 分别在 6144 token 处 `finish_reason=length`，耗时 155.503 和 236.982 秒。请求 2 的元数据为 prompt 4180、completion 6144、响应 20950 字节；请求 3 同样返回长度截断。 | 1 次 full + 2 次 summary-only 人工恢复；第 3 次结束后 execution 已确认停止。无总结发布，冻结快照、36 段转写及 3 次请求记录保留。未重置预算。 |
| C，05:52 UTC | `stage0-summary-v5`；output 8192；其他模型和 context 不变；配置 SHA-256 `3b6cec303efd72f49db87f33b24f6b90aba5d45eeaaf97dd15c16dfb8698d9dd`；模板 SHA-256 `d263f899715a28b9668f7b1c82234b1890e94a4f7ee3d06e4655e2e7282fc6cf` | 36 段和 1 个合法总结成功；总结耗时 6.258 秒，finish reason `stop`，prompt 约 4165 token、completion 198 token、响应 1268 字节。 | 模型链路成功；Playwright 随后因旧断言仍硬编码 v4 而失败。测试 guard 保留该 task 与数据；这是验收脚本断言错误，不是模型失败。 |
| D，最终浏览器通过 | 同 C 的 v5/8192 冻结配置；每 task 最多 3 次 | 36 次 ASR 全成功；总结 1 次成功，5.541 秒，finish reason `stop`，prompt 4165 token、completion 188 token、响应 1236 字节。 | 完整浏览器、引用播放、API/Worker 重启与读回全部通过。成功后 E2E 按既有流程清理本次测试账户/视频/task；验收日志留存。 |

第一次 3072 和第二次 6144 的输出都正好触及输出 token 上限；输入约 4.2k token、task snapshot context 为 256k，HTTP 总/读取超时为 300/240 秒。供应商文档说明 `finish_reason=length` 可由输出上限或上下文上限导致；结合本次输入/完成 token 元数据，判断主要阻塞是 v4 生成内容过长，而非上下文容量或 HTTP 超时。这是依据请求证据作出的推断，不是供应商内部诊断。

新 task 使用 v5 提示词：固定 3 个章节、3 个要点，限制标题和要点长度，并保持原结构、单片段引用和现有校验。v4 原指令、模板 SHA-256 `2d3fc767670fce33a83bbfa93209cd1c0087ee7f50581ede1a7f613ff1354949` 与 legacy 3072 配置保持兼容；代码更新后，数据库内 13 个已持久化 v4 attempt 快照均可解析。没有更改任何旧 task 的配置快照或检查点匹配规则。

## 最终真实浏览器验收

使用 `frontend/e2e/stage5-flow.spec.ts` 和 Chromium，在 `http://127.0.0.1:5174` 通过普通原始文件上传创建新账户、视频和分析 task；未使用分片上传。

| 阶段 | 结果 |
| --- | --- |
| 上传 | 32.579 秒；上传返回 201，文件字节数与样片一致 |
| 异步分析 | 从提交到完成 129.947 秒 |
| ASR | 36 次真实请求，汇总耗时 61.201 秒；expected/completed 均为 36，所有片段均持久化 |
| LLM | 真实 `Qwen/Qwen3.5-4B`，v5；1 次请求，5.541 秒；只发布 1 个通过现有结构、引用和时间校验的总结 |
| 总结结构 | 3 个章节、3 个知识要点；6 条证据全部命中对应转写原文及视频时间（6/6） |
| 引用回看 | 点击两条不同引用；播放器跳到目标时间并保持播放，Playwright 断言通过 |
| 重启读回 | 确认 task execution 已停止后，仅重启 Stage 2 API/Worker；API 健康后页面重载，再读回 task、36 段转写和总结，状态断言通过 |

最终 Playwright 输出的安全审计摘要含 `apiWorkerRestartReadback=true`。完成后 E2E 清理其成功测试账户和视频；此前失败/脚本断言失败的验收任务没有被清理。没有记录课程原文、完整转写或原始模型响应。

## 资源测量

最终通过的浏览器任务窗口约为 05:56:20–05:59:52 UTC。`.scratch/final-stage-0/resource-sampler.ps1` 每 10 秒采样一次；实际间隔约 12.5 秒，因采样还会递归统计文件并读取 Docker stats。CSV 记录 E 盘可用空间、主机可用内存、Stage 2 `app` 下含隐藏文件的数量/字节数，以及 API/Worker/PostgreSQL/Redis 容器内存。

| 指标 | 处理前 | 窗口内采样峰值/低点 | 处理后 |
| --- | ---: | ---: | ---: |
| E 盘可用空间 | 140,691,009,536 B（131.029 GiB） | 最低 137,401,507,840 B（127.965 GiB） | 137,469,476,864 B（128.028 GiB） |
| Stage 2 `app` 文件数 | 16 | 56 | 16 |
| Stage 2 `app` 字节数 | 15,247,745,587 B | 18,536,913,566 B | 15,247,745,587 B |
| 主机可用内存 | 13,822,959,616 B | 最低 11,978,526,720 B（11.156 GiB） | 13,820,936,192 B |
| API 容器内存 | 约 73.06 MiB | 98.69 MiB | 约 66.08 MiB |
| Worker 容器内存 | 约 105.2 MiB | 129.4 MiB | 约 83.75 MiB |
| PostgreSQL 容器内存 | 约 52.23 MiB | 59.34 MiB | 约 58.98 MiB |
| Redis 容器内存 | 约 9.18 MiB | 9.18 MiB | 约 8.70 MiB |

应用目录峰值比处理前多 3,289,167,979 B（约 3.063 GiB）；处理结束后回到完全相同的目录字节数和文件数，表示本次通过测试产生的临时文件已清理。E 盘可用空间结束值仍比处理前少 3,221,532,672 B；这个差额没有由 `app` 目录或本项目 Docker 分类解释，归因未验证，不能当作业务目录持久增长或本项目 Docker 增量。

Stage 2 四个容器的物理 JSON 日志通过 Docker Desktop WSL 文件路径按记录时间统计：处理前 347,524 B，窗口内追加 7,620 B，处理后 355,144 B；稍后的健康/数据库核查后当前总量 358,448 B。四个容器日志配置均为 `json-file`、最多 2 个文件、每个 10 MiB。容器可写层前后均为 API 16,384 B、PostgreSQL 86,016 B、Redis 4,096 B、Worker 24,576 B，合计 131,072 B；这项只做了处理前/后静态测量，处理中峰值未单独采样。

资源采样方法：`Get-PSDrive E` 读取卷可用字节，`Win32_OperatingSystem.FreePhysicalMemory` 读取主机可用内存，`Get-ChildItem -Force -Recurse` 统计 Stage 2 `app` 的文件/字节数，`docker compose stats --no-stream` 读取服务容器内存。容器可写层用 `docker inspect --size` 的 `SizeRw`；Docker 全局分类用 `docker system df`，API 镜像字节数用 `docker image inspect .Size`。物理日志文件位于 `\\wsl.localhost\docker-desktop\mnt\docker-desktop-disk\data\docker\containers\<container-id>`；按 JSON 记录时间筛选运行前、运行区间和运行后记录，并按 UTF-8 行字节数累加，没有输出或保存日志正文。

Docker 构建前全局统计为镜像 5.034 GB、容器 103.5 MB、卷 321.1 MB、Build Cache 7.683 GB；构建和验收后为 5.034 GB、103.5 MB、321.1 MB、7.684 GB。构建前后 Stage 2 API 镜像大小分别为 938,276,723 B 和 938,277,146 B（净差 423 B）；容器可写层前后相同。以 Build Cache 显示约增加 1 MB、镜像净差 423 B、可写层无差值及四服务日志最大保留 80 MiB 作保守估计，Docker 新增占用上界约 81 MiB，低于 7 GiB；全局 Docker 统计中其他项目未清理或迁移。

Stage 2 E 盘目录末次大小（不含 `.env` 与 secrets 内容）为：`.venv` 160,384,331 B、`acceptance` 537,262 B、`app` 15,247,745,587 B、`backups` 46,558 B、`postgres` 67,010,277 B、`redis` 170,663 B。样片不存入 Git。

## 工程验证

| 命令/检查 | 结果 |
| --- | --- |
| `python.exe -m pytest tests/test_summarization.py::test_versioned_summary_contract_preserves_v4_and_accepts_bounded_v5_output_budget -q`（Stage 2 venv） | 1 passed；固定验证 v4 hash、v4/6144 与 v5/8192 合同 |
| `data/mvp-stage-2/.venv/Scripts/python.exe -m pytest -q` | 83 passed，1 个 Starlette/AnyIO `BlockingPortal` 弃用警告 |
| `data/mvp-stage-0/.venv/Scripts/python.exe -m pytest probes/stage0/tests -q` | 32 passed；一次与后端测试并行时有 1 项 2 秒 deadline 断言受调度延迟影响，单独重跑 32 项通过 |
| README 完整 Ruff 命令 | 通过 |
| README 完整 mypy 命令 | 32 个源文件通过 |
| README `pip check` | `No broken requirements found` |
| Stage 1/Stage 2 Compose `config --quiet` | 两个私密配置均解析通过 |
| Stage 2 API `alembic check` | `No new upgrade operations detected` |
| `npm run lint` | 通过 |
| `npm run test -- --run` | 3 个测试文件、6 项通过 |
| `npm run build` | TypeScript 检查及 Vite 构建通过 |
| Playwright `stage5-flow.spec.ts`，v5/8192 | 1 passed（2.9 分钟）；完整流程、两条引用、重启读回和测试数据清理通过 |
| `git diff --check` | 通过；交付前再次运行 |

Windows 上原有 `test_ffprobe_timeout_stops_child_process_with_bounded_diagnostics` 使用 0.2 秒启动窗口会因 Python 子进程启动时序间歇失败。测试现在只在 Windows 使用 1.5 秒受控窗口并相应放宽总耗时断言，超时分类、子进程停止和 PID marker 断言仍保留；定向测试及全量后端套件通过。

关键复现命令（PowerShell，从仓库根目录执行）：

```powershell
$env:PYTHONPATH = (Resolve-Path 'src').Path
& .\data\mvp-stage-2\.venv\Scripts\python.exe -m pytest -q
& .\data\mvp-stage-2\.venv\Scripts\python.exe -m pytest tests\test_summarization.py::test_versioned_summary_contract_preserves_v4_and_accepts_bounded_v5_output_budget -q
& .\data\mvp-stage-0\.venv\Scripts\python.exe -m pytest probes\stage0\tests -q
& .\data\mvp-stage-2\.venv\Scripts\python.exe -m ruff check src tests migrations scripts\stage1 scripts\stage2 scripts\stage4 probes\stage0
& .\data\mvp-stage-2\.venv\Scripts\python.exe -m mypy --explicit-package-bases src migrations scripts\stage1 scripts\stage2 scripts\stage4 probes\stage0
& .\data\mvp-stage-2\.venv\Scripts\python.exe -m pip check
```

前端在 `frontend/` 下运行 `npm run lint`、`npm run test -- --run`、`npm run build`。完整真实浏览器命令在 `frontend/` 下运行：

```powershell
$env:MVP_ACCEPTANCE_VIDEO = (Resolve-Path '..\video\agent-skill介绍.mp4').Path
$env:MVP_ACCEPTANCE_PROMPT_VERSION = 'stage0-summary-v5'
$env:MVP_STAGE6_RESTART_SERVICES = '1'
npm run e2e -- e2e/stage5-flow.spec.ts --output=../data/mvp-stage-2/acceptance/playwright-stage0-v5-8192-pass1
```

Stage 1/2 配置检查分别使用 `docker compose --env-file data\mvp-stage-1\.env -p videoclip-mvp-stage1 -f compose.yaml config --quiet` 和 `docker compose --env-file data\mvp-stage-2\.env -p videoclip-mvp-stage2 -f compose.yaml config --quiet`；迁移检查使用 `docker compose --env-file data\mvp-stage-2\.env -p videoclip-mvp-stage2 -f compose.yaml exec -T api alembic check`。

## 账户证据、边界与未验证项

- 本机 API 凭据和真实请求证明本次 `SenseVoiceSmall` 与 `Qwen/Qwen3.5-4B` 可调用；这不能证明账户仍有免费额度或零费用。没有登录账户账单/余额页面或付款记录，账户侧可用额度、价格和实际扣费未验证。本地 `.env` 的零费率字段仅作为本地配置，不作为账单事实。
- SiliconFlow 官方发布说明已标记 `/user/info` 用户信息接口停用；因此本次没有用本地零价格替代账户账单证据：[官方发布说明](https://docs.siliconflow.cn/docs/release-notes/overview)。模型 API 文档说明 `finish_reason=length` 可能来自输出上限或上下文上限，见[官方文本生成文档](https://docs.siliconflow.cn/docs/userguide/capabilities/text-generation)。官方 Qwen3.5-4B 模型卡给出的架构上下文长度高于本任务 256k 快照，但不代表 SiliconFlow 账户账单：[模型卡](https://huggingface.co/Qwen/Qwen3.5-4B)。
- 本窗口不验 20 分钟真实模型处理、不实现最终版分片上传/自动恢复/Beat/维护 Worker/排队补偿/监督器/媒体单独删除，也不评估总结语义准确率。
- 容器可写层的处理中峰值没有单独采样；E 盘空闲空间前后差额也未归因。两项按测量事实和限制如实记录，不扩展为容量限制或清理操作。
- 第一次真实浏览器尝试使用 Playwright 默认的 `frontend/test-results` 输出目录；该目录执行前已存在，但内容未作清点，Playwright 可能清理过其中原有的 ignored 工件。之后的浏览器尝试都使用本窗口专用输出目录；此影响无法事后确认或恢复。

## 可复现产物

- `.scratch/final-stage-0/resource-sampler.ps1`：Stage 2 E 盘、业务目录与容器内存采样脚本。
- `.scratch/final-stage-0/resource-samples.csv`：早期 v4 任务的资源轨迹。
- `.scratch/final-stage-0/resource-samples-v5.csv`：v5 任务前、运行中和结束后的资源轨迹。
- 失败 Playwright 输出和失败 task/attempt 留在本机 Stage 2 acceptance 数据目录/数据库；其中可能有页面上下文，只保留本地，不加入 Git。
