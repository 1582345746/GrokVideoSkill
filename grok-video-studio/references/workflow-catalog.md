# Workflow Catalog

Workflow definitions live in `assets/workflow-templates/*.json`. Edit those JSON files to improve titles, questions, and prompt guidance without changing the Python client.

The catalog is intentionally general. Short drama is supported but is not the default or highest-priority workflow.

Product route is separate from workflow. The four routes are text-to-video, image-to-video, episodic series, and sourced news video. A supplied-image animation is an I2V variant, not a fifth route. Use `series-init` only when ordered episodes share canon and continuity; every series episode still selects one internal workflow from this catalog.

| ID | Title | Primary input |
| --- | --- | --- |
| `general-video` | 通用视频项目 | Goal, optional references |
| `text-to-video` | 文生视频 | Text; defaults to QuickAI JSON with no reference images |
| `single-image-animation` | 单图动画 | One image |
| `character-consistent-story` | 角色一致性故事 | One single-sheet character master |
| `dance-performance` | 人物跳舞与表演 | One person image |
| `comedy-action` | 搞笑动作视频 | One subject image |
| `product-ad` | 产品广告 | Product image or keyframe |
| `scene-animation` | 插画与风景动效 | One scene image |
| `short-drama` | 人物短剧与多镜头叙事 | Script plus character master |
| `news-video` | 热点新闻视频 | Current web sources plus verified claim mappings |

For character workflows, create one master sheet image containing the same character's front, side, and back or full-body views. Use that sheet only to derive per-shot keyframes. Send the current shot keyframe, not the multi-view sheet, to image-to-video.

`single-image-animation` is an internal I2V preset. It defaults to `generate_image=false`: put the supplied file in `video_references`. It does not need the QuickAI image credential. `character-consistent-story` derives keyframes and therefore needs the image credential. A series can reuse one master per character across all episodes; see `series-schema.md`.

`news-video` is initialized with `news-init`, not plain `init`. Codex must browse current sources and complete `news.json`; generation is blocked until `news-validate` passes. See `news-schema.md`.

The selected `video_mode` and `video_provider` are part of the project contract. Do not infer the mode from whether an image happens to exist in state.
