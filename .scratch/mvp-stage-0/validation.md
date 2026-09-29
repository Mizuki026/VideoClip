# 第 0 窗口：模型与环境验证

- 日期：2026-09-29
- 分支：codex/mvp

结论：**本窗口当前范围内验收通过，可进入第 1 窗口。** 此结论只适用于本窗口验证和用户确认的范围；不代表 MVP 整体验收完成。上传 1 GiB 单文件上限与业务数据目录 5 GiB 运行期硬限制、容量准入和写满拒绝测试均已由用户暂时取消，不计作通过项或未通过项。

## 范围与交付

实现与验证仅涉及 probes/stage0/、对应测试、.gitignore、阶段规格和产品 / 技术文档；没有实现后续窗口业务 API、数据库模型、Worker、任务状态机、正式模型适配器或前端。

- 授权课程源视频位于仓库根目录 video/，并加入 Git 忽略规则；探针输出、音频切片、运行报告、PostgreSQL / Redis 持久化与备份均在 E:\VideoClip\data\mvp-stage-0 下。
- 探针接受 video/ 和私密 data/mvp-stage-0/input/ 下的 MP4 样本，媒体有效时长须大于 0 且不超过 1200 秒；不检查或拒绝上传文件字节数。
- 1,116,803,236 字节的实际样本成功通过读取、ffprobe、FFmpeg 音频提取、完整切片、真实 ASR 和真实 LLM 总结闭环。此大小超过 1 GiB，实际证明探针没有按 1 GiB 拒绝。
- 密钥只从 data/mvp-stage-0/.env 读取；命令行输出只含非敏感模型 / 配置元数据和随机报告名，未打印密钥、转写、引文或响应正文。
- 总结最多 3 次请求（首次加最多 2 次受限修复）；响应上限 1 MiB，媒体和网络步骤均有期限及停止宽限期。没有加入正式数据库检查点或业务自动重试。

用户暂时取消的存储范围决定也同步记入 [MVP 产品设计](../../docs/product/mvp.md)、[MVP 技术设计](../../docs/technical/mvp.md) 和本阶段规格。普通文件系统写入失败仍作为普通 I/O 操作失败处理；本版不承诺数据目录总量上限。

## 真实模型闭环

最后一次完整实测报告保存在私密目录 data/mvp-stage-0/reports/3fef05b503bf.json。全量报告和 ASR 转写不纳入 Git。

| 项目 | 实测 |
| --- | --- |
| ASR | FunAudioLLM/SenseVoiceSmall；13 个 30 秒切片（最后一片约 8.6 秒）均返回 HTTP 200；请求合计 12.576 秒，单次最长 2.016 秒。 |
| LLM | Qwen/Qwen3.5-4B；提示词 stage0-summary-v4；enable_thinking=false；输出预留 3072 tokens；HTTP 200。 |
| 总结请求 | 第 1 次 149.045 秒，在 3072 completion tokens 处截断；报告保留 prompt / completion 用量。第 2 次使用一次修复后 111.499 秒完成，finish_reason=stop。总计 2 次请求，未超过 3 次上限。 |
| 输出与引用 | 4 个章节、6 个知识要点、10 条引用。LLM 选择支持该条内容的片段编号；探针从对应 ASR 原文机械选取与摘要文字最长连续重合的最多 48 字符片段，再按 NFKC、大小写折叠、移除空白的规则验证连续匹配。实测引文长度为 22–48 个字符。非法片段或模型直接提交 quote 会失败。 |
| 耗时 | 完整探针 276.693 秒；LLM 阶段 260.553 秒。 |
| 用量 | 本次两次 LLM 调用合计 prompt_tokens=3,626、completion_tokens=3,399（包含第 1 次截断响应）。 |
| 费用依据 | 本地私密配置中的账户模型价格为零，按实际返回 token 与 ASR 分钟数估算为 USD 0.00。报告明确标记这不是账单回执。用户配置确认使用账户免费模型；实际剩余额度和账单页面未直接读取，运行期间未观察到 429 限流。 |

以前的真实尝试也观察到 LLM 超时、引文不匹配、候选索引错误和输出截断；所有最终结果都必须通过相同的结构与来源校验，失败调用不算通过。当前提示词由真实输出试验迭代得出。技术术语和语义质量仍需课程熟悉者人工审阅；产品设计明确不以语义完整性或知识点覆盖率作为工程验收门槛。

硅基流动官方资料：

- [ASR 转写接口](https://docs.siliconflow.cn/docs/api/audio-transcriptions-post)
- [Chat Completions 接口](https://docs.siliconflow.cn/docs/api/chat-completions-post)
- [文本生成和上下文说明](https://docs.siliconflow.cn/docs/userguide/capabilities/text-generation)
- [免费模型、费率及限流说明](https://docs.siliconflow.cn/docs/userguide/faqs/rate-limit-and-upgradation)

官方说明与本地账户配置用于费用依据；实际可用性另外由上述真实请求确认。费率、限额和模型目录会变动，后续运行前应重新核对账户控制台。

## 20 分钟预算与媒体时间轴

### 上下文预算

最后一次运行观察到 ASR 转写共 6,723 UTF-8 字节，对应 368.597 秒。按完整转写密度线性外推到 1200 秒，并加 1.5 倍密度余量：

- 完整转写估算：ceil(6,723 × 1,200 / 368.597 × 1.5) = 32,831 UTF-8 字节。
- 固定系统提示、完整提示词及 40 个片段头：2,444 字节。
- 输出预留：3,072 tokens；消息框架余量：64 tokens。
- 估算上界合计：38,411 tokens；模型配置上下文：256,000 tokens，低于配置长度。
- 本次服务端实际 prompt_tokens 总计 3,626（包括一次修复请求）；短样本成功本身不代替 20 分钟估算。本次没有实际转写一段 20 分钟视频，课程语速差异仍是估算限制。

UTF-8 字节数作为文本 token 数的保守上界；估算不截断转写，完整 prompt、输出预留、40 段头及 64 token 框架余量均计入。估算方法和结果也记录在最终探针报告中。

### 时间映射

授权样本：video/如何选择适合自己的agent.mp4。

| 证据 | 结果 |
| --- | --- |
| 文件与时长 | 1,116,803,236 字节，368.600 秒（6 分 8.6 秒）。 |
| 容器与编码 | MP4 家族；单路 H.264 yuv420p 视频、单路 AAC 音频；音频提取为 16 kHz、单声道、s16le。 |
| 时间基与起点 | 视频时间基 1/30000，音频 1/48000；容器和两条流的 start_time 均为 0.000000 秒。 |
| 实际播放器检查 | 配置目录中的 ffplay 在 0、30、360 秒开始播放，每次播放约 2 秒；视频帧正常推进，A/V 差值约在 ±0.020 秒内。探针保留的 13 段视频区间与音频区间相同，映射偏移为 0 秒。 |
| 延迟轨拒绝 | 在私密 input/ 中用授权源构造视频起点 0、音轨延迟 0.5 秒的 62 秒 MP4 fixture；ffprobe 识别偏移后，探针返回 UNSUPPORTED_TIMELINE_OFFSET，且在进入 ASR / LLM 前拒绝。安全报告为 data/mvp-stage-0/reports/798417e40231.json。 |

据此仅将“本次验证的 MP4 + H.264 yuv420p + AAC + 单音视频流 + 容器 / 两条流起点均为零”列为已实测候选。没有将时间戳偏移统一修正，也不宣称所有 MP4 均受支持；其他编码、非零起点、多音轨和未知时间基仍应拒绝或另行验证。

以下命令可重现播放器跳转及延迟音轨拒绝。fixture 和全部生成文件只写入私密 data/mvp-stage-0/input/：

~~~~powershell
$sample = 'video\如何选择适合自己的agent.mp4'
foreach ($position in @(0, 30, 360)) {
  & 'C:\Program Files\ffmpeg\bin\ffplay.exe' -hide_banner -loglevel warning -stats -ss $position -t 2 -autoexit $sample
  if ($LASTEXITCODE -ne 0) { throw "ffplay seek $position failed: $LASTEXITCODE" }
}
$delayed = 'data\mvp-stage-0\input\stage0-delayed-audio-rejection.mp4'
& 'C:\Program Files\ffmpeg\bin\ffmpeg.exe' -y -hide_banner -loglevel error -i $sample -itsoffset 0.5 -i $sample -t 62 -map 0:v:0 -map 1:a:0 -c:v copy -c:a copy -copyts -avoid_negative_ts disabled -movflags +faststart $delayed
& data\mvp-stage-0\.venv\Scripts\python.exe probes\stage0\probe.py $delayed
~~~~

FFmpeg / ffprobe 位于 C:\Program Files\ffmpeg\bin，实测版本为 2026-09-28-git-84779ade26-full_build-www.gyan.dev。

## PostgreSQL / Redis 与 Docker

| 项目 | 结果 |
| --- | --- |
| E 盘数据 | PostgreSQL 16.15 与 Redis 7.4.11 使用本项目 Compose 和 E 盘 bind mount。docker inspect 确认 PG 数据 / WAL、Redis AOF / RDB、备份及恢复副本均位于 data/mvp-stage-0 下，没有改用默认 Docker 卷。 |
| 重启 | 在 PostgreSQL 与 Redis 容器各自重启后，探针专用 PostgreSQL 行与 Redis key 可重新读取。 |
| 备份恢复 | PostgreSQL 使用 pg_dump / pg_restore 恢复到隔离探针库并核验；Redis RDB 复制到独立路径并由恢复容器读取。BGSAVE 与 BGREWRITEAOF 后台完成状态已确认。证据只保留在被忽略的 data/mvp-stage-0/postgres、redis、backup 目录。 |
| Docker 基线 | 处理前记录：263 个镜像 / 4.094 GB，18 个容器 / 101.9 MB，15 个卷 / 318.6 MB，187 项构建缓存 / 6.649 GB。复核：镜像、卷、构建缓存未变；本项目增加 3 个停止容器，容器汇总约 102 MB。 |
| 本项目容器 | 三个 Stage 0 容器 SizeRw 总计 49,152 字节；原验证记录的 JSON 日志实测合计 28,713 字节，合计新增约 77,865 字节，远低于 7 GiB。没有新增镜像或构建缓存；原有 Docker 项目未被修改。媒体探针在宿主机运行，未为尚不存在的业务 API / Worker 构建镜像。 |

本窗口只允许建立数据库探针所需的目录与测试数据。用户已取消的 5 GiB 硬限制、I / M 预算、容量准入水位、临界水位维护记录写入以及磁盘写满拒绝测试均未实施、未执行，也没有标记为通过。

## 复现

在 PowerShell 中执行。密钥只保留在本地私密配置，不粘贴到命令行：

~~~~powershell
$env:Path = 'C:\Program Files\ffmpeg\bin;' + $env:Path
& data\mvp-stage-0\.venv\Scripts\python.exe -m pytest probes\stage0\tests -q
& data\mvp-stage-0\.venv\Scripts\ruff.exe check probes\stage0
& data\mvp-stage-0\.venv\Scripts\mypy.exe --explicit-package-bases --strict probes\stage0\probe.py probes\stage0\tests\test_probe.py
& data\mvp-stage-0\.venv\Scripts\python.exe -m compileall -q probes\stage0
& data\mvp-stage-0\.venv\Scripts\python.exe probes\stage0\probe.py video\如何选择适合自己的agent.mp4
~~~~

必要配置位于 data/mvp-stage-0/.env：SiliconFlow API key、ASR / LLM 模型、免费资格确认和价格依据、256000 上下文长度、3072 输出上限、HTTP / 媒体有限期限、1200 秒媒体时长、30 秒切片、音频采样规格和提示词版本。探针忽略旧的 MAX_VIDEO_BYTES 设置，不把它作为准入条件。

PostgreSQL / Redis 仅使用本项目参数验证：

~~~~powershell
docker compose -p videoclip-mvp-stage0 --env-file data\mvp-stage-0\.env -f probes\stage0\compose.yaml config --quiet
.\probes\stage0\verify-persistence.ps1
~~~~

安全停用时只对同一个 videoclip-mvp-stage0 项目执行 docker compose stop 或 down；down 不删除 E 盘 bind mount 数据。不要清理全局 Docker、其他项目或 E:\VideoClip\data 根目录。

## 自动验证

最后一轮代码验证执行结果：

| 命令 | 结果 |
| --- | --- |
| python -m pytest probes/stage0/tests -q | 32 passed |
| ruff check probes/stage0 | All checks passed |
| mypy --explicit-package-bases --strict probes/stage0/probe.py probes/stage0/tests/test_probe.py | Success: no issues found in 2 source files |
| python -m compileall -q probes/stage0 | 退出码 0 |
| python -m pip check | No broken requirements found |
| PowerShell Parser 检查两个 Stage 0 脚本 | passed |
| docker compose ... config --quiet | 退出码 0 |
| git diff --check | 退出码 0 |

受控模拟覆盖 HTTP 挂起 / 连续慢响应、媒体进程超时和停止、鉴权 / 限流分类、响应与媒体输出上限、非法结构和引用、输出截断时用量保留及最多 3 次总结请求。真实运行实际观察到 HTTP 超时、输出截断和修复成功；没有真实遇到鉴权错误或 429。

## 关卡状态与限制

| 关卡 | 状态 | 证据 |
| --- | --- | --- |
| 1. 真实免费 ASR → LLM → 引用校验 | **通过** | 最新报告 3fef05b503bf.json：13/13 真实 ASR 请求成功；真实 LLM 两次调用后结构、片段和原文连续引用校验通过。 |
| 2. 20 分钟估算与时间映射 | **通过（估算 + 本样本实测）** | 保守 token 上界 38,411 / 256,000；实际 FFplay 播放 / 跳转零起点样本并观察 A/V 同步，偏移 fixture 在真实探针中明确拒绝。20 分钟转写未实际运行。 |
| 3. E 盘 PostgreSQL / Redis 持久化 | **通过** | 本项目 bind mount、重启读取、PostgreSQL 与 Redis 备份 / 恢复记录齐全。 |
| 4. 5 GiB 硬限制、准入水位与写满拒绝 | **用户取消** | 不实施、不测试、不作为关卡。 |
| 5. 有界执行与非法输出 | **通过** | 32 项探针测试、Ruff、严格 mypy、compileall、配置与脚本检查通过。 |
| 6. Docker 新增占用 ≤ 7 GiB | **通过（Stage 0 探针范围）** | 无新镜像 / 构建缓存；仅约 77,865 字节本项目容器写入层及日志。完整业务容器尚未建设，故本证据不代表未来应用镜像峰值。 |
| 7. 隔离、配置及交接 | **通过** | 源样本路径被 Git 忽略；所有临时数据、输出、报告和数据库备份留在隔离 data/mvp-stage-0；密钥无终端输出、无提交。 |

剩余限制：账户实际剩余额度及账单未从控制台读取；本次 USD 0.00 是基于用户配置价格的模型用量估算。20 分钟上界来自本次样本的转写密度外推，不代表真实 20 分钟视频实测。摘要术语和语义质量未人工量化，课程熟悉者可在进入后续验收前审阅私密转写 / 总结。Stage 0 结论只允许进入第 1 窗口，不代表 MVP 完成。
