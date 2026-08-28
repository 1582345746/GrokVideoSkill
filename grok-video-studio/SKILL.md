---
name: grok-video-studio
description: Plan and build resumable standalone, episodic, or sourced-news AI video projects with QuickAI and QuickAI New image/video generation, series bibles, per-episode approval and continuity state, reusable character masters, current-news evidence contracts, deterministic subtitles, editable workflow templates, clean-frame review, audio-preserving assembly, validated MP4 downloads, and lightweight post-production. Use for text-to-video, image-to-video, supplied-image animation, character consistency, product ads, scene animation, multi-shot narratives, short drama, multi-episode series, current hot-news videos, screenplay or shot-list creation, subtitles, batch generation, or requests such as 写剧本、生图、生视频、图生视频、图片生成视频、分镜视频、角色一致性视频、人物三视图、短剧、连续剧、多集视频、生成下一集、热点新闻视频、新闻视频、字幕、批量生成视频 or 合并视频.
---

# Grok Video Studio

Create the creative plan with Codex. Use the bundled scripts for credentials, paid API calls, durable state, downloads, validation, assembly, technical QA, and lightweight post-production. The installed CLI reports version `1.5.0`.

## Setup

1. When the user supplies provider keys in the installation conversation, Codex must perform configuration itself. Run `python scripts/grok_video_studio.py configure --credentials-stdin --skip-test` in a managed process and send exactly one JSON object through that process's stdin: `{"quickai_image_key":"...","quickai_video_key":"...","quickainew_video_key":"..."}`. Any unused role may be omitted, but at least one key is required. Legacy `quickai_key` and `quickainew_key` payloads remain supported. Do not ask the user to open PowerShell.
2. Never place keys in command-line arguments, temporary files, `SKILL.md`, project files, source control, or terminal output. On Windows, configuration stores them with DPAPI under the current user's local application-data directory. The installed Skill directory remains secret-free and updateable.
3. If the user has not supplied keys in the conversation, use the original hidden interactive `configure` flow instead of inventing credentials.
4. Run `python scripts/grok_video_studio.py doctor` and resolve the configured credential roles and FFmpeg checks before paid generation. A missing unused role is allowed. Skip `doctor` when the user asks to avoid all real upstream tests.
5. Confirm the requested video mode before creating a project. Defaults are text-to-video -> QuickAI JSON and image-to-video -> QuickAI New multipart. A project-level `video_provider` may explicitly override this route when the corresponding key is configured. Do not route through the Canvas browser proxy unless the user explicitly requests a future bridge adapter.
6. Run `version` after installation. The repository root includes `install.ps1`; Codex can use `-ConfigureFromStdin -SkipProviderTest` to install and configure in one managed terminal session.

Read [references/api-contracts.md](references/api-contracts.md) when diagnosing endpoints or provider responses. Read [references/error-matrix.md](references/error-matrix.md) when a request fails.

## Select A Workflow

When the user's goal is unclear, run `python scripts/grok_video_studio.py capabilities` and present the returned titles in the conversation. Let the user reply with a title or workflow ID. Do not use an OS popup. Do not force short drama or a fixed shot count.

Run `python scripts/grok_video_studio.py describe <workflow-id>` for the selected questions and prompt guidance. Workflow JSON files are editable under `assets/workflow-templates/`. Read [references/workflow-catalog.md](references/workflow-catalog.md) for the catalog and routing rules.

First choose one product route:

- Text-to-video: use a standard project with `video_mode=text-to-video` and no image references.
- Image-to-video: use a standard project with `video_mode=image-to-video`. A supplied image, a generated keyframe, and a character-master-derived keyframe are variants of the same product route. `single-image-animation` is only an internal I2V preset, not a separate product entry.
- Episodic series: use `series-init` only for ordered episodes that share canon or continuity. Every episode remains a standard T2V or I2V project internally.
- Sourced news video: use `news-init`. Codex performs current web research, records sources and claim mappings, and then reuses the standard T2V or I2V pipeline.

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

## Create An Episodic Series

1. Initialize the series and all episode skeletons without spending:

   `python scripts/grok_video_studio.py series-init <series-folder> --title "..." --premise "..." --episodes 20 --episode-seconds 90 --mode image-to-video --video-provider quickainew`

2. Fill `series.json` with the premise, season arc, stable style, characters, locations, props, and every episode title, synopsis, starting state, and intended ending. Then fill every `episodes/ep-NNN/project.json` story and shot prompt. Let the user review these creative files before any paid request.
3. For identity-critical I2V, run `series-generate-characters`. It generates one persistent single-sheet master for every enabled character and synchronizes the same reference into each episode. T2V series do not require image masters unless explicitly enabled.
4. Run `series-preflight --episode ep-001`, then `series-approve ep-001`. Only approved episodes can generate, and a later episode cannot be approved before earlier episodes are accepted.
5. Run `series-run --episode ep-001` or `series-run --next`. Generation stops at `needs_review`; it does not silently generate the rest of the season.
6. Review the final video and all exported review frames. Run `series-accept ep-001 --continuity-summary "..."` only after visual review. Record the actual visible end state, not merely the planned ending.
7. For “生成下一集”, run `series-next`, then `series-context` to load the season outline, previous accepted summaries and artifacts, and the current full project. Preflight and approve the returned episode before `series-run --next`.

Read [references/series-schema.md](references/series-schema.md) before editing `series.json` or managing episode lifecycle state.

## Create A Sourced News Video

1. Run `news-init <project-folder> --title "..." --topic "..." --window-hours 24 --target-seconds 60`. This creates a standard video project plus `news.json` and makes no paid request.
2. Browse the current web. When the user asks for automatic hotspots, choose a recent, relevant topic that can be supported by exact primary/authoritative pages and independent reporting. Compare the event date with publication dates.
3. Fill `news.json` with actual search queries, exact HTTPS source pages, publishers, publication/access times, source types, visual-rights status, atomic claims, and per-shot narration/claim mappings. Use at least two distinct publishers.
4. Do not copy source images or footage when `visual_rights=facts-only`. Generate original explanatory visuals, and never represent an AI reconstruction as authentic event footage.
5. Set `editorial.status=verified` only after resolving source conflicts. Run `news-validate`; standard generation remains blocked until it passes.
6. Fill the standard `project.json` story and prompts from the verified script, then run `preflight` and `run` normally. Preserve `news.json` with the delivery as its evidence manifest.

Read [references/news-schema.md](references/news-schema.md) before researching or writing a news video.

## Use A Character Master

For identity-critical standalone character workflows:

1. Generate one character master image containing front, side, and back or full-body views of the same character on one canvas. Do not generate separate view files.
2. Use that single sheet as an image reference to derive each shot's scene keyframe.
3. Send only the current shot keyframe to image-to-video. Never send the multi-view master sheet directly to the video model.

Run `generate-character` independently or let `run` create it before shot keyframes. Preserve the generated master path in `state.json`.

For episodic I2V, define multiple characters under `series.json.characters` and use `series-generate-characters`. A shot's `character_ids` automatically selects those characters' copied masters as keyframe image-edit references. The current keyframe alone is sent to the video provider. Prompt-only T2V can preserve textual identity locks but cannot guarantee strict identity over many episodes.

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

Keep every final composed image and video prompt at or below 4096 characters. This is the tested QuickAI boundary inclusive; treat 3800 characters as the working ceiling. Preflight reports the final length and remaining budget, and the preflight/API clients enforce the hard limit without silently truncating creative instructions.

Projects default to a clean frame (`allow_ui_elements=false`): generated footage must not contain accidental app controls, counters, comments, captions, logos, watermarks, or stickers. Set the project or shot override to `true` only when the script intentionally depicts an interface, then review that shot visually.

Video contracts are explicit in `project.json`: `video_mode` is `text-to-video` or `image-to-video`; `video_provider` is `quickai` or `quickainew`; resolution is `480p`, `720p`, or `1080p`; aspect ratio is provider-supported. T2V never sends reference images, even when an old keyframe exists in state. I2V sends only explicit references or the current shot keyframe.

## Delivery Gate

1. Require the single-sheet character master when enabled and every requested keyframe when `generate_image` is true.
2. Require every video to have a `.mp4` filename, MP4 signature, readable video stream, positive duration, and valid dimensions.
3. Normalize assembled output to H.264, `yuv420p`, 30 fps, and the requested canvas. Assembly preserves source audio by default and inserts silent AAC for clips without audio; set `defaults.audio_policy` to `mute` only for an intentional silent delivery.
4. Check `deliverables/final.mp4` and its recorded media metadata.
5. Run `qa <project-folder>`. Treat orientation/dimension errors as technical failures and review black/freeze warnings. Open every image listed under `review_frames`, including the end frame of every shot; reject unintended UI, captions, logos, watermarks, identity drift, anatomy defects, and unnatural motion before delivery. Technical QA never marks this visual review complete automatically.
6. Report failed or unresolved task IDs without exposing credentials.
7. Preserve `state.json`; it is the resume contract and contains no secrets.

Use `postprocess <input.mp4> <output.mp4>` for optional background music, voice-over, burned SRT subtitles, and fades. Use `cover <input.mp4> <cover.jpg>` to export a publishing cover. These commands cover lightweight delivery; use a dedicated editing skill for complex transitions, motion graphics, dialogue editing, or a full timeline.

Use `subtitles <project-folder>` to export `deliverables/subtitles.srt` from explicit per-shot subtitle cues, a shot's `subtitle`/`narration`, or news narration. Add `--burn` to create `deliverables/final-subtitled.mp4` with local FFmpeg. This always preserves the clean `final.mp4`; never ask the generative video model to draw ordinary subtitles. For exact word-level alignment to synthesized speech, create timing from the eventual TTS/audio provider before burning.
