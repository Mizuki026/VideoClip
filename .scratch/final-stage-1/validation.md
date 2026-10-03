# 最终版第 1 窗口：分片上传后端验收记录

日期：2026-10-03
仓库：`E:\VideoClip`，分支 `codex/mvp`
范围：仅分片上传后端、迁移、启动恢复、安全回收业务入口及其验证。浏览器续传和周期调度留待第 2 窗口。

## 结果摘要

- 后端接口、持久化迁移、恢复逻辑、租约、错误合同和隔离故障测试已完成；现有普通上传保持可用。
- Stage 2 真实 HTTP 最终验收通过：授权样片 3,221,243,133 字节、SHA-256 `e08fdf8772a508ce8811f46f28f330ec493ba50d000977c8f10e273fb54ff8b3`，8 MiB 分片，385 片。注入 1 MiB 请求中断后，服务端确认偏移保持 8,388,608；从该偏移续传、完成及重复完成均成功，重复完成返回同一视频。视频详情、认证 HEAD 和三个 Range 内容比对通过。
- HTTP 验收只进行上传/登记/读取，不创建分析任务、不调用真实 ASR/LLM。验收后通过现有视频删除接口删除临时视频，再清除本窗口的完成上传记录和随机测试账户。
- 复核 Stage 2 数据库最近一小时的新增尝试、分析执行、ASR 请求和总结请求均为 0；重启 Worker 未触发历史模型任务。
- 浏览器页面仍使用普通上传，页面续传和周期调度未实施。因此只完成分片续传阶段的后端窗口，不将整个阶段标为完成。

## 工作区与保护措施

- 开始时 `git status --short` 为空，未发现既有未提交改动。
- 先备份 Stage 2 PostgreSQL 到 Git 忽略的数据目录，并用 `pg_restore --list` 确认备份可读；备份为 `data/mvp-stage-2/backups/final-stage-1-before-20261003.dump`。没有读取或输出私密配置值。
- Stage 1 后端测试使用独立数据库 `videoclip_mvp_stage1_test`。为运行测试临时启动 Stage 1 PostgreSQL，验收后已停止；Stage 2 API/PostgreSQL 继续运行。
- 两次实际 HTTP 尝试所创建的视频、上传行和测试账户均已清理。第一次流程完成了上传和媒体验收，但验收客户端没有把清理 SQL 环境变量传入容器，导致最终账户清理断言失败；排查确认只剩该次随机生成账户且无视频、上传或分析记录。修正客户端传参并清理该账户后，第二次完整验收通过。故本窗口实际上传授权样片两次，而非一次；两次均未提交分析并均清理了业务数据。该重复是本地验收清理脚本缺陷后的复验，不是产品重试行为。
- 清理保留两枚本窗口创建的 1 字节租约标记：`MediaStore.remove_lease` 不 unlink 固定 inode 标记，避免跨进程 advisory lock 在 unlink/recreate 竞态中同时授予两把锁。`.part`、登记视频文件、视频记录、上传会话记录及临时用户均已清除。

## 数据迁移与历史身份

迁移 `c52d4b5ad389 -> e103a7c9f2d1` 已应用于现有 Stage 2 数据库。迁移前后按主键身份和业务关联生成的行数/摘要完全相同：

| 表 | 行数 | 迁移前后相同的身份摘要 |
| --- | ---: | --- |
| `upload_handoffs` | 8 | `3b41bfd27e954755108c239bc833d192` |
| `videos` | 8 | `3f5ef400324c66c5b1fff49ba8c08ee4` |
| `analysis_tasks` | 8 | `6af28c901a2c81480d6e6debfd2eb64a` |
| `analysis_attempts` | 14 | `33e1a3aa0b1d8ee92986c60748439594` |
| `transcript_segments` | 330 | `9327dc8368c8fb23dbc19ec963d6d1d6` |
| `analysis_summaries` | 2 | `2d2873f13578a7d24a5298e6f59a1c55` |

迁移后 8 条历史上传仍为 `ordinary / complete`，`upload_chunks` 初始行数为 0。迁移没有重建业务 UUID、task、attempt、转写、总结或冻结配置。Stage 2 的 `alembic check` 报告 `No new upgrade operations detected`。

## 实现合同

完整请求/响应、状态图和稳定错误表以[定稿规格](spec.md)为准；本地运行条件和下一窗口入口摘要如下：

- `POST /uploads` 创建会话；必需的 `Idempotency-Key` 按所有者唯一，元数据相同的重放返回原会话。
- `GET /uploads/{id}` 返回确认偏移、下一索引及每片摘要；不续期。所有权不符按不存在返回 404。
- `PUT /uploads/{id}/chunks/{index}` 接收原始二进制与 `X-Chunk-SHA256`，默认顺序分片 8,388,608 字节，最后一片更小。实际流长度和摘要由服务端核验；文件 `fsync` 后才事务提交分片索引/长度/摘要和确认偏移。
- `POST /uploads/{id}/complete` 校验全部确认字节、分片及完整摘要和现有媒体规则；重放返回同一个 `Video`。
- `DELETE /uploads/{id}` 放弃未完成会话。登记后的媒体只能经 `DELETE /videos/{id}` 删除。
- 状态包括 `receiving -> validating -> promoting -> complete`，以及 `expired`、`abandoned`、`failed` 清理终态。到期/失败清理意图失败时保留会话以供重试；已进入 `promoting` 的持久化交接优先恢复登记。
- 会话自创建或最后一次新分片/完全相同分片重放起有效 24 小时；查询、无效请求和中断传输不续期。
- 下一窗口的安全业务入口：`videoclip.resumable_uploads.reclaim_expired_uploads(factory, store, now=None, limit=100) -> int`。它跳过活跃租约，只处理到期/终态会话，清理失败保留意图。窗口 1 通过直接调用/测试验证，没有安装 Beat、Worker 或调度器。
- 修改请求条件：用户登录 Cookie、CSRF Cookie、`X-CSRF-Token` 和配置一致的 `Origin`。本地 Stage 2 HTTP 来源为 `http://127.0.0.1:58002`，页面来源为 `http://127.0.0.1:5174`；测试 API 使用现有私密配置对应的 Stage 2 PostgreSQL 和应用存储目录。
- 下一窗口需先运行 `scripts/stage2/run-stage2.ps1` 使当前代码/迁移生效，再运行 Web 页面；Stage 2 API 健康检查通过，Stage 1 测试 PostgreSQL 当前停止。

稳定业务错误主要有：

| HTTP | 错误码 | 条件 |
| ---: | --- | --- |
| 404 | `UPLOAD_NOT_FOUND` | 会话不存在或当前用户不拥有它 |
| 409 | `UPLOAD_BUSY`、`UPLOAD_OUT_OF_ORDER`、`UPLOAD_CHUNK_CONFLICT`、`UPLOAD_INCOMPLETE`、`UPLOAD_INVALID_STATE`、`UPLOAD_ALREADY_COMPLETED`、`UPLOAD_IDEMPOTENCY_CONFLICT` | 并发租约、序号/重放/状态/幂等冲突 |
| 410 | `UPLOAD_EXPIRED`、`UPLOAD_ABANDONED` | 续传会话已过期或放弃 |
| 422 | `INVALID_UPLOAD_METADATA`、`CHUNK_LENGTH_MISMATCH`、`CHUNK_DIGEST_MISMATCH`、`UPLOAD_DIGEST_MISMATCH`、`UPLOAD_INVALID_MEDIA` | 请求元数据、分片、完整摘要或媒体规则失败 |
| 500 | `UPLOAD_STORAGE_CORRUPT` | 确认文件缺失、短缺或已确认内容损坏 |
| 503 | `UPLOAD_CONFIRMATION_PENDING`、`UPLOAD_MEDIA_VALIDATION_FAILED`、`UPLOAD_FINALIZATION_PENDING` | 确认/媒体探测/交接需要状态查询、重试或恢复 |
| 507 | `UPLOAD_STORAGE_FAILED` | 磁盘或权限等存储写入失败 |
| 202 | `UPLOAD_CLEANUP_PENDING` | 安全清理暂未完成，保留意图以便重试 |

## 自动化与工程检查

执行命令及结果：

| 检查 | 命令 | 结果 |
| --- | --- | --- |
| 相关上传回归 | `& .\data\mvp-stage-2\.venv\Scripts\python.exe -m pytest tests/test_resumable_upload.py tests/test_upload_video.py -q` | 27 passed，1 条既有 Starlette/AnyIO deprecation warning |
| 后端全量 | `& .\data\mvp-stage-2\.venv\Scripts\python.exe -m pytest -q` | 97 passed，1 条相同 deprecation warning |
| Stage 0 回归 | `& .\data\mvp-stage-0\.venv\Scripts\python.exe -m pytest probes/stage0/tests -q` | 32 passed |
| Ruff | `& .\data\mvp-stage-2\.venv\Scripts\python.exe -m ruff check src tests migrations scripts/stage1 scripts/stage2 scripts/stage4 probes/stage0` | All checks passed |
| mypy | `& .\data\mvp-stage-2\.venv\Scripts\python.exe -m mypy --explicit-package-bases src migrations scripts/stage1 scripts/stage2 scripts/stage4 probes/stage0` | Success，34 source files |
| 依赖一致性 | `& .\data\mvp-stage-2\.venv\Scripts\python.exe -m pip check` | No broken requirements found |
| 前端 lint | `npm run lint`（`frontend/`） | 通过 |
| 前端单测 | `npm run test -- --run`（`frontend/`） | 3 个文件、6 个测试通过 |
| 前端构建 | `npm run build`（`frontend/`） | TypeScript 检查及 Vite 构建通过 |
| Stage 1 Compose | `docker compose --env-file data/mvp-stage-1/.env -p videoclip-mvp-stage1 -f compose.yaml config --quiet` | 通过 |
| Stage 2 Compose | `docker compose --env-file data/mvp-stage-2/.env -p videoclip-mvp-stage2 -f compose.yaml config --quiet` | 通过 |
| Stage 2 迁移模型 | `docker compose --env-file data/mvp-stage-2/.env -p videoclip-mvp-stage2 -f compose.yaml exec -T api alembic check` | No new upgrade operations detected |
| 验收客户端静态语法 | `& .\data\mvp-stage-2\.venv\Scripts\python.exe -m py_compile .scratch/final-stage-1/http-acceptance.py` | 通过 |

未运行 Playwright：本窗口没有更改前端或默认上传交互；前端 lint、单测及构建均通过。浏览器续传交互回归属于第 2 窗口，并应使用其专用输出目录。

上传测试还覆盖多片/短尾片、查询、相同重放及冲突、乱序、长度/摘要错误、提前/重复完成、跨用户隔离和 CSRF、中断回滚、写文件后 DB 提交失败、DB 提交后响应丢失、API 重启后续传、已确认文件损坏、并发写与完成/放弃竞争、改名后恢复、磁盘失败、非法媒体、到期回收、活跃租约和清理重试。旧普通上传测试包含普通上传、媒体读取和删除回归。

首次定向测试发现短尾片完全相同重放被错误判断为索引冲突；实现改为以已持久化片数计算下一索引后，定向及全量测试通过。该修正已包含在当前结果中。

## 实际 HTTP 与资源变化

验收客户端：`.scratch/final-stage-1/http-acceptance.py`。该脚本从本地 Stage 0 授权样片重新读取内容；先核对大小与完整摘要，创建随机账户并只使用上传/视频读取接口，注入部分请求体中断，再从服务端返回的确认偏移续传。未把 Cookie、密码、课程内容或响应正文写入输出。

成功结果摘要：

```text
result=PASS
source_bytes=3221243133
chunk_size=8388608
confirmed_chunks=385
confirmed_offset_after_interrupt=8388608
duplicate_completion_same_video=true
video_query_and_authenticated_ranges=PASS
video_removed=true
test_account_removed=true
analysis_submitted=false
```

HTTP 结束后业务行数恢复到迁移前：上传 8、视频 8、task 8；近 30 分钟内匹配验收随机账户格式的记录为 0。迁移后原有行身份摘要未变化。Stage 2 API 健康，原视频鉴权媒体范围读取返回 404；临时样片本身和其上传/视频记录已清除。

观察到的应用目录与 E 盘值：

| 时点 | Stage 2 应用目录 | E 盘可用空间 |
| --- | ---: | ---: |
| HTTP 验收前 | 16 个文件，15,247,745,587 字节 | 140,691,697,664 字节 |
| HTTP 验收后 | 18 个文件，15,247,745,589 字节 | 140,691,329,024 字节 |

增加的两个文件是两次真实验收对应的 1 字节 `.lock` 租约标记；样片 `.part` 与最终媒体已删除。E 盘可用空间净减少 368,640 字节，其归因未验证；本次没有采样容器可写层处理峰值。这些读数不改变第 0 窗口“容器可写层峰值未验证、E 盘差额未归因”的状态。

## 遗留项及准入判断

第 0 窗口事项继续保持未验证：容器可写层运行峰值、E 盘空闲空间差额归因、账户账单/余额，以及完整 20 分钟模型处理。本窗口不依赖模型调用或 20 分钟模型容量；实测授权样片已两次成功经 HTTP 续传、登记和鉴权读取，且当前 E 盘可用空间约 140.69 GB。上述事项不阻塞第 2 窗口浏览器接入和定时调度，但仍属于后续容量、账务和最终产品验收范围；不得据此宣称它们已通过。

**窗口判定：已完成后端范围；可以进入第 2 窗口。** 依据是迁移身份保持、后端/前端工程检查通过、Stage 0 回归通过、故障测试通过以及真实 HTTP 续传和安全清理通过。第 2 窗口只实现浏览器摘要/重选/续传及周期触发 `reclaim_expired_uploads`，本窗口不预先实现这些能力。
