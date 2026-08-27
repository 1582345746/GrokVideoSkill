---
name: grok-video-studio
description: Plan and build resumable AI video projects with QuickAI image generation, QuickAI New Grok video generation, editable workflow templates, single-sheet character masters, dynamic shot planning, validated MP4 downloads, and arbitrary clip assembly. Use for text-to-video, image-to-video, single-image animation, character consistency, dance or comedy motion, product ads, scene animation, multi-shot narratives, short drama, screenplay or shot-list creation, batch generation, or requests such as 写剧本、生图、生视频、图生视频、分镜视频、角色一致性视频、人物三视图、短剧、批量生成视频 or 合并视频.
---

# Grok Video Studio

Create the creative plan with Codex. Use the bundled scripts for credentials, paid API calls, durable state, downloads, validation, assembly, technical QA, and lightweight post-production. The installed CLI reports version `1.3.0`.

## Setup

1. When the user supplies provider keys in the installation conversation, Codex must perform configuration itself. Run `python scripts/grok_video_studio.py configure --credentials-stdin --skip-test` in a managed process and send exactly one JSON object through that process's stdin: `{"quickai_key":"...","quickainew_key":"..."}`. Either key may be omitted, but at least one is required. Do not ask the user to open PowerShell.
2. Never place keys in command-line arguments, temporary files, `SKILL.md`, project files, source control, or terminal output. On Windows, configuration stores them with DPAPI under the current user's local application-data directory. The installed Skill directory remains secret-free and updateable.
3. If the user has not supplied keys in the conversation, use the original hidden interactive `configure` flow instead of inventing credentials.
4. Run `python scripts/grok_video_studio.py doctor` and resolve the configured provider and FFmpeg checks before paid generation. A missing unused provider key is allowed. Skip `doctor` when the user asks to avoid all real upstream tests.
5. Confirm the requested video mode before creating a project. Defaults are text-to-video -> QuickAI JSON and image-to-video -> QuickAI New multipart. A project-level `video_provider` may explicitly override this route when the corresponding key is configured. Do not route through the Canvas browser proxy unless the user explicitly requests a future bridge adapter.
6. Run `version` after installation. The repository root includes `install.ps1`; Codex can use `-ConfigureFromStdin -SkipProviderTest` to install and configure in one managed terminal session.

Read [references/api-contracts.md](references/api-contracts.md) when diagnosing endpoints or provider responses. Read [references/error-matrix.md](references/error-matrix.md) when a request fails.

## Select A Workflow

When the user's goal is unclear, run `python scripts/grok_video_studio.py capabilities` and present the returned titles in the conversation. Let the user reply with a title or workflow ID. Do not use an OS popup. Do not force short drama or a fixed shot count.

Run `python scripts/grok_video_studio.py describe <workflow-id>` for the selected questions and prompt guidance. Workflow JSON files are editable under `assets/workflow-templates/`. Read [references/workflow-catalog.md](references/workflow-catalog.md) for the catalog and routing rules.

## Create A Project

1. Initialize a local project:

   `python scripts/grok_video_studio.py init <project-folder> --title "..." --topic "..." --workflow <id> --target-seconds <seconds> --mode text-to-video --video-provider quickai --video-resolution 480p`

2. Let the CLI plan a variable number of shots from the target duration, or override with `--shots`. Every shot must be 1-15 seconds; no project is fixed to eight clips.
3. Fill `project.json`: story, concise identity and style bibles, and every shot's image and video prompts. Keep stable shot IDs.
4. Put user-supplied references under `assets/references/` and use only project-relative paths.
5. Run `python scripts/grok_video_studio.py preflight <project-folder>` before spending. Review request counts, total duration, prompt lengths, warnings, and errors.
6. Set `budget.image_request`, `budget.video_request`, and `budget.max_estimated_cost` when a hard project spending ceiling is required. Every attempted paid request is recorded in `state.json` before it is sent.
7. Run `audit` to review structured character IDs, wardrobe changes, adjacent-shot continuity notes, and the manual review checklist.

Read [references/project-schema.md](references/project-schema.md) before editing `project.json`. Read [references/prompt-contract.md](references/prompt-contract.md) before writing prompts.

## Use A Character Master

For identity-critical character workflows:

1. Generate one character master image containing front, side, and back or full-body views of the same character on one canvas. Do not generate separate view files.
2. Use that single sheet as an image reference to derive each shot's scene keyframe.
3. Send only the current shot keyframe to image-to-video. Never send the multi-view master sheet directly to the video model.

Run `generate-character` independently or let `run` create it before shot keyframes. Preserve the generated master path in `state.json`.

## Generate And Resume

- Run the complete pipeline with `python scripts/grok_video_studio.py run <project-folder>`.
- Generate or register the one-sheet master with `generate-character`.
- Generate only keyframes with `generate-images`.
- Submit and poll only video shots with `generate-videos`.
- Resume existing task IDs with `resume`. A resumed task must not create a second paid task.
- Add repeatable `--shot <shot-id>` to process selected shots. Partial `run` operations do not auto-assemble.
- Add `--progress` to generation or resume commands to receive JSONL progress events on stderr while the final JSON result remains on stdout.
- Inspect durable state with `status`.
- Assemble completed clips with `assemble`.
- Normalize and combine arbitrary existing clips with `assemble-files <output.mp4> <clip...>`.

Treat every image or video create request as billable. The script records an attempt before sending it and does not retry an ambiguous create failure. `--retry-failed` requires `--retry-reason "..."`; the reason and prior task state are preserved in history. Polling, model discovery, and content download may retry safely with bounded backoff and a circuit breaker.

Keep every final composed image and video prompt at or below 4096 characters. Treat 3800 characters as the working ceiling. The preflight and API clients enforce this; do not silently truncate creative instructions.

Video contracts are explicit in `project.json`: `video_mode` is `text-to-video` or `image-to-video`; `video_provider` is `quickai` or `quickainew`; resolution is `480p`, `720p`, or `1080p`; aspect ratio is provider-supported. T2V never sends reference images, even when an old keyframe exists in state. I2V sends only explicit references or the current shot keyframe.

## Delivery Gate

1. Require the single-sheet character master when enabled and every requested keyframe when `generate_image` is true.
2. Require every video to have a `.mp4` filename, MP4 signature, readable video stream, positive duration, and valid dimensions.
3. Normalize assembled output to H.264, `yuv420p`, 30 fps, and the requested canvas; core assembly intentionally omits audio.
4. Check `deliverables/final.mp4` and its recorded media metadata.
5. Run `qa <project-folder>`. Treat orientation/dimension errors as technical failures; review black/freeze warnings. Identity, wardrobe, hands, limbs, facial anatomy, and motion naturalness always require human or visual-model review.
6. Report failed or unresolved task IDs without exposing credentials.
7. Preserve `state.json`; it is the resume contract and contains no secrets.

Use `postprocess <input.mp4> <output.mp4>` for optional background music, voice-over, burned SRT subtitles, and fades. Use `cover <input.mp4> <cover.jpg>` to export a publishing cover. These commands cover lightweight delivery; use a dedicated editing skill for complex transitions, motion graphics, dialogue editing, or a full timeline.
