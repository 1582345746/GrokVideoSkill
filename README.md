# Grok Video Studio

面向 Codex 的可恢复 AI 视频制作技能。当前版本 `v1.5.0`，提供四个产品入口：

- 文生视频：QuickAI 文生视频，支持单镜头和多镜头项目。
- 图生视频：QuickAI 生图生成角色母版/镜头关键帧，再由 QuickAI New 动画；用户现成图片也走同一路线。
- 连续剧：先规划整季，再逐集预检、审批、生成和验收，保留跨集人物与剧情连续性。
- 新闻视频：Codex 先检索当前网络信息，记录来源、事实主张和逐镜头引用关系，通过事实门禁后才能生成。

技能同时支持 SRT 字幕导出、本地 FFmpeg 字幕烧录、音频保留、断点恢复、请求预算、干净画面约束和交付 QA。

## 安装

推荐直接把下面的话发给 Codex，由 Codex 完成安装、DPAPI 凭据保存和诊断；不要把 Key 写入仓库文件：

```text
请安装 Grok Video Studio 技能：
仓库：https://github.com/1582345746/GrokVideoSkill.git
QuickAI 生图 Key：<QUICKAI_IMAGE_KEY>
QuickAI 文生视频 Key：<QUICKAI_VIDEO_KEY>
QuickAI New 图生视频 Key：<QUICKAINEW_VIDEO_KEY>

请通过标准输入配置凭据，在 Windows 上使用 DPAPI 保存，不要把 Key 写入源码、项目文件或命令行。安装后运行 version 和 doctor；不要发起付费生成测试，除非我明确授权。
```

也可以只安装仓库内容：

```powershell
git clone https://github.com/1582345746/GrokVideoSkill.git
cd GrokVideoSkill
.\install.ps1 -Force
```

配置完成后，凭据按职责分开使用：

| 凭据 | 用途 |
| --- | --- |
| QuickAI 生图 Key | 角色母版和镜头关键帧 |
| QuickAI 文生视频 Key | Prompt-only 文生视频 |
| QuickAI New 图生视频 Key | 参考图/关键帧动画 |

旧版 QuickAI/QuickAI New 配置仍可兼容迁移。Windows 凭据保存在当前用户本地应用数据目录的 DPAPI 加密文件中，不进入技能目录。

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
为当前项目生成 SRT，并用本地 FFmpeg 输出带字幕副本。保留原始 final.mp4，不要让视频模型直接绘制字幕。
```

## 产品边界

- FFmpeg 可以生成、烧录字幕，也能合并已有配音和背景音乐；不需要额外剪辑技能完成这些基础操作。
- 本技能尚不内置 TTS 服务。需要自动配音时，应先由可用的 TTS/配音服务生成音频，再交给本技能混音和字幕对齐。
- 上游视频即使带音轨，也可能只是静音 AAC；`has_audio=true` 只代表存在音轨，不代表已经有可听对白或环境声。
- 新闻热点检索由运行技能的 Codex 使用实时网络完成，CLI 负责保存证据合同并在生成前验证，不能把模型记忆当作新闻来源。
- 技术 QA 不能替代人工观片。角色漂移、异常肢体、意外 UI、字幕、Logo 和水印必须逐镜头检查。

详细工作流和命令见 [`grok-video-studio/SKILL.md`](grok-video-studio/SKILL.md)。
