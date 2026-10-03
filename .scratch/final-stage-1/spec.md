# 最终版第 1 窗口：分片上传后端规格

状态：接口与后端范围已定稿；浏览器续传与周期调度留待第 2 窗口。

## 目标与边界

交付带 Cookie/CSRF 与用户隔离的 HTTP 分片上传后端。客户端能创建会话、查询服务端确认进度、顺序写入分片、完成登记或放弃会话。唯一视频登记继续由 `Video.upload_id` 唯一约束及现有上传交接完成。保留旧 `POST /videos` 普通上传和全部调用方。

本窗口不实现浏览器摘要或续传页面、不更改页面默认上传方式、不部署 Beat/维护 Worker/定时任务，也不自动提交分析或调用模型。过期回收实现为可由下一窗口调用的业务操作，并在本窗口直接调用验证。第 0 窗口的长样本基线继续有效，但容器可写层处理中峰值、E 盘空闲差额归因、账户余额/计费及完整 20 分钟真实模型处理仍未验证；本窗口不补跑长视频模型。

## 持久化与分片规则

- `UploadHandoff` 增加上传协议类型、声明总字节、预期 SHA-256、服务端确认偏移、到期时间和创建幂等键；旧行回填为 `ordinary`，旧列值、业务 UUID 和外键不重建。
- 新增 `UploadChunk`，按 `(upload_id, chunk_index)` 唯一，保存字节偏移、长度、SHA-256 和创建时间；删除未完成会话时随会话级联清理。完整 `Video` 仍通过既有唯一的 `upload_id` 约束只登记一次。
- 固定分片大小为 **8,388,608 字节（8 MiB）**。除最后一片外，每片必须恰为该长度；最后一片为剩余的 1 至 8,388,608 字节。总长度大于 0；不增加单文件业务上限，数据库 `BIGINT` 表示范围是存储类型边界。
- 分片从索引 0 开始，严格顺序提交；偏移等于 `chunk_index * chunk_size`。会话的 `confirmed_offset` 只在文件完整写入、实际长度及 SHA-256 验证通过并完成 `fsync` 后随分片记录在一个数据库事务中推进。
- 所有分片写在 `incoming/<upload_id>.part` 的同一受控文件。每次写、完成、放弃及到期回收都持有同一上传租约。确认偏移后的任何尾部均为未确认数据；下一次状态查询或写入先截回 `confirmed_offset` 并同步，再继续。文件短于确认偏移、分片清单不连续或已确认分片摘要不符时返回 `UPLOAD_STORAGE_CORRUPT`，不推进偏移。
- 完成时按已确认分片清单核对全部长度和 SHA-256，再核对声明的完整 SHA-256。校验通过后持久化 `promoting` 交接意图和媒体信息，原子改名到现有受控媒体路径，再调用现有 Video 登记逻辑。会话已完成时重复完成返回同一 Video。媒体非法或整文件摘要冲突会进入失败终态，必须放弃/回收后重建会话。

## HTTP 合同

所有修改请求（创建、写片、完成、放弃）都须带有效的会话 Cookie、CSRF Cookie、`X-CSRF-Token` 和与配置完全相等的 `Origin`；继续使用现有安全中间件。查询要求登录 Cookie。上传 ID 不是授权凭证，所有查询和修改都先按当前用户归属会话；其他用户访问返回与不存在相同的 404。

### 创建

`POST /uploads`，JSON：

```json
{"file_name":"course.mp4","total_bytes":12345678,"sha256":"<64 位小写十六进制 SHA-256>"}
```

必须带 `Idempotency-Key`（1–128 个可见 ASCII 字符）。首次创建返回 201；同一用户、相同幂等键及相同文件名/字节数/摘要重放返回原会话（200）；相同键但元数据不同返回 `UPLOAD_IDEMPOTENCY_CONFLICT`（409）。新会话初始状态为 `receiving`，偏移为 0，`expires_at` 为创建时间后 24 小时。

响应包括 `upload_id`、`state`、`chunk_size`、`total_bytes`、`confirmed_offset`、`expires_at`。文件名经现有 `safe_display_name` 处理，只用于展示。

### 查询

`GET /uploads/{upload_id}` 返回会话状态、总字节、8 MiB 规格、已确认偏移、下一分片索引、分片索引/偏移/长度/摘要、到期时间、稳定错误码及完成后的 `video_id`。不续期。获取租约后核验已确认文件区间并截掉未确认尾部；活跃租约返回 `UPLOAD_BUSY`（409）。

### 写入分片

`PUT /uploads/{upload_id}/chunks/{chunk_index}`，`Content-Type: application/octet-stream`，`X-Chunk-SHA256` 为请求体的 64 位小写十六进制摘要，请求体为原始二进制。服务端流式计数/散列，不信任 `Content-Length`；实际长度必须等于该索引的预期长度。

仅当前下一索引可新增。首次接受后响应包含索引、偏移、长度、摘要、`confirmed_offset`、到期时间和 `replayed=false`。较早索引仅在已保存的长度/摘要及实际请求体均相同且文件内容核验通过时幂等返回 `replayed=true`，否则 `UPLOAD_CHUNK_CONFLICT`（409）。较大索引返回 `UPLOAD_OUT_OF_ORDER`（409，带服务端确认偏移/下一索引）。新分片及完全相同的有效重放把会话到期时间延长至成功活动时刻后 24 小时；无效/中断请求和只读查询不续期。

### 完成

`POST /uploads/{upload_id}/complete`，无请求体。完整性/媒体规则检查通过并登记后返回既有 `UploadResponse` 结构（HTTP 200）。重放返回同一个 Video ID。确认偏移未达到总长度返回 `UPLOAD_INCOMPLETE`（409）；媒体不合法、整文件摘要不一致和存储损坏分别返回稳定终态错误。已持久化 `promoting` 的会话由启动恢复或重复完成继续交接；文件改名后进程退出不创建第二个视频。

### 放弃

`DELETE /uploads/{upload_id}`。未完成会话持租约后持久化 `abandoned` 与清理意图，再安全删除 `.part`/未登记目标文件。清理成功返回 204；磁盘清理失败保留 `abandoned` 状态、错误码和清理意图，返回 202，重放可继续清理。不存在或已成功清理的 ID 返回 204；别的用户拥有的现存 ID 返回 404。已完成会话返回 `UPLOAD_ALREADY_COMPLETED`（409），不得删除视频；视频继续只经既有视频删除接口删除。

## 状态与错误

续传会话状态：

```text
receiving --有效完成请求--> validating --完整摘要及媒体校验通过--> promoting --> complete
    |                            |                                  |
    +--超时回收--> expired       +--摘要/媒体失败--> failed           +--启动恢复/完成重放
    +--放弃------> abandoned           |                                  --> complete
                                          +--显式放弃/回收--> 清理并移除会话
```

`complete` 不可被放弃或过期回收；`expired`、`abandoned`、`failed` 只允许安全清理重试。清理意图未完成时保留会话和意图，不能丢弃恢复依据。`promoting` 代表已通过媒体规则并进入交接；有效目标文件优先恢复登记，而不是误删。普通上传保持原状态和启动恢复策略。

稳定上传错误码及 HTTP 状态：

| HTTP | 错误码 | 含义 |
| --- | --- | --- |
| 404 | `UPLOAD_NOT_FOUND` | 当前用户不存在/不拥有该会话 |
| 409 | `UPLOAD_IDEMPOTENCY_CONFLICT` | 创建幂等键对应的元数据不同 |
| 409 | `UPLOAD_BUSY` | 写入/交接/清理租约正在由另一操作持有 |
| 409 | `UPLOAD_OUT_OF_ORDER` | 分片索引不是下一期望索引 |
| 409 | `UPLOAD_CHUNK_CONFLICT` | 已确认索引的重放内容不一致 |
| 409 | `UPLOAD_INCOMPLETE` | 完成时仍有未上传字节 |
| 409 | `UPLOAD_INVALID_STATE` | 会话状态不允许请求 |
| 409 | `UPLOAD_ALREADY_COMPLETED` | 放弃请求命中已登记视频 |
| 410 | `UPLOAD_EXPIRED` | 会话已到期；只可查询失败状态或回收，不能续传/完成 |
| 410 | `UPLOAD_ABANDONED` | 会话已放弃 |
| 422 | `INVALID_UPLOAD_METADATA` | 元数据、文件摘要或幂等键格式非法 |
| 422 | `CHUNK_LENGTH_MISMATCH` | 请求体实际长度不符合该片预期 |
| 422 | `CHUNK_DIGEST_MISMATCH` | 请求体与 `X-Chunk-SHA256` 不匹配 |
| 422 | `UPLOAD_DIGEST_MISMATCH` | 所有分片组成的文件与声明 SHA-256 不匹配 |
| 422 | `UPLOAD_INVALID_MEDIA` | ffprobe/现有媒体规则拒绝文件 |
| 500 | `UPLOAD_STORAGE_CORRUPT` | 文件短于确认偏移、清单不连续或已确认内容摘要异常 |
| 503 | `UPLOAD_CONFIRMATION_PENDING` | 分片文件已同步但确认事务结果不确定；先查状态，再按偏移幂等续传 |
| 503 | `UPLOAD_MEDIA_VALIDATION_FAILED` | ffprobe 超时/不可用等环境故障；保留可重试会话，不报告媒体已完成 |
| 503 | `UPLOAD_FINALIZATION_PENDING` | 交接意图已保存，登记待重启恢复或幂等重试 |
| 507 | `UPLOAD_STORAGE_FAILED` | 磁盘/权限等写入失败，偏移不推进 |
| 202 | `UPLOAD_CLEANUP_PENDING` | 清理失败；意图保留且可重试 |

认证与 CSRF 失败沿用现有 401/403 响应；不引入第二套鉴权错误。

## 有效期与回收入口

会话有效期为 24 小时，从创建或最后一次成功确认的新分片/完全相同重放起算。检查进度、错误请求、部分传输和登录活动不延长有效期。到期后写片和首次完成返回 `UPLOAD_EXPIRED`。完成在有效期内开始并已进入 `validating/promoting` 的操作在持有租约期间可完成；若进程退出，`promoting` 按已持久化交接恢复。

提供同步业务操作 `reclaim_expired_uploads(factory, store, now=None, limit=100) -> int`，返回本次成功清理的会话数；只处理续传协议中已到期、已放弃、终态失败或待重试的清理意图。操作按批次持租约，遇活跃租约跳过；清理失败保留意图供下次调用。对到期的 `promoting` 会话先按既有交接规则尝试安全恢复，不删除有效的已交接视频。启动恢复跳过未到期 `receiving/validating` 会话、继续恢复 `promoting`，并沿用旧普通上传回收规则。此入口可由第 2 窗口周期调度调用；本窗口不创建任何调度组件。

## 验收重点

在隔离测试中覆盖顺序多片与短尾片、查询、完全重复与冲突重放、乱序、真实长度/摘要错误、提前/重复完成、账号隔离与 CSRF；中断请求、文件写入后数据库提交前故障、提交后丢响应、API 重启、文件短缺/损坏、并发租约、完成与放弃竞争、改名后退出、磁盘写入/删除失败、非法媒体、到期回收及清理重试。Stage 2 实际 HTTP 用第 0 窗口授权样片只上传一次，主动中断后查询确认偏移并续传；检查重复完成、登记字节数/摘要、视频及鉴权媒体读取，结束后只清理本窗口新建且安全回收的会话/视频/测试账户，不提交分析。

阶段 1 仅在后端接口、迁移、恢复、租约、隔离测试和真实 HTTP 验证完成后标记后端范围完成。浏览器续传页面及周期回收调度属于下一窗口，不得在本规格或技术/产品文档中提前标为完成。
