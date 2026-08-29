# Grok Video Studio 多角色音频开发清单与状态

更新时间：2026-08-29

本文是 `voice-module-development-handoff.zh-CN.md` 的执行状态，不替代原交接文档。代码保持一条 `main` 主线；Voicebox/Qwen 是首期切片，CosyVoice 继续兼容，VoxCPM 作为后续实验提供方，不拆成第二套技能。

## P0：第 1 集解锁

- [x] 建立统一 `audio.tts_provider` 和角色级 `voice.provider`。
- [x] 建立 `draft|auditioned|approved|temporary-test|rejected` 音色状态门禁。
- [x] 兼容旧 `voice_id` 和已授权 `reference_audio` 项目。
- [x] 建立项目级 `voice-catalog.json`，记录候选、模型、revision、seed、授权、WAV 哈希和 QA。
- [x] 接入 Voicebox loopback REST：健康检查、模型状态、预设查询、档案创建、生成、SSE 状态、WAV 下载。
- [x] 支持 Qwen CustomVoice 0.6B/1.7B 与 Voicebox Kokoro 预设合同。
- [x] 固定 Qwen 0.6B revision、Apache-2.0 许可证和约 2.5 GB 下载估算。
- [x] 实现 `voice-list`、`voice-doctor`、`voice-audition`、`voice-approve`、`voice-reject`、`voice-import`、`voice-catalog`。
- [x] 实现 `series-voice-sync`，只同步已批准音色。
- [x] 未批准、拒绝、缺失、未授权和重复音色会阻断本地对白及单集审批。
- [x] 临时音色必须由 `audio.allow_temporary_voices=true` 显式放行。
- [x] 试听执行采样率、声道、时长、响度和静音比例技术检查。
- [x] 对白缓存包含提供方、模型 revision、音色身份/参考哈希、文本、seed 和表演参数。
- [x] 换音色只重建对应角色对白与后续口型结果，不重做图片、干净视频或字幕文本。
- [x] Voicebox 模拟服务集成测试通过，CosyVoice 原有缓存回归保留。
- [x] `voicebox-setup-plan` 可无副作用检查源码、uv、Python、GPU、E 盘空间、模型缓存和服务。
- [ ] 经用户批准后创建 Voicebox 隔离 Python 3.12 环境。
- [ ] 经用户批准后只下载固定 Qwen CustomVoice 0.6B 快照。
- [ ] 真实启动 Voicebox，验证 `/health`、`/models/status` 和预设接口。
- [ ] 为周小满用 Dylan、钱多多用 Eric 各生成中性/喜剧/低落试听。
- [ ] 用户分别听审并接受或拒绝两个角色音色。
- [ ] 写回全剧合同并运行 `series-voice-sync`。
- [ ] 运行 `series-preflight --episode ep-001`，确认音色门禁解除。
- [ ] 在上述步骤前不发起图片、视频、整集对白或 MuseTalk 请求。

## P1：产品化补全

- [x] CosyVoice 从对白工作流硬编码迁移到提供方注册表。
- [x] 不同角色默认禁止共用同一预设、档案或参考音频哈希。
- [x] Voicebox 输出保存到项目，不依赖 Voicebox 数据库绝对路径。
- [x] 基础技能与模型权重分离，Codex 可代管批准后的安装步骤。
- [ ] 实现 `voice-design` 和 VoxCPM 试听母版工作流。
- [ ] 为 VoxCPM 建立稳定 loopback 服务合同和模拟测试。
- [ ] 真实验证 Qwen 0.6B 两个中文男声的显存、速度与音质。
- [ ] 真实验证 Qwen 1.7B；未验收前不作为本机默认。
- [ ] 真实验证 Kokoro 中文回退，只允许标记 `temporary-test`。
- [ ] 用两份不同合成参考音频回归 CosyVoice 零样本角色路由。
- [ ] 增加口型缓存的显式状态文件和仅受影响角色的失效报告。
- [ ] 增加削波比例和更严格的 LUFS/峰值验收。
- [ ] 建立脱敏、可复现的 Voicebox/CosyVoice/MuseTalk 媒体验收包。
- [ ] 完成 VoxCPM GPU OOM 可操作错误和真实 CPU 耗时验证。

## 分发与用户操作

- [x] 保留文生视频、图生视频、连续剧、新闻视频四个产品入口。
- [x] GitHub 拉取代码安装仍为优先路线；独立安装器继续冻结。
- [x] 技能更新不读取或迁移 `secrets.dpapi` 内容，不把 Key 写入仓库。
- [x] 用户只需安装/更新技能并描述目标；Codex 负责运行诊断、展示下载计划和执行已批准步骤。
- [x] 任何 Git 拉取、环境创建、依赖安装、模型下载、服务启动和付费请求都有独立授权边界。
- [ ] 在干净安装副本上执行 `version`、`capabilities`、`voicebox-setup-plan`、全量测试和技能校验。

## 《小满进城记》审核顺序

1. 读取系列合同与第 1 集实际说话角色，只处理周小满和钱多多。
2. 运行只读 Voicebox 安装计划，向用户报告固定模型、许可证、下载量和本机状态。
3. 用户批准后创建隔离环境、下载固定模型并启动 loopback 服务。
4. 每个角色只生成试听，向用户逐个展示 WAV，不生成整集对白。
5. 用户批准音色后写回 `series.json` 和 `voice-catalog.json`。
6. 运行 `series-voice-sync` 与第 1 集预检；把结果交给用户审核。
7. 只有用户再次批准后，才进入 12 张关键帧和 12 段图生视频阶段。
8. 干净视频验收后再生成 19 句对白；停止 TTS 后才启动 MuseTalk。
