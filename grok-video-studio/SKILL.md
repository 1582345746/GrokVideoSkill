---
name: grok-video-studio
description: Plan and build resumable standalone, episodic, or sourced-news AI videos with QuickAI and QuickAI New generation, hash-bound pixel evidence, multidimensional visual classification, native or local dialogue, Voicebox/Qwen or CosyVoice TTS, optional MuseTalk lip sync, series continuity, news evidence, deterministic subtitles, clean-frame/audio QA, validated MP4, and FFmpeg editing with per-shot controls plus optional ChatCut or experimental Jianying handoff. Use for text-to-video, image-to-video, supplied-image animation, 真人/动漫/动画 type selection, character consistency, speaking characters, voice-over, voice auditions, lip sync, ads, multi-shot narratives, short drama, multi-episode series, current-news videos, subtitles, batch generation, editing, or requests such as 写剧本、生图、生视频、图生视频、人物讲话、角色配音、音色试听、口型同步、短剧、连续剧、多集视频、生成下一集、热点新闻视频、字幕、批量生成视频、剪辑、转场、特效、滤镜 or 合并视频.
---

# Grok Video Studio

Create the creative plan with Codex. Use the bundled scripts for credentials, paid API calls, durable state, visual evidence and profiling, voice auditions and approval, downloads, validation, dialogue, assembly, technical QA, and resumable editing. The installed CLI reports version `2.3.0` (`evidence-editor`).

The director layer applies to every video project: standalone T2V, I2V, supplied-image animation, short drama, ads, performance, sourced news, and episodic series. It compiles story beats into shot roles, camera coverage, performance progression, sound intent, cuttable exits, and edit windows. Episodic series add season canon and approval state; they do not own the director layer.

For v1 projects, read `references/v2.0-upstream-first-migration.zh-CN.md` before changing audio, subtitle, prompt, retry, or series fields.

For visual type selection, read [references/visual-profile-schema.md](references/visual-profile-schema.md). `photoreal` describes appearance only. Reserve `real-person` for supplied/captured actual-human identity with confirmed provenance, use `synthetic-human` for an AI-generated live-action-looking person, and use `human-like-fictional` for every near-real anime or CG character. For supplied pixels, run `visual-evidence`, inspect every returned frame with Codex multimodal vision, run `visual-review-record`, and then use `visual-review-apply --confirm`. Do not replace pixel review with the filename heuristic.

## Setup

1. When the user supplies provider keys in the installation conversation, Codex must perform configuration itself. Run `python scripts/grok_video_studio.py configure --credentials-stdin --skip-test` in a managed process and send exactly one JSON object through that process's stdin: `{"quickai_image_key":"...","quickai_video_key":"...","quickainew_video_key":"..."}`. Any unused role may be omitted, but at least one key is required. Legacy `quickai_key` and `quickainew_key` payloads remain supported. Do not ask the user to open PowerShell.
2. Never place keys in command-line arguments, temporary files, `SKILL.md`, project files, source control, or terminal output. On Windows, configuration stores them with DPAPI under the current user's local application-data directory. The installed Skill directory remains secret-free and updateable.
3. If the user has not supplied keys in the conversation, use the original hidden interactive `configure` flow instead of inventing credentials.
4. Run `python scripts/grok_video_studio.py doctor` and resolve the configured credential roles and FFmpeg checks before paid generation. A missing unused role is allowed. Skip `doctor` when the user asks to avoid all real upstream tests.
5. Confirm the requested video mode and any provider preference before creating a project. Text-to-video and image-to-video both default to QuickAI with safe QuickAI New fallback. If the user says “生视频选择 QuickAI New”, “use QuickAI New for video”, or equivalent, pass `--video-provider quickainew`; write `video_provider=quickainew` and `video_provider_policy=fixed`, and send both T2V and I2V directly to QuickAI New without first calling QuickAI. An explicit `--video-provider quickai` similarly fixes QuickAI. Use `--video-provider-policy automatic` only when the user wants the fallback chain. Ambiguous create failures and polling timeouts keep the original task and never trigger a second paid create. Do not route through the Canvas browser proxy unless the user explicitly requests a future bridge adapter.
6. Run `version` after installation. The repository root includes `install.ps1`; Codex can use `-ConfigureFromStdin -SkipProviderTest` to install and configure in one managed terminal session.
7. Before choosing optional services, run `python scripts/grok_video_studio.py install-plan --profile basic|upstream-dialogue|precise-subtitles|precise-voice|lip-sync`. This check has no machine side effects and reports missing FFmpeg, Docker, or NVIDIA prerequisites, key roles, model disk estimate (about 10 GB for precise voice and 17 GB for lip sync including safety margin), and consent requirements. The old names `core`, `native-dialogue`, `local-voice`, and `full-dialogue` are accepted as aliases.
8. Ask whether the user needs character speech and lip sync. New projects default to the `upstream-dialogue` profile (`audio.mode=native-dialogue`, `generate_audio=true`, `subtitle_source=none`); use `basic` explicitly for silent/source-audio work. `upstream-dialogue` adds no local dependency. `precise-subtitles` uses only local FFmpeg. `precise-voice` needs Docker, NVIDIA GPU support, CosyVoice, and model weights. `lip-sync` additionally needs MuseTalk. Never download models or build runtimes without explicit approval.
9. For an approved local profile, run `components-plan`, choose component source/model locations with the user, then run `components-configure`, `components-install --accept-downloads`, `components-setup --accept-downloads --include-models`, `components-start`, and `components-doctor`. Services bind to host loopback only. For `full-dialogue`, use `--component cosyvoice` and `--component musetalk` as sequential stages on 8 GB GPUs; use `--component all` only after confirming sufficient VRAM. Stop them with `components-stop` when not needed.
10. For multi-character preset voices, prefer Voicebox with Qwen CustomVoice. Run `voicebox-setup-plan --source <voicebox-repo> --models-root <E-drive-cache> --data-root <E-drive-data>` first; it is read-only. Do not create the Python environment, install dependencies, download the pinned model, start Voicebox, or generate auditions until the user approves those boundaries. Codex should perform the approved setup rather than asking the user to run each command.

11. Keep the frame layout contract explicit. New projects default to `frame_layout=single-full-frame`, `allow_multi_panel=false`, and `layout_risk_policy=block`. A single-full-frame shot is one continuous physical camera view covering the whole canvas. `split-screen`, `triptych`, and `comic-panel` require both an explicit project/shot override and `allow_multi_panel=true`; use them only for a deliberate montage, comparison, phone-call, surveillance, or graphic-design shot. They are never a generic style keyword. A new vertical multi-character T2V single-frame shot is blocked before paid generation because the tested gateways frequently return repeated horizontal panels; route it through an approved single-frame keyframe and I2V, or explicitly accept the risk with `layout_risk_policy=allow`.

`components-setup --include-models` is resumable. It performs a disk-space preflight, reuses a complete model directory without starting Docker, records a per-model state file under the configured models root, and verifies required files with SHA-256 before reuse. A partial or corrupted model is downloaded again; `components-start` refuses to launch when required model files are missing or empty.

## Distribution Modes

Codex-managed installation is the default: copy the Skill, run `install-plan`, send provider keys once through `configure --credentials-stdin`, and run `doctor`. For optional local services, Codex must show the component plan and request approval before any Git checkout, Docker build, model download, or service start.

The repository also contains a transparent standalone PowerShell installer:

```powershell
.\install.ps1 -Force -Interactive
```

The wizard asks for a capability profile, prompts for keys through the CLI's hidden input, and asks separately before installing system dependencies or downloading optional services and models. For automation, use `-InstallProfile basic|upstream-dialogue|precise-subtitles|precise-voice|lip-sync`; approved system package installation additionally requires `-InstallSystemDependencies -AcceptSystemDependencyChanges`, while local model downloads require `-InstallComponents -IncludeComponentModels -AcceptComponentDownloads`. `-ConfigureFromStdin` remains the non-echoed JSON path for managed Codex installs.

The selected profile is saved as non-secret metadata in the per-user config directory and is exposed by `capabilities`. Explicit `--audio-mode` and `--subtitle-source` values override it; otherwise new projects inherit the saved profile and fall back to native dialogue with no subtitle delivery.

Use `install.ps1 -Check` to verify an existing installation without changing it, `install.ps1 -Repair -Force` to replace a damaged copy, and `install.ps1 -Uninstall` to remove only the Skill directory. Upgrades move the previous Skill directory to a timestamped sibling backup before copying; a failed copy restores the previous directory. When no `-InstallProfile` is supplied, an upgrade or repair preserves the saved profile. Credentials, projects, component checkouts, and model weights are never removed by uninstall.

Read [references/api-contracts.md](references/api-contracts.md) when diagnosing endpoints or provider responses. Read [references/error-matrix.md](references/error-matrix.md) when a request fails. Read [references/workflow-catalog.md](references/workflow-catalog.md) before adding or changing a preset.

## Director And Layout Gate

Use a director mode whenever the request contains a story, character performance, dialogue exchange, action, or a requested visual rhythm. `single-shot` remains appropriate for one simple clip. `cinematic-short`, `dialogue-scene`, `silent-cinema`, `action-scene`, `comedy-scene`, `product-ad`, `performance`, `montage`, and `news-report` provide structured coverage rules. Add genre packs such as `historical`, `wuxia`, `sci-fi`, `family`, `romance`, `comedy`, `disaster`, `rural`, or `suspense` for domain-specific guidance.

Each narrative shot should state one visible event, one `shot_role`, one main continuous action, its camera and motivated movement, the environment motion and sound, the performance progression, and how it exits into the next cut. Avoid writing every shot as a complete mini-scene or requiring a final pose. `edit_in`, `edit_out`, and `timeline_duration` let the generator create a little extra material while the editor keeps only the natural middle and cuts before a sigh, freeze, or generic ending. QA treats pending visual review as incomplete and reports layout, UI, caption, identity, anatomy, motion, and audio checks separately.

For two-person dialogue, prefer establishing/wide coverage, over-shoulder or single-speaker closeups, the listener's reaction, and a cuttable two-shot. For action, split discovery, strike, dodge, impact, reaction, and escape into separate shots. For a silent scene, use environment and object inserts instead of filling every shot with dialogue. Three-panel imagery is a special shot design, not the default solution for any of these patterns.

## Select A Workflow

When the user's goal is unclear, run `python scripts/grok_video_studio.py capabilities` and present the returned titles in the conversation. Let the user reply with a title or workflow ID. Do not use an OS popup. Do not force short drama or a fixed shot count.

Run `python scripts/grok_video_studio.py describe <workflow-id>` for the selected questions and prompt guidance. Workflow JSON files are editable under `assets/workflow-templates/`. Read [references/workflow-catalog.md](references/workflow-catalog.md) for the catalog and routing rules.

First choose one product route:

- Text-to-video: use a standard project with `video_mode=text-to-video` and no image references.
- Image-to-video: use a standard project with `video_mode=image-to-video`. A supplied image, a generated keyframe, and a character-master-derived keyframe are variants of the same product route. `single-image-animation` is only an internal I2V preset, not a separate product entry.
- Episodic series: use `series-init` only for ordered episodes that share canon or continuity. Every episode remains a standard T2V or I2V project internally.
- Sourced news video: use `news-init`. Codex performs current web research, records sources and claim mappings, and then reuses the standard T2V or I2V pipeline.

Then choose one audio mode and subtitle source:

- `preserve`: keep provider/source audio. This does not ask the model to create spoken dialogue.
- `mute`: intentional silent delivery.
- `native-dialogue`: send exact dialogue and `generate_audio=true` to the video provider. This is fastest and requires no local model, but speech wording, voice, baked captions, and lip sync remain generative and require human review.
- `local-voice`: generate each approved line with the character's `voice.provider` (Voicebox or CosyVoice), fit it to the declared timeline, preserve/duck source audio, normalize loudness, export deterministic SRT, and keep a resumable `dialogue-state.json`.
- `local-lipsync`: perform `local-voice`, then send the mixed video and dialogue track to the localhost MuseTalk wrapper. Use this only when mouth synchronization is required.

`audio.subtitle_source` is independent from the audio mode. `upstream` preserves any captions rendered by the provider or supplied source and creates no local SRT; `project` derives deterministic subtitles from dialogue, timed cues, narration, or news segments; `none` intentionally delivers no subtitle sidecar or burn. The CLI accepts `--source auto|upstream|project|none` on `subtitles` and the same choice on `dialogue-render`.

For exact wording or a clean subtitle-free master, recommend `local-voice` or `local-lipsync`; native generation may ignore clean-frame instructions and burn dialogue text into pixels. Read [references/dialogue-and-components.md](references/dialogue-and-components.md) before editing voices or installing local services.

Before rendering local dialogue, keep voice selection as its own approval stage:

1. Run `voice-list --provider voicebox --engine qwen_custom_voice --service-url http://127.0.0.1:17493`.
2. Generate only review candidates with `voice-audition <workspace> <character-id> ...`; do not generate a season's dialogue during casting.
3. Present the WAV and its technical QA to the user. Use `voice-approve` or `voice-reject` only from the user's decision.
4. For a series, run `series-voice-sync`; it copies only approved voices. `temporary-test` requires `audio.allow_temporary_voices=true` and must not be described as final casting.
5. Run project/episode preflight again. A draft, auditioned, rejected, missing, unauthorized, or accidentally duplicated voice blocks local dialogue and episode approval.

`voice-catalog.json` is the durable casting record. Cache signatures include provider, model revision, voice identity or reference hash, text, seed, and performance controls. Changing one character's voice invalidates only that character's dialogue and downstream lip-sync output; it does not regenerate images, clean clips, or subtitle text.

## Create A Project

1. Initialize a local project:

   `python scripts/grok_video_studio.py init <project-folder> --title "..." --topic "..." --workflow <id> --target-seconds <seconds> --mode text-to-video --video-resolution 480p`

   Add `--video-provider quickai|quickainew` when the user explicitly chooses a video upstream; explicit selection is fixed by default. Add `--video-provider-policy automatic` only for a requested fallback chain. Add `--install-profile precise-subtitles|precise-voice|lip-sync` when the project should inherit the selected audio and subtitle defaults from an installation capability profile. Explicit `--audio-mode` and `--subtitle-source` values take precedence.

2. Let the CLI plan a variable number of shots from the target duration, or override with `--shots`. Every shot must be 1-15 seconds; no project is fixed to eight clips.
3. Fill `project.json`: story, concise identity and style bibles, and every shot's image/video prompts. Add a `shot_role` (`establishing`, `wide`, `medium`, `closeup`, `over_shoulder`, `insert`, `reaction`, `transition`, or `ending_hook`) plus location, time, weather, lighting, props, camera motion, environment motion, and ending pose where relevant. For speech, add stable character voice data and timed `dialogue` lines; the dialogue text is authoritative only when `subtitle_source=project`. Keep stable shot and line IDs.
4. Put user-supplied references under `assets/references/` and use only project-relative paths.
5. Resolve the visual contract before spending: use `--visual-medium auto|photoreal|2d-anime|3d-anime|stylized-3d|motion-graphics|hybrid` on `init`, or run `visual-profile` and then persist a confirmed manual profile. Review `subject_nature`; do not equate photoreal rendering with a real person.
   - For supplied images or videos, run `visual-evidence <project> <media...> --frames 5`. This writes normalized JPG evidence, SHA-256 values, a manifest, and a review template under `deliverables/visual-evidence/`; it does not call a paid upstream.
   - Codex must inspect the returned frame paths, not only filenames or folder labels. Record the observed axes with `visual-review-record <manifest> --reviewer-id <id> ... --frame-evidence frame-01="..." --confirm`.
   - Apply only an accepted receipt with `visual-review-apply <project> <manifest> <receipt> --confirm`. Changed evidence frames, a changed manifest, low confidence, or missing actual-person provenance blocks application and falls back to explicit review.
   - Pixel appearance cannot prove actual identity. `real-person` requires a confirmed `captured-real-person` provenance statement; near-real anime/CG remains `human-like-fictional`, and live-action-looking generated people use `synthetic-human`.
6. Run `python scripts/grok_video_studio.py preflight <project-folder>` before spending. Review request counts, total duration, prompt lengths, visual profile confidence, warnings, and errors.
7. Set `budget.image_request`, `budget.video_request`, and `budget.max_estimated_cost` when a hard project spending ceiling is required. Every attempted paid request is recorded in `state.json` before it is sent. The default retry policy is three total attempts (initial request plus two retries); provider failover counts toward that total. A `submission_unknown` create is never recreated automatically.
8. Run `audit` to review structured character IDs, wardrobe changes, adjacent-shot continuity notes, visual medium, and the manual review checklist.

Read [references/project-schema.md](references/project-schema.md) before editing `project.json`. Read [references/prompt-contract.md](references/prompt-contract.md) before writing prompts.

The editable preset catalog currently contains `general-video`, `text-to-video`, `single-image-animation`, `character-consistent-story`, `short-drama`, `cinematic-short`, `dialogue-scene`, `silent-cinema`, `action-scene`, `product-ad`, `dance-performance`, `performance`, `comedy-action`, `comedy-scene`, `scene-animation`, and `news-video`. These IDs are stable internal templates; they do not add new provider capabilities or product routes. Custom workflows may extend a built-in template from the per-user workflow directory; see `docs/custom-workflow-development.zh-CN.md`.

## Create An Episodic Series

1. Initialize the series and all episode skeletons without spending:

   `python scripts/grok_video_studio.py series-init <series-folder> --title "..." --premise "..." --episodes 20 --episode-seconds 90 --mode image-to-video`

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
- Resume existing task IDs with `resume`. A normal resume must not create a second paid task. If repeated polling has timed out and the user explicitly authorizes replacement, use `resume <project> --shot <id> --replace-lost-task --retry-failed --retry-reason "..."`; this first checks the known task's status and content endpoints. It recovers existing content when available and creates a replacement only when both checks confirm `404`, while preserving the old task ID in history.
- Add repeatable `--shot <shot-id>` to process selected shots. Partial `run` operations do not auto-assemble.
- Review one completed asset with `review-shot <project> <shot-id> --kind image|video --decision approve|reject --notes "..."`. Approved images are hash-locked in state. Rejected assets are preserved, and replacement generation requires `--retry-failed --retry-reason "..."` so the additional paid request is explicit.
- Add `--progress` to generation or resume commands to receive JSONL progress events on stderr while the final JSON result remains on stdout.
- Inspect durable state with `status`.
- Assemble completed clips with `assemble`.
- Normalize and combine arbitrary existing clips with `assemble-files <output.mp4> <clip...>`.
- Create and validate a resumable edit contract with `edit-plan <project>` and `edit-validate <project>`.
- Edit-plan v2 supports `--shot-filter SHOT=PRESET`, `--shot-speed SHOT=RATE`, and `--boundary AFTER_SHOT=TYPE:SECONDS`, plus `--normalize-lufs` or `--no-normalize`. It reads v1 plans through an in-memory compatibility migration.
- Render native FFmpeg editing with `edit <project>`. This creates `deliverables/final-edited.mp4`, exports hash-bound boundary preview frames under `deliverables/edit-preview/`, and preserves the clean `deliverables/final.mp4`; read [references/editing-backends.md](references/editing-backends.md) before choosing ChatCut or Jianying.

Treat every image or video create request as billable. The script records an attempt before sending it and does not retry an ambiguous create failure. `--retry-failed` requires `--retry-reason "..."`; the reason and prior task state are preserved in history. Polling, model discovery, and content download may retry safely with bounded backoff and a circuit breaker.

Keep every final composed image, video, and character-master prompt at or below 4096 UTF-8 bytes. Treat 3800 bytes as the working ceiling. Preflight reports characters, UTF-8 bytes, remaining space, full/compact/minimal versions, and a compression suggestion. Generation selects a complete variant and never silently truncates creative instructions; if the minimal variant still exceeds the hard limit, it blocks with the exact byte budget.

Projects default to a clean frame (`allow_ui_elements=false`): generated footage must not contain accidental app controls, counters, comments, captions, logos, watermarks, or stickers. Set the project or shot override to `true` only when the script intentionally depicts an interface, then review that shot visually.

Do not treat a present audio stream as proof of audible speech. QA measures sample rate, channels, duration, mean/peak volume, and silence ratio. For native dialogue, inspect the video and listen to the line; use optional ASR only as a review signal, never as the source of approved text. For local dialogue, run `dialogue-render <project> [--burn-subtitles]` after clean video assembly. Never store unlicensed voice samples; `reference_audio` requires `consent=synthetic`, `owned`, or `licensed` plus the exact `reference_text`. Never clone public figures or third parties without specific rights.

Video contracts are explicit in `project.json`: `video_mode` is `text-to-video` or `image-to-video`; `video_provider` is the preferred provider (`quickai` by default, or explicitly fixed `quickainew`); resolution is `480p`, `720p`, or `1080p`; aspect ratio is provider-supported. Both QuickAI and QuickAI New advertise T2V and I2V. QuickAI reports model-default audio generation; QuickAI New receives explicit `generate_audio`. Neither currently supports video reference/edit/extend or preset/file audio reference. T2V never sends reference images, even when an old keyframe exists in state. I2V sends only explicit image references or the current shot keyframe. MP4/WAV references are blocked and reserved for future `video-edit`, `video-extend`, `preset_voice_reference`, and `audio_file_reference` routes. `state.json` records a stable request ID, separate attempt IDs, provider task IDs, sanitized failure categories, the complete provider attempt history, and the final provider.

## Delivery Gate

1. Require the single-sheet character master when enabled and every requested keyframe when `generate_image` is true.
2. Require every video to have a `.mp4` filename, MP4 signature, readable video stream, positive duration, and valid dimensions.
3. Normalize assembled output to H.264, `yuv420p`, 30 fps, and the requested canvas. Assembly preserves source audio by default and inserts silent AAC for clips without audio; set `defaults.audio_policy` to `mute` only for an intentional silent delivery.
4. Check `deliverables/final.mp4` and its recorded media metadata.
5. Run `qa <project-folder>`. Treat orientation errors as technical failures. A provider may return a smaller frame with the requested aspect ratio; report that as a scaling warning. For image-to-video, `preflight` lists each still's format, dimensions, target ratio, and clarity warnings. The QuickAI adapter center-crops valid stills to a temporary JPEG request image because the current gateway rejects PNG data URIs; source files remain unchanged. Review black/freeze/audio warnings. Open every image listed under `review_frames`, including the end frame of every shot; reject unintended UI, captions, logos, watermarks, identity drift, anatomy defects, and unnatural motion before delivery. Technical QA never marks this visual review complete automatically.
6. Report failed or unresolved task IDs without exposing credentials.
7. Preserve `state.json`; it is the resume contract and contains no secrets.

Use `postprocess <input.mp4> <output.mp4>` for one-off background music, voice-over, burned SRT subtitles, and fades. For repeatable transitions, filters, clip windows, audio mix, and a full timeline, use `edit-plan` plus native `edit`. ChatCut is optional and task-scoped; Jianying draft export is experimental and never the automatic fallback. The edited output is separate from the clean master.

Use `subtitles <project-folder>` to export `deliverables/subtitles.srt`. Timed dialogue is preferred, then explicit subtitle cues, a shot's `subtitle`/`narration`, or news narration. Add `--burn --style clean|cinematic|news` to create `deliverables/final-subtitled.mp4` with local FFmpeg. For `native-dialogue`, first inspect the source for provider-baked captions; burning is blocked until `--confirm-source-clean` is supplied. This always preserves the clean `final.mp4`; a rejected subtitle design can be re-burned or omitted without another provider request. Never ask the generative video model to draw ordinary subtitles. `dialogue-render` reuses the declared dialogue windows for exact TTS/SRT alignment.
