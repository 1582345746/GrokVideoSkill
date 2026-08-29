# Grok Video Studio 多角色音频模块开发交接清单

> [!WARNING]
> 本文档记录本地音频模块的历史开发边界。当前技能已升级到 v2.0.0 `upstream-first`：新项目默认 `native-dialogue`、`generate_audio=true`、`subtitle_source=none`；本地 TTS/口型组件仍保留为可选维护路线。请同时阅读 [`v2.0 迁移说明`](v2.0-upstream-first-migration.zh-CN.md)。

更新日期：2026-08-29
适用仓库：`<GROK_VIDEO_SKILL_REPO>`
当前连续剧项目：`<SERIES_PROJECT>`

## 1. 本轮目标

本任务必须分成两条互不阻塞的路线：

1. **快速交付路线**：尽快为第 1 集生成两个明显不同的中文男声，继续完成关键帧、图生视频、对白、字幕、混音和口型全流程测试。
2. **技能产品化路线**：把现有只面向 CosyVoice 的对白实现改造成多音频提供方架构，正式支持预设音色、声音克隆和文字音色设计。

不要为了开发正式音频模块而阻塞第 1 集测试，也不要把第 1 集的临时音色直接当作全剧最终选角。

## 2. 当前事实

### 2.1 仓库和设备

- 本交接清单基于 Grok Video Studio `1.8.0`；当前实现版本为 `2.0.0`。
- 仓库分支：`main`。
- 最近已知提交：`d26728a docs: add comprehensive v1.8 handoff`。
- GPU：NVIDIA GeForce RTX 3060 Ti，8 GB 显存。
- 检查时空闲显存约 6.3 GB；运行本地模型前应停止其他 GPU 服务。
- 内存：约 64 GB。
- 系统 Python：3.9.12，不满足新的 TTS 项目要求。
- 已安装 `uv 0.11.7`，应使用它创建隔离的 Python 环境，不要修改现有 Anaconda。
- FFmpeg：`D:\ffmpeg-7.0.2-essentials_build\bin\ffmpeg.exe`。

### 2.2 第 1 集状态

- 第 1 集路径：`episodes\ep-001`。
- 总时长：120 秒。
- 镜头数：12。
- 精确对白：19 句。
- 说话角色只有：
  - 周小满 `zhou-xiaoman`
  - 钱多多 `qian-duoduo`
- `audio.mode`：`local-lipsync`。
- `audio.subtitle_source`：`project`。
- 角色母版已经生成并由用户接受。
- 当前未批准、未生成第 1 集关键帧和视频。
- 当前预检只因两名角色缺少正式音色合同而失败：

```text
character qian-duoduo.voice requires voice_id or reference_audio
character zhou-xiaoman.voice requires voice_id or reference_audio
```

### 2.3 现有六份音频是什么

目录：

```text
<SERIES_PROJECT>\assets\voice-auditions
```

这些 WAV 是 Windows 系统 TTS 制作的试听候选，不是 CosyVoice 输出，也没有写入正式项目音色合同。

四名男性角色共用 `Microsoft Kangkang` 基础音色，只修改了速度和音高。因此音频文件虽然不同，人物声音身份仍基本相同。用户已明确反馈区分度不合格。

这些文件只能作为失败回归样本或临时链路测试素材，不能作为产品级全剧音色。

## 3. CosyVoice 到底负责什么

### 3.1 CosyVoice能够生成音频

当前技能已经实现了 CosyVoice 本地 HTTP 适配器。给它以下任意一种有效音色身份后，它能够根据指定文字生成 WAV：

1. 模型内置的 `voice_id`。
2. 经授权的 `reference_audio` 加精确 `reference_text`。

当前完整对白流程还能够：

- 按 `speaker` 把不同角色路由到不同音色。
- 为每句对白生成独立 WAV。
- 根据对白窗口调整时长。
- 生成 `dialogue-state.json` 并复用缓存。
- 生成确定性 SRT。
- 混合对白轨、压低原声并归一化响度。
- 把配音版视频发送给 MuseTalk 做口型同步。

### 3.2 为什么当前看起来“不能生成”

不是 CosyVoice 引擎失效，而是输入合同没有完成：

1. 本机安装的是 `FunAudioLLM/Fun-CosyVoice3-0.5B-2512`。
2. 这个模型目录没有 `spk2info.pt`，健康接口不会提供可直接分配的内置说话人列表。
3. 周小满和钱多多当前都只有 `{"provider":"cosyvoice"}`，没有 `voice_id`，也没有正式 `reference_audio`。
4. CosyVoice 服务当前停止，`127.0.0.1:9880` 不可访问。
5. 现有六份试听没有被复制到 `assets/voices/`，也没有写入授权类型和精确原文。

因此预检在付费生成前阻断，这是正确行为。

### 3.3 CosyVoice不适合独自解决什么

当前安装的 CosyVoice3 适合“已有声音身份后的精确对白生成”，不适合只根据“青年男声、成熟女声”这种文字凭空设计六个稳定且彼此不同的角色音色。

它的 `instruct` 可以调整语速、情绪、方言和表演方式，但同一参考声音经过不同指令后仍然是同一声音身份。不能把改变音高、速度或情绪当作换了一个角色。

所以产品需要在 CosyVoice 前面增加“音色来源与选角”模块。

## 4. 可利用的两个新项目

### 4.1 Voicebox

源码：

```text
<VOICEBOX_SOURCE>
```

提交：`51f49dea198384b4eb6087b72c17057c6eb1c1cd`

状态：只有源码，没有 `.venv`，没有模型缓存，后端要求 Python 3.12。

Voicebox 是本地音色管理与 TTS 服务，不是单一模型。它已经提供 REST API、MCP、角色音色档案和多个 TTS 后端。

当前最有用的后端：

#### Qwen CustomVoice 0.6B

- 模型登记大小约 1.2 GB。
- 不需要参考音频。
- 支持自然语言表演指令。
- 内置中文音色：
  - `Dylan`：年轻北京男声。
  - `Eric`：活泼成都男声。
  - `Uncle_Fu`：成熟低沉男声。
  - `Vivian`：明亮年轻女声。
  - `Serena`：温柔年轻女声。
- 推荐作为第 1 集的首选快速方案：
  - 周小满 -> `Dylan`
  - 钱多多 -> `Eric`

#### Kokoro 82M

- 模型登记大小约 350 MB。
- CPU 友好。
- 提供 4 个中文男声和 4 个中文女声。
- 适合最快验证六角色路由，但中文自然度和戏剧表现需要人工试听。
- 如果 Qwen CustomVoice 安装或显存验收失败，使用 Kokoro 作为第 1 集回退方案。

Voicebox 当前没有 VoxCPM 后端。不要假设把两个仓库放在相邻目录后就能自动互相调用。

### 4.2 VoxCPM

源码：

```text
<VOXCPM_SOURCE>
```

提交：`f5a1c6a6b901bc732e20f0d59a369f6829ad717a`

状态：只有源码，没有 `.venv`，没有模型权重；要求 Python 3.10 至 3.12、PyTorch 2.5 以上和 CUDA 12 以上。

VoxCPM2 能通过自然语言描述直接创造音色，不要求先提供真人参考音频：

```text
（23岁中国青年男性，声音真诚明亮，略有自然乡音，语速自然）
娘，你放心，我会一步一步把路走稳。
```

它适合后续正式“角色声音设计”流程：

```text
角色声音描述
  -> 按固定 seed 生成 3 个试听候选
  -> 用户接受 1 个
  -> 保存 voice-master.wav 和生成元数据
  -> 以该声音母版生成或克隆角色全部对白
```

限制：VoxCPM2 官方项目文档标注约 8 GB 显存。本机 3060 Ti 处于边界，当前空闲显存不足 8 GB，可能 OOM。CPU 路线代码可选且本机内存足够，但速度和算子兼容性仍需真实验收。

因此本轮不要让 VoxCPM2 阻塞第 1 集。它属于正式音色设计的第二阶段验收项。

## 5. 路线A：尽快完成第1集音频

### A0. 边界

- 只处理周小满和钱多多。
- 先生成试听，不直接生成 19 句正式对白。
- 不修改其他四名角色音色。
- 不启动 MuseTalk。
- 不发起 QuickAI 图片或视频请求。
- 不删除现有 Windows TTS 候选。

### A1. 建立Voicebox隔离环境

- [ ] 确认 Voicebox 工作树状态，保留用户已有 `.vs/`，不要删除。
- [ ] 使用 `uv` 安装隔离的 Python 3.12，不修改系统 Python 或 Anaconda。
- [ ] 模型和缓存放在 E 盘，不占用系统盘。
- [ ] 仅启动本地后端需要的部分；不为本任务构建 Tauri、Bun 或 Rust 桌面端。
- [ ] 服务只绑定 `127.0.0.1`。
- [ ] 先运行 Voicebox 后端自检，再下载模型。

建议环境和缓存位置：

```text
<VOICEBOX_SOURCE>\.venv
<MODEL_CACHE>\voicebox
<SERVICE_DATA>\voicebox
```

不要在未经核对 Voicebox 配置读取逻辑前硬编码环境变量。先确认其 `backend/config.py` 和启动命令如何读取模型、数据库和生成文件目录。

### A2. 首选Qwen CustomVoice 0.6B

- [ ] 只下载 `Qwen/Qwen3-TTS-12Hz-0.6B-CustomVoice`。
- [ ] 检查模型许可证和固定 revision，将许可证、revision、大小写入测试报告。
- [ ] 启动后端并检查 `/health`、`/models/status` 和预设音色接口。
- [ ] 建立两个测试音色档案：
  - 周小满：`Dylan`
  - 钱多多：`Eric`
- [ ] 每个角色只生成 3 句试听：中性、喜剧、低落。
- [ ] 保存生成 seed、模型 revision、speaker、instruct、文本和输出 SHA-256。
- [ ] 技术检查采样率、声道、时长、非静音、削波和文件可读性。
- [ ] 用户试听并分别接受或拒绝两名角色。

建议试听文字：

```text
周小满：娘，你放心，我在城里会认真找工作。路再难，我也会一步一步走稳。
钱多多：我不是爱吐槽，我是替生活做现场解说。真有事你开口，我肯定在。
```

建议表演指令：

```text
周小满：青年男性，真诚朴实，略带进城后的兴奋，语速自然，不要播音腔。
钱多多：青年男性，语速稍快，有吐槽和喜剧感，但不要尖厉或卡通化。
```

### A3. 回退到Kokoro

如果 Qwen CustomVoice 0.6B 无法在本机稳定运行：

- [ ] 下载并启动 Kokoro 82M。
- [ ] 从 4 个中文男声中试听选择两个差异最大的音色。
- [ ] 只把它们标记为 `temporary-test`。
- [ ] 继续完成第 1 集技术链路，不把音色质量记为产品通过。

### A4. 把试听结果接到当前技能

正式适配器尚未开发前，可以将用户接受的 Qwen/Kokoro试听导出为两段合成参考音频，然后继续使用现有 CosyVoice 零样本克隆链路。

要求：

- [ ] 两段参考音频必须来自不同预设音色。
- [ ] 保留逐字准确的 `reference_text`。
- [ ] 授权类型写为 `synthetic`。
- [ ] 复制至全剧：

```text
assets/voices/zhou-xiaoman.wav
assets/voices/qian-duoduo.wav
```

- [ ] 更新 `series.json`：

```json
{
  "provider": "cosyvoice",
  "reference_audio": "assets/voices/zhou-xiaoman.wav",
  "reference_text": "与该音频逐字一致的文字",
  "consent": "synthetic",
  "status": "temporary-test"
}
```

- [ ] 运行 `series-sync`，让两份参考音频和音色合同同步进全部单集。
- [ ] 重新运行 `series-preflight --episode ep-001`。
- [ ] 确认音色错误消失后才允许批准第 1 集。

### A5. 第1集后续顺序

- [ ] 生成 12 张第 1 集关键帧并逐张审核。
- [ ] 审核通过后生成 12 段图生视频。
- [ ] 合并并验收干净无字幕、无 UI 的 `final.mp4`。
- [ ] 只启动 CosyVoice。
- [ ] 生成 19 句对白，核对角色路由确实是两个声音。
- [ ] 生成 `dialogue-track.wav` 和确定性 SRT。
- [ ] 生成普通对白版并检查响度、静音比例和对白时间窗。
- [ ] 停止 CosyVoice。
- [ ] 只启动 MuseTalk，复用缓存对白完成口型同步。
- [ ] 检查口型、字幕、人物一致性和最终音视频同步。

RTX 3060 Ti 只有 8 GB 显存，CosyVoice、Qwen、VoxCPM、MuseTalk不能同时运行。每个阶段完成后必须卸载或停止当前模型，再进入下一阶段。

### A6. 快速路线验收标准

- [ ] 周小满和钱多多听感明显不是同一人。
- [ ] 19 句对白全部按 `speaker` 使用正确角色音色。
- [ ] 对白文字与 SRT 完全一致。
- [ ] 每句对白落在声明的开始和结束时间内。
- [ ] 无全静音、爆音、严重截断、明显错字或串角色。
- [ ] 普通对白版和口型版均保留干净母版。
- [ ] 临时音色有明确标记，不能误报为正式全剧选角完成。

## 6. 路线B：技能音频模块产品化

### B0. 架构目标

现有逻辑把 `local-voice` 等同于 CosyVoice。应改成：

```text
audio.mode = local-voice | local-lipsync
audio.tts_provider = cosyvoice | voicebox | voxcpm
```

`audio.mode` 决定是否本地配音和口型；`tts_provider` 决定用哪个引擎生成对白。字幕和口型不得绑定到某个 TTS 品牌。

建议公共接口：

```text
health()
capabilities()
list_voices()
design_voice()
audition_voice()
synthesize_line()
validate_voice_contract()
```

### B1. 扩展角色音色合同

- [ ] 保留现有 `voice_id`、`reference_audio`、`reference_text`、`consent`。
- [ ] 增加：
  - `provider`
  - `model`
  - `model_revision`
  - `voice_type`: `preset|reference|designed`
  - `voice_status`: `draft|auditioned|approved|temporary-test|rejected`
  - `preset_voice_id`
  - `design_prompt`
  - `seed`
  - `instruct_text`
  - `master_audio`
  - `approved_at`
  - `approved_by`
  - `source_license`
- [ ] 为旧项目保留向后兼容。
- [ ] 未批准音色不能进入正式 `series-approve`；`temporary-test` 只能在显式测试标志下放行。

### B2. 增加音色命令

- [ ] `voice-list`：列出提供方、模型、音色、语言和硬件要求。
- [ ] `voice-import`：导入自有、授权或合成参考音频。
- [ ] `voice-design`：根据自然语言设计角色音色。
- [ ] `voice-audition`：只生成试听，不要求存在最终视频。
- [ ] `voice-approve`：写入审核状态和不可变元数据。
- [ ] `voice-reject`：记录拒绝原因并保留候选。
- [ ] `series-voice-sync`：把全剧已批准音色同步至单集。
- [ ] `voice-doctor`：检查服务、模型、音色身份、授权和重复使用。

### B3. Voicebox适配器

- [ ] 通过 localhost REST API 接入，不把 Voicebox源码复制进技能。
- [ ] 支持服务地址配置，默认只允许 `127.0.0.1`。
- [ ] 支持预设音色查询、创建档案、生成、轮询、下载WAV和错误恢复。
- [ ] 首期只正式支持：
  - Qwen CustomVoice 0.6B/1.7B预设音色。
  - Kokoro中文预设音色。
- [ ] MCP只作为可选的人机交互入口；批量项目渲染以REST适配器为准。
- [ ] 不让 Voicebox 自动播放代替保存项目WAV。
- [ ] 输出复制到项目自己的 `assets/dialogue/`，不要让 `dialogue-state.json`依赖Voicebox数据库绝对路径。

### B4. VoxCPM适配器

- [ ] 先做独立可行性测试，不直接承诺默认安装。
- [ ] 检查 GPU、CPU、模型下载空间和许可证。
- [ ] 支持 `design`、`clone` 和固定 seed。
- [ ] 音色设计必须先生成试听母版，再基于已接受母版生产对白。
- [ ] 不允许每句对白只用 `design_prompt`重新随机生成，否则同一角色会漂移。
- [ ] 保存模型 revision、设计描述、seed、母版哈希和生成参数。
- [ ] RTX 3060 Ti GPU OOM 时返回可操作错误，不静默切到极慢 CPU。
- [ ] CPU路线只有真实生成并记录耗时后才能标记支持。

### B5. 保留并重构CosyVoice适配器

- [ ] 保持现有项目兼容。
- [ ] 将 CosyVoice 从 `dialogue_workflow.py`中的硬编码依赖移到提供方注册表。
- [ ] 健康检查返回内置说话人列表和模型类型。
- [ ] 无内置说话人时，错误信息明确要求参考音频，而不是笼统提示“服务不可用”。
- [ ] 检查不同角色是否误用同一参考音频哈希。
- [ ] 相同参考音频用于多个角色时默认阻断，只有显式测试模式可以放行。
- [ ] `instruct_text`只被视为表演控制，不能被视为独立声音身份。

### B6. 音色库和授权

- [ ] 建立项目级 `voice-catalog.json`。
- [ ] 声音文件保存在项目或用户配置目录，不进入Git仓库。
- [ ] 记录来源：`synthetic|owned|licensed`。
- [ ] 参考真人声音必须要求精确授权说明。
- [ ] 禁止公众人物、影视角色、主播或无权第三方声音克隆。
- [ ] 导出项目时默认不附带不能再分发的声音素材。
- [ ] 删除音色时检查是否被连续剧引用，避免破坏可恢复状态。

### B7. 多角色一致性和缓存

- [ ] 缓存签名必须包含：文本、角色、提供方、模型revision、音色ID或母版哈希、表演指令和生成参数。
- [ ] 替换音色后只失效对应角色的对白与口型缓存，不重新生成图片和原始视频。
- [ ] 字幕文本变化时失效该句音频、SRT和口型。
- [ ] 音色变化但文字不变时不应重写字幕文本。
- [ ] 连续剧后续集复用同一已批准音色，不得自动重新选角。

### B8. QA和测试

- [ ] 单元测试：三种提供方的合同验证和错误映射。
- [ ] 单元测试：不同 `speaker`必须调用不同音色身份。
- [ ] 单元测试：重复参考音频检测。
- [ ] 单元测试：临时音色和批准音色门禁。
- [ ] 集成测试：Voicebox模拟服务。
- [ ] 集成测试：VoxCPM模拟CLI或服务。
- [ ] 回归测试：现有CosyVoice缓存仍可复用。
- [ ] 真实本地测试：Qwen CustomVoice两个中文男声。
- [ ] 真实本地测试：CosyVoice用两份不同合成参考音频生成相同测试句。
- [ ] 真实本地测试：替换音色后只重建对白和MuseTalk结果。
- [ ] 音频QA：采样率、声道、时长、峰值、平均响度、静音比例、削波。
- [ ] 人工QA：人物差异、年龄匹配、语气、自然度、错字、串角色。

## 7. 推荐产品档位

| 档位 | 引擎 | 是否需要参考音频 | 目标 |
| --- | --- | --- | --- |
| 快速流程测试 | Voicebox + Kokoro | 否 | 最低安装和显存成本，快速区分角色 |
| 标准预设音色 | Voicebox + Qwen CustomVoice | 否 | 中文质量、不同人物和表演控制平衡 |
| 自定义角色音色 | VoxCPM2音色设计 | 否 | 从文字创建原创角色声音 |
| 精确声音克隆 | CosyVoice / Qwen Base / VoxCPM | 是 | 使用已授权或已生成的声音母版生产全剧对白 |
| 口型同步 | MuseTalk | 使用已经生成的对白 | 修正人物口型，不负责生成声音 |

## 8. 不要做的事情

- 不要删除或覆盖用户已有的两份外部仓库。
- 不要修改系统Python或破坏Anaconda。
- 不要同时启动多个高显存TTS模型和MuseTalk。
- 不要把Windows TTS调速结果宣传为六个独立人物音色。
- 不要把表演指令当成声音身份。
- 不要下载或克隆无授权的第三方声音。
- 不要在音色审核前产生第1集付费图片或视频请求。
- 不要在没有用户审核的情况下自动生成整季对白。
- 不要把外部模型权重或用户声音提交到GitHub。

## 9. 完成定义

### 快速路线完成

以下条件全部满足才算第1集音频解锁：

1. Voicebox本地后端可启动和停止。
2. 周小满、钱多多拥有两个明显不同的中文男声。
3. 用户已试听并接受测试音色。
4. 音色元数据和授权类型已写入全剧合同。
5. `series-preflight --episode ep-001`通过音色门禁。
6. 未发起未经批准的图片或视频请求。

### 产品化路线完成

以下条件全部满足才算音频模块可以产品化：

1. CosyVoice、Voicebox、VoxCPM使用统一提供方接口。
2. 支持预设、参考克隆和文字设计三种音色来源。
3. 具备试听、审核、同步、缓存、替换和授权合同。
4. 不同角色误用同一声音能够被检测。
5. 替换音色不要求重做图片和原始视频。
6. 三种提供方均有自动测试；宣称支持的本地路径有真实媒体验收。
7. README、技能说明、安装档位和用户使用话术同步更新。

## 10. 新对话启动话术

将下面整段发送给新的Codex对话：

```text
请接手开发 Grok Video Studio 多角色音频模块，并先解锁《小满进城记》第1集。

工作区：
<GROK_VIDEO_SKILL_REPO>

必须先完整阅读：
<GROK_VIDEO_SKILL_REPO>\docs\voice-module-development-handoff.zh-CN.md
<GROK_VIDEO_SKILL_REPO>\HANDOFF-GROK-VIDEO-STUDIO-v1.8.0.zh-CN.md

连续剧项目：
<SERIES_PROJECT>

外部源码：
<VOICEBOX_SOURCE>
<VOXCPM_SOURCE>

严格按文档分两条路线执行。第一阶段优先使用Voicebox的Qwen CustomVoice 0.6B，为周小满选择Dylan、钱多多选择Eric，每人先生成试听，等待我审核；失败时回退Kokoro。不要先生成第1集图片或视频，不要启动MuseTalk，不要使用无授权声音。

试听通过后，将两份不同合成音色作为temporary-test合同接入现有CosyVoice流程，运行series-sync和第1集预检。只有我明确批准后才能继续付费关键帧和图生视频。

并行完成技能的多TTS提供方设计，但不要为了重构阻塞第1集。保留CosyVoice，新增Voicebox和VoxCPM适配器。所有模型使用隔离环境，模型缓存放E盘，不修改系统Python，不删除用户现有文件，不提交模型权重或声音素材。

开始前先报告：Git状态、Python/uv、GPU空闲显存、Voicebox/VoxCPM源码提交、模型是否已缓存、计划下载大小和落盘位置。随后按清单逐项实施、测试并更新状态。
```
