# Grok Video Studio

[![Grok Video Studio CI](https://github.com/1582345746/GrokVideoSkill/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/1582345746/GrokVideoSkill/actions/workflows/ci.yml)

面向 Codex 的可恢复 AI 视频制作技能。当前版本为 `v1.8.0`，仓库只使用 `main` 分支。

它把剧本、分镜、付费请求、断点恢复、连续性、字幕、对白、口型和交付 QA 放进同一个可审计项目，而不是让 Codex 临时拼接若干不可恢复的生成命令。

## 产品路线

| 路线 | 适用场景 | 默认生成链路 |
| --- | --- | --- |
| 文生视频 | 单条视频、多镜头故事、无参考图创作 | QuickAI 文生视频 |
| 图生视频 | 用户现成图片动画、产品图、角色母版和镜头关键帧 | QuickAI 生图 + QuickAI New 图生视频 |
| 连续剧 | 多集共享角色、世界观和剧情连续性 | 每集使用文生视频或图生视频，逐集审批与验收 |
| 新闻视频 | 当前热点、来源核验、旁白型资讯视频 | Codex 联网检索并建立证据合同，再进入标准视频链路 |

用户现成图片的动画属于图生视频，不是第五条产品路线。

## 核心能力

- 先展示剧本、分镜、提示词、请求数量和预算，用户批准后才付费生成。
- 每次创建任务前写入 `state.json`，支持轮询恢复、断点续跑和指定镜头重试。
- 默认禁止意外字幕、点赞、评论、弹幕、按钮、Logo、水印和贴纸。
- 图生视频可复用单张角色母版，并为每个镜头派生关键帧。
- 连续剧保存整季设定、逐集审批状态和上一集实际结尾。
- 新闻视频保存来源、发布时间、事实主张和逐镜头引用关系。
- 生成干净母版，再通过 FFmpeg 输出对白版、字幕版、混音版和封面。
- QA 检查格式、时长、方向、黑帧、冻结、音量和静音占比，并要求人工查看每个镜头的审阅帧。

## 推荐安装方式

推荐直接让 Codex 从仓库安装或更新。独立安装器路线已经冻结，代码仍保留，但不再作为优先分发方式。

### 新用户安装话术

把下面内容连同自己的 Key 发给 Codex。不要把真实 Key 提交到 GitHub、项目 JSON 或文档中。

```text
请从 main 分支安装 Grok Video Studio 技能：
仓库：https://github.com/1582345746/GrokVideoSkill.git

QuickAI 生图 Key：<QUICKAI_IMAGE_KEY>
QuickAI 文生视频 Key：<QUICKAI_VIDEO_KEY>
QuickAI New 图生视频 Key：<QUICKAINEW_VIDEO_KEY>

要求：
1. 使用仓库根目录 install.ps1 安装 basic 档位。
2. 通过标准输入把三个 Key 配置给技能；Windows 使用 DPAPI 保存。
3. 不得把 Key 写入源码、项目文件、临时文件、命令行参数或终端输出。
4. 安装后运行 version、install.ps1 -Check 和 doctor。
5. doctor 可以访问模型列表，但不要发起任何付费生图或生视频测试。
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
.\install.ps1 -Force -InstallProfile basic
```

## 凭据职责

| 凭据 | 用途 |
| --- | --- |
| QuickAI 生图 Key | 角色母版和镜头关键帧 |
| QuickAI 文生视频 Key | 无参考图的文生视频 |
| QuickAI New 图生视频 Key | 用户图片或当前镜头关键帧的动画 |

Windows 凭据通过 DPAPI 保存在当前用户的本地应用数据目录，不进入技能目录。旧版 `quickai_key` 和 `quickainew_key` 配置仍可迁移，但新安装应按职责分开配置。

## 安装档位

| 档位 | 能力 | 本地要求 |
| --- | --- | --- |
| `basic` | 生成、恢复、拼接、QA，保留上游或源音频 | FFmpeg |
| `upstream-dialogue` | 让视频上游生成对白和口型 | FFmpeg，无本地 AI |
| `precise-subtitles` | 从已批准剧本/时间轴导出 SRT 并烧录字幕副本 | FFmpeg |
| `precise-voice` | CosyVoice 精确配音、时间轴、混音和字幕 | Docker、NVIDIA GPU、约 10 GB 模型空间 |
| `lip-sync` | 精确配音并使用 MuseTalk 修正人物口型 | Docker、NVIDIA GPU、约 17 GB 模型空间 |

`precise-subtitles` 根据项目中的对白、旁白或字幕时间轴生成确定性字幕，不等同于对任意视频执行语音识别。可选组件必须先展示安装计划，并在用户明确批准后才下载或启动；服务只监听 `127.0.0.1`。

## 音频与字幕

音频路线和字幕来源是两个独立选择。

| 音频模式 | 说明 |
| --- | --- |
| `preserve` | 保留上游或用户源视频音频 |
| `mute` | 明确交付静音版本 |
| `native-dialogue` | 上游生成对白、声音和口型，速度快但逐字内容与内嵌字幕不可控 |
| `local-voice` | CosyVoice 按批准文本生成精确对白，FFmpeg 对齐、混音和归一化 |
| `local-lipsync` | 在精确对白基础上调用 MuseTalk 做口型同步 |

字幕来源：

- `upstream`：保留上游或源画面中已有字幕，不生成本地 SRT。
- `project`：根据批准的对白、旁白或字幕提示生成可审校 SRT，可烧录为独立副本。
- `none`：不交付字幕。

始终保留 `deliverables/final.mp4` 干净母版。字幕样式不满意时重新烧录即可，不需要重新付费生成视频。上游原生对白可能自行把字幕画进视频，烧录本地字幕前必须先人工确认源画面干净。

## 四个最常用话术

### 文生视频

```text
使用 $grok-video-studio 做一个 30 秒 9:16 文生视频。先给我完整故事、人物设定、分镜提示词、请求数量和预算，不要立即生成。画面默认禁止字幕、点赞、评论、弹幕、按钮、Logo 和水印。我确认后再生成，完成后运行 QA 并逐镜头检查审阅帧。
```

### 图生视频

```text
使用 $grok-video-studio 把我提供的图片制作成 8 秒 9:16 图生视频。不要重新生图，锁定人物身份、服装、构图和背景，只增加自然眨眼、呼吸和轻微转头。先预检并等我批准，再调用 QuickAI New；完成后交付原片和 QA 报告。
```

### 连续剧

```text
使用 $grok-video-studio 规划一部 20 集连续短剧，每集 60-90 秒。先创建系列项目，写完整季大纲、角色设定、每集剧情和分镜提示词供我审核，不要生成。审核后只预检并生成第一集；以后我说“生成下一集”时，先读取上一集验收后的实际结尾和连续性状态，再等待我批准。
```

### 新闻视频

```text
使用 $grok-video-studio 检索最近 24 小时热点新闻，选择一个至少有两个独立可靠来源支持的主题。先展示来源链接、发布时间、事实主张、冲突项、旁白和分镜，不要生成。通过 news-validate 且我确认后，再制作 60 秒新闻视频；AI 画面必须标为示意，不能冒充现场素材。
```

## 测试与使用文档

逐模块标准话术、费用边界、操作顺序和验收标准见：

- [完整测试与使用指南](docs/testing-and-usage-guide.zh-CN.md)
- [产品化验收清单](docs/productization-checklist.md)
- [技能运行说明](grok-video-studio/SKILL.md)
- [项目字段规范](grok-video-studio/references/project-schema.md)
- [连续剧字段规范](grok-video-studio/references/series-schema.md)
- [新闻证据规范](grok-video-studio/references/news-schema.md)
- [对白和本地组件](grok-video-studio/references/dialogue-and-components.md)
- [错误处理矩阵](grok-video-studio/references/error-matrix.md)

## 产品边界

- 图像和视频创建请求可能收费；预检、状态查询、下载、FFmpeg 处理和本地 QA 不会创建新的上游生成任务。
- 提示词硬上限为 4096 字符，建议把最终合成提示词控制在 3800 字符以内。
- 文生视频的人物一致性是文本约束下的尽力而为；严格一致性应使用角色母版、逐镜头关键帧和图生视频。
- `native-dialogue` 的声音、逐字内容、内嵌字幕和口型是生成式结果，不能承诺完全准确。
- CosyVoice 和 MuseTalk 是可选本地组件，不随基础技能静默安装。
- 精确字幕来自批准文本与时间轴；当前没有把任意带声音视频自动转写成精确字幕的 ASR 产品入口。
- 技术 QA 不能代替人工观片和听审。人物漂移、异常肢体、意外界面元素、错误对白和不自然口型必须人工确认。
- 新闻事实必须来自当前网络来源；AI 生成画面不得冒充真实新闻现场。
