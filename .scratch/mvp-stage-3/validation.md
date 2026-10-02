# 第 3 窗口验收记录：人工恢复与故障处理

> 结论：**第 3 窗口人工恢复与故障处理阶段通过。** 本记录只覆盖后端手动恢复、成功检查点复用、受控删除与临时清理；不代表正式总结、引用发布、`succeeded` 或前端完成。
> 验收日期：2026-10-02；分支：`codex/mvp`；实现提交：`941749a2fb33e3719852ea65af0d9611a8b8ea9a`；真实指纹核对补充：`07dc4b78257a66bc1494a9a8280ab6fc17c050e3`；删除进度边界修正：`d3c22fdaa71b5cf85c95c689ae7605ab4978a0e1`。

## 范围与基线

- 开始时分支为 `codex/mvp`，HEAD 为 `ff320bd4a6edf9dca035b3dc420e6b4bdb338176`，工作区干净；第 2 窗口成果 `2b5254b` 及其后已有提交保留。
- 真实业务服务使用 Stage 2 PostgreSQL / Redis / 文件目录；测试套件只写隔离的 `videoclip_mvp_stage1_test` 与 `data/mvp-stage-1/test-data/app`，不清空或修改 Stage 2 业务库。
- 部署前 Stage 2 Alembic 为 `4c97a62f1a0b`，task 状态为 `awaiting_summary:2, failed:1`，未确认 execution 为 0。迁移 `a81f06d2d712` 仅增加 attempt 父子关系、清理错误码和转写来源 attempt；迁移后既有三条 task 状态与未确认 execution 数保持不变。
- Stage 1 API / PostgreSQL 容器 ID `1fea4a0c8fa4…` / `4d2eaf6a4277…` 始终未替换。Stage 2 PostgreSQL / Redis 容器 ID `8c7104b25f30…` / `e811da814ed4…` 保持不变；API、单 Worker 按本次代码构建重建及重启。

## 实现摘要

- 同一视频保留一个 task；每次恢复在该 task 下创建有父子关系的新 attempt。Celery 消息绑定 task UUID 和 attempt UUID，旧版仅携带 task UUID 的消息安全忽略。
- `POST /videos/{video_id}/tasks/{task_id}/recover` 校验 owner、CSRF、视频状态、当前 attempt、投递事实及停止确认。未领取的 `pending` / `unknown` / 明确失败可原子失效；`sent` 未领取需先达到 300 秒提示阈值；已领取执行必须同时具备 attempt 和 execution 停止确认。完整转写的 `awaiting_summary` 不可恢复。
- 恢复复用原冻结配置，在独立工作目录重建片段清单；仅复用源身份、清单指纹、片序、音频 / 视频区间、音频摘要和 ASR 请求指纹都匹配的已提交成功片段，并记录来源 attempt。仅对其余片段发起 ASR。
- 已提交任务的视频删除先持久化 `deleting` 状态和清理意图、使 task / attempt 失效；未确认停止前不清工作目录、原视频或数据库记录。`GET /videos/{video_id}/deletion` 返回所属用户的删除进度；`POST /videos/{video_id}/cleanup` 仅清理已确认停止 attempt 的临时文件，保留原视频和转写，不创建 attempt 或调用模型。
- 删除或清理中，`GET /videos/{video_id}` 返回稳定 `VIDEO_DELETION_IN_PROGRESS`；错误和待停止事实只从所属用户的 `/deletion` 进度接口读取。
- 有界维护脚本只操作 Stage 2 单个 Worker。按实际 execution UUID 运行 `scripts/stage2/stop-and-confirm-worker.ps1`，脚本以零宽限停止容器并核实退出后才写入停止事实；清理脚本另外校验 attempt 与安全路径。

## 真实 API → Worker → ASR → 中断 → 恢复验收

使用 `scripts/stage2/recovery-acceptance.py` 从仓库授权样片生成隔离的 368 秒 MP4，创建临时账号，经真实 Stage 2 API 上传并使用已配置的 SenseVoice ASR。受控样本为 13 个 30 秒切片（末片按实际 8 秒处理）；只保存验证所需摘要，不在此复制转写全文。

| 项目 | 实际结果 |
| --- | --- |
| 视频 / task | `27052709-eb7f-411c-a6c8-212bd5219005` / `41e58fe4-1487-4072-a1c0-82e59f716746` |
| 受控上传 | 9,299,976 字节；SHA-256 `ca2c0039df5804cfaa158d50f787fd5893a99c720832025813fda8b71fbaf821`；媒体时长 368.0 秒 |
| 原 attempt / 实际停止 execution | `5fdedf68-068d-4b69-997f-cd9d50309061` / `19efad7c-5f71-453a-b58d-09bd76b96bc0` |
| 停止时检查点 / 已登记请求 | 3 / 4；原 attempt 确认为失败且停止，未提交的第 4 次请求不能作为成功检查点 |
| 恢复子 attempt | `af0d93b9-efcf-41ab-a3f0-9e07486dce92`；父 attempt 与编号 2 核对通过 |
| 子 attempt 复用 / 新发请求 | 3 个已提交片段复用；音频与 ASR 请求指纹、清单 / 源指纹、配置摘要均与父 attempt 一致；对剩余 10 片发起 10 次请求 |
| 转写 / 重启 | 13/13 完整；所有者读取的复用文本 SHA-256、时间区间和 `source_attempt_id` 一致；API / Worker 重启后仍可读完整转写 |
| 最终状态 | `awaiting_summary`，`summary_status=waiting`，未标记 `succeeded`，没有调用 LLM |
| 测试视频与账号 | 视频删除闭环返回完成；测试 task、视频、临时账号及 fixture 已清除 |

未提交检查点的 ASR 请求无法证明供应商侧是否已处理完成；恢复只对缺少已提交成功检查点的片段重调。因此本实现不承诺供应商调用恰好一次。真实执行脚本还会比较父子 attempt 的冻结配置、源与 manifest 指纹，以及复用行的音频 / 请求指纹；转写内容仅以摘要核验，不将原文写入验收记录。

首次短样本试跑与长样本试跑均在验收脚本调用 Windows PowerShell 5.1 时，于停止步骤返回无有效输出的非零状态；两次均由 `finally` 清理测试视频并恢复 Stage 2 Worker，既有 task 计数保持不变。排查后让验收器优先调用仓库环境的 PowerShell 7 `pwsh`，并对单 Worker 使用即时容器停止；最终上述真实运行成功。早期试跑留下的两个空临时账号均在确认无视频 / task 关联后按准确用户名删除；会话恢复后的最终数据库核对及空账号清理见最后一节。

## 隔离测试及工程检查

完整后端测试与 Stage 0 探针回归均通过：

```powershell
$env:PYTHONPATH = 'src'
& .\data\mvp-stage-2\.venv\Scripts\python.exe -m pytest -q
& .\data\mvp-stage-0\.venv\Scripts\python.exe -m pytest probes\stage0\tests -q
```

- 后端：`68 passed`；仅有 Starlette 测试客户端依赖的 AnyIO `BlockingPortal` 别名弃用警告，与本次改动无关。
- Stage 0：`32 passed`。
- Stage 2 直接相关的 `test_analysis.py` 与 `test_upload_video.py`：`44 passed`（完整后端套件随后覆盖全部测试）。
- Ruff 检查通过；32 个 Python 文件格式检查通过；Mypy 对 18 个源 / 脚本文件检查通过；`pip check` 为 `No broken requirements found.`；验收脚本 `py_compile` 通过。
- Stage 1 / Stage 2 Compose `config --quiet` 均通过；部署后 Stage 2 `alembic check` 为 `No new upgrade operations detected.`；Stage 1 / Stage 2 PowerShell 脚本解析通过；`git diff --check` 通过。

覆盖的隔离场景另包括恢复资格与排队阈值、同父恢复幂等、绑定旧 attempt 的消息拒绝、请求 / 配置 / 源文件不匹配、恢复与领取 / 删除竞态、已领取但停止未确认时恢复和文件删除均被阻止、清理失败重试不丢检查点、完整 `awaiting_summary` 不可重跑，以及转写清单只复制完全匹配的成功检查点。Stage 1 未分析视频删除及 Stage 2 现有上传测试回归通过。

## 部署后状态、数据保护与 Docker 占用

- Stage 2 迁移头为 `a81f06d2d712`；既有任务状态仍为 `awaiting_summary:2, failed:1`，未确认 execution 为 0。最终 Stage 1 / Stage 2 `/health` 均为 `ok`，Stage 2 PostgreSQL / Redis healthy，Worker 数量为 1。
- 第 2 窗口原 task `8182f66b-a140-411d-ba2d-7df6882da107` 在本次迁移与真实运行后仍为 `awaiting_summary`，其当前 attempt 检查点仍为 13/13，execution 停止确认仍存在。
- 最终容器 ID：Stage 1 API / PostgreSQL `1fea4a0c8fa4…` / `4d2eaf6a4277…`；Stage 2 API / Worker / PostgreSQL / Redis `34914e08fdf6…` / `d836c10f86e1…` / `8c7104b25f30…` / `e811da814ed4…`。恢复后复核时 API / Worker 镜像均为同一构建，API 健康，Worker 数量为 1。
- `docker system df` 在构建前 / 后的镜像总量均为 5.118 GB；构建缓存从 7.676 GB 到 7.677 GB，净增约 1 MB；容器数量及 volume 数量未增。Stage 2 现有容器可写层约 72 KiB；Compose 将每容器日志限制为 2 × 10 MiB。以镜像、缓存净增、容器可写层和日志上限核算，本窗口 Docker 新增占用远低于 7 GiB。业务视频及数据库持久化在 E 盘 bind mount，不计入容器可写层。

部署及真实验收复现命令：

```powershell
.\scripts\stage2\run-stage2.ps1
$env:PYTHONPATH = 'src'
& .\data\mvp-stage-2\.venv\Scripts\python.exe scripts\stage2\recovery-acceptance.py --duration-seconds 368 --timeout-seconds 1800
```

第 2 窗口的 13/13 转写仍处于 `awaiting_summary`，其业务记录及授权样片未被用于本窗口的恢复或删除。正式 LLM 总结、引用发布、前端页面和 `/api` 代理继续留给后续窗口。

## 中断后续接复核

本记录在代码提交、真实恢复和测试完成后遇到会话中断；续接时未重做昂贵的真实 ASR 闭环，而是先复核现场。Stage 1 / Stage 2 `/health` 均为 `ok`；Stage 2 迁移仍为 `a81f06d2d712`、任务状态计数仍为 `awaiting_summary:2, failed:1`、未确认 execution 为 0，原第 2 窗口 task 仍为 13/13 且停止确认存在；`alembic check` 无新迁移差异，Worker 仍为单实例。复核发现一个生成命名空间用户 `stage3-d516eca91f8e`，关联视频、task、上传交接均为 0，仅有会话；按验收脚本相同的精确用户名及无视频 / task 条件移除，用户外键级联会话后再次确认 `stage3-*` 用户数为 0。该清理未触及既有业务账户或视频。
