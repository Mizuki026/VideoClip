# 第 0 窗口：模型与环境验证

日期：2026-09-29

分支：`codex/mvp`（开始时 HEAD=`6e8405c`，其父提交为 `b7e1210`）

结论：**阻塞；当前不允许进入第 1 窗口。** 独立探针、自动验证和 PostgreSQL / Redis E 盘持久化已完成；真实免费模型闭环、实际播放器时间轴以及 5 GiB 硬限制仍缺少必要条件。各项未执行内容没有标为通过。

## 范围与交付

实现限于 `probes/stage0/`、对应测试、忽略数据的 `.gitignore` 和本记录；未创建正式业务模块、API、业务表、Worker、任务状态或容量预留生命周期。

已完成的探针能力：

- SiliconFlow ASR 分片串行调用和 Chat Completions 总结；调用参数来自 E 盘 ACL 保护的本地 `.env`。代码拒绝非零模型价格，不从进程环境读取密钥，不自动重试模型请求。
- 对 1–3 分钟样本生成完整 30 秒片段清单；ASR 结果必须覆盖清单；总结采用严格结构，引用必须指向本次片段，引用经过 NFKC、大小写折叠、移除空白后按连续子串匹配。时间只按引用片段推导。
- 总结最多 3 次请求（首次加最多 2 次格式 / 引用修复）。上下文估算不截断转写，使用 UTF-8 字节上界、完整提示词、40 个片段头、输出预留和 64 个消息框架 token；20 分钟估算按样本转写密度外推并增加 1.5 倍密度余量。报告同时记录服务端返回的实际 `prompt_tokens` / `completion_tokens`（若响应提供）。
- 媒体探针限定候选输入为 `.mp4`、H.264 `yuv420p`、AAC、单音轨 / 单视频流、格式和音视频流起点均为 0 且时间基可读取。偏移或证据不足时明确拒绝。该候选白名单尚未通过实际课程视频及播放器核验，不能当作已验证播放白名单。
- HTTPX 有有限连接 / 读写期限、async 总期限和 1 MiB 流式响应上限；FFmpeg 子进程有总期限、停止宽限、stdout 上限，stderr 不写入报告。错误报告只记录错误码，不输出响应正文、密钥或转写文本到终端。

## 本机基线

| 项目 | 检查结果 |
| --- | --- |
| Windows / shell | PowerShell 7.6.5；E: 为 NTFS。当前 PowerShell 非提升管理员；`fsutil quota query E:` 返回 `Error 5: Access is denied.` |
| E: 空间 | 检查开始时 272,628,707,328 字节总量、163,578,130,432 字节可用；验证后可用 163,384,496,128 字节。期间的全盘差值不能单独归因于探针。 |
| Python | 主机 `python` 为 3.13.12（另有 Python 3.14.2）；探针隔离环境使用 3.13.12。 |
| FFmpeg | 主机 PATH 未找到 `ffmpeg` 或 `ffprobe`；因此版本、音频提取、切片和媒体时间映射实测未执行。 |
| Docker | Docker Engine 29.8.0，Compose v5.5.1，Linux engine；Docker root 为 `/var/lib/docker`。 |
| Docker 基线 | `docker system df`：263 镜像 / 4.094 GB，18 容器 / 101.9 MB，15 卷 / 318.6 MB，187 构建缓存 / 6.649 GB；检查时 0 个容器运行。原有对象均保留。 |
| 原有实现 | 仓库开始时只有 MVP 设计 / 技术文档和本窗口 spec；没有探针、代码、已有验证记录或媒体样本。 |
| API 配置 / 样本 | 检查开始时没有 SiliconFlow 私密配置或相关进程环境变量；`data/mvp-stage-0/input` 中没有视频。准备脚本只生成空白模型配置和本地随机数据库口令。 |

官方能力资料（2026-09-29 查阅）：

- ASR 官方端点为 `/v1/audio/transcriptions`，要求 Bearer token，文档列出 `FunAudioLLM/SenseVoiceSmall` 和 `TeleAI/TeleSpeechASR`；这不证明当前账号有免费额度或当前可调用：[ASR API 文档](https://docs.siliconflow.cn/docs/api/audio-transcriptions-post)。
- 对话接口为 `/v1/chat/completions`，模型列表、价格和上下文长度依赖模型广场 / 账号当前状态：[Chat Completions API](https://docs.siliconflow.cn/docs/api/chat-completions-post)、[文本生成与上下文说明](https://docs.siliconflow.cn/docs/userguide/capabilities/text-generation)。
- 官方说明免费模型调用费用为 0，但账号认证、免费模型列表和速率限制按模型 / 账号变化；当前账号未能查询：[速率、免费计费与额度说明](https://docs.siliconflow.cn/docs/userguide/faqs/rate-limit-and-upgradation)。没有把历史第三方页面的模型目录视作当前账号证据。
- Windows NTFS 的 `fsutil quota` 是按卷、按用户限额；Microsoft 文件服务器 FSRM 文档支持目录配额，但当前主机查询权限不足，尚未验证可用的项目隔离配额：[NTFS fsutil quota](https://learn.microsoft.com/en-us/windows-server/administration/windows-commands/fsutil-quota)、[FSRM 目录配额](https://learn.microsoft.com/en-us/windows-server/storage/fsrm/create-quota)。

## 关卡状态

| 关卡 | 状态 | 证据与缺口 |
| --- | --- | --- |
| 1. 真实 ASR → LLM → 引用校验 | **阻塞 / 未执行** | 探针配置名和合法样本路径已提供，但本机没有 API key、账号可用的免费模型清单、用户授权课程样本，也没有 FFmpeg。未发送真实模型请求；模型耗时、用量、账单和技术术语表现均未测。 |
| 2. 20 分钟预算与媒体时间轴 | **阻塞 / 未执行** | 已实现可复现估算：样本 transcript UTF-8 字节 ÷ 样本秒数 × 1200 × 1.5，另加固定提示词、40 段头、输出 token 和 64 token 框架余量；但无真实样本 / 模型，不能填入观测转写密度或服务端 token 计数。音频偏移 fixture 单测会拒绝非零起点；未运行 ffprobe，也未通过浏览器 / 播放器对齐验收。候选白名单仅为 MP4 + H.264 `yuv420p` + AAC + 所有起点为零，尚未批准为支持格式。 |
| 3. E 盘持久化 / 重启 / 备份恢复 | **通过** | 专属 Compose 使用 PostgreSQL `16-alpine@sha256:3c5c8892…`、Redis `7.4-alpine@sha256:520775a4…`，实测版本 PostgreSQL 16.15、Redis 7.4.11。`docker inspect` 确认 PG 数据、WAL、备份、Redis AOF / RDB、恢复副本均绑定到 `E:\VideoClip\data\mvp-stage-0`。探针行及 Redis key 在对应容器重启后可读；PG `pg_dump` / `pg_restore` 到独立探针数据库成功；Redis RDB 复制到隔离目录并由恢复容器读取成功。Redis BGSAVE 与 BGREWRITEAOF 后台完成状态也已轮询确认。证据文件位于被忽略的 `data/mvp-stage-0/postgres`、`redis`、`backup`。 |
| 4. 5 GiB 底层硬限制、I / M 和临界水位 | **阻塞 / 未执行** | E: 是 NTFS，但当前会话没有提升权限，`fsutil quota query E:` 被拒绝；现有 E 盘绑定目录没有底层限额。没有用监测 / 告警冒充硬限制，也未做空间拒写、后台持久化峰值配额、临界水位维护写入与回收测试。`L=5,368,709,120` 字节；因底层强制边界尚不存在，`I`、`M` 与普通准入水位 `L-I-M` 不作虚假定值。观测到探针数据目录当前共 160,798,082 字节（含 88,616,012 字节隔离 Python 环境；PG 目录 72,178,302 字节、Redis 目录 396 字节、备份目录 1,986 字节）；这是小型初始化观测，不代表基础设施峰值或维护预算。 |
| 5. 有界执行 / 非法输出 | **通过（受控模拟）** | 19 个 pytest 全过。模拟持续等待与慢速分块 HTTP 均受总期限终止；媒体挂起子进程在宽限内退出；401、429 分别分类且不重试；超大响应 / 媒体输出拒绝；错误引用失败；总结请求上限精确为 3。没有真实 API 失败可记录，故真实鉴权 / 限流 / 上游错误仍未观测。 |
| 6. Docker 新增占用 ≤ 7 GiB | **当前探针配置通过；完整样本关卡未执行** | 前后 `docker system df`：镜像保持 263 / 4.094 GB、卷保持 15 / 318.6 MB、构建缓存保持 187 / 6.649 GB；只增加本项目 3 个停止容器，容器总显示由 101.9 MB 至 102 MB。三个本项目容器 `SizeRw` 共 49,152 字节，JSON 日志实际合计 28,713 字节，合计约 77,865 字节；日志轮转上限为每容器 2 × 10 MiB。PG / Redis 镜像在开始基线中已存在，没有拉取新镜像或构建；未修改其他项目。未处理真实媒体，故真实样本后的峰值尚未测。 |
| 7. 隔离、配置、交接 | **部分通过** | `data/` 被 Git 忽略；准备脚本将数据 ACL 限定到当前用户、SYSTEM 和 Administrators；模型密钥只由固定私密文件 `data/mvp-stage-0/.env` 加载，不支持环境变量覆盖，当前为空。探针、锁文件、Compose、测试、启动 / 持久化脚本和本记录将与本窗口逻辑提交一并交付。 |

Docker 新增占用还没有构建 / 真实课程处理样本峰值。当前 Docker 工作集已有约 11.16 GB 的全局对象；这不是本窗口的新增量，也没有清理或迁移它。

## 依赖与版本锁定

| 部件 | 实测版本 | 锁定 / 状态 |
| --- | --- | --- |
| Python | 3.13.12 | 探针虚拟环境 |
| HTTPX | 0.28.1 | `requirements.lock` |
| pytest | 8.3.5 | `requirements.lock` |
| Ruff | 0.11.4 | `requirements.lock` |
| mypy | 1.15.0 | `requirements.lock` |
| anyio / httpcore / certifi / idna | 4.15.1 / 1.0.9 / 2026.7.22 / 3.20 | `requirements.lock` |
| PostgreSQL / Redis | 16.15 / 7.4.11 | Compose 镜像 digest 固定 |
| FFmpeg / ffprobe | 无 | 尚未安装 / 验证；不宣称兼容性 |

锁文件包括隔离环境的直接和传递依赖。本记录不声明未来版本兼容。

## 复现命令

所有私密配置、模型返回、转写、音频、媒体、数据库、备份及探针工作文件放在受 ACL 保护且 Git 忽略的 `E:\VideoClip\data\mvp-stage-0`。密钥绝不传入命令行或聊天。

```powershell
.\probes\stage0\prepare-private-data.ps1
python -m venv data\mvp-stage-0\.venv
& data\mvp-stage-0\.venv\Scripts\python.exe -m pip install -r probes\stage0\requirements.lock
```

在 `data\mvp-stage-0\.env` 本地填写：

- `SILICONFLOW_API_KEY`；`SILICONFLOW_ASR_MODEL`、`SILICONFLOW_LLM_MODEL` 使用当前账号目录中确认免费的模型 ID。
- `SILICONFLOW_FREE_USE_CONFIRMED=I_CONFIRMED_BOTH_MODELS_ARE_FREE_IN_MY_ACCOUNT`。
- 两个 `*_PRICE_EVIDENCE` 的当前账号模型广场 / 账单依据；ASR `*_USD_PER_MINUTE` 与 LLM 输入 / 输出每百万 token 价格均须是已核实的 `0`。非零价格会被探针拒绝。
- `LLM_CONTEXT_TOKENS` 填模型广场当前上下文长度；请求参数、最大输出、期限、诊断上限、30 秒切片和提示词版本均可在同一文件配置。

安装并将同一 FFmpeg 发行版的 `ffmpeg.exe`、`ffprobe.exe` 加到当前进程 PATH 后，复制一段**授权使用的 1–3 分钟真实中文技术课程 MP4** 到 `data\mvp-stage-0\input\`，运行：

```powershell
& data\mvp-stage-0\.venv\Scripts\python.exe probes\stage0\probe.py data\mvp-stage-0\input\sample.mp4
```

成功或失败摘要仅输出状态和随机报告名；详细报告、转写和时间映射保留在 `data\mvp-stage-0\reports` 与 `work`。报告中的估计费用按 ASR 分钟价和 LLM 实际 token 数计算，必须再与 SiliconFlow 账号账单核对。探针当前不记录人工技术术语准确率，真实运行后需由课程熟悉者审阅私密转写并补入本记录。

仅重启 / 验证本项目数据库：

```powershell
docker compose -p videoclip-mvp-stage0 --env-file data\mvp-stage-0\.env -f probes\stage0\compose.yaml config --quiet
.\probes\stage0\verify-persistence.ps1
docker compose -p videoclip-mvp-stage0 --env-file data\mvp-stage-0\.env -f probes\stage0\compose.yaml ps -a
```

安全停用只用上述同一 `-p videoclip-mvp-stage0` 的 `docker compose ... stop` 或 `down`；`down` 不删除 E 盘 bind-mounted 数据。移除探针数据前先确认 PostgreSQL / Redis 已停止、备份和报告已不再需要，再只删除精确路径 `E:\VideoClip\data\mvp-stage-0`。不得对其他 Compose 项目、全局 Docker 数据或 `E:\VideoClip\data` 根目录执行清理。

## 本窗口检查命令及结果

直接测试先运行：

```powershell
& data\mvp-stage-0\.venv\Scripts\python.exe -m pytest probes\stage0\tests -q
```

结果：`19 passed`。随后运行：

```powershell
& data\mvp-stage-0\.venv\Scripts\ruff.exe check probes\stage0
& data\mvp-stage-0\.venv\Scripts\mypy.exe --explicit-package-bases --strict probes\stage0\probe.py probes\stage0\tests\test_probe.py
& data\mvp-stage-0\.venv\Scripts\python.exe -m compileall -q probes\stage0
docker compose -p videoclip-mvp-stage0 --env-file data\mvp-stage-0\.env -f probes\stage0\compose.yaml config --quiet
```

结果分别为 Ruff 全通过、mypy `Success: no issues found in 2 source files`、compileall 退出码 0、Compose 配置解析退出码 0。两份 PowerShell 脚本也通过 PowerShell Parser 语法检查。没有运行前端构建、完整业务测试或真实模型请求，因为仓库没有对应实现，且这些不属于本窗口独立探针范围。

## 下一步与范围决定

1. 提供一段获授权的真实中文课程样本，放入上述 `input` 目录；本地创建并填写受 ACL 保护的 SiliconFlow `.env`，不在聊天传密钥。之后才能测当前账号免费 ASR / LLM、真实费用、术语表现、20 分钟 token 估算和普通 / 偏移视频时间轴。
2. 请本机管理员提供并启用覆盖 `E:\VideoClip\data` 所有业务写入者的专属 5 GiB 硬限制文件系统 / 卷；保留 E 盘路径可以由管理员准备专用限额卷并挂载到该路径。当前用户会话既非提升管理员，NTFS `fsutil quota` 也只提供按卷 / 用户限制，所以本探针无法自行证明目录硬限制。配置完成后才能测试小配额拒写、实际 5 GiB 配置覆盖、后台峰值、临界水位维护写入和回收，再确定 `I`、`M` 与 `L-I-M`。
3. 若这台主机无法提供满足上述条件的受限存储，暂停第 1 窗口，先由产品范围决定调整运行环境 / 存储位置或重新讨论 5 GiB 硬上限；不能以定时统计、告警或本次低占用观测替代硬限制。
