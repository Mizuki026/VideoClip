# 最终版第 5 窗口验证记录

> 状态：实现和验收完成，已通过隔离故障注入；未调用真实供应商模型。
> 日期：2026-10-04（Asia/Shanghai）。

## 基线、范围与数据保护

- 基线分支 `codex/mvp`，HEAD `b60682474aca25fe53ecf30ef587228ade28e94b`，父提交 `99340d5a91a3aac56128f4d5480ffb20fbcb1f02`；修改前工作区干净。没有切换分支、重置或改动历史业务库。
- 第 4 窗口部署迁移为 `b4c91e7a2d60`；本窗口迁移以该版本为父级。Stage 5 隔离数据库完成全链迁移，最终 `alembic_version=7c2a91e5b04d`。
- 按要求阅读根 `AGENTS.md`、其中指定的 `docs/agents/domain.md`、产品/技术最终文档、README、第 3/4 窗口规格与验证记录，并核对模型调用、总结修复、检查点复用、人工恢复、停止确认和投递协调代码及测试。
- 隔离验收唯一 Compose 项目为 `videoclip-mvp-stage5-20261004t023806z-bc633129`，数据保留于忽略目录 `data/mvp-stage-5/acceptance/20261004t023806z-bc633129/`。完成后只停止了该项目的容器；没有删除其卷/数据，没有触碰其他 Docker 项目、密钥配置或历史业务数据。
- Playwright 每次使用独立输出目录。验收数据、临时认证材料、日志及合成课程文本均未暂存；提交只包含代码、测试和文档。

## 自动化验证

| 验证 | 结果 |
| --- | --- |
| 后端全量（隔离 Stage 5 测试数据库） | `141 passed`；仅有一条 Starlette/AnyIO 弃用提示 |
| Stage 0 回归 | `32 passed` |
| Ruff `src tests scripts/stage2/final-stage5-acceptance.py` | 通过；修改 Python 文件格式检查通过 |
| 项目配置的 `mypy src/videoclip` | 29 个源模块通过 |
| `pip check` | 无依赖冲突 |
| 前端 lint | 通过，无 warning |
| 前端 Vitest | 5 个文件、14 个测试通过 |
| 前端 TypeScript/build | 通过，Vite production build 成功 |
| `git diff --check` | 通过 |
| Compose 配置、隔离服务启动及迁移 | 通过；隔离库最终迁移为 `7c2a91e5b04d` |

## Beat 到两个 Worker 的隔离验收

执行入口：`scripts/stage2/run-final-stage5-acceptance.ps1 -TimeoutSeconds 900`。该入口在专用 PostgreSQL、Redis、Beat、maintenance Worker、analysis Worker、API 和本地 HTTPS 模型替身组成的 Compose 项目中使用生产模型客户端。等待观察时暂停/恢复的是此隔离项目的 maintenance Worker。

- 两个任务共用同一 LLM 账户引用：首个 429 持久化 120 秒冷却；另一个任务在 LLM 门禁等待时已完成 ASR，冷却解除后由 Beat/维护 Worker 自动续接。已完成转写没有重跑；正常等待未增加自动恢复/总结请求预算。浏览器等待状态通过。
- 受控 Worker 子进程故障通过 Stage 3 Supervisor 停止证明后续接；浏览器显示未确认停止时不提供接管入口。全部 14 个 execution 最终 `stop_confirmed_at` 均已写入，峰值未确认 execution 不超过 1。
- LLM 5xx 和超时均分类为白名单可恢复失败，各自一次 summary-only 自动恢复成功；两者都没有重做完整转写。鉴权失败保留人工处理原因且未自动重试。
- 连续模型失败的预算用例到达 `auto_recovery_count=2`、总结请求数 `3` 后停止；完整转写保留，无总结发布。补投仍由原 Stage 4 字段独立计数。
- 最终合成库为 7 个任务、14 个 attempt/execution；5 个任务成功且各只有 1 个已发布总结，2 个失败任务无发布，重复总结数为 0。全部任务合计 6 次自动恢复，12 条总结请求。模型替身统计 ASR 8 次、LLM 12 次，未访问真实模型服务。
- Beat 日志包含周期任务 `coordinate-analysis-deliveries`；maintenance Worker 报告等待/恢复扫描并续接；analysis Worker/Supervisor 日志记录可信停止确认。

## 浏览器结果与修正记录

通过的浏览器场景：共享模型等待、未确认停止提示、两个预算耗尽提示，以及恢复资格和永久错误状态。恢复/永久错误用例第一次发现鉴权错误仍显示通用错误码文案，断言未通过；因此增加 `LLM_AUTHENTICATION_FAILED` 本地化提示和映射单测，并在保留的同一隔离数据上以新 Playwright 输出目录重跑该真实浏览器用例，结果 `1 passed`。修正后的前端 lint、14 项 Vitest 和 production build 均通过。

整合脚本在上述第一次 UI 断言处退出非零，因此脚本末尾汇总没有自动写入结果文件。失败前的调度/数据库证据完整保留；修正后重新运行了失败的浏览器用例，并独立核对最终数据库聚合、模型替身计数、Beat/Worker 日志和迁移版本。上述结果均为实测，不将该次整合脚本退出码描述成成功。

## 已验证的参数与纳入语义

- 共享冷却键为 `(provider, 非秘密 account_reference, model)`；密钥轮换继续用同一账户引用。429 的合法 Retry-After 秒数/HTTP-date优先；缺失/非法时使用有界 equal-jitter 指数退避。冷却只延长，短提示不会缩短已有截止。纯函数显式注入 UTC 时间和随机分位数。
- 默认退避 `60–900 秒`；单次等待本地上限 `86,400 秒`；累计上限 `259,200 秒`。供应商截止超出单次/累计本地上限时转人工，不截断冷却。累计等待持久化，重启和新 attempt 不清零，重复扫描不重置期限。
- 每 task 自动执行恢复最多 `2` 次；总结请求跨自动/人工 attempt 最多 `3` 次，预留即扣、不退还；Stage 4 补投保留默认 `8` 次及 `24 小时` 可推进期限，冷却等待不重置其计数/期限。
- 新任务由当前代码显式初始化 Stage 5 状态；迁移不自动纳入历史 `failed` / `awaiting_summary`，不伪造历史预算、账户身份或冻结快照。部署后显式操作才可按规格登记历史任务。

## 未覆盖边界

- 没有对 SiliconFlow 真实账户发请求；供应商限额、账务、真实 Retry-After 分布和费用均未实测。官方账户额度契约与 RFC 9110 依据见 `spec.md`。
- 容器/宿主机强停、DB 断连、停止确认写库失败、监督器死亡等边界由单元/事务/Stage 2–4 回归覆盖；本轮实时 Compose 注入覆盖了受控子进程故障和成功停止确认，没有伪称每种宿主机故障都在本次浏览器链路逐一实际注入。
- 项目 mypy 目标为 `src/videoclip`；测试模块不在配置的 mypy 目标中。
- 另行试跑未配置的 `mypy src tests` 时报告 9 个测试模块共 85 条类型错误；这不是项目配置的 mypy 目标，本窗口不将其报告为通过，也未用该扫描结果替代测试运行证据。
