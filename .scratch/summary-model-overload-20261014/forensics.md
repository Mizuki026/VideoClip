# 总结模型持续失败：调试前现场记录

记录日期：2026-10-14（Asia/Shanghai）

范围：视频 `4afe835d-700a-418b-b159-9472e2def110` 的总结失败。附件截图按现场证据使用；其中另一张视频记录不属于本次复现。

## 固定复现路径

1. 上传 `如何选择适合自己的agent.mp4`（约 6:08、1.0 GiB），等待语音转写完成。
2. 总结请求进入第三方模型；界面第一次显示 `LLM_MODEL_OVERLOADED`。
3. 等待自动恢复，或在失败页面点击“手动恢复分析”，再观察是否出现总结。

预期：完整转写保留，模型恢复后发布总结。现场结果：截图显示转写 13/13 段；总结失败并显示自动恢复 2/2、总结请求 2/3。随后界面回到 `queued_summary` 等待领取。

## 环境与边界证据

- 前端页面为 `127.0.0.1:5174`，Stage 2 API 端口为 `127.0.0.1:58002`；API `/health` 返回 HTTP 200。
- `videoclip-mvp-stage2` 的 API、analysis worker、maintenance worker、Beat、PostgreSQL、Redis 容器均为 running；API、PostgreSQL、Redis health 为 healthy。故障期 Worker 无容器重启。
- PostgreSQL、Redis 与队列服务可用；分析任务已有成功领取、13 个转写检查点和总结阶段的执行记录。不是上传、转写、浏览器到 API 或队列投递失败。
- Stage 2 Compose 读取 `data/mvp-stage-2/.env`。没有读取或记录密钥值。浏览器网络截图里的 200 请求是视频状态、事件与转写查询，不能观测服务器到模型供应商的 HTTP 请求。
- 预期的上游错误在 Worker 中被归一化为稳定错误码；本次没有原始 Python traceback。诊断依据是数据库 `analysis_events` 与 `summary_requests` 的持久状态，未保存提示词、转写文本、密钥或供应商响应正文。

## 模型请求与状态时间线（UTC）

视频对应 task：`b7018aaf-db0e-4fe0-8ac0-d64c56dd94f1`。三次 attempt 均为 `summary_only`，均已完成执行领取。

| Attempt | 请求时间 | 结束时间 | 持久化结果 |
| --- | --- | --- | --- |
| `6c303810-b82e-43c5-afdc-b99b11a055ef` | 13:52:01 | 13:52:05 | `summary_requests` 记为 `failed / LLM_MODEL_OVERLOADED`，耗时 4.27 秒 |
| `2a79fd33-5615-422c-91e2-e510d2c589a3` | 13:54:12 | 13:54:18 | `failed / LLM_MODEL_OVERLOADED`，耗时 5.58 秒；事件为系统自动恢复 |
| `ce037b32-0e47-40cb-949e-e93081ac8ad2` | 13:57:10 | 14:01:11 | `failed / LLM_PHASE_TIMEOUT`，耗时 241.03 秒；事件为系统自动恢复 |

第三次请求在 13:57:10 产生 `summary_requested` 和 `SummaryRequest(status=started)`，之后记录为 `summary_failed`，因此模型调用不是被前端请求拦住或未发出。当前 task 为 `failed`，summary 为 `failed`，自动恢复 2/2、总结请求 3/3；之后没有再创建模型请求的预算。

事件时间线中没有 `manual_recovery_created` 或 `manual_continuation_created`。截图证明按钮可见，但当前持久化证据不能证明该按钮曾成功提交；两次后续重试都明确记录为 system automatic recovery。需避免把截图的“等待领取”误读成已经发出第三方请求，或把仅有状态轮询的浏览器 Network 列表误当成模型请求日志。

## 首个偏离点与待修复根因

上传、转写检查点、排队、投递、Worker 领取均符合预期。总结适配器先收到两个上游过载结果，第三次请求等待超时。`llm_client._status_code(503)` 将 HTTP 503 映射为 `LLM_MODEL_OVERLOADED`；代码只对 429 保存 `Retry-After` 并写入共享模型冷却。因而 503 不会按供应商建议等待，也没有共享冷却保护，自动恢复会在有限的三次总结请求额度内继续重试繁忙模型。

直接故障边界在第三方模型服务（两次 503 语义错误、一次请求阶段超时）；本地首个可修复偏离点在 LLM 对 503 的恢复处理没有像 429 一样保存退避信息并进入共享冷却。修复目标是在不放宽三次请求预算、不切换供应商/模型的前提下，安全保留 503 的 `Retry-After`，并让自动/人工续接在共享冷却到期前等待。通过本地替身验证；本次不会对已耗尽额度的真实视频追加请求。
