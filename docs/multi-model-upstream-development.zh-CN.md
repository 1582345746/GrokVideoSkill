# 多模型与多上游：当前实现说明

本文记录多模型路由改造后的本地代码行为和仍需外部验证的事项。配置/代码实现不等于目标 API 已完成端到端验证；尤其是只读模型目录返回匹配项，不能证明创建、查询、下载或输出质量契约都有效。

## 当前行为

- 新项目默认视频模型为 `seedance-2.0-mini-720p`，默认请求分辨率为 `720p`。显式传入 `--video-model` 或更改安装配置中的 `default_video_model` 后，新项目使用所选模型；实际输出分辨率仍需看下载文件 QA。
- Grok `grok-imagine-video-1.5` 仍可显式选择。模型路由只在同一个 canonical model 的候选上游之间切换，不会因某模型失败而改用另一模型。
- Seedance 的内置上游顺序为 `sub2api`、`newapi`。自动策略优先尝试 Sub2Api；只有运行时把失败判定为允许安全重试时，才会以独立尝试记录继续到同模型的 NewApi。NewApi 不是跨模型回退。
- Grok 的内置路由也保留 Sub2Api 优先、NewApi 次选。用户可以在项目或镜头上固定已配置上游，也可以使用自动策略。
- 视频上游可以用命名 ID 注册。`provider_kind`/`adapter` 描述服务类型和协议客户端；上游 ID 区分具体配置实例。自定义上游密钥保存在本机 DPAPI 秘密存储或由环境变量提供，不写入项目与运行状态。
- 发起新的付费创建前，路由会过滤未启用、缺少 URL/Key、不支持所选模型或被能力配置排除的上游，并要求上游模型目录匹配映射后的远端模型 ID。这只是必要检查，不是完整协议或能力验证。
- `image_base_url`、`image_model` 和图片凭据角色独立于视频设置。切换视频模型、视频上游或视频 Key 不会改写生图地址、模型或凭据。

Seedance 内置默认路由等价于：

```json
{
  "default_video_model": "seedance-2.0-mini-720p",
  "video_model_routes": {
    "seedance-2.0-mini-720p": {
      "default_upstream": "sub2api",
      "upstreams": ["sub2api", "newapi"]
    },
    "grok-imagine-video-1.5": {
      "default_upstream": "sub2api",
      "upstreams": ["sub2api", "newapi"]
    }
  }
}
```

自定义路由可通过 `configure --video-upstream-config ID|TITLE|ADAPTER|KIND|BASE_URL|MODEL[,MODEL]` 注册，再用 `--video-upstream-model-map UPSTREAM_ID|CANONICAL_MODEL=REMOTE_MODEL` 保存本地 canonical ID 到渠道完整模型名的映射，最后用 `--video-model-route MODEL=UPSTREAM[,UPSTREAM]` 声明模型对应的有序上游。第一个上游是首选项。`newapi-json` 表示采用 NewApi 的统一 JSON 视频协议；`newapi-multipart` 表示旧的 multipart 协议。密钥通过对应的安全配置或环境变量提供，不要把 Key 放入参数、项目、运行状态、文档或 Git。

当前用户的 Seedance 远端模型映射为：XingAPI 使用 `seedance-2.0-mini-720P`，算力 AI 使用 `004系列/sd2.0(933/480p最多15秒/720p最多12秒)`。这些是渠道模型名，不应改写成另一个大小写或自行补充括号。

## 项目选择与兼容

项目字段 `video_model` 指定 canonical model ID；镜头可用 `shots[].video_model` 覆盖。项目 `video_route` 可设置 `preferred_upstream` 与 `policy=automatic|fixed`；`video_upstream` 是便捷的固定选择。镜头级 `video_route`/`video_upstream` 可以覆盖项目路由。初始化命令支持 `--video-model` 和 `--video-upstream`；旧 `--video-provider sub2api|newapi`、`--video-provider-policy automatic|fixed` 继续读取为兼容字段。

旧项目和新项目有意采用不同的缺省解析：

- 初始化新项目时，会把当前配置的默认视频模型写入 `project.json`；没有显式配置时为 Seedance。
- 旧项目若没有 `video_model`，继续按 `grok-imagine-video-1.5` 解析，以免升级后改变原项目的生成模型。旧 `video_provider` 会映射为相应的上游偏好。
- 有远端任务 ID 或处于已提交/未知提交等恢复状态的任务，优先使用运行状态中记录的模型与原上游尝试来恢复；更改安装默认值或候选顺序不应迁移已有任务。
- 旧任务签名兼容比较已加入，仍需通过旧格式任务恢复回归确认其可恢复到原身份；不能因此创建重复付费任务。`submission_unknown` 不自动重建。
- 生图字段和旧项目格式继续向前兼容，不要求批量重写项目。

项目样例及字段定义见 [project-schema.md](../grok-video-studio/references/project-schema.md)。

## XingAPI 状态

`xingapi-seedance` 是可选的独立命名视频上游，不是 `sub2api` 的别名，也不会取代 Seedance 默认候选顺序。未配置时，默认顺序是 Sub2Api 优先、NewApi 次选；专用 XingAPI 配置存在时，代码会把它追加到 Seedance 候选末尾。其参与生成仍受启用状态、模型映射、凭据、路由策略和安全失败分类约束。

只读模型目录已确认 XingAPI 返回远端模型 ID `seedance-2.0-mini-720P`；本地 canonical ID `seedance-2.0-mini-720p` 的专用配置会保存这条大小写精确的映射。当前代码把此专用配置接到 Sub2Api 形状的 JSON 视频客户端，并支持该映射。模型目录查询本身不能证明 XingAPI 的创建字段、异步状态、恢复、下载地址/内容或生成视频契约兼容。没有完成受控端到端验收前，应将这些能力视为未验证；不能仅凭 `/v1/models` 命中就宣称可稳定生成。

## 安全与任务身份

每个新视频尝试绑定所选模型、映射后的远端模型、上游 ID、adapter、请求签名和远端任务信息。已创建任务只能到原上游查询和下载；原配置不存在或不匹配时应停止并要求人工处理，不能在另一实例上盲目轮询或重建。

自动回退必须满足现有错误分类与预算/重试策略，并为每个上游尝试单独留痕。认证错误、余额错误、参数错误以及提交结果不明的超时/服务端错误不能当作安全失败。所有回退都保持同一 canonical model，禁止 Seedance 与 Grok 相互替代。

## 尚需完成的验证

代码已经有模型路由、命名上游、镜头覆盖和旧任务恢复路径；以下工作仍决定实际服务能否稳定用于生产：

1. 对每个“模型 × 上游 × adapter”组合核验远端模型 ID、鉴权、T2V/I2V 参数、时长/尺寸/画幅、音频、参考图、异步轮询、任务恢复、结果下载和错误分类。模型目录查询只能完成模型存在性的一部分检查。
2. 先在不创建任务的条件下完成只读配置和模型发现；确认请求字段与费用后，再对每个组合做受控单镜头端到端验收，并记录实际画面尺寸、时长、音轨、内容完整性和恢复结果。
3. 为 Seedance Sub2Api/NewApi、Grok Sub2Api/NewApi 和可选 XingAPI 分别建立可复现的路由、回退、旧任务恢复和提交未知测试，包括旧格式签名恢复。确认不同模型之间绝不会回退。
4. 核对上游声明的能力与实际限制一致。若某组合的能力未知或尚未测试，应在产品配置中明确标记，不要把宽松的默认能力字段当作服务商承诺。
5. 对生图隔离做回归：修改视频默认模型、路由或视频凭据后，图片 Base URL、模型、凭据角色及图片请求签名保持不变。
6. 端到端验收通过后，再更新用户配置指南和发行说明；仓库/技能改名应作为单独迁移，保留旧入口和旧项目发现能力。

不得为文档或路由开发把用户 Key 写入代码、命令行历史、日志、测试快照或 Git；也不得把“模型出现在目录中”描述成“视频协议已验证”。
