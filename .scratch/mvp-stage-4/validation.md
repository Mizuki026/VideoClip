# 第 4 窗口验收记录：正式总结与引用发布

> 结论：**第 4 窗口后端正式总结与引用发布阶段通过。** 本记录不代表 React 页面、同源 `/api` 代理、浏览器点击播放或整个 MVP 已完成。验收日期：2026-10-02；分支：`codex/mvp`；实现提交：`60219d3`。

## 范围与保护

- 按 `.scratch/mvp-stage-4/spec.md` 实施正式总结、严格引用验证、持久化发布与 summary-only 恢复；没有实现 React 页面、`/api` 代理或公开部署。
- 第 2 窗口的既有授权 task `8182f66b-a140-411d-ba2d-7df6882da107` 仍为 `awaiting_summary`，原 attempt 与 13/13 转写保持不变。另一个旧 `awaiting_summary` task 也做了只读检查。两者都因持久化清单指纹与现存片段重算结果不一致而无法通过来源校验；API 返回 `TRANSCRIPT_SOURCE_MISMATCH`。没有改写旧记录、强制重领旧 attempt 或调用 LLM。
- 预迁移备份：`data/mvp-stage-2/backups/videoclip-stage4-pre-migration.dump`，46,558 字节；SHA-256：`74DDBB29559E60C8624C7E5A3071DD6D46C8882B0302514299078F1B11B4E09E`；`pg_restore --list` 检查通过。迁移前 Stage 2 有 3 个 task、2 个 `awaiting_summary`，没有未确认 execution。
- 为遵守旧清单来源校验，使用原授权视频经正式上传 API 建立两个独立验收样本，没有复用失配的旧转写。第一个诊断 task `c91346ba-eb64-4275-a451-0ece5990fe6b` 保留为两次 `LLM_PHASE_TIMEOUT` 失败记录。成功 task `f3e31e89-0323-4cb5-992a-62151ea42ab7` 的视频 SHA-256 与授权来源相同。Stage 2 最终 5 个视频 / task、1 个总结、0 个未确认 execution；原三条业务记录、账号、视频和转写未删除。临时验收 session 在各次运行的 `finally` 中撤销并删除。
- Stage 2 PostgreSQL / Redis 容器和数据目录未重建；只重建 / 重启本项目 API 与单 Worker。Stage 1 与 Stage 2 API 最终均健康。未清理其他 Docker 项目、镜像或缓存。

## 实现

- `src/videoclip/summarization.py`：固定 `stage0-summary-v4` / `summary-v1` 结构、旧快照可证明参数的兼容规则、完整转写清单与来源验证、提示词 / 响应结构哈希、上下文预算、带修复反馈的请求指纹，以及按 NFKC / casefold / 空白规则生成连续原文引文和视频区间。
- `src/videoclip/llm_client.py`：SiliconFlow 有界 HTTP 适配器；密钥只从既有 `SILICONFLOW_API_KEY` 运行环境读取。响应体、诊断长度、连接 / 读 / 写 / 总期限均受限；日志与审计保留稳定错误码、耗时、usage、finish reason 和供应商追踪 ID，不写完整 prompt、转写或原始模型响应。
- `src/videoclip/analysis.py`、`analysis_worker.py`、`main.py`：幂等总结接力、task / attempt / execution 资格约束、summary-only 恢复、拒绝不完整或失配清单、只读 `GET /videos/{id}/summary`。模型调用外不持有长事务；结果、task / attempt 成功状态、请求审计和清理意图同事务写入；删除竞态或发布失败不能暴露半成品。请求最多三次按**整个 task**累计，恢复 attempt 不重置额度。
- `src/videoclip/models.py`、`schemas.py` 与迁移 `migrations/versions/c52d4b5ad389_summary_publication.py`：总结状态、发布表与请求审计表、唯一 task / video 发布约束和响应结构。`services.py` 及 `scripts/stage2/stop-and-confirm-worker.ps1` 同步总结清理与停止确认状态。
- `scripts/stage4/summary-acceptance.py`：仅输出稳定错误码、模型 / 版本 / 计数 / 耗时的受限真实验收辅助程序；不输出引文、转写或模型响应。

## 真实闭环

- 真实模型：SiliconFlow `Qwen/Qwen3.5-4B`；提示词 `stage0-summary-v4`；结构 `summary-v1`；温度 0.2、关闭 thinking、输出预留 3072 tokens、上下文 256,000 tokens。官方资料将该 4B 预览模型列为免费模型，并说明免费模型调用费用为 0；本次真实调用成功，但未读取账户余额或账单回执。[模型说明](https://www.siliconflow.cn/news/yg6n19y2g6frnp4koxu8ye48) · [免费模型费用与限额](https://docs.siliconflow.cn/docs/userguide/faqs/rate-limit-and-upgradation)
- 新样本完整执行 13 次真实 SenseVoice ASR 后发起总结。第一次总结请求在 240.802 秒遇到 HTTP 读取超时；Worker 记录失败并确认停止。通过现有手动恢复 API 创建 summary-only 子 attempt，重用 13/13 成功片段，恢复 attempt 的 ASR 请求为 0。其两次 LLM 请求分别以 `finish_reason=length` 和 `stop` 结束。task 总计 3 次 LLM 请求，累计请求时间 423.805 秒；恢复阶段墙钟时间 195.835 秒。
- 最终结果有 5 章、5 个知识要点、10 条引用。验收器逐条检查引用指向当前完整成功转写、引用是对应原文中不超过 48 字符的连续子串、时间恰等于来源片段视频区间且满足媒体时长边界：10/10 原文匹配、10/10 时间匹配。没有在记录中复制标题、原文或引文；工程验收不承诺结论的语义完整性。
- 对 3 个章节和 2 个知识要点做了人工抽样，主题与各自引用片段一致，未见明显错位；抽样文字没有复制到验收记录。
- API 与 Worker 在发布后重启；重启后所属用户总结接口和转写接口读回成功。task / summary 为 `succeeded`，唯一总结可见；原授权视频哈希一致，旧 `awaiting_summary` task 仍为等待状态。审计的 3 条请求终态为 failed / failed / succeeded，finish reason 为 null / length / stop；第一条的有界错误码为 `LLM_PHASE_TIMEOUT`。
- 较早的独立诊断样本使用旧 45 秒读取期限，出现两次约 45.7 秒读取超时；其 task 保留为失败，未超过 task 级三次上限。新 task 默认读取期限提高到 240 秒、总期限 300 秒；旧 task 快照仍保持原值。此次真模型运行也实测读取超时后可在确认停止的 summary-only attempt 中恢复。

## 故障与验证覆盖

- 隔离 PostgreSQL 测试覆盖完整 / 缺失 / 重复 / 错序转写清单、来源和冻结配置、上下文预算、错误分类、请求指纹、结构字段与引用编号、伪造或空 quote、错误片段、无效时间、非有限数值、三次 task 级请求上限、截断修复、发布事务失败、删除竞态、总结恢复不调用 ASR、清理与结果状态分离、owner 读取隔离及重复接力幂等。
- 本窗口完整后端 `82 passed`；Stage 0 回归 `32 passed`。Ruff lint 通过，34 个文件格式检查通过；Mypy 19 个源文件通过；`git diff --check` 通过。pytest 仅有一条既有 Starlette / AnyIO `BlockingPortal` 弃用警告。
- Stage 2 迁移后 `alembic_version=c52d4b5ad389`；`alembic check` 返回 `No new upgrade operations detected.`；Stage 1 / Stage 2 Compose 配置解析通过；容器 `pip check` 返回 `No broken requirements found.`。
- 实际命令：

  ```powershell
  uv run --no-project --with-requirements requirements.lock --with pytest==8.3.5 --with httpx==0.28.1 --with-editable . pytest -q
  $env:PYTHONPATH = (Get-Location).Path
  uv run --no-project --with-requirements probes/stage0/requirements.lock pytest probes/stage0/tests -q
  uv run --no-project --with-requirements requirements.lock --with ruff==0.9.10 ruff check src tests migrations scripts/stage4
  uv run --no-project --with-requirements requirements.lock --with ruff==0.9.10 ruff format --check src tests migrations scripts/stage4
  uv run --no-project --with-requirements requirements.lock --with mypy==1.14.1 mypy src scripts/stage4/summary-acceptance.py
  docker compose --env-file data/mvp-stage-2/.env -p videoclip-mvp-stage2 -f compose.yaml stop --timeout 30 api worker
  docker compose --env-file data/mvp-stage-2/.env -p videoclip-mvp-stage2 -f compose.yaml run --rm --no-deps api alembic upgrade head
  docker compose --env-file data/mvp-stage-2/.env -p videoclip-mvp-stage2 -f compose.yaml build api
  docker compose --env-file data/mvp-stage-2/.env -p videoclip-mvp-stage2 -f compose.yaml up -d --no-deps api worker
  docker compose --env-file data/mvp-stage-2/.env -p videoclip-mvp-stage2 -f compose.yaml restart --timeout 30 api worker
  docker compose --env-file data/mvp-stage-2/.env -p videoclip-mvp-stage2 -f compose.yaml exec -T api alembic check
  ```

## 持久化与限制

- Stage 2 新 API 镜像为 938,276,711 字节；第 4 窗口前的 Stage 2 镜像为 938,043,498 字节，增量 233,213 字节。最终 Docker 全机镜像汇总 5.118 GB、构建缓存 7.681 GB；此前验收记录的缓存为 7.678 GB，观测净增约 3 MB。容器可写层约 131 KiB，日志仍使用每容器 2 × 10 MiB 上限；没有 prune。Docker 新增占用远低于 7 GiB。E 盘剩余 154,094,567,424 字节。
- 失败模型请求不能保证供应商恰好一次；PostgreSQL 事务只保证本地不发布半成品。样本人工抽样未发现明显错位，但不证明总结完整性或语义准确性。页面播放交互待后续窗口验收；当前 API 只提供后端路由，未实现 `/api` 代理或页面。
