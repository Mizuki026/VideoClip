# 时间线与事务边界

## 数据源和事件表

`analysis_events` 是用于 UI 追溯的轻量记录。task、attempt、execution、分片检查点、总结请求、媒体/整条清理状态仍是当前事实源；页面或恢复服务不得从事件反推当前状态，也不能用事件绕过 task 状态门禁。

每条事件具备数据库时间、视频/task/attempt/execution 关联、事件类型、阶段、稳定错误码、actor 和经过白名单校验的内部 request reference。允许类型在 `src/videoclip/events.py` 固定；引用只接受 summary-request UUID 或 transcript-segment UUID/序号格式，不写密钥、Cookie、原文、完整转写或模型响应。

## 事务与幂等顺序

`record_analysis_event()` 在调用方业务 Session 中运行：锁定同一 `AnalysisTask` 行，按 `(task_id, event_key)` 查重，再插入并 flush。事件和调用方的状态更新使用同一个事务；事务回滚时二者一起回滚。task 行锁为同一 task 的事件顺序提供串行边界，唯一约束保护并发幂等；全局 bigserial ID 提供分页游标。

投递预留/发送/补投、execution claim/failure、转写检查点、转写完成、总结请求/响应/失败/发布、模型等待/429、自动/人工恢复、停止确认、attempt 清理、媒体清理和整条清理在对应状态操作事务中记录。外部 HTTP、模型、FFmpeg 和文件删除不在长数据库事务内；先提交阶段事实/预留，再执行外部副作用，完成后用新的业务事务提交检查点或结果事件。event 不是外部副作用的“恰好一次”证明。

媒体删除/整条删除继续走现有视频/task 行锁、清理意图和文件租约。媒体清理事件与状态事件写在触发状态变化的业务事务中；文件系统操作在租约下运行，随后事务记录完成或稳定错误码。路径/租约本身不证明 Worker 进程停止或已打开媒体读流关闭。

## 读取、删除和升级

`GET /videos/{video_id}/events` 先检查当前用户对视频的所有权，按 `owner_id + video_id` 过滤；`limit` 1–100，使用 `before_id` 游标，按事件 ID 稳定顺序返回，设置 `private, no-store`。刷新或 API 重启后从 PostgreSQL 重新读取。整条视频删除对事件的外键级联；视频删除后事件端点 404。历史状态不回填为事件，所以旧记录可显示当前状态摘要，但不得伪称拥有迁移前时间线。

Schema migration `1a7e3c9d5b42_analysis_events` 从 Stage 6 的 `d42f6c8a901e` 后增加事件表。共享冷却索引差异由 ORM 声明 `ix_shared_model_cooldowns_until` 收敛；数据库已有索引继续保留，已应用历史 migration 不重写。当前 `alembic check` 报 `No new upgrade operations detected`。

## 实测

Stage 2 真实 task 删除前事件审计为 47 条、39 个关联 request reference；API/Worker 重启后页面读取到投递、claim、checkpoint、转写完成、summary 请求/发布和 stop confirmed。之后用户通过整条删除执行级联，8 个相关接口均 404。历史记录的 event count 保持 0，不伪造补录。
