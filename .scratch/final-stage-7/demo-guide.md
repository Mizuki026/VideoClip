# Stage 7 本地演示说明

## 当前服务

- 前端：`http://127.0.0.1:5174`（完成验收后保持运行）
- Stage 2 API：`http://127.0.0.1:58002`
- PostgreSQL host port：`55443`
- 当前迁移：`1a7e3c9d5b42`

服务仅绑定本机，不是公开部署。Stage 2 保留既有本地用户和历史记录；本窗口测试账户/视频已删除，不在 Stage 2 留存新展示记录。

## 启动、迁移和停止

从 PowerShell 仓库根目录启动 Stage 2；该脚本检查停止门禁和队列状态、构建/复用镜像、升级迁移并检查健康状态：

```powershell
Set-Location E:\VideoClip
.\scripts\stage2\run-stage2.ps1
```

若要单独检查/执行当前迁移：

```powershell
docker compose --env-file data/mvp-stage-2/.env -p videoclip-mvp-stage2 -f compose.yaml exec -T api alembic current
docker compose --env-file data/mvp-stage-2/.env -p videoclip-mvp-stage2 -f compose.yaml exec -T api alembic upgrade head
docker compose --env-file data/mvp-stage-2/.env -p videoclip-mvp-stage2 -f compose.yaml exec -T api alembic check
```

在另一个 PowerShell 窗口启动前端：

```powershell
Set-Location E:\VideoClip\frontend
npm run dev -- --host 127.0.0.1 --port 5174
```

关闭前端用 `Ctrl+C`。确认当前没有未确认 execution 后，在仓库根目录运行：

```powershell
.\scripts\stage2\stop-stage2.ps1
```

此脚本遇到未确认 execution 会拒绝完整停止。不得跳过停止证明或清理 Redis/数据库来“清空”状态。

## 正常演示流程

1. 登录演示者拥有的本地账户；已有 Stage 2 账户和视频属于原本的本地数据所有者，不在未经同意时展示。
2. 用有权使用的授权视频创建一条记录。本次核验样片位于 `video\agent-skill介绍.mp4`，实际约 17 分 41 秒、3.22 GB；用户选择其他授权文件也可演示上传。
3. 可在上传进行中暂停；刷新后重新选择原文件，观察客户端重新计算摘要和服务端确认偏移续传。重复完成不会创建重复视频。
4. 提交分析，观察事件时间线和真实 ASR/LLM 结果。摘要引用逐处与转写原文/时间对应；点击两处引用定位并继续播放。
5. 要保留笔记时使用“删除原视频，保留笔记”，检查播放器关闭、GET/HEAD/Range 返回 410，转写和摘要继续可读。随后用“删除整条记录”完成全量删除，确认相关接口 404。

该流程再次调用外部模型。虽然当前两个模型在官方公开信息与用户本机配置中显示免费，但账户剩余额度和调用后账单缺证；开始真实调用前先在账户页面确认仍免费且额度充足。程序没有付费模型兜底。

## 受控故障演示

以下脚本使用独立 Compose 与本地 HTTPS 模型替身，不调用供应商：

```powershell
Set-Location E:\VideoClip
.\scripts\stage2\run-queue-coordination-acceptance.ps1
.\scripts\stage2\run-final-stage5-acceptance.ps1
.\scripts\stage2\run-final-stage6-acceptance.ps1
```

每个脚本生成新的 run-id 和输出目录。Stage 4 展示繁忙排队、ACK 后续推进和迟到消息拒绝；Stage 5 展示 429 共享冷却、恢复和独立预算；Stage 6 展示停止确认、媒体取消/清理及笔记保留。Stage 5/6 使用受控测试环境；演示时说明其是替身故障链路，真实供应商流程是单独的 Stage 2 样片运行。

## 清理及数据所有权

- 当前 Stage 2 有 6 个本地用户、8 条历史视频/笔记。它们是既有本地数据，保留，不使用“清理全部”或 Docker prune；仅由相应账户所有者决定是否通过产品删除。
- 第 7 窗口创建的临时账户及视频已删除，数据库计数回到前置基线；原 17 分钟源文件仍在仓库授权素材目录并保持原样。
- 演示新建的每条记录属于登录账户。若不保留笔记，执行“删除整条记录”；若要保留笔记，执行“删除原视频，保留笔记”，之后可再删除整条记录。产品没有账户自助删除能力，清理视频不会删除账户。
- Playwright/模型响应、账户凭据、页面故障上下文只留在 Git 忽略的 `data/mvp-stage-2/acceptance/`；不公开、不提交、不上传。
