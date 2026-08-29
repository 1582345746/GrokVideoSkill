# Project Contract

`project.json` is the creative input. `state.json` is script-owned runtime state. Never put credentials in either file.

```json
{
  "version": 1,
  "title": "Example",
  "topic": "Story goal",
  "workflow": "character-consistent-story",
  "workflow_title": "角色一致性故事",
  "workflow_guidance": {},
  "video_mode": "image-to-video",
  "video_provider": "quickai",
  "video_provider_policy": "automatic",
  "target_duration_seconds": 18,
  "story": "Short screenplay",
  "character_bible": "Concise stable identity, clothing, and props",
  "style_bible": "Concise stable visual language",
  "characters": [{
    "id": "lead",
    "name": "Lead",
    "identity": "Stable face, hair, age, and body description",
    "wardrobe": "Canonical wardrobe",
    "references": [],
    "voice": {
      "provider": "cosyvoice",
      "voice_id": "optional model speaker",
      "reference_audio": "assets/voices/lead.wav",
      "reference_text": "Exact transcript of the owned reference",
      "consent": "synthetic"
    }
  }],
  "audio": {
    "mode": "native-dialogue",
    "language": "zh-CN",
    "generate_audio": true,
    "preserve_source_audio": true,
    "duck_source_audio": true,
    "subtitle_source": "none"
  },
  "character_master": {
    "enabled": true,
    "mode": "single-sheet",
    "generate": true,
    "path": "assets/references/character-master.png",
    "prompt": "One turnaround sheet containing front, side, and back views of the same character",
    "source_references": [],
    "image_size": "1024x1024",
    "image_quality": "auto"
  },
  "defaults": {
    "image_size": "1536x1024",
    "image_quality": "auto",
    "video_size": "1280x720",
    "video_seconds": 6,
    "video_resolution": "480p",
    "video_aspect_ratio": "16:9",
    "audio_policy": "preserve"
  },
  "allow_ui_elements": false,
  "limits": {
    "max_image_requests": 12,
    "max_video_requests": 8,
    "max_total_video_seconds": 60,
    "max_reference_images": 9,
    "max_prompt_bytes": 4096
  },
  "retry_policy": {
    "max_total_attempts": 3,
    "max_retries": 2,
    "counts_provider_failover": true
  },
  "budget": {
    "currency": "CNY",
    "image_request": 0.0,
    "video_request": 0.0,
    "max_estimated_cost": null
  },
  "shots": [{
    "id": "shot-001",
    "summary": "What happens",
    "shot_role": "medium",
    "scene_id": "office",
    "location": "Office",
    "time": "late afternoon",
    "weather": "clear",
    "lighting": "soft window light",
    "props": ["phone"],
    "camera": "medium shot",
    "camera_motion": "slow push-in",
    "environment_motion": "curtains move gently",
    "ending_pose": "hands resting at the desk",
    "environment_sound": "quiet room tone",
    "sound_effects": [],
    "character_ids": ["lead"],
    "continuity_notes": "Keep wardrobe, eyeline, lighting, and prop position from the previous shot",
    "narration": "",
    "subtitle": "",
    "dialogue": [{
      "id": "line-001",
      "speaker": "lead",
      "text": "Authoritative spoken and subtitle text",
      "start": 0.2,
      "end": 2.8,
      "emotion": "calm",
      "subtitle": true,
      "lip_sync": true
    }],
    "wardrobe": {},
    "continuity_change": false,
    "image_prompt": "One still keyframe",
    "video_prompt": "One continuous motion",
    "generate_image": true,
    "use_character_master": true,
    "image_references": [],
    "video_references": [],
    "image_size": "1536x1024",
    "video_size": "1280x720",
    "video_resolution": "480p",
    "video_aspect_ratio": "16:9",
    "seconds": 6
  }]
}
```

## Character master

`mode` must be `single-sheet`. The file contains all character views on one canvas and counts as one image request and one image reference. When `generate` is false, `path` must already exist. When `generate` is true, `generate-character` writes the actual output path to `state.json`.

`use_character_master` prepends the single sheet to a shot's image references. It does not add that sheet to video references. When `video_references` is empty and a generated keyframe exists, only that keyframe becomes the video reference.

Every character selected by `shot.character_ids` also contributes its `characters[].references` to keyframe image generation automatically. This supports multiple reusable character masters. Explicit `shot.image_references` are added after the selected character references, and the combined unique reference count must stay within `max_reference_images`.

## Limits and state

Every clip is 1-15 seconds. Final composed image, video, and character-master prompts cannot exceed 4096 UTF-8 bytes; 3800 is the recommended working ceiling. Preflight exposes full/compact/minimal versions and remaining byte space. Limits are hard preflight gates and request counts include a generated character master.

`video_mode` must be `text-to-video` or `image-to-video`; `video_provider` must be `quickai` or `quickainew`; `video_provider_policy` must be `automatic` or `fixed`. New projects default to QuickAI plus `automatic`. Supplying `--video-provider quickai|quickainew` is an explicit user selection and defaults the policy to `fixed`; `--video-provider-policy automatic` opts back into a fallback chain. When automatic QuickAI safely fails and the QuickAI New video key is configured, the runtime records a separate provider attempt and can continue with QuickAI New. Resolution is limited to `480p`, `720p`, and `1080p`; aspect ratio is limited to `16:9`, `9:16`, `1:1`, `4:3`, `3:4`, `2:3`, or `3:2`.

New project keyframes follow the video orientation: `16:9`/`4:3`/`3:2` use `1536x1024`, `9:16`/`3:4`/`2:3` use `1024x1536`, and `1:1` uses `1024x1024`. Character master sheets remain square. Preflight warns when a generated keyframe's orientation conflicts with its target video aspect ratio because that mismatch increases cropping and composition drift.

`defaults.audio_policy` is `preserve` by default. Assembly preserves source audio and inserts silent AAC audio for clips that have no audio, keeping the final timeline stream-compatible. Set it to `mute` only when a silent delivery is intentional. `allow_ui_elements` defaults to `false`; a shot may set it to `true` only when the script explicitly shows an app or social-video interface. The clean-frame rule is a prompt constraint and still requires visual QA because an upstream model can hallucinate overlays.

`audio.mode` is `preserve`, `mute`, `native-dialogue`, `local-voice`, or `local-lipsync`. Native dialogue requires `generate_audio=true`; local modes require it to be false. Local modes require a character voice with either `voice_id` or a consented project-relative `reference_audio`. Reference audio also requires its exact `reference_text` and `consent=synthetic|owned|licensed`.

`audio.subtitle_source` is `upstream`, `project`, or `none`. `upstream` preserves provider/source caption pixels and does not create local SRT; `project` uses the dialogue/cue contract for deterministic SRT and optional FFmpeg burn; `none` suppresses subtitle delivery. New projects default to `none`; older projects without this field retain `project` for backwards compatibility. This setting is independent from `audio.mode`.

`retry_policy.max_total_attempts` defaults to three total billable attempts (initial request plus two retries). `max_retries` must not imply more attempts than this total; configure four total attempts explicitly when three retries are required. Provider failover consumes one of the same attempts. `submission_unknown` is never recreated automatically.

Video reference/edit/extend and audio reference are separate future capabilities. Current `image_references` and `video_references` accept still images only; MP4/WAV files are blocked during validation rather than silently ignored.

New projects can inherit this pair from an installation profile with `init`, `series-init`, or `news-init --install-profile <profile>`. The profile is only a default; explicit project flags override it, and changing the installation profile never rewrites existing projects.

Runtime states include `pending`, `submitting`, `queued`, `in_progress`, `completed`, `failed`, `submission_unknown`, and `poll_timeout`. A task ID is sufficient to resume polling without another create request. `request_id` stays stable across safe provider fallback, while every billable write has a distinct `attempt_id` under `provider_attempts`. Paths must be project-relative and stay inside the project.

`review-shot` records the reviewed file SHA-256, decision, notes, and timestamp. An approved image is marked `locked=true`; a rejected image or video keeps its original asset and task metadata but moves to a failed review state. Regeneration then requires the normal explicit retry reason.

## Narration and subtitles

`narration` and `subtitle` are optional strings. `subtitle` is a one-cue shorthand covering most of the shot. For precise timing, use `subtitles` and provide non-overlapping `start`/`end` seconds relative to that shot; do not use `subtitle` and `subtitles` together. Each cue must fit within the shot duration.

The `subtitles` command prefers precise cues, then `subtitle`, then `narration`, then sourced-news narration. It exports UTF-8 SRT. With `--burn`, local FFmpeg creates `final-subtitled.mp4` while preserving clean `final.mp4`. Native-dialogue sources must first be visually checked for provider-baked captions; the CLI requires `--confirm-source-clean` before burning another subtitle layer.

When a shot has `dialogue`, it cannot also use `subtitle` or `subtitles`. Dialogue lines are preferred for SRT and define the TTS/mix timeline. IDs are unique project-wide, speakers reference known characters, lines do not overlap, and each `0 <= start < end <= shot.seconds`. `dialogue-render` keeps generated line assets under `assets/dialogue/` and resumable signatures in `dialogue-state.json`.

## Characters, continuity, and budget

`characters` is optional and supports more than one named character. A shot selects participants with `character_ids`; their identity and canonical wardrobe are injected into both keyframe and motion prompts. `scene_id` and `continuity_notes` make adjacent-shot continuity auditable. Set `continuity_change` to true when a deliberate wardrobe or scene discontinuity is part of the story.

Series-managed episode projects may also contain `series_context`. The CLI owns this object and synchronizes the series ID, episode number, declared starting state, previous accepted episode summary, and intended ending. The previous accepted summary and current starting state are injected into composed prompts. Do not store credentials in this object.

Budget rates are operator estimates, not provider invoices. When `max_estimated_cost` is non-null, preflight rejects an over-budget plan and runtime blocks the next create request before its attempt would exceed the ceiling. `state.json.budget_usage` counts actual create attempts, including explicitly authorized retries.
