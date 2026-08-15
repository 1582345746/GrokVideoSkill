---
name: grok-video-studio
description: Plan and build resumable AI video projects with QuickAI image generation, QuickAI New Grok video generation, editable workflow templates, single-sheet character masters, dynamic shot planning, validated MP4 downloads, and arbitrary clip assembly. Use for text-to-video, image-to-video, single-image animation, character consistency, dance or comedy motion, product ads, scene animation, multi-shot narratives, short drama, screenplay or shot-list creation, batch generation, or requests such as 写剧本、生图、生视频、图生视频、分镜视频、角色一致性视频、人物三视图、短剧、批量生成视频 or 合并视频.
---

# Grok Video Studio

Create the creative plan with Codex. Use the bundled scripts for credentials, paid API calls, durable state, downloads, validation, and assembly.

## Setup

1. Run `python scripts/grok_video_studio.py configure` in an interactive terminal. Never request or pass API keys in chat or command-line arguments.
2. Run `python scripts/grok_video_studio.py doctor` and resolve both provider and FFmpeg checks before paid generation.
3. Keep the default direct providers. QuickAI handles images; QuickAI New handles videos. Do not route through the Canvas browser proxy unless the user explicitly requests a future bridge adapter.

Read [references/api-contracts.md](references/api-contracts.md) when diagnosing endpoints or provider responses. Read [references/error-matrix.md](references/error-matrix.md) when a request fails.

## Select A Workflow

When the user's goal is unclear, run `python scripts/grok_video_studio.py capabilities` and present the returned titles in the conversation. Let the user reply with a title or workflow ID. Do not use an OS popup. Do not force short drama or a fixed shot count.

Run `python scripts/grok_video_studio.py describe <workflow-id>` for the selected questions and prompt guidance. Workflow JSON files are editable under `assets/workflow-templates/`. Read [references/workflow-catalog.md](references/workflow-catalog.md) for the catalog and routing rules.

## Create A Project

1. Initialize a local project:

   `python scripts/grok_video_studio.py init <project-folder> --title "..." --topic "..." --workflow <id> --target-seconds <seconds>`

2. Let the CLI plan a variable number of shots from the target duration, or override with `--shots`. Every shot must be 1-15 seconds; no project is fixed to eight clips.
3. Fill `project.json`: story, concise identity and style bibles, and every shot's image and video prompts. Keep stable shot IDs.
4. Put user-supplied references under `assets/references/` and use only project-relative paths.
5. Run `python scripts/grok_video_studio.py preflight <project-folder>` before spending. Review request counts, total duration, prompt lengths, warnings, and errors.

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
- Inspect durable state with `status`.
- Assemble completed clips with `assemble`.
- Normalize and combine arbitrary existing clips with `assemble-files <output.mp4> <clip...>`.

Treat every image or video create request as billable. The script records an attempt before sending it and does not retry an ambiguous create failure. Use `--retry-failed` only after inspecting the provider and accepting possible duplicate billing. Polling and content download may retry safely.

Keep every final composed image and video prompt at or below 4096 characters. Treat 3800 characters as the working ceiling. The preflight and API clients enforce this; do not silently truncate creative instructions.

## Delivery Gate

1. Require the single-sheet character master when enabled and every requested keyframe when `generate_image` is true.
2. Require every video to have a `.mp4` filename, MP4 signature, readable video stream, positive duration, and valid dimensions.
3. Normalize assembled output to H.264, `yuv420p`, 30 fps, and the requested canvas; core assembly intentionally omits audio.
4. Check `deliverables/final.mp4` and its recorded media metadata.
5. Report failed or unresolved task IDs without exposing credentials.
6. Preserve `state.json`; it is the resume contract and contains no secrets.

Treat subtitles, voice-over, music, transitions, and complex timelines as optional post-production. Use FFmpeg or a dedicated editing skill only when the user asks for those deliverables.
