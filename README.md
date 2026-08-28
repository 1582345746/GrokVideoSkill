# Grok Video Studio

面向 Codex 的可恢复 AI 视频制作技能。当前版本 `v1.8.0`，提供四个视频入口、五种音频模式和五个可选安装档位：

- 文生视频：QuickAI 文生视频，支持单镜头和多镜头项目。
- 图生视频：QuickAI 生图生成角色母版/镜头关键帧，再由 QuickAI New 动画；用户现成图片也走同一路线。
- 连续剧：先规划整季，再逐集预检、审批、生成和验收，保留跨集人物与剧情连续性。
- 新闻视频：Codex 先检索当前网络信息，记录来源、事实主张和逐镜头引用关系，通过事实门禁后才能生成。

技能同时支持上游原生人物对白、本地 CosyVoice 精确配音、可选 MuseTalk 口型同步、SRT 字幕、FFmpeg 混音/字幕烧录、断点恢复、请求预算、干净画面约束和音视频 QA。

安装档位：`basic`（基础）、`upstream-dialogue`（上游原声对白）、`precise-subtitles`（精确字幕）、`precise-voice`（精确对白）、`lip-sync`（精确对白 + 口型同步）。旧组件名 `core`、`native-dialogue`、`local-voice`、`full-dialogue` 继续兼容。

## 安装

推荐直接把下面的话发给 Codex，由 Codex 完成安装、DPAPI 凭据保存和诊断；不要把 Key 写入仓库文件：

```text
请安装 Grok Video Studio 技能：
仓库：https://github.com/1582345746/GrokVideoSkill.git
QuickAI 生图 Key：<QUICKAI_IMAGE_KEY>
QuickAI 文生视频 Key：<QUICKAI_VIDEO_KEY>
QuickAI New 图生视频 Key：<QUICKAINEW_VIDEO_KEY>

请通过标准输入配置凭据，在 Windows 上使用 DPAPI 保存，不要把 Key 写入源码、项目文件或命令行。安装后运行 version 和 doctor；不要发起付费生成测试，除非我明确授权。

安装前先运行 `install-plan --profile <档位>` 展示依赖和磁盘计划。默认不装本地 AI；`precise-voice`/`lip-sync` 只有在我明确同意后才下载模型、构建 Docker 镜像或启动服务。
```

也可以只安装仓库内容：

```powershell
git clone https://github.com/1582345746/GrokVideoSkill.git
cd GrokVideoSkill
.\install.ps1 -Force
```

需要独立向导时运行：

```powershell
.\install.ps1 -Force -Interactive
```

向导会询问档位、密钥、系统依赖安装和本地服务下载授权。自动化安装使用 `-InstallProfile`；允许 winget 安装 FFmpeg/Docker 时额外传 `-InstallSystemDependencies -AcceptSystemDependencyChanges`；涉及本地服务模型时还必须传 `-InstallComponents -IncludeComponentModels -AcceptComponentDownloads`。NVIDIA 驱动由用户手动安装，安装器只检测不替换。

维护命令：`.\install.ps1 -Check` 只检查当前安装；`.\install.ps1 -Repair -Force` 修复安装并在失败时回滚；`.\install.ps1 -Uninstall` 只移除技能目录，不删除 Key、项目、组件源码或模型。升级/修复时如果没有显式传入 `-InstallProfile`，安装器会保留当前档位。

仓库维护者可运行 `.\build-release.ps1` 生成 ZIP、版本清单和 SHA-256 校验文件；发布前应使用 `-SigningCertificateThumbprint <thumbprint>` 对安装脚本做 Authenticode 签名。用户可用 `Get-FileHash .\GrokVideoSkill-v1.8.0.zip -Algorithm SHA256` 与同名 `.sha256` 文件核对下载完整性；校验和用于防止传输损坏，只有有效的 Authenticode 签名才能验证发布者身份。

配置完成后，凭据按职责分开使用：

| 凭据 | 用途 |
| --- | --- |
| QuickAI 生图 Key | 角色母版和镜头关键帧 |
| QuickAI 文生视频 Key | Prompt-only 文生视频 |
| QuickAI New 图生视频 Key | 参考图/关键帧动画 |

旧版 QuickAI/QuickAI New 配置仍可兼容迁移。Windows 凭据保存在当前用户本地应用数据目录的 DPAPI 加密文件中，不进入技能目录。

本地服务是可选组件。`core` 和 `native-dialogue` 不增加安装负担；`local-voice`/`full-dialogue` 需要 Docker Desktop、NVIDIA GPU 支持和较大的模型下载。Codex 会先运行 `components-plan` 展示来源、固定提交、目录和服务端口，获得同意后才运行安装。服务只发布到本机 `127.0.0.1`，不用时可停止，项目和模型不会被删除。

按当前固定模型实测，`precise-voice` 需要约 10 GB 模型空间，`lip-sync` 需要约 17 GB（含安全余量）；实际安装还应预留 Docker 镜像和临时下载空间，以 `install-plan` 输出为准。模型安装会在 `models/.gvs-model-state.json` 保存 revision、必需文件、大小和 SHA-256 状态；中断后可继续，发现文件损坏会重新下载对应模型。

字幕来源和音频路线独立选择：`audio.subtitle_source=upstream` 保留上游/原片字幕，`project` 生成可审校的确定性 SRT，`none` 不输出字幕。`subtitles --source project` 可在审阅后从上游默认切换到本地字幕；失败时始终保留干净母版，不需要重新付费生成。

新建项目时可用 `--install-profile precise-voice` 或 `--install-profile lip-sync` 继承安装档位的对白/字幕默认值；也可以用显式 `--audio-mode`、`--subtitle-source` 覆盖，已有项目不会被安装器改写。

## 使用话术

文生视频：

```text
使用 $grok-video-studio 做一个 30 秒竖屏文生视频。先给我看完整故事、分镜提示词、请求数量和预算；我批准后再生成。画面不要出现字幕、点赞、评论、弹幕、按钮、Logo 或水印。
```

图生视频：

```text
使用 $grok-video-studio 把这张图片生成 8 秒竖屏视频。锁定人物身份、服装、构图和背景，只让人物自然眨眼并轻微转头。先预检，我批准后再调用上游。
```

连续剧：

```text
使用 $grok-video-studio 规划一部 20 集连续短剧，每集 1-2 分钟。先创建系列项目，写完整季大纲、角色设定、每集剧情和分镜提示词供我审核，不要生成。审核后只生成第一集；以后我说“生成下一集”时，先读取前集成片、验收后的实际结尾和连续性状态，再预检并等待批准。
```

新闻视频：

```text
使用 $grok-video-studio 检索最近 24 小时的热点新闻，选择一个至少有两个独立可靠来源支持的主题。先给我看来源链接、发布时间、事实主张和旁白脚本；我确认后再生成 60 秒新闻视频。AI 画面必须标明为示意，不要把生成画面冒充现场素材。
```

字幕交付：

```text
为当前项目生成 SRT，并用本地 FFmpeg 输出带字幕副本。先让我选择 clean、cinematic 或 news 样式；始终保留原始 final.mp4。native-dialogue 必须先人工确认源视频没有模型自带字幕，再允许烧录。字幕效果不好时换样式重烧或直接使用干净母版，不要重新付费生成视频，也不要让视频模型直接绘制字幕。
```

人物原生说话（轻量、生成式）：

```text
使用 $grok-video-studio 做一个 10 秒文生视频，让原创AI主持人说“你好，这是今天的重点新闻”。使用 native-dialogue，先给我看对白时间轴和提示词再生成。生成后检查真实音量、口型、逐字内容和是否意外烧入字幕；不要把有音轨等同于有可听对白。
```

精确配音（推荐可控路线）：

```text
使用 $grok-video-studio 为当前项目做人物对白。对白文字必须逐字准确，作为字幕唯一来源；使用 local-voice，先检查我的合成/自有/已授权声音素材和 reference_text，再生成逐句音频、混音、SRT 和带字幕副本。保留干净 final.mp4。
```

精确配音和口型：

```text
使用 $grok-video-studio 的 full-dialogue 档位，为原创AI人物生成精确中文对白并做口型同步。先展示组件下载和磁盘计划，等我明确批准后再安装 CosyVoice、MuseTalk 和模型；服务只监听本机。完成后交付干净母版、对白版、字幕版和 QA 报告。
```

## 产品边界

- FFmpeg 可以生成/烧录字幕、时间拉伸、对白混音、背景声压低和响度归一；这些基础交付不需要额外剪辑技能。
- `native-dialogue` 已验证能从 QuickAI 得到可听 AAC 人声，但上游可能自行烧入字幕，声音、逐字准确性和口型仍是生成式结果。
- `native-dialogue` 可以先单独导出 SRT；只有人工确认源视频无内嵌字幕后，才可用 `subtitles --burn --confirm-source-clean` 生成字幕副本，防止重复字幕。
- `local-voice` 用可选 CosyVoice 服务保证对白文本和字幕来自同一合同；`local-lipsync` 再调用 MuseTalk。两者不会随默认技能静默安装。
- `full-dialogue` 在 8GB 显卡上按阶段运行：先 `components-start --profile full-dialogue --component cosyvoice` 合成并缓存对白，再切换 `--component musetalk` 做口型；已缓存对白不会重复请求 CosyVoice。
- QA 会检查平均/峰值音量和静音占比；`has_audio=true` 仍只代表存在音轨，不能替代听审或可选 ASR 核对。
- 声音参考必须是合成、自有或已授权素材，并填写准确参考文本；不支持无授权克隆第三方或公众人物声音。
- 新闻热点检索由运行技能的 Codex 使用实时网络完成，CLI 负责保存证据合同并在生成前验证，不能把模型记忆当作新闻来源。
- 技术 QA 不能替代人工观片。角色漂移、异常肢体、意外 UI、字幕、Logo 和水印必须逐镜头检查。

详细工作流和命令见 [`grok-video-studio/SKILL.md`](grok-video-studio/SKILL.md)。

产品化架构、权限、安全、升级和发布验收项见 [`docs/productization-checklist.md`](docs/productization-checklist.md)。
