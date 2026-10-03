# 最终版第 3 窗口规格：可信停止确认

## 目标与基线

仅当停止证据同时绑定 execution、attempt/task、Worker 实例代次和受控 Linux 进程边界，且本地受控执行者及其后代已经退出后，才登记停止确认并释放全局执行位。不能证明停止时保留未确认记录，继续阻断恢复与危险清理，并保留人工停机入口。

开始时分支为 `codex/mvp`，HEAD 为 `eb5f8ed`（第 2 窗口交接），工作区干净。Stage 2 PostgreSQL、Redis、API 和 analysis Worker 健康运行；Beat 与 maintenance Worker 当时未运行。开始验收前核对到未确认 execution 为 0、running attempt 为 0、analysis 队列长度为 0；历史 task 状态为 `awaiting_summary:2, failed:4, succeeded:2`，已发布总结为 2。未触碰这些历史行，也未投递分析消息或调用模型。

领域文档布局检查未发现根 `CONTEXT.md`、`CONTEXT-MAP.md` 或 `docs/adr/`；按 `docs/agents/domain.md` 继续读取代码和已有规格。

## 范围

- 将每次分析 execution 绑定到一次性 Worker 实例 UUID、Linux boot ID、执行进程 PID/start ticks、独立 session ID 与明确的边界版本；PID、主机名、数据库连接 PID只作辅助证据。
- 领取记录提交后必须向同容器监督器完成监督登记，登记确认前不允许开始 FFmpeg 或模型副作用。登记前崩溃保留未确认占位；登记后监督器依据边界状态处理退出。
- 当前 FFmpeg/ffprobe 和 ASR/LLM 本地请求子进程都位于 execution session 内的独立 process group；监督器在执行者退出后扫描整个 session，终止并等待所有受控后代，再由统一事务登记确认。
- 监督器作为分析 Worker 容器入口，监督 Celery Worker 与执行边界；不自动重放任务或创建 attempt。监督器失效、容器/宿主机强停、PID/session 身份不匹配或无法读全 `/proc` 时不确认。
- 生命周期事务核对 execution、attempt 绑定、Worker 代次与停止证据；相同确认幂等，任务终态、停止确认和临时文件清理分别管理。数据库确认失败只对同一证据作有限重试；没有证据时保留占位。
- 人工脚本先验证唯一 Stage 2 Worker、目标 execution 绑定代次与容器 ID，再零宽限停止并核实同一容器已退出，最后调用统一业务操作登记人工证据。历史 execution 缺少新身份时不补造绑定、不追溯确认。
- 保持全局唯一未确认执行约束、attempt 一次领取、迟到写回门禁、单 analysis Worker、队列隔离、Beat/维护 Worker 仅回收临时上传、现有 Celery ACK 策略和请求预算。

## 非目标

不实现分析排队协调、投递补偿、共享模型冷却、自动恢复/重放、分析恢复扫描、媒体与笔记分离删除，也不改动第 2 窗口维护 Worker 的职责。不声称阻止恶意逃逸到新 session 的本机代码，也不撤销已经送达供应商的请求。

## 验收

1. 单元及事务测试覆盖执行身份、监督登记顺序、重复/错误/旧代次/迟到证据、状态竞争、确认失败及预算不变。
2. 用生产监督入口和真实 Linux 子进程验证：强杀受控执行者后先保留残留 FFmpeg/请求模拟子进程，未清空前不确认；全部停止后仅确认对应 execution。
3. 断开执行者数据库连接但保持本地进程存活，确认未确认占位阻止接管与清理。
4. 覆盖监督器死亡、容器强停和未注册/身份缺失证据，确认保留占位并可转人工。
5. 注入停止后数据库不可用，确认有限同身份重试或人工处理，不生成第二执行者。
6. 验证迟到结果、旧消息、成功/业务失败/超时及人工恢复回归，且请求预算不变。
7. 故障验收只使用隔离数据与本地模型替身，不调用真实付费模型，不改写历史业务行。
8. 先跑直接测试，再跑后端全量、Stage 0 回归、Ruff、mypy、pip check、Compose/迁移检查和前端 lint/单测/构建；检查部署后的健康状态和第 2 窗口队列隔离/回收边界。
9. 记录命令、进程身份/退出证据、数据库结果、运行状态、剩余风险；输出不得包含密钥、课程原文或敏感响应。

## 关键决策

Docker Linux 容器内 FFmpeg/ffprobe 和 ASR/LLM 请求使用 `subprocess.Popen` 启动。原 FFmpeg/ffprobe 的 `start_new_session=True` 会离开父执行者的 session，所以本窗口改为给每个受控执行者建立一次性 Linux session，媒体与请求子进程各自使用新 process group、但保留同一 session。真实进程验收确认这些生产客户端 PID、FFmpeg 和本地 HTTP 替身确实处于边界中。监督器扫描、终止并等待整个 session，不能只检查 PID 或单个进程组。主动进入新 session 的恶意进程、不可读边界和非 Linux 执行环境一律不自动确认。

没有满足边界的监督证据时，不从 PostgreSQL advisory lock 释放、worker 超时、容器重启或 PID 缺失推断停止。新的 identity 列允许为历史行保留 NULL；不作伪造回填。

## 当前进度

- [x] 分支、交接提交、工作区、运行服务、历史状态与分析队列基线核对。
- [x] 读取分析领取、媒体进程、生命周期确认、人工恢复和清理链路；核实子进程使用独立 session。
- [x] 实现可空身份迁移、监督登记门禁、PID 1 生产监督入口及统一停止证据事务；历史身份保持 NULL。
- [x] 真实 Linux 进程残留、执行 DB 连接断开、确认写库不可用、监督器/容器强停和精确容器人工确认验收。
- [x] 事务身份/幂等/迟到/失败测试、后端/Stage 0/前端/静态检查、Compose/迁移/脚本检查。
- [x] 更新产品、技术、README 和本窗口记录，并按 Conventional Commits 拆分实现与文档逻辑提交。

## 实际受支持边界

- 自动证明范围：本机 Linux Stage 2 Worker 容器内，绑定的 execution session 及该 session 中 FFmpeg、`asr_client`、`llm_client` 等受控进程组。监督器必须能读完整 `/proc` 并核对 boot ID、PID/start ticks、session 和 Worker generation。
- 需要人工处理：监督器/容器/宿主机强停，数据库确认持续不可用，历史记录无新绑定字段，或 `/proc`、进程 session 身份无法可靠验证。保持 `stop_unconfirmed` / `STOP_CONFIRMATION_REQUIRED`，不释放全局执行位、不恢复、不清理工作目录。
- 不承诺：恶意本机代码主动 `setsid()` 逃逸、容器外进程、宿主机死亡后的内核级证明，以及撤销模型供应商已收到的请求或恢复已消耗费用。
- 当前窗口不增加分析恢复扫描、重放、补投、排队协调或新 attempt；Stage 2 Beat 和 maintenance Worker 继续只做临时上传回收。

## 验收记录

执行结果、命令、停止证据、服务状态和未验证边界记录于同目录 `validation.md`。
