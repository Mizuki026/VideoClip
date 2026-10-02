# 第 5 窗口验收记录：页面闭环

> 结论：**第 5 窗口页面闭环通过。** 验收日期：2026-10-02；分支：`codex/mvp`；实现提交：`f8c0f93`。

## 范围与保护

- 按 `.scratch/mvp-stage-5/spec.md` 实现 React + TypeScript 注册 / 登录、视频列表与详情、真实 task 状态、转写 / 合法总结展示、手动恢复 / 总结续接 / 清理、删除进度和鉴权视频回看。
- 页面与 API 使用同源入口 `http://127.0.0.1:5174`。Vite 开发和预览均固定 5174 并启用 `strictPort`；`/api` 转发到 `http://127.0.0.1:58002`，只剥除 `/api` 前缀。没有监听或使用 5173。
- Stage 2 私密运行配置的 `APP_ORIGIN` 调整为 `http://127.0.0.1:5174`，增加 `MVP_STAGE2_API_URL=http://127.0.0.1:58002`，使既有 Python 验收器仍直连 API 并发送准确 Origin。Stage 1 配置及数据库未改。
- 新增的 task 关联响应字段为向后兼容的 `analysis_task_id`，由所属用户视频读模型返回；此前已在提交 `4640d93` 增加并通过后端测试。
- 两个历史来源失配 task 与第 4 窗口成功结果均未修改。浏览器样本使用受控 `stage5-*` 账户和授权视频；每次失败执行均先核实执行停止事实，再经用户 API 删除视频并清理空测试账户。故障夹具只用于新建的 `stage5fixture-*` 账户。
- 测试输出、浏览器 Cookie / CSRF 值、真实转写、总结和引用没有写入本记录或构建产物。交互证据按脱敏后的路由、方法和状态码记录，不复制响应正文或资源 ID。

## 本地运行

从仓库根目录执行：

```powershell
Set-Location E:\VideoClip\frontend
npm ci
npm run dev       # http://127.0.0.1:5174
npm run build     # TypeScript 检查并生成 dist
npm run preview   # 构建产物仍使用 http://127.0.0.1:5174
npm run lint
npm test
npm run e2e
```

`VIDEOCLIP_API_TARGET` 可覆盖代理目标，默认 `http://127.0.0.1:58002`。Stage 2 仍由根目录 `compose.yaml` 的独立项目 `videoclip-mvp-stage2` 启动；准备私密数据后执行 `scripts/stage2/run-stage2.ps1`。浏览器源由 `APP_ORIGIN` 精确匹配。

## 浏览器交互证据

| 路径 / 操作 | 脱敏后的实际结果 |
| --- | --- |
| 页面源与代理 | 页面源 `http://127.0.0.1:5174`；同源 `/api/health` 为 200，`/api/auth/csrf` 为 200。Playwright 在 dev 和 build preview 各验证了入口。 |
| 注册、会话、CSRF | 注册 201；登录返回 HttpOnly 会话 Cookie；`/auth/me` 使用 Cookie 返回 200。带错误 CSRF 的登出请求返回 403，会话仍为 200。清除 Cookie 后，在活动页面触发受保护操作，页面提示登录状态失效并清除视频播放器；重新登录成功。 |
| 原始上传 | 对授权样本执行原始文件请求 `POST /api/videos`，返回 201，携带 `X-Video-Filename`，请求未使用 multipart。真实样本字节数为 1,116,803,236（大于 1 GiB）；另用隔离小样本快速验证请求与删除。 |
| 分析与持久化 | `POST /videos/{id}/tasks` 返回 202；真实 ASR 完成 13/13 段，task 达到可总结 / 成功状态。刷新详情后从 API 读回同一 task、attempt 和总结，不依赖页面内存。真实总结响应为 200，task 与 summary 为 `succeeded`。 |
| 媒体代理 | 同源 HEAD 返回 200 且长度等于视频字节数；`Range: bytes=0-31` 返回 206 / 32 字节；不可满足的 Range 返回 416 和 `Content-Range: bytes */<长度>`。原生 `<video>` 使用鉴权内容 URL；两条相距超过 1 秒的引用分别定位到不同原视频片段并持续播放，媒体 `readyState >= 2`、定位后播放时间继续前进。390 像素窄屏表单与详情无水平溢出。 |
| 隔离与删除 | 退出后会话和媒体内容请求返回 401；第二账户读取视频、task、转写、总结和媒体均返回 404。完整流程删除返回 202 或 204 后依 API 状态完成；删除后媒体、转写和总结新请求返回 404 / 410。 |
| 清理失败与重复恢复夹具 | 暂停前确认 Stage 2 无排队 / 运行 task、无未确认 execution、Redis `analysis` 队列为空。仅在新建测试 task 上制造投递失败及清理失败；页面同时展示清理重试和手动恢复。清理 API 返回 `cleaned`，task 仍可恢复；恢复 API 返回 202，随后对同一父 attempt 并发重复提交两次仍返回同一子 attempt。PostgreSQL 复核为 2 个 attempt、0 转写片段、0 总结请求、0 execution。隔离视频经 API 删除后，测试账户移除。 |

没有提交截图。上述脱敏状态表和 Playwright 断言作为受限交互证据；Playwright 失败目录未保留转写或模型响应，也没有把屏幕正文复制到记录。

## 自动化检查

- 前端：`npm run lint` 通过；`npm test` 为 2 个文件 / 5 项通过；`npm run build` 中 TypeScript 检查和 Vite 构建通过。组件测试还验证了已发布总结在临时清理失败时仍可见；重试仅发送清理 POST，不发起分析或总结写请求。
- 浏览器：dev 入口 smoke 1 项通过；完整注册、1.1 GiB 原始上传、真实转写 / 总结、引用播放、隔离、退出和删除流程此前为 2 项通过 / 1 项按环境跳过；持久化成功 task / summary 场景 1 项通过。新增同源会话、CSRF、HEAD / 206 / 416 浏览器场景 1 项通过；恢复 / 清理 / 重复请求隔离夹具 1 项通过。
- 真实流程复跑的不确定性：扩展安全检查后又运行完整真实分析时，供应商请求出现 `LLM_PHASE_TIMEOUT`。执行记录确认 Worker 已停止；受控视频经 API 删除，空账户清理，未确认 execution 为 0。该次复跑不计作通过，也未覆盖先前完整真实浏览器成功记录。新加的安全 / Range 和恢复夹具场景分别独立通过。
- 后端：Stage 2 完整 pytest 为 82 项通过，伴随 1 条既有 Starlette / AnyIO `BlockingPortal` 弃用警告；Stage 0 回归为 32 项通过。`ruff check src tests migrations scripts/stage2`、相关脚本格式检查、Mypy（21 个文件）与 Stage 2 脚本 `compileall` 通过。
- `TRANSCRIPT_SOURCE_MISMATCH` 的固定中文说明和终态不轮询由前端状态测试覆盖；原两条业务记录未被浏览器或夹具修改。其真实账户没有用于新浏览器测试，以避免触碰历史数据。

## 服务与资源

- 完成后 Stage 1 / Stage 2 API `/health` 均为 200；Stage 2 PostgreSQL、Redis 健康，单 Worker 恢复运行。故障夹具投递的消息在测试视频删除后由 Worker 消费为过期无效消息，`analysis` 队列最终为 0；数据库 queued / running task 与未确认 execution 均为 0。
- 构建缓存排除 `frontend/node_modules`、`dist`、测试输出。Docker build context 为 84.37 KiB；Stage 2 镜像 938,276,760 字节，相比变更前镜像 938,276,711 字节增加 49 字节；缓存观测从约 7.681 GB 到 7.682 GB，增量约 1 MB，远低于 7 GiB。没有 prune，也没有清理或迁移其他 Docker 项目。
- 本次临时 8 秒小视频、所有生成视频 / task 和受控账户已清理。正式授权视频、既有成功总结、两个历史失配 task、Stage 1 / Stage 2 业务库和私密配置均保留。

## 交付限制

- 本窗口没有实现公开部署、发布或第 6 阶段展示包装。第 6 窗口仍需单独完成最终复查与展示验收。
- 历史失配记录保留且有稳定错误说明；本次未对其做模型请求或浏览器改写。总结供应商存在偶发读取超时，界面保留后端失败 / 恢复状态，不把失败显示成成功。
