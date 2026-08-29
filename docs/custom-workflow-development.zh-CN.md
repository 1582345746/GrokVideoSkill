# 二次开发与自定义工作流

Grok Video Studio 把“产品路线”和“内部工作流预设”分开。T2V、I2V、连续剧和新闻视频是稳定产品路线；`assets/workflow-templates/*.json` 是可编辑的问答和提示词指导。新增一个创意模板通常不需要修改 Python；新增上游、媒体类型或状态语义才需要代码和合同变更。

## 先判断属于哪一类

| 需求 | 正确扩展点 | 不应做的事 |
| --- | --- | --- |
| 换提问、镜头建议或提示词顺序 | 新增/修改 workflow template | 不复制一套 CLI |
| 用户图片直接动画 | `single-image-animation` 或新 I2V preset | 不强制重新生图 |
| 角色一致性 | character master + per-shot keyframe | 不把三视图母版直接发给视频模型 |
| 新视频上游 | provider adapter、capabilities、API contract、error matrix | 不复用另一上游的 Key 或字段 |
| MP4 编辑/延展 | 未来独立 `video-edit`/`video-extend` 路线 | 不塞进 `input_reference` |
| 预设音色或音频文件参考 | 未来独立音频参考路线 | 不冒充普通 I2V 能力 |
| 字幕、混音、封面 | 本地 FFmpeg 后处理 | 不重新创建上游视频任务 |

## 预设模板开发

模板位于 `grok-video-studio/assets/workflow-templates/<id>.json`，至少包含：

- `id`、`title`、`summary`；
- `default_shots`、`preferred_clip_seconds`；
- `character_master` 和 `shot_defaults`；
- `guidance.ask`、`guidance.story`、`guidance.image_prompt`、`guidance.video_prompt`。

建议：

1. ID 使用小写 kebab-case，发布后不要改名；改名会让旧项目无法识别预设。
2. 用 `preferred_clip_seconds <= 15`，一个镜头只放一个可在 15 秒内完成的连续动作。
3. 明确是否需要图片凭据。用户已有图片的模板应将 `generate_image` 设为 `false`。
4. 在 `guidance.video_prompt` 写清楚构图、景别、镜头运动、环境运动、收尾姿态和禁止元素。
5. 不在模板里写模型名称、Key、价格或未经验证的上游字段。

加入模板后运行：

```powershell
python grok-video-studio/scripts/grok_video_studio.py capabilities
python grok-video-studio/scripts/grok_video_studio.py describe <new-workflow-id>
```

模板只改变规划指导，不会自动改变产品路线、上游选择、重试次数或预算门禁。

## 新增字段或路线的顺序

1. 先更新 `references/project-schema.md` 或新建独立 schema，说明默认值、必填条件、迁移方式和旧项目行为。
2. 更新 `references/api-contracts.md`，写清楚 endpoint、凭据职责、请求字段、响应解析和不支持能力。
3. 更新 `references/error-matrix.md`，把提示词过长、尺寸/比例/时长冲突、参考素材错误、模型不支持、瞬时失败和 `submission_unknown` 分开。
4. 在 Python 中加入预检和状态记录；每次创建必须保存 request ID、attempt ID、task ID、提供方、参数、错误分类和提示词版本。
5. 不改变默认重试合同：三次总尝试（首次加两次重试），备用提供方计入总次数；`submission_unknown` 不自动重建，已有 task ID 只恢复。
6. 为正常路径、拒绝路径、恢复路径和重复执行路径添加离线测试。涉及真实上游时，先用最小一次付费验收，不把 Key 或媒体写入 Git。
7. 同步 README、SKILL.md、安装话术、模块话术、迁移说明、产品化清单和版本号。

## 新增上游适配器的合同

必须分别声明：

- T2V、I2V、T2I 是否支持；
- 是否支持视频参考、编辑、延展；
- 音频是模型默认生成还是显式参数；
- 是否支持预设音色参考或音频文件参考；
- 参考图格式、上传方式、最大尺寸和比例约束；
- 查询、下载、任务终态和错误包装；
- 上游 Key 的独立凭据角色。

T2V 请求不得因为 state 中存在旧关键帧而携带参考图；I2V 只发送当前镜头关键帧。任何 MP4/WAV 参考请求都必须在预检阻断，直到独立路线和上游合同完成。

## 费用、秘密和交付边界

- `preflight`、`validate`、`status`、`resume`、下载、FFmpeg、QA 和审阅帧不创建新的上游生成任务。
- 默认提示词硬上限为 4096 UTF-8 字节，建议最终版本不超过 3800；保留完整、精简、最小三版，禁止静默截断。
- 创建前检查剩余预算；图片、视频和角色母版共用重试策略。
- Key 只通过受管 stdin 配置并由 Windows DPAPI 保存，不能出现在命令行、JSON、日志、文档或提交记录。
- `deliverables/final.mp4` 是干净母版；字幕、配音、口型和封面都是独立衍生物。
- native-dialogue 的音色、逐字对白、口型和内嵌字幕必须人工审核，不能写成确定性能力。

## 测试和发布门槛

```powershell
python -m unittest discover -s tests -p "test_*.py" -v
Get-ChildItem grok-video-studio\scripts -Filter *.py -Recurse | ForEach-Object { python -m py_compile $_.FullName }
git diff --check
```

发布前至少核对：

- `capabilities` 输出与真实 provider adapter 一致；
- 新旧项目都能通过 `validate`；
- T2V/I2V 参考图行为符合路线；
- 失败重试不会重复创建已知任务；
- 安装、修复、升级不覆盖 DPAPI 凭据、项目、源码和模型；
- README、SKILL、schema、错误矩阵和用户话术使用同一版本号。

