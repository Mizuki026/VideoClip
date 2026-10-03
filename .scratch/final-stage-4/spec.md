# 第 4 窗口：排队协调与投递补偿

## 目标和边界

允许不同视频在分析执行位占用时提交，并让尚未领取的当前 attempt 持续获得 PostgreSQL 协调。Celery 消息只携带 task / attempt 标识；数据库保存资格、队列顺序、操作代次、下次检查和补投预算。消息发送成功、超时或消失均不代表已经领取或从未执行。

本窗口仅恢复“当前 attempt 仍未开始”的投递机会。任何已创建 execution 的 attempt 都不会由协调器再次激活，即使之后停止已确认也不在本窗口自动创建重试 attempt。全局未确认 execution、可信停止证明和 `claim_count=0` 的一次领取限制继续是接管门禁。停止证明仍只支持 Stage 2 Linux execution session；监督器/容器强停、缺少绑定身份和不可验证证据保持人工处理。

不实现共享模型冷却、执行失败后的自动恢复、限流等待续接、自动恢复最多两次的调度、原视频与笔记分离删除，也不扩展第 3 窗口停止证明边界。

## 数据和资格

新增的 `analysis_tasks` 协调字段为 `queue_entered_at`、`delivery_next_check_at`、`delivery_retry_count`、`delivery_retry_limit`、`delivery_operation_count`、`delivery_deadline_at`、`delivery_blocked_since`、`delivery_wait_reason` 和 `delivery_failure_code`；`task_deliveries.operation_generation` 记录当前 attempt 的发送操作代次。字段迁移为 nullable，保留历史行的 `NULL` 语义。

- 新提交为同一视频先查找并返回已关联 task；不同视频各自登记队列资格。首次进入可执行队列时写入 `queue_entered_at`。
- 队列顺序以 `(queue_entered_at, task.id)` 升序确定。并列时间由稳定 task UUID 决胜。HTTP 发送顺序和 Redis 顺序不能改变顺序。
- 人工创建子 attempt 时以新时间重新进入队列，task 级补投次数、操作次数和截止时间保留；旧 attempt 因非当前而不能被迟到消息重新领取。若历史任务此前没有协调字段，只有明确符合资格的人工恢复才一次性初始化新预算。
- 发送预留、Worker 领取都重新检查当前 video / task / attempt 绑定、可用性和 `claim_count=0`。领取还检查全局无未确认 execution 并要求该任务为最早可领取队列项。不存在、取消、失败、非当前或已领取 attempt 不能被补投激活。

## 统一投递操作

HTTP 提交、人工恢复、Beat 扫描最终调用 `coordinate_analysis_delivery`。统一业务操作分三段：

1. 短事务锁住 video、task、当前 attempt 和 delivery，并用同一 PostgreSQL execution advisory key 的事务锁与 Worker 领取门禁争用；核对资格及预算，唯一递增操作代次并将 delivery 记为 `pending`。
2. 事务外调用 Celery publisher。
3. 短事务核对仍是同一代次、同一当前 attempt 且仍未领取后写入 `sent`、`unknown` 或明确失败；迟到回执不能覆盖更新代次或已领取状态。

进程在 reservation commit 后、发消息前退出时，持久化 `pending` 在操作超时后可补偿。发送成功但确认写回失败仍保留 `pending`，之后允许同一 attempt 的新代次补投。已发送未领取也会在下次检查后补投。消息重复、HTTP 与扫描重合、两个扫描者重合或发送结果未知都可能产生重复消息，但只有一个 Worker 可以取得一次领取；外部发布不承诺恰好一次。

analysis Worker 保持并发 1、prefetch 1 和既有 ACK 策略。忙时或队列顺序不符的消息可以被消费并 ACK；数据库中的 `delivery_next_check_at` 仍保留后续协调。维护 Worker 只扫描和发起投递，不领取分析任务，也不调用 ASR / LLM。

## 补投参数与语义

以下是 `ANALYSIS_DELIVERY_*` 配置默认值及验证范围：

| 参数 | 默认值 | 配置范围 | 用途 |
| --- | ---: | ---: | --- |
| `ANALYSIS_DELIVERY_RETRY_LIMIT` | 8 次 | 1–32 | 首次操作之外，最多接受的唯一补投 reservation 数。 |
| `ANALYSIS_DELIVERY_DEADLINE_SECONDS` | 86,400 秒（24 小时） | 60–604,800 | 第一次 reservation 开始的活跃投递等待期限。 |
| `ANALYSIS_DELIVERY_SCAN_LIMIT` | 4 项 | 1–8 | 每轮维护扫描上界。 |
| `ANALYSIS_DELIVERY_INTERVAL_SECONDS` | 60 秒 | 60–3,600 | Beat 的投递协调节拍。 |
| `ANALYSIS_DELIVERY_OPERATION_TIMEOUT_SECONDS` | 60 秒 | 30–600 | `pending` 操作无回执后的恢复等待。 |
| `ANALYSIS_DELIVERY_BACKOFF_BASE_SECONDS` | 60 秒 | 1–3,600 | 补投退避基数。 |
| `ANALYSIS_DELIVERY_BACKOFF_MAX_SECONDS` | 900 秒 | 60–86,400 | 退避上限，须不小于基数。 |

默认值依据：每 60 秒扫描与每轮最多 4 项，和现有上传回收节奏及维护 Worker 的并发 1 相配，限制单轮数据库工作量并让两项维护任务轮转。`pending` 等待 60 秒后再补偿，避免在尚可能收到正常回执时立即重复发布。8 次独立补投能覆盖短暂 broker/Worker 故障，同时将重复消息上限明确化；60 秒起步、最高 15 分钟的指数退避限制故障期发送频率。24 小时只计算可推进时间，为长时间 broker 异常提供有限恢复窗口；正常执行占位、停止确认和公平排队暂停期限，避免把用户看见的正常等待耗成投递失败。所有值允许部署配置并有硬边界；它们不承诺供应商调用恰好一次，也不扩展到执行失败自动恢复。

计数和时钟规则：

- 首次发布不扣 retry；第一次被接受的补投 reservation 原子递增一次。因此默认最多 9 个唯一发送 reservation（1 次首次发送 + 8 次补投）。`delivery_operation_count` 是 task 级总 reservation 数，`TaskDelivery.operation_generation` 是每个 attempt 的代次。
- 扫描未到期、重复扫描、幂等重放、已存在的发送结果读取和正常排队等待不扣次数。新 reservation 被接受时即扣费；其后发生进程退出或结果未知不会退回次数。
- 活跃截止时间从首个 reservation 开始，仅计算投递可推进时间。可信执行占位、停止确认阻断及 `queue_order_wait` 暂停该时钟；解除后按阻断时长顺延。长视频正常占用与停止确认缺失不会误耗投递期限。
- 可重试发布结果的退避为 `min(max, base × 2^retry_count)`；默认延迟依次为 60、120、240、480、900 秒（之后封顶）。未到期操作不忙循环。
- 到达 retry 上限或未阻断的活跃截止时间后，task / attempt / delivery 进入明确 failed 状态，记录 `ANALYSIS_DELIVERY_RETRIES_EXHAUSTED` 或 `ANALYSIS_DELIVERY_DEADLINE_EXPIRED`，撤销队列资格。迟到消息无法在失败终态启动执行。
- 补投字段独立于执行失败恢复额度和总结请求额度。attempt 序号不作为补投计数；恢复或新 attempt 不重置同一 task 的补投预算。

默认退避与一轮 60 秒扫描可在暂态 broker 故障时限制重复流量，同时避免 Redis ETA/countdown 充当长时业务状态。24 小时只限制可推进但一直无法领取的 delivery，不把被可信 execution 或公平顺序阻断的等待误算成投递异常。范围保持可配置且有硬边界。

## 维护锁和运行组件

复用现有单实例 Beat 和 `maintenance` Worker，同时保留临时上传回收。队列协调每 60 秒调度一次，扫描最多 4 项；分析与回收有各自 Redis 单飞键和 PostgreSQL advisory lock，不互相占用业务锁。Redis 锁为 180 秒 TTL，释放时核对 owner token；遗留锁自行过期，Celery 周期任务软/硬超时为 140/150 秒。数据库连接丢失时 PostgreSQL advisory lock 随连接释放。两项工作因此可以在同一维护 Worker 的并发 1 队列中继续轮转，单轮扫描有界。

API、首次发送、人工恢复和维护扫描调用同一投递业务规则。maintenance Worker 不执行分析，Beat 不持有任务资格事实，Redis 不作为业务事实源。

## 历史数据策略

迁移只添加本窗口必需的 nullable 协调字段和索引，不更新旧业务行。已有 `failed` / `awaiting_summary` 不会自动转为 queued；不按 attempt 序号回填预算；不改冻结快照、来源指纹失配记录、已发布总结或检查点。扫描只选有明确 `queue_entered_at` 与初始化预算的 task。符合既有人工恢复规则的历史 task 可由用户显式恢复，此时初始化一次新 Stage 4 预算；不借此推断供应商曾否收到旧请求。

## 验收标准

1. 提交幂等、排队稳定顺序、发送代次、重复请求/扫描、HTTP 与扫描并发、删除与发送竞争均验证。
2. 故障注入覆盖 reservation 提交后发送前退出、发送成功但确认写回丢失、已发送未领取、代次迟到回执和迟到旧 attempt 消息。
3. 隔离真实 Compose 链路覆盖 Beat→Redis→maintenance Worker→analysis Worker、A 持有可信 Linux session 时 B/C 入队、忙时消息被 ACK、释放后 B/C 依数据库顺序取得唯一领取、最大同时未确认 execution 为 1。
4. 隔离时钟验证补投计数/期限耗尽、阻断暂停、重放不重复扣费、总结请求额度不变，并验证执行失败不由本窗口自动重跑。
5. 运行第 3 窗口停止门禁与第 2 窗口周期回收回归；迁移后核验旧 task/delivery 新字段仍为 NULL。

验收命令和证据见同目录 `validation.md`。第 0 窗口的未验证样片、计费和资源事项继续以其验收记录为准。
