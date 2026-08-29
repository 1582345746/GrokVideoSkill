# 模块使用话术

本文是可以直接复制给 Codex 的模块级标准话术。所有示例都使用占位符，不包含任何真实 Key。默认原则是“先规划和预检，用户批准后才创建可能收费的上游任务”。

## 先说清楚四个产品路线

| 需求 | 使用入口 | 是否需要 QuickAI 生图 Key |
| --- | --- | --- |
| 没有参考图，直接描述画面 | `init --mode text-to-video` | 否 |
| 已有一张图片，直接让图片动起来 | `init --mode image-to-video` + `single-image-animation` | 否 |
| 先按角色/场景生成关键帧，再做动画 | `init --mode image-to-video` + `character-consistent-story` 或自定义工作流 | 是 |
| 多集共享人物、地点和剧情连续性 | `series-init` | 取决于每集是 T2V 还是 I2V |
| 当前新闻、来源和事实主张可追溯 | `news-init` | 取决于分镜路线 |

视频参考 MP4、视频编辑/延展、预设音色参考和音频文件参考目前不是 I2V 的扩展字段。请求这些能力时应让预检阻断；未来分别使用 `video-edit`、`video-extend`、`preset_voice_reference` 和 `audio_file_reference` 路线。

## 上游选择话术

### 默认 QuickAI

```text
使用 $grok-video-studio 制作 <主题>。生视频上游不特别指定，按默认规则优先 QuickAI；只有明确属于安全失败的情况才允许在总尝试次数内使用 QuickAI New。先展示路线、分镜、最终提示词、请求数量、预算和预检结果，不要立即创建任务。
```

### 固定 QuickAI New

```text
使用 $grok-video-studio 制作 <主题>，生视频选择 QuickAI New。
文生视频和图生视频都必须固定直连 QuickAI New，不先调用 QuickAI，也不自动切换其他视频上游。项目中保存 video_provider=quickainew、video_provider_policy=fixed。先预检并等待我批准，再创建任务。
```

### 固定 QuickAI

```text
使用 $grok-video-studio 制作 <主题>，生视频固定选择 QuickAI。
文生视频和图生视频都不要回退到 QuickAI New。失败时保留 state.json、错误分类和任务状态，除非我明确批准新的重试，否则不要创建替代任务。
```

### 明确要求自动备用

```text
使用 $grok-video-studio 制作 <主题>，允许 QuickAI 优先、QuickAI New 安全备用。
只有在上游明确拒绝且没有任务 ID、或已知任务进入终态失败时才允许切换；网络超时、5xx、提交结果不明和轮询超时都不能自动创建第二个任务。备用调用计入总尝试次数。
```

## 安装、升级和诊断

### 新用户安装

```text
请从 main 分支安装 Grok Video Studio：
仓库：https://github.com/1582345746/GrokVideoSkill.git

请使用 upstream-dialogue 档位。通过受管进程标准输入配置 QuickAI 生图 Key、QuickAI 视频 Key（T2V/I2V）、QuickAI New 视频 Key；Windows 使用 DPAPI 保存。Key 不得出现在命令行、项目文件、日志、源码或终端输出中。
安装后运行 version、install.ps1 -Check、doctor、capabilities 和 install-plan --profile upstream-dialogue。不要发起付费生成，不要下载或启动 Voicebox、CosyVoice、MuseTalk、Docker 镜像或模型。
```

只有静音或保留源音频时才使用：

```text
安装 Grok Video Studio 的 basic 档位。只保留上游/源音频或明确交付静音版本，不安装任何本地 AI 组件；完成后做只读诊断，不进行付费生成。
```

### 旧版升级

```text
请将现有 Grok Video Studio 更新到仓库 main 最新提交。
先检查仓库是否在 main、工作区是否干净；有未提交修改时只报告，不覆盖。工作区干净后执行 install.ps1 -Repair -Force，并保留 DPAPI 凭据、安装档位、项目、角色母版、可选组件源码和模型缓存，不要求重新输入 Key，不重新下载完整模型。
完成后运行 version、install.ps1 -Check、doctor、capabilities 和离线测试；不要发起付费生成。
```

### 只读诊断

```text
对当前 Grok Video Studio 做只读诊断：运行 version、capabilities、install-plan --profile upstream-dialogue、doctor 和 install.ps1 -Check。报告版本、默认视频上游、QuickAI/QuickAI New 的 T2V/I2V 能力、三个凭据职责、FFmpeg、可选组件状态和缺失依赖。不要输出 Key，不创建项目，不发起生成请求。
```

## 产品路线话术

### 文生视频 T2V

```text
使用 $grok-video-studio 做一个 <时长> 秒、<比例> 的文生视频，主题是 <主题>。
不要参考图、不要生图、不要创建角色母版。每个镜头只安排一个连续动作，并加入景别、镜头运动、环境运动和收尾姿态。禁止字幕、画面文字、Logo、水印和短视频 UI。先展示剧本、分镜、提示词字节数、请求数和预算，等我回复“批准生成”后再生成并运行 QA。
```

### 现成图片直接 I2V

```text
使用 $grok-video-studio 将 <图片路径> 做成 <时长> 秒图生视频。
不要重新生图；把图片作为唯一当前镜头参考，保持人物/产品、服装、构图、背景和文字位置，只增加 <动作>。先复制并预检图片格式、尺寸、比例、清晰度和主体完整性，展示一次视频请求和预算，批准后生成。完成后对比输入图与首帧、关键帧、尾帧，并报告最终上游和 QA。
```

### 先生关键帧再 I2V

```text
使用 $grok-video-studio 为 <主题> 做 <镜头数> 个图生视频镜头。
先用 QuickAI 生图，每个镜头单独生成关键帧；不要把多视图角色母版直接发送给视频模型。每张关键帧生成后暂停，展示图片、哈希、提示词版本和费用，等我接受后锁定。全部关键帧接受后，再按 <QuickAI/QuickAI New/自动备用> 逐镜头生成视频。每个视频请求只发送当前镜头关键帧。
```

### 角色母版

```text
使用 $grok-video-studio 建立一个原创角色母版：<角色身份>。
先生成一张包含同一角色正面、侧面和全身视图的单张母版，锁定脸型、发型、服装、道具和颜色。通过人工检查后，再为每个镜头派生独立关键帧；视频模型只接收当前镜头关键帧，不接收多视图母版。严格一致性要求下不要改用纯 T2V。
```

### 原生对白、声音和口型

```text
使用 $grok-video-studio 制作带原生对白的视频。audio.mode=native-dialogue、generate_audio=true、subtitle_source=none。
把角色、对白、情绪、环境音和音效写进视频提示词；不要自动生成本地字幕或配音。生成后检查是否有音频轨、对白是否可听、音画和口型是否基本同步，并打开首帧/关键帧/尾帧检查模型是否把对白文字烧进画面。原生对白逐字内容、音色、口型和是否内嵌字幕都不能承诺完全准确。
```

### 精确本地配音

```text
对已经验收的干净视频使用 local-voice。先运行 install-plan --profile precise-voice、components-doctor 和 voice-doctor，只报告缺失依赖、源码提交、模型 revision、许可证、GPU、端口和磁盘需求。得到批准后再安装/启动组件；先 voice-list，再为每个实际说话角色 voice-audition，逐个等待我接受或拒绝，最后 dialogue-render。保留 deliverables/final.mp4，不覆盖干净母版。
```

### MuseTalk 口型

```text
对已经完成精确配音的原创角色视频使用 local-lipsync。先展示 lip-sync 安装和模型计划，不自动下载。确认 CosyVoice 音频和对白通过听审后，分阶段启动 CosyVoice 与 MuseTalk；8 GB 显存只运行一个阶段。口型输出必须是独立衍生文件，完成后检查嘴部同步、闪烁、脸部畸变和音频一致性。
```

### 字幕

```text
根据项目中已经批准的对白/旁白/时间轴生成确定性字幕。先导出 SRT 供我审校，再按 <clean/cinematic/news> 样式烧录到独立副本；不要覆盖 deliverables/final.mp4，也不要为改字幕重新生成视频。native-dialogue 生成的源画面必须先确认没有模型内嵌字幕。
```

### 连续剧

```text
使用 $grok-video-studio 规划 <集数> 集连续短剧，每集 <时长> 秒。
先填写季主题、冲突升级、中点、高潮、结尾钩子、角色/地点/道具和每集动态分镜；每集覆盖 establishing、动作、反应、非对白视觉和 ending_hook。只预检并批准 ep-001 后生成；生成后人工验收并用实际结尾运行 series-accept，下一集只能继承验收后的连续性状态。
```

### 新闻视频

```text
使用 $grok-video-studio 制作最近 <时间范围> 的热点新闻视频。必须联网检索，至少使用两个独立可靠来源；先写 news.json，保存来源 URL、发布时间、访问时间、事实主张、冲突项、claim_id、旁白和镜头映射。先 news-validate，不通过就不要生成。AI 画面标为示意，不得冒充现场素材。
```

## 内部预设话术

这些 ID 是可编辑的工作流预设，不是新的产品路线。可用 ID 以 `capabilities` 输出为准。

```text
使用 $grok-video-studio 的 <预设 ID> 预设制作 <主题>。先运行 describe <预设 ID>，根据它的提问补齐输入，再生成完整故事、镜头、提示词和预检；等待批准后才付费生成。完成后按该预设的重点运行人工 QA。
```

当前预设重点：

| ID | 适合 | 特别检查 |
| --- | --- | --- |
| `general-video` | 目标尚未明确的通用项目 | 先在 T2V/I2V 之间做路线选择 |
| `text-to-video` | 无参考图、简洁动作 | T2V 不发送任何参考图 |
| `single-image-animation` | 用户已有一张图 | 不生图；只发送该图片 |
| `character-consistent-story` | 角色母版和多镜头一致性 | 母版只用于派生当前镜头关键帧 |
| `short-drama` | 单集人物短剧 | 动作、反应和连续性，不全是正面对白 |
| `product-ad` | 产品展示和广告 | 产品结构、包装文字、商标位置 |
| `dance-performance` | 舞蹈、唱跳、表演 | 全身构图、四肢和动作连续性 |
| `comedy-action` | 表情反应和轻喜剧 | 节拍、停顿、禁止短视频 UI |
| `scene-animation` | 插画、建筑、风景 | 分层运动，禁止整体画面漂移 |
| `news-video` | 当前新闻 | 来源、claim 映射和示意标记 |

## 恢复、重试和交付

```text
检查 <项目目录> 的 project.json、state.json、status 和 logs。列出每个镜头的图片/视频状态、request_id、task_id、提供方、尝试历史、提示词版本和已下载文件。
已有 task_id 的任务只允许 resume、轮询和下载；submission_unknown、网络超时、5xx 或轮询超时不要自动重新创建。创建失败且没有 task_id 时，先说明重复计费风险，只有我明确给出重试原因才创建新任务。达到三次总尝试上限后停止，并给出恢复命令。
```

```text
对 <项目目录> 运行最终 QA。检查容器、H.264/yuv420p、分辨率、方向、帧率、时长、音频轨、音量、静音占比、黑帧、冻结、字幕流、Logo、水印和 UI。打开每个镜头首帧、关键帧和尾帧，人工记录人物一致性、场景连续性、动作连贯性、口型和意外文字。技术 QA 通过不等于人工验收通过。
```

## 二次开发话术

```text
我要在 Grok Video Studio 上新增一个工作流：<名称和目标>。
先阅读 grok-video-studio/references/project-schema.md、workflow-catalog.md、api-contracts.md、error-matrix.md 和 tests/test_grok_video_studio.py。优先只修改 assets/workflow-templates/<id>.json 和文档；如果需要新字段，先给出向后兼容的 schema 变更、预检规则、state 记录、费用门禁和测试计划。不要把 Key 写进仓库，不要把 MP4/WAV 塞进普通 input_reference，不要破坏现有预设 ID。完成后运行完整离线测试、py_compile、git diff --check，并说明是否需要付费验收。
```
