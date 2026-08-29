# Grok Video Studio

[![Grok Video Studio CI](https://github.com/1582345746/GrokVideoSkill/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/1582345746/GrokVideoSkill/actions/workflows/ci.yml)

面向 Codex 的可恢复 AI 视频制作技能。当前版本为 `v2.0.1 upstream-first`，发布分支为 `main`。

它把创意合同、分镜、上游请求、断点恢复、连续性、可选音频和交付 QA 保存为项目文件。规划、预检、状态查询、下载、FFmpeg 和 QA 都不创建新的上游生成任务；只有批准后的生图/生视频/试听请求可能收费。

## 产品路线

| 路线 | 适用场景 | 默认生成链路 |
| --- | --- | --- |
| 文生视频 | 单条视频、多镜头故事、无参考图创作 | QuickAI 文生视频 |
| 图生视频 | 用户现成图片直接动画，或关键帧/角色母版动画 | 现成图片直接动画只调用视频上游；关键帧路线才调用 QuickAI 生图，再调用视频上游 |
| 连续剧 | 多集共享角色、世界观和剧情连续性 | 每集使用文生视频或图生视频，逐集审批与验收 |
| 新闻视频 | 当前热点、来源核验、旁白型资讯视频 | Codex 联网检索并建立证据合同，再进入标准视频链路 |

用户现成图片的动画属于图生视频，不是第五条产品路线。

文生视频和图生视频在未指定上游时默认优先 QuickAI，并使用 `automatic` 策略只对安全分类失败尝试 QuickAI New。用户明确说“生视频选择 QuickAI New”时，必须固定直连 QuickAI New，不先调用 QuickAI；明确选择 QuickAI 时同理。默认总尝试次数为 3 次，备用也计入次数，提交结果不明时不自动重建。

## 核心能力

- 先展示剧本、分镜、提示词、请求数量和预算，用户批准后才付费生成。
- 每次创建任务前写入 `state.json`，支持轮询恢复、断点续跑和指定镜头重试。
- 默认禁止意外字幕、点赞、评论、弹幕、按钮、Logo、水印和贴纸；原生对白仍可能被上游烧进画面，必须人工检查。
- 图生视频支持两种不同链路：用户图片直接动画，或角色母版/镜头提示词生成关键帧后再动画。
- 连续剧保存整季设定、逐集审批状态和上一集实际结尾。
- 新闻视频保存来源、发布时间、事实主张和逐镜头引用关系。
- 保留 `deliverables/final.mp4` 干净母版，再通过 FFmpeg 输出对白版、字幕版、混音版和封面等独立衍生物。
- QA 检查格式、时长、方向、黑帧、冻结、音量和静音占比，并要求人工查看每个镜头的审阅帧。

## 当前模块与预设

| 模块 | 当前效果 | 边界 |
| --- | --- | --- |
| T2V / I2V | QuickAI 与 QuickAI New 均可生成和恢复任务；QuickAI 还负责 T2I | 单镜头 1-15 秒，长片通过多镜头拼接 |
| 角色与关键帧 | 角色母版、用户参考图、逐镜头关键帧和哈希锁定 | 严格一致性仍需要人工审核 |
| 原生对白 | 上游尝试生成对白、声音和口型 | 逐字、音色、口型和内嵌字幕不确定 |
| 精确字幕/配音/口型 | FFmpeg 字幕，Voicebox/CosyVoice 配音，MuseTalk 口型 | 本地 AI 组件可选，不随基础安装下载 |
| 连续剧 | 季设定、逐集审批、实际结尾连续性、下一集恢复 | 不会未经批准批量生成整季 |
| 新闻视频 | 当前来源、事实主张和逐镜头证据映射 | 需要联网研究，AI 画面不能冒充现场 |
| 后期与 QA | 拼接、混音、字幕副本、封面、媒体和审阅帧检查 | 技术 QA 不能替代人工观片/听审 |

内置可编辑预设包括 `general-video`、`text-to-video`、`single-image-animation`、`character-consistent-story`、`short-drama`、`product-ad`、`dance-performance`、`comedy-action`、`scene-animation` 和 `news-video`。运行 `capabilities` 查看清单，运行 `describe <id>` 查看该预设的提问和提示词指导。预设只改变规划方式，不会虚构新的上游能力。

## 推荐安装方式

推荐直接让 Codex 从仓库 `main` 安装或更新。仓库根目录的 `install.ps1` 是受保护的事务式安装器；它会先做版本和依赖检查，升级时保留 DPAPI 凭据、项目、可选组件源码和模型缓存。

复制即用的安装、升级、上游选择和每个模块的话术见 [用户安装与使用话术](docs/user-installation-and-usage-prompts.zh-CN.md) 与 [模块使用话术](docs/module-usage-prompts.zh-CN.md)。

### 新用户安装话术（无付费测试）

把下面内容连同自己的 Key 发给 Codex。不要把真实 Key 提交到 GitHub、项目 JSON 或文档中。

```text
请从 main 分支安装 Grok Video Studio 技能：
仓库：https://github.com/1582345746/GrokVideoSkill.git

QuickAI 生图 Key：<QUICKAI_IMAGE_KEY>
QuickAI 视频 Key（T2V/I2V）：<QUICKAI_VIDEO_KEY>
QuickAI New 视频 Key：<QUICKAINEW_VIDEO_KEY>

要求：
1. 使用仓库根目录 install.ps1 安装 `upstream-dialogue` 档位；需要静音或保留源音频时才显式选择 `basic`。
2. 通过标准输入把三个 Key 配置给技能；Windows 使用 DPAPI 保存。
3. 不得把 Key 写入源码、项目文件、临时文件、命令行参数或终端输出。
4. 安装后运行 version、install.ps1 -Check 和 doctor。
5. `doctor` 只做凭据、模型列表和媒体依赖诊断，不发起任何付费生图或生视频测试。
6. 不要安装 CosyVoice、MuseTalk、Docker 模型或其他可选组件，除非我之后明确批准。
```

### 已安装旧版时的升级话术

```text
请把这台机器已经安装过的 Grok Video Studio 更新到仓库 main 分支最新版：
仓库：https://github.com/1582345746/GrokVideoSkill.git

先检查现有仓库和安装目录。工作区干净时使用 fast-forward 拉取 main；如果发现本地未提交修改，先报告，不要覆盖。然后使用 install.ps1 -Repair -Force 更新技能。

要求保留已有 Windows DPAPI 凭据、安装档位、项目、CosyVoice/MuseTalk 源码和模型，不要要求我重新输入 Key，不要重新下载已有模型。完成后运行 version、install.ps1 -Check、doctor 和 capabilities，并报告版本、凭据角色、FFmpeg、可选组件状态；不要发起付费生成测试。
```

手动安装只需：

```powershell
git clone --branch main --single-branch https://github.com/1582345746/GrokVideoSkill.git
cd GrokVideoSkill
.\install.ps1 -Force -InstallProfile upstream-dialogue
```

## 凭据职责

| 凭据 | 用途 |
| --- | --- |
| QuickAI 生图 Key | T2I、角色母版和需要先生关键帧的 I2V |
| QuickAI 视频 Key（T2V/I2V） | QuickAI 的 T2V 与 I2V 视频请求 |
| QuickAI New 视频 Key | QuickAI New 的 T2V 与 I2V；自动备用或显式固定选择 |

Windows 凭据通过 DPAPI 保存在当前用户的本地应用数据目录，不进入技能目录。旧版 `quickai_key` 和 `quickainew_key` 配置仍可迁移，但新安装应按职责分开配置。

## 安装档位

| 档位 | 能力 | 本地要求 |
| --- | --- | --- |
| `basic` | 生成、恢复、拼接、QA，保留上游或源音频 | FFmpeg |
| `upstream-dialogue` | 让视频上游生成对白和口型 | FFmpeg，无本地 AI |
| `precise-subtitles` | 从已批准剧本/时间轴导出 SRT 并烧录字幕副本 | FFmpeg |
| `precise-voice` | CosyVoice 精确配音、时间轴、混音和字幕 | Docker、NVIDIA GPU、约 10 GB 模型空间 |
| `lip-sync` | 精确配音并使用 MuseTalk 修正人物口型 | Docker、NVIDIA GPU、约 17 GB 模型空间 |

多角色预设音色采用同一条代码主线中的 Voicebox 路线，不另建第二套技能。先运行无副作用的 `voicebox-setup-plan`，由 Codex 报告源码提交、隔离 Python、GPU、缓存、固定模型 revision、许可证和预计下载量；用户批准后再由 Codex 分阶段安装、启动和试听。基础技能更新不捆绑模型权重，用户无需逐项手动安装。

`precise-subtitles` 根据项目中的对白、旁白或字幕时间轴生成确定性字幕，不等同于对任意视频执行语音识别。可选组件必须先展示安装计划，并在用户明确批准后才下载或启动；服务只监听 `127.0.0.1`。

## 上游能力合同

运行 `python scripts/grok_video_studio.py capabilities` 可以看到当前安装的真实合同。QuickAI 支持 T2I、T2V、I2V；QuickAI New 支持 T2V、I2V，不提供 T2I。两者当前均不支持视频参考、视频编辑/延展、预设音色参考或音频文件参考。QuickAI 的音频是模型默认能力，不能假设一定生成；QuickAI New 使用显式 `generate_audio` 字段。MP4/WAV 不能塞进普通 `input_reference`，预检会阻断并指向未来独立路线。

QuickAI 网关当前拒绝 PNG data-URI 图生视频请求；技能会在预检检查参考图格式、尺寸、比例和清晰度，并仅在请求体内将有效静帧中心裁剪为临时 JPEG，原图保持不变。QuickAI New 使用原始图片 multipart 上传。这个兼容处理只影响请求体，不改变项目中的原图。

## 音频与字幕

音频路线和字幕来源是两个独立选择。

| 音频模式 | 说明 |
| --- | --- |
| `preserve` | 保留上游或用户源视频音频 |
| `mute` | 明确交付静音版本 |
| `native-dialogue` | 上游生成对白、声音和口型，速度快但逐字内容与内嵌字幕不可控 |
| `local-voice` | 按角色选择 Voicebox/Qwen 或 CosyVoice 已批准音色，FFmpeg 对齐、混音和归一化 |
| `local-lipsync` | 在精确对白基础上调用 MuseTalk 做口型同步 |

字幕来源：

- `upstream`：保留上游或源画面中已有字幕，不生成本地 SRT。
- `project`：根据批准的对白、旁白或字幕提示生成可审校 SRT，可烧录为独立副本。
- `none`：不交付字幕。

始终保留 `deliverables/final.mp4` 干净母版。字幕样式不满意时重新烧录即可，不需要重新付费生成视频。上游原生对白可能自行把字幕画进视频，烧录本地字幕前必须先人工确认源画面干净。

多角色音色必须先试听后使用：`voice-list` 列出预设，`voice-audition` 只生成候选 WAV，用户听审后运行 `voice-approve` 或 `voice-reject`，连续剧再运行 `series-voice-sync`。未批准音色、无授权参考音频和不同角色误用同一音色都会阻断预检；所有候选与模型 revision、seed、授权、音频哈希和技术 QA 记录在项目级 `voice-catalog.json`。

## 四个最常用话术

### 文生视频

```text
使用 $grok-video-studio 做一个 30 秒 9:16 文生视频。先给我完整故事、人物设定、分镜提示词、请求数量和预算，不要立即生成。画面默认禁止字幕、点赞、评论、弹幕、按钮、Logo 和水印。我确认后再生成，完成后运行 QA 并逐镜头检查审阅帧。
```

### 图生视频

```text
使用 $grok-video-studio 把我提供的图片制作成 8 秒 9:16 图生视频。不要重新生图，锁定人物身份、服装、构图和背景，只增加自然眨眼、呼吸和轻微转头。先预检并等我批准，再按默认规则优先调用 QuickAI；只有安全分类失败才切换 QuickAI New。完成后交付原片、最终提供方、尝试历史和 QA 报告。
```

### 连续剧

```text
使用 $grok-video-studio 规划一部 20 集连续短剧，每集 60-90 秒。先创建系列项目，写完整季大纲、角色设定、每集剧情和分镜提示词供我审核，不要生成。审核后只预检并生成第一集；以后我说“生成下一集”时，先读取上一集验收后的实际结尾和连续性状态，再等待我批准。
```

连续剧进入配音选角阶段时可继续发送：

```text
使用 $grok-video-studio 继续这个连续剧项目。先读取 series.json、voice-catalog.json 和第 1 集 project.json，只处理本集实际说话角色。先运行 voicebox-setup-plan 和 voice-doctor，不下载、不启动、不生成正片；报告固定模型、许可证、下载量和本机状态。得到我批准后，为每个角色分别生成试听并把 WAV 给我听，每个候选都等我接受或拒绝。全部通过后运行 series-voice-sync 和 series-preflight --episode ep-001；不要自动生成图片、视频、整集对白或启动 MuseTalk。
```

### 新闻视频

```text
使用 $grok-video-studio 检索最近 24 小时热点新闻，选择一个至少有两个独立可靠来源支持的主题。先展示来源链接、发布时间、事实主张、冲突项、旁白和分镜，不要生成。通过 news-validate 且我确认后，再制作 60 秒新闻视频；AI 画面必须标为示意，不能冒充现场素材。
```

## 测试与使用文档

逐模块标准话术、费用边界、操作顺序和验收标准见：

- [完整测试与使用指南](docs/testing-and-usage-guide.zh-CN.md)
- [用户安装与使用话术](docs/user-installation-and-usage-prompts.zh-CN.md)
- [模块使用话术](docs/module-usage-prompts.zh-CN.md)
- [二次开发与自定义工作流](docs/custom-workflow-development.zh-CN.md)
- [v2.0 upstream-first 迁移说明](docs/v2.0-upstream-first-migration.zh-CN.md)
- [v2.0.1 付费生成验收记录](docs/paid-acceptance-2026-08-30.md)
- [v1.8.0 开发交接文档](HANDOFF-GROK-VIDEO-STUDIO-v1.8.0.zh-CN.md)
- [多角色音频模块开发交接清单](docs/voice-module-development-handoff.zh-CN.md)
- [多角色音频开发清单与当前状态](docs/multi-tts-implementation-status.zh-CN.md)
- [产品化验收清单](docs/productization-checklist.md)
- [技能运行说明](grok-video-studio/SKILL.md)
- [项目字段规范](grok-video-studio/references/project-schema.md)
- [连续剧字段规范](grok-video-studio/references/series-schema.md)
- [新闻证据规范](grok-video-studio/references/news-schema.md)
- [对白和本地组件](grok-video-studio/references/dialogue-and-components.md)
- [错误处理矩阵](grok-video-studio/references/error-matrix.md)

## 二次开发

只新增一种内容组织方式时，优先增加 `grok-video-studio/assets/workflow-templates/<id>.json`，无需复制 CLI。需要新增字段、上游或媒体路线时，再同步修改 schema、provider adapter、能力报告、错误矩阵、迁移规则和测试。稳定 ID、付费门禁、三次总尝试、`submission_unknown` 不重建、已知 task ID 只恢复，以及 T2V/I2V 的参考图隔离都必须保留。完整步骤见 [二次开发与自定义工作流](docs/custom-workflow-development.zh-CN.md)。

## 产品边界

- 图像和视频创建请求可能收费；预检、状态查询、下载、FFmpeg 处理和本地 QA 不会创建新的上游生成任务。
- 用户已有图片直接做 I2V 不需要 QuickAI 生图 Key；只有角色母版或镜头关键帧路线需要生图 Key。
- 提示词硬上限为 4096 UTF-8 字节，建议把最终合成提示词控制在 3800 字节以内。预检会同时显示完整、精简、最小版本、字节数、剩余空间和压缩建议，绝不静默截断。
- 文生视频的人物一致性是文本约束下的尽力而为；严格一致性应使用角色母版、逐镜头关键帧和图生视频。
- `native-dialogue` 的声音、逐字内容、内嵌字幕和口型是生成式结果，不能承诺完全准确。
- Voicebox/Qwen、CosyVoice、VoxCPM 和 MuseTalk 都是可选本地组件，不随基础技能静默安装；VoxCPM 当前仍是实验路线。
- 精确字幕来自批准文本与时间轴；当前没有把任意带声音视频自动转写成精确字幕的 ASR 产品入口。
- 技术 QA 不能代替人工观片和听审。人物漂移、异常肢体、意外界面元素、错误对白和不自然口型必须人工确认。
- 新闻事实必须来自当前网络来源；AI 生成画面不得冒充真实新闻现场。
