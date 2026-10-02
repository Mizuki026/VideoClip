# 第 2 窗口验收记录：第一条异步转写链路

> 结论：**第 2 窗口异步转写阶段通过。** 本结论只覆盖真实异步转写链路及停止/检查点事实；人工恢复、检查点复用、正式 LLM 总结、引用发布和完整用户结果仍待后续窗口。
> 验收日期：2026-10-02；分支：`codex/mvp`。

## 范围与环境

- Stage 2 在现有 FastAPI、PostgreSQL、Alembic 和受控文件存储上增加分析 task/attempt、投递与执行事实、转写清单/检查点、Redis 和 Celery Worker。API 与 Worker 共用镜像；Redis 消息只含任务 UUID。
- Compose 项目为 `videoclip-mvp-stage2`，API 回环端口 58002、PostgreSQL 回环端口 55443，Redis 只在 Compose 网络内开放。实际私密配置 `data/mvp-stage-2/.env` 被 Git 忽略；只提交不含密钥的 `config.stage2.example.env`。
- Stage 1 项目 `videoclip-mvp-stage1` 继续运行在 58001/55442。验收前后其 API / PostgreSQL 容器 ID 为 `1fea4a0c8fa4…` / `4d2eaf6a4277…`，未停止或替换。
- 实际版本：Python 3.13.12、Celery 5.6.3、Kombu 5.6.2、redis-py 5.2.1、HTTPX 0.28.1、PostgreSQL 16.15、Redis 7.4.11、FFmpeg / ffprobe 5.1.9-0+deb12u1。依赖版本固定在输入清单与锁文件。
- Worker 单实例、并发 1、预取 1、JSON 序列化、无结果后端；`acks_late=False`、`reject_on_worker_lost=False`、`publish_retry=False`，无业务自动重试。

## 真实 ASR 验收

验收脚本 `scripts/stage2/acceptance.py` 通过 API 注册/登录、上传授权样片、提交任务、轮询任务与转写接口，然后重启 API / Worker 并再次读取结果。最终接受任务如下：

| 项目 | 结果 |
| --- | --- |
| 视频 ID / 任务 ID | `7dbb0246-ee86-42df-9b39-80becf3f37a0` / `8182f66b-a140-411d-ba2d-7df6882da107` |
| 样片 | `video/如何选择适合自己的agent.mp4` |
| 字节数 / SHA-256 | 1,116,803,236 / `2b4230d3277d8f0fd3370db11dce78532d20ae51f2dc1149f1d0cdba932eec7a` |
| 视频时长 | 368.597312 秒 |
| ASR 模型 | `FunAudioLLM/SenseVoiceSmall` |
| 配置 SHA-256 | `e1c16dba81ec26367e1dfb754623289cf012591e3f13b354ac34047dd77cd382` |
| 源文件 / 清单指纹 | `d8ea8c891c7fac6caeeeb751bf4777bce1b779a332fcfd6b0d87c588d781ffae` / `33494f8700749bf41910a2d6bba4e470a846324b507a9994015cd1ee52ef925f` |
| 清单 / 完成片段 / ASR 请求 | 13 / 13 / 13；每片一个 HTTP 200 请求 |
| ASR 累计耗时 / 整体处理耗时 | 43.461 秒 / 59.92 秒 |
| 最终状态 | `awaiting_summary`、`summary_status=waiting`、执行停止已确认，非 `succeeded` |
| 服务重启后 | 任务仍为 13/13；转写接口返回完整 13 片 |

PCM 规格为 16 kHz、单声道、s16le，30 秒切片；时间轴 0–30、30–60，依此类推，最后一片为 360–368.59725 秒，实际长度 8.59725 秒。文本不复制到验收文档。13 个实际 ASR trace ID（按片序）：

```text
ti_nzbosjauwalq6w07oa, ti_epkfy9ozd75kknx1eh, ti_iq05236njicehmzxi5,
ti_brd1alkqgeuxg8o6cw, ti_4t2lga33kt7dw37s23, ti_zptnzu9u2d3qtsk8y5,
ti_ntn1q0bv482cb9x5zv, ti_k3awo7zot62i1pzsr2, ti_0dpvly5qbss257t2a8,
ti_r6h0xqr25lo1ox41em, ti_bt08hdkpxy9qljpw1q, ti_cbscz6ie1vwgu84v7r,
ti_lveg3fkg8xq43bvnrc
```

另有一条真实 ASR 任务 `d45f837f-7cf7-41f4-903b-9dd4b6f31c83`，同样 13/13 完成。未调用 LLM 或发布摘要。重启前验收逐片检查了片序、非空文本、状态和视频区间；重启后重新读取任务和转写，核对完整标志和总数。**重启后未做逐字节文本及逐项区间相等比较**，因此只确认数据可读且数量/完整性一致。

## 故障、停止确认与清理

- 隔离 PostgreSQL 测试覆盖提交后未发送、broker 确认丢失、重复/乱序/迟到消息、重复领取、两个视频竞争全局执行权、投递与领取竞态、删除门闩竞争、ASR 返回后检查点提交失败、FFmpeg 超时、慢 ASR、模拟视频失效和数据库会话终止。旧执行标识不能写结果；检查点提交失败不允许同一 attempt 再次调用模型。已领取任务不会被投递异常覆盖；无法证明子进程退出时保留占位并禁止清理/新执行。
- 一次真实受控 Worker 停止演练最初发现子进程组停止辅助代码将 `ProcessLookupError` 错判为停止未确认。修复后用执行 ID `482c07c8-b5de-4ff6-9ef0-339368eca784` 再演练；对应 task 以 `WORKER_STOPPED_OPERATOR` 失败、领取次数 1、ASR 请求数 0 持久化。attempt `dd0e4365-7865-4680-b873-1dc65157bf36` 经退出核验后由受限脚本登记 `cleaned`。视频和业务记录保留。
- Stage 2 启停及限定清理命令：

  ```powershell
  .\scripts\stage2\prepare-stage2.ps1
  .\scripts\stage2\run-stage2.ps1
  .\scripts\stage2\stop-and-confirm-worker.ps1 -ExecutionId 482c07c8-b5de-4ff6-9ef0-339368eca784
  .\scripts\stage2\cleanup-confirmed-attempt.ps1 -AttemptId dd0e4365-7865-4680-b873-1dc65157bf36
  ```

  后两条是已完成演练的记录，不可照搬到其他执行。核验脚本只操作 Stage 2 Worker，且须将容器退出关联到执行记录；清理脚本再次核对数据库停止事实、attempt ID 与受限路径。无法确认时保留占位和文件。本窗口没有新 attempt 的人工恢复入口。

## 持久化、兼容性与 Docker 预算

- 容器挂载实测：API / Worker 工作目录为 `E:/VideoClip/data/mvp-stage-2/app`，PostgreSQL 为 `.../postgres`，Redis AOF/RDB 为 `.../redis`，密钥文件来自 `.../secrets/postgres_password`。数据均落于 E 盘项目私有目录。
- Stage 2 迁移 `4c97a62f1a0b_async_transcription` 已应用；Alembic 检查返回 `No new upgrade operations detected.` pytest 使用隔离库 `videoclip_mvp_stage1_test` 和 `data/mvp-stage-1/test-data/app`，不清空 Stage 1 业务库。
- 路由保留第 1 窗口兼容形式：`POST /videos/{id}/tasks`、`GET /videos/{id}/tasks/{task_id}`、`GET /videos/{id}/transcript`。部署代理应剥除 `/api` 前缀再转发，如 `/api/videos/{id}/tasks` → `/videos/{id}/tasks`。实际代理和前端未实现。
- Docker 基线 / 构建后：总镜像约 5.000 / 5.118 GB，缓存 206 项、7.555 GB / 224 项、7.676 GB；镜像汇总增约 118 MB、构建缓存约 121 MB。没有新增 Docker 命名卷；Stage 2 容器可写层合计约 69 KB。每容器日志上限 2×10 MB，内存及 PID 有上限。镜像、缓存增量、日志预算和可写层总和远低于 7 GiB；现有 Docker 缓存总量主要在本窗口前已存在。
- 验收过程中保留 3 份相同授权样片上传副本（每份 1,116,803,236 字节）。只在确认停止后清理 attempt 临时工作目录，不删除视频或转写记录。交付时 Stage 2 API、PostgreSQL、Redis 和一个 Worker 均保持运行。

## 验证命令与结果

PowerShell 命令如下。完整集成测试使用第 1 窗口的独立测试数据库。

```powershell
$env:PYTHONPATH = 'src'
& .\data\mvp-stage-2\.venv\Scripts\python.exe -m pytest -q
& .\data\mvp-stage-0\.venv\Scripts\python.exe -m pytest probes\stage0\tests -q
& .\data\mvp-stage-1\.venv\Scripts\python.exe -m ruff check src tests scripts/stage1 scripts/stage2 migrations
& .\data\mvp-stage-1\.venv\Scripts\python.exe -m ruff format --check src tests scripts/stage1 scripts/stage2 migrations
& .\data\mvp-stage-1\.venv\Scripts\python.exe -m mypy src scripts/stage2/acceptance.py
& .\data\mvp-stage-2\.venv\Scripts\python.exe -m pip check
& .\data\mvp-stage-2\.venv\Scripts\python.exe scripts/stage2/acceptance.py
docker compose --env-file data/mvp-stage-2/.env -p videoclip-mvp-stage2 -f compose.yaml config --quiet
docker compose --env-file data/mvp-stage-1/.env -p videoclip-mvp-stage1 -f compose.yaml config --quiet
docker compose --env-file data/mvp-stage-2/.env -p videoclip-mvp-stage2 -f compose.yaml exec -T api alembic check
```

- 完整后端：60 passed；Stage 0 探针回归：32 passed。
- Ruff 检查通过，29 个文件格式检查通过；Mypy 16 个源文件通过；pip check 为 `No broken requirements found.`
- Stage 1 和 Stage 2 Compose 配置解析、Stage 2 Alembic 漂移检查、5 个 Stage 2 PowerShell 脚本语法解析均通过。
- 最终健康检查中 Stage 1 与 Stage 2 API 的 `/health` 均为 `ok`；Stage 2 PostgreSQL/Redis healthy，Worker 单实例运行。

## 限制与下一步

- `sent` 只表示发布确认，不保证 Redis 消息永不丢失；`unknown` 不伪报为执行失败。外部 ASR 返回而数据库检查点失败时不宣称片段成功，也不保证外部请求恰好一次。
- 不自动接管停止未确认的执行，不自动重试；人工恢复和检查点复用留给第 3 窗口。
- 不调用 LLM，不生成摘要，不把转写完成当成分析成功；正式总结、引用校验/发布和页面留给第 4 窗口及之后。
- 业务文件大小上限与业务目录 5 GiB 运行期硬限制按既定决策维持取消；媒体时长、E 盘位置、单执行和期限约束仍有效。
