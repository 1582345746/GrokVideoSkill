# GrokVideoSkill 文生视频与图生视频开发交接文档

> 交接日期：2026-08-27  
> 交接目标：在新的 Codex 工作区中继续开发、测试并发布 `GrokVideoSkill`。  
> 当前工作区根目录应设置为：`E:\MyFiles\ToolSkills\GrokVideoSkill`

## 1. 项目目标

本技能是一个供 Codex 调用的、可恢复的 AI 视频工作流技能。Codex 负责理解需求、写剧本、拆分镜头、生成提示词和组织交付；Python CLI 负责凭据、上游请求、轮询、媒体下载、断点恢复、技术质检和视频合并。

本轮开发要把视频能力明确拆成两种模式：

1. **文生视频（T2V）**：不依赖参考图。Codex 先写剧本和分镜，再把每个镜头的动作提示词提交给视频模型。单个镜头最多 15 秒。
2. **图生视频（I2V）**：使用当前镜头关键帧或用户提供的参考图生成视频。角色一致性、关键帧和参考图流程继续保留。

两个上游都可能具备两种能力，但本项目当前必须按用户需求固定选择访问入口，不能因为“两个上游都能生成视频”而自动混用或静默回退。最新确认的默认路由是：**文生视频走 QuickAI，图生视频走 QuickAI New**。

当前默认策略：

| 项目模式 | 默认提供方 | 协议 | 说明 |
| --- | --- | --- | --- |
| `text-to-video` | `quickai` | JSON `/v1/videos/generations` | 文生视频主路径，直接用 QuickAI 文生视频 Key |
| `image-to-video` | `quickainew` | multipart `/v1/videos` | 图生视频主路径，使用 QuickAI New 图生视频 Key |
| 任一模式 | 可手动覆盖 | 由提供方适配器决定 | 允许 QuickAI/QuickAI New 互换，便于以后接 Seedance |

这不是对上游能力的否定，而是本项目的稳定路由约定。QuickAI New 目前确认可用于图生视频；QuickAI 的文生视频是本轮优先验证的路径。模式和提供方必须写入项目合同，不能通过“有没有图片”隐式猜测。若以后需要用 QuickAI 做 I2V，或用 QuickAI New 做 T2V，必须通过项目级 `video_provider` 显式覆盖，并保留对应协议适配器。

## 2. 相关项目位置

### 2.1 当前技能仓库

路径：`E:\MyFiles\ToolSkills\GrokVideoSkill`

主要文件：

- `grok-video-studio/SKILL.md`：Codex 使用说明和安全规则。
- `grok-video-studio/scripts/grok_video_studio.py`：CLI、项目合同、工作流、凭据配置、生成、轮询、QA、组装。
- `grok-video-studio/scripts/media_client.py`：图片客户端和视频客户端。
- `grok-video-studio/scripts/gvs_common.py`：配置、DPAPI、HTTP、下载、通用错误处理。
- `grok-video-studio/scripts/provider_contracts.py`：不同上游返回结构的通用解析器。
- `grok-video-studio/scripts/media_tools.py`：ffmpeg/ffprobe 媒体检查和后处理。
- `grok-video-studio/assets/workflow-templates/`：可编辑工作流模板，已有 `text-to-video.json`、`general-video.json`、`single-image-animation.json` 等。
- `grok-video-studio/references/`：API、项目结构、提示词、工作流和错误矩阵文档。
- `tests/test_grok_video_studio.py`：本地假上游集成测试。
- `install.ps1`：复制技能并可通过 stdin 配置凭据。

当前 Git 状态（交接前）：工作区干净；最近提交为：

- `fb1d59b feat: let Codex configure credentials during install`
- `c31bb46 feat: harden video workflow and delivery tooling`
- `ab539ad feat: add resumable Grok video studio skill`

当前 `origin` 只有 GitHub：`https://github.com/1582345746/GrokVideoSkill.git`。Gitee 远程地址需要新会话重新验证，历史上尝试访问 `https://gitee.com/1582345746/GrokVideoSkill.git` 时网络连接失败，不能假定仓库已存在。

### 2.2 已实现生图/生视频的主站项目

路径：`E:\MyFiles\QuickAiCanvas`

这是主站，包含：

- 主站视频模块：文生视频、图生视频。
- Canvas 内嵌视频创作台：独立代码模块，不能因本技能开发而修改其既有行为。
- QuickAI、QuickAI New、第三方渠道配置。
- 已有较完整的前端视频请求适配逻辑，可作为本技能协议实现的参考。

可重点参考：

- `canvas/web/src/services/api/video-request.ts`
- `canvas/web/src/services/api/video.ts`
- `web/src/features/video/VideoGenerationPage.tsx`

主站当前已经体现出两个重要事实：

1. QuickAI 使用 `/videos/generations` JSON 请求，任务查询路径为 `/videos/generations/{id}`，并使用 `/content` 下载。
2. QuickAI New 使用 `/videos` multipart 请求，分辨率需要单独传 `resolution`，图片参考使用重复的 `input_reference` 文件字段。

这部分代码只能作为协议和错误处理参考。用户此前明确：**Sub2API、NewApi 不修改；主站和技能可以修改。**

### 2.3 QuickAI 中转站源码

路径：`E:\MyFiles\Sub2ApiTransfer`

用途：QuickAI 风格的 OpenAI 兼容中转站和 Grok 媒体转发实现。只读查看，不修改。

重点参考目录/文件：

- `backend/internal/handler/grok_media.go`
- `backend/internal/service/grok_media.go`
- `backend/internal/handler/endpoint.go`
- `backend/internal/server/routes/gateway.go`
- `backend/internal/service/account_test_service.go`
- `README.md` 中 Grok 媒体接口说明

已确认的路由包括：

- `POST /v1/videos/generations`
- `GET /v1/videos/generations/{request_id}`
- `GET /v1/videos/generations/{request_id}/content`

QuickAI 端会读取和计费 `resolution`。只传 `size` 或传入非 `480p`、`720p`、`1080p` 的值可能被按默认/高档处理。技能必须把用户选择的分辨率以 `resolution` 明确传给上游，并在本地签名、状态和 QA 中记录。

源码中还可以看到 `reference_images`、`image`、`input_reference` 等图片字段的兼容归一化。具体字段以当前上游源码和实测响应为准，不要给同一个请求重复发送语义相同但可能触发严格校验的字段。

### 2.4 QuickAI New / NewApi 中转站源码

路径：`E:\MyFiles\NewApiTransfer`

用途：New API 风格的中转站和异步视频任务实现。只读查看，不修改。

重点参考：

- 视频任务适配器和请求 DTO。
- 分辨率、时长、`input_reference` 校验。
- 任务状态、内容下载和错误返回结构。

用户要求保持该项目不变。技能应适配它已有的协议，而不是要求它修改入参。

## 3. 已知上游协议

### 3.1 QuickAI JSON 视频协议

上游基地址：`https://quickai.hn.takin.cc`。配置时保存 origin，不要把 `/v1` 写进配置。

模型发现：

```http
GET /v1/models
Authorization: Bearer <QuickAI key>
```

视频创建：

```http
POST /v1/videos/generations
Content-Type: application/json
Authorization: Bearer <QuickAI key>
```

建议请求结构：

```json
{
  "model": "grok-imagine-video-1.5",
  "prompt": "一个连续、可在 15 秒内完成的动作描述",
  "seconds": 8,
  "aspect_ratio": "16:9",
  "resolution": "480p"
}
```

图生视频时，在上述 JSON 中增加参考图。文档列出的字段为 `reference_images` 和 `input_reference`；Sub2API 源码还兼容 `image`/`images`。实现时应在客户端内统一为一个明确的策略：

- T2V：不发送任何图片字段。
- I2V：单张首帧优先使用上游明确支持的 `input_reference`；多参考图使用 `reference_images` 数组，元素为 `{ "url": "..." }`。
- 本地图片需要转成 HTTPS URL 或 data URL。不能把本机路径直接发给远端。

用户提供的接口文档明确要求：

- `seconds` 为 1-15 秒。
- `resolution` 只能是 `480p`、`720p`、`1080p`；其他值可能统一按 1080p 计费。
- `aspect_ratio` 支持 `16:9`、`9:16`、`1:1`、`4:3`、`3:4`、`2:3`、`3:2`。
- `reference_images` 最多 7 张。
- `input_reference` 仅 1 张，表示首帧推演。

创建成功通常返回 `id`/`task_id`、`status`、`progress`、`seconds`、`size` 等字段。任务查询和成品下载应优先使用：

```http
GET /v1/videos/generations/{task_id}
GET /v1/videos/generations/{task_id}/content
```

兼容实现可以在内容接口失败时读取状态响应中的 `url`、`result_url` 或 `video_url`，但仍需下载字节并执行 MP4/ffprobe 检查。

### 3.2 QuickAI New multipart 视频协议

上游基地址：`https://quickainew.hn.takin.cc`。

**最新上游确认：QuickAI New 当前可用于图生视频。** 因此技能在用户说“图生视频”且未指定其他提供方时，必须选择 QuickAI New，并发送参考图；不能仍然把请求发到 QuickAI 的文生视频入口。用户说“文生视频”时，默认选择 QuickAI。两条默认路由如下：

```text
文生视频 -> https://quickai.hn.takin.cc -> JSON /v1/videos/generations
图生视频 -> https://quickainew.hn.takin.cc -> multipart /v1/videos
```

只有项目明确设置 `video_provider` 时才允许改变这两个默认映射；切换提供方时必须同时使用该提供方对应的 Key 和协议。

创建：

```http
POST /v1/videos
Content-Type: multipart/form-data; boundary=...
Authorization: Bearer <QuickAI New key>
```

至少包含：

- `model`
- `prompt`
- `seconds`
- `resolution`
- 可选 `aspect_ratio`
- 可选 `size`
- I2V 时重复发送 `input_reference` 文件字段

T2V 可以在不带 `input_reference` 的情况下调用同一接口。I2V 只发送当前镜头关键帧/参考图，不要把角色多视图 master sheet 直接作为视频参考图。

任务查询和内容接口通常为：

```http
GET /v1/videos/{task_id}
GET /v1/videos/{task_id}/content
```

已有 `provider_contracts.py` 会处理多种包装层、snake_case/camelCase 的任务 ID、状态、进度和结果 URL；新适配器应复用它，不要重新写一套脆弱的字段解析。

## 4. 当前技能实现与缺口

当前版本在 `grok-video-studio/scripts/grok_video_studio.py` 中是 `SKILL_VERSION = "1.2.1"`。

当前实现的关键行为：

- `QuickAIImageClient` 只负责 `/v1/images/generations` 和 `/v1/images/edits`。
- `QuickAINewVideoClient` 负责 multipart `/v1/videos`。
- `clients()` 会调用 `load_settings()`，目前要求两个 Key 同时存在。
- `generate_videos()` 总是调用 `video_references()`；有生成关键帧时会自动把关键帧作为视频参考图。
- `init_project()` 没有项目级 `video_mode`、`video_provider`、`video_resolution`、`aspect_ratio` 字段。
- `configure` 的 stdin JSON 目前要求 `quickai_key` 和 `quickainew_key` 都是字符串且都非空。
- `save_settings()`/`load_settings()` 的密钥逻辑也是“双 Key 必须同时存在”。
- QuickAINew 客户端当前主要发送 `model`、`prompt`、`seconds`、`size` 和 `input_reference`，需要补齐 `resolution`、`aspect_ratio`。
- 技能文档当前表述为 QuickAI 负责图片、QuickAI New 负责视频，已经不符合新的双提供方、双模式目标。

因此当前技能**还不能**直接满足“只给 QuickAI 文生视频 Key，Codex 写剧本并生成视频”的要求。

## 5. 建议的实现设计

### 5.1 客户端层

在 `media_client.py` 增加一个独立的 `QuickAIVideoClient`，不要把 QuickAI 视频请求塞进图片客户端：

- `list_models()`：`GET /v1/models`。
- `create()`：JSON `POST /v1/videos/generations`。
- `query()`：`GET /v1/videos/generations/{task_id}`。
- `poll()`：复用现有状态、重试和 circuit breaker 逻辑。
- `download()`：优先 `/content`，失败后尝试状态中的公开结果 URL，并执行 `assert_mp4()`。
- `create()` 必须传 `seconds`、`resolution`、`aspect_ratio`；T2V 不传图片字段。

扩展 `QuickAINewVideoClient.create()`：

- 新增 `resolution` 和 `aspect_ratio` 参数。
- T2V 允许 `references=[]`。
- I2V 继续以重复 `input_reference` 文件字段提交。
- 不自动重试 POST 创建，避免重复计费。

可抽取一个内部视频轮询基类/帮助函数，但不要为了抽象而重写已有的状态错误逻辑。

### 5.2 配置层

建议保持现有配置文件兼容，采用可选 Key，而不是让旧配置失效：

- `quickai_key`：可用于 QuickAI 生图、QuickAI T2V/I2V。
- `quickainew_key`：可用于 QuickAI New T2V/I2V。
- 至少一个 Key 必须存在；未使用的提供方不应导致 `doctor` 失败。
- 配置中增加默认提供方字段，例如 `default_video_provider: "quickai"`，但项目级字段优先。
- `configure --credentials-stdin` 接受一个或两个 Key。建议支持可选 `video_provider`，并保持旧的双 Key JSON 仍可用。
- DPAPI secret JSON 可以只保存一个 Key；不得把 Key 写入项目、命令行、日志、SKILL.md、Git 或临时明文文件。
- 环境变量继续支持 `GVS_QUICKAI_KEY` 和 `GVS_QUICKAINEW_KEY`。

如果升级配置版本，必须提供旧版本自动迁移；不要因为加了可选字段而强制用户重新录入现有密钥。

### 5.3 项目合同层

建议在 `project.json` 增加：

```json
{
  "video_mode": "text-to-video",
  "video_provider": "quickai",
  "defaults": {
    "video_resolution": "480p",
    "video_aspect_ratio": "16:9",
    "video_size": "1280x720",
    "video_seconds": 8
  }
}
```

镜头可以覆盖默认值：

```json
{
  "video_mode": "image-to-video",
  "video_provider": "quickainew",
  "video_resolution": "480p",
  "video_aspect_ratio": "9:16",
  "video_size": "720x1280",
  "seconds": 8,
  "generate_image": true,
  "video_references": []
}
```

兼容规则：

- 新建 `text-to-video` 工作流时默认 `video_mode=text-to-video`、`video_provider=quickai`、`generate_image=false`。
- 新建图生视频工作流时默认 `video_mode=image-to-video`、`video_provider=quickainew`。
- 旧项目缺少这些字段时按旧行为解释为 `image-to-video + quickainew`，不能因为升级而突然把旧项目变成纯文生视频。
- `video_mode=text-to-video` 时，`generate_videos()` 必须完全跳过 `video_references()`，即使 state 中存在旧关键帧也不能偷偷发送。
- `video_mode=image-to-video` 时，才解析显式参考图或已生成关键帧。
- 所有模式都强制 `seconds` 1-15，并把 resolution 限定为 `480p/720p/1080p`。

CLI 建议增加：

```text
init ... --mode text-to-video --video-provider quickai --video-resolution 480p
init ... --mode image-to-video --video-provider quickainew --video-resolution 720p
```

不应要求用户直接编辑复杂 JSON 才能选择模式；Codex 可以通过 CLI 初始化后再填充剧本和提示词。

### 5.4 运行层

`generate_videos()` 应按以下顺序工作：

1. 读取并验证项目模式、提供方、分辨率、比例、时长。
2. 根据 `video_provider` 选择 QuickAI 或 QuickAI New 客户端。
3. T2V 模式构造无图片字段的请求；I2V 模式解析限制数量内的参考图。
4. 把 `mode`、`provider`、`model`、`seconds`、`resolution`、`aspect_ratio`、参考图摘要写入签名和 state，但绝不写入 Key。
5. 创建前先写入预算尝试和 `submission_unknown` 保护状态。
6. 创建成功后只持久化 task ID，轮询恢复不得重复创建。
7. 下载后执行 MP4、尺寸、时长、编码和方向检查。上游实际输出与请求不同必须在 QA 中明确标记，不要伪装成已按请求生成。

`doctor()` 应按已配置的 Key 和默认提供方执行检查：

- QuickAI Key 存在才检查 QuickAI。
- QuickAI New Key 存在才检查 QuickAI New。
- 缺少未使用提供方不应把 doctor 判为失败。
- 当前选定视频提供方的模型不存在时才判定视频能力不可用。

### 5.5 Seedance 预留

当前只接入 Grok 模型，不要为了未来 Seedance 提前改动 Sub2API/NewApi，也不要把 Seedance 字段混进 Grok 请求。保留以下扩展边界即可：

- `video_provider` 使用稳定枚举/适配器选择，而不是大量 `if model.startswith(...)`。
- 将来 Seedance 单独实现 `SeedanceVideoClient` 和参数归一化（比例、分辨率、时长、参考视频/音频）。
- Grok 项目合同中的 `video_mode`、`video_provider`、`video_resolution` 不要绑定某个模型名称。
- 现阶段模型默认仍为 `grok-imagine-video-1.5`，不要自动改成 preview 模型。

## 6. 安全与计费要求

- 用户提供的真实 Key 只允许通过进程 stdin、Windows DPAPI 或运行时环境变量传递。
- 不能把 Key 放入 Git、文档、测试 fixture、项目 JSON、state.json、events.jsonl、命令行参数或终端输出。
- 视频和图片创建都视为计费请求。创建 POST 不自动重试。
- 对超时、连接中断等无法确认是否已创建的情况，必须保持 `submission_unknown`，要求用户确认后才允许 `--retry-failed --retry-reason`。
- 真实上游测试优先用 1 个镜头、1 秒或最小允许时长、480p；测试前在对话中说明会产生上游消耗。
- API Key 若曾在聊天中暴露，发布前提醒用户轮换；文档和 Git 中不得重新显示它。

## 7. 测试清单

### 7.1 单元/集成测试

扩展 `tests/test_grok_video_studio.py` 的假服务器，至少覆盖：

1. QuickAI T2V：POST `/v1/videos/generations`，断言 JSON 包含 `seconds`、`resolution=480p`、`aspect_ratio`，且没有图片字段。
2. QuickAI I2V：单参考图使用 `input_reference`；多参考图使用 `reference_images`，不发送本机路径。
3. QuickAI New T2V：multipart `/v1/videos` 无 `input_reference`，但包含 `resolution` 和 `aspect_ratio`。
4. QuickAI New I2V：重复 `input_reference` 文件字段，包含 resolution/aspect_ratio。
5. 项目模式切换：同一个 CLI 能分别生成 T2V 与 I2V，且签名包含 mode/provider。
6. 单 Key 配置：只有 QuickAI Key 时 `configure`、`doctor` 和 T2V 可以工作；只有 QuickAI New Key 时 New API 视频可以工作；旧双 Key 配置仍兼容。
7. 旧项目迁移：没有新增字段的 project.json 仍按旧 I2V 行为运行。
8. resolution 限制：480p/720p/1080p 接受，其他值在本地验证阶段拒绝。
9. 任务恢复：已有 task ID 的 `resume` 不再次 POST 创建。
10. 下载结果执行 MP4 和 ffprobe 检查，错误响应不误判为成功。

### 7.2 推荐命令

在目标仓库根目录执行：

```powershell
python -m unittest discover -s tests -v
python -m py_compile grok-video-studio/scripts/*.py
python C:\Users\FBX\.codex\skills\.system\skill-creator\scripts\quick_validate.py grok-video-studio
```

若系统临时目录不可用，先设置一个可写的临时目录再测试，例如：

```powershell
$env:TEMP = "E:\TEMP"
$env:TMP = "E:\TEMP"
```

### 7.3 真实上游最小测试

不要把真实 Key 写入命令历史。使用受控 stdin：

```powershell
python grok-video-studio/scripts/grok_video_studio.py configure --credentials-stdin --skip-test
```

stdin JSON 可以只提供 QuickAI Key，也可以提供两个 Key。真实测试顺序：

1. `doctor` 检查模型和 ffmpeg/ffprobe。
2. 创建一个 `text-to-video` 项目，1 个镜头、1 秒、480p。
3. `preflight`、`generate-videos`、`status`、`qa`。
4. 确认请求日志/状态中的 provider、mode、resolution 与实际请求一致。
5. 再做一次 I2V 测试；如果上游返回 400，保存脱敏后的状态和 HTTP 错误，不要立即重复扣费。

真实测试只使用用户在当前对话提供的 Key；最终报告只说成功/失败和脱敏请求信息，绝不显示 Key。

## 8. 文档更新清单

实现后必须同步更新：

- `grok-video-studio/SKILL.md`：改为说明双模式、双提供方、单 Key/双 Key 安装方式和明确的安全流程。
- `grok-video-studio/references/api-contracts.md`：分别记录 QuickAI JSON 与 QuickAI New multipart，不要再写成“QuickAI 只负责图片”。
- `grok-video-studio/references/project-schema.md`：增加 mode/provider/resolution/aspect_ratio 和旧项目兼容规则。
- `grok-video-studio/references/error-matrix.md`：补充未配置提供方、模式/提供方不匹配、resolution 被上游降级、I2V 字段不兼容等错误。
- `grok-video-studio/references/workflow-catalog.md`：明确 `text-to-video` 是无参考图的脚本生视频流程。
- `grok-video-studio/agents/openai.yaml`：默认提示词要告诉 Codex 先确认 T2V/I2V，再选择可用提供方和 Key。

不要在技能仓库新增一份与 `SKILL.md` 重复的 README/安装说明；安装话术应在发布交接中提供。

## 9. Git 与 Gitee 发布

发布前检查：

```powershell
git status --short
git diff --check
git diff -- grok-video-studio tests
```

确认没有 Key、临时文件、测试输出和用户数据后：

```powershell
git fetch origin --tags
git add grok-video-studio tests HANDOFF-T2V-I2V-PROVIDER-ADAPTER.zh-CN.md
git commit -m "feat: add dual-provider text-to-video workflow"
```

Gitee 远程地址必须先验证：

```powershell
git ls-remote https://gitee.com/1582345746/GrokVideoSkill.git
```

若地址有效且用户确认使用该仓库：

```powershell
git remote add gitee https://gitee.com/1582345746/GrokVideoSkill.git
git push gitee main
```

不要把真实 Key 放在 commit message、tag、Git remote URL 或发布输出中。若需要版本号，建议把本轮功能版本升为 `1.3.0`，并同步 CLI 的 `SKILL_VERSION`、`SKILL.md` 和发布说明。

## 10. 给其他用户的安装与使用话术

发布完成后，可以把下面这段直接发给其他用户。不要把测试 Key 放进去，让每个用户提供自己的 Key：

```text
请安装 Grok Video Studio 技能：

仓库：<发布后的 Gitee 仓库地址>

安装后，请把你自己的上游 Key 发给 Codex，并说明用途：
1. 只做 QuickAI 文生视频：提供 QuickAI 的视频 Key，并说明上游地址（默认 https://quickai.hn.takin.cc）。
2. 需要图生视频，或同时使用 QuickAI New：再提供 QuickAI New Key，并说明上游地址（默认 https://quickainew.hn.takin.cc）。

不要把 Key 写进项目文件、命令行参数或代码。Codex 会通过受控输入保存凭据，并先运行 doctor 检查模型和 ffmpeg。

使用时直接告诉 Codex：
- “写一个 15 秒以内的剧本并做文生视频”；或
- “用这张图做图生视频”；

Codex 会先确认文生/图生模式、模型、比例、分辨率和时长，再生成项目和提示词。单个镜头最长 15 秒；每次真实生成都可能产生上游费用，生成前请先确认预算。
```

## 11. 交接结论

当前仓库已经具备稳定的项目、预算、重试保护、任务恢复、下载和媒体 QA 基础，但还没有完成本轮双提供方双模式改造。下一会话应先将工作区根目录切换到本仓库，读取本文件和 `skill-creator` 的 `SKILL.md`，然后按“客户端层 → 配置层 → 项目合同 → 运行层 → 测试 → 真实最小请求 → 文档 → Gitee 发布”的顺序实施。

最重要的边界：

- 不修改 `E:\MyFiles\Sub2ApiTransfer`。
- 不修改 `E:\MyFiles\NewApiTransfer`。
- 可以读取两者源码用于协议核对。
- `E:\MyFiles\QuickAiCanvas` 仅作已实现主站视频协议和错误处理参考；本技能开发不要改变 Canvas 代码。
- 任何真实 Key 都不进入仓库、日志、测试 fixture 或本交接文档。
