# Series Contract

Use a series project only for ordered episodes that share characters, locations, props, style, or story continuity. A standalone clip, a one-image animation, and a standalone multi-shot story remain standard `project.json` projects.

## Directory layout

```text
series-root/
  series.json
  series-state.json
  assets/
    character-masters/
    references/
  episodes/
    ep-001/
      project.json
      state.json
      assets/
      clips/
      deliverables/
    ep-002/
  logs/
```

`series.json` is the author-edited season plan and canon. `series-state.json` is CLI-owned runtime state. Each episode remains a complete standard video project, so all existing generation, resume, QA, assembly, and budget behavior is reused.

## `series.json`

```json
{
  "version": 1,
  "contract_version": "2.0",
  "id": "city-story",
  "title": "City Story",
  "premise": "The series premise",
  "season_arc": "The beginning, escalation, midpoint, and ending of the season",
  "season_theme": "The question or emotional theme of the season",
  "conflict_escalation": "How stakes increase across episodes",
  "midpoint": "The season midpoint reversal",
  "climax": "The season climax",
  "ending_hook": "The final unresolved hook",
  "style_bible": "Stable visual language for every episode",
  "locations": [],
  "props": [],
  "characters": [{
    "id": "lead",
    "name": "Lead",
    "identity": "Stable age, face, hair, build, and distinguishing details",
    "wardrobe": "Canonical wardrobe and signature props",
    "voice": {
      "provider": "voicebox",
      "voice_type": "preset",
      "voice_status": "approved",
      "preset_engine": "qwen_custom_voice",
      "preset_voice_id": "Dylan",
      "provider_profile_id": "voicebox-profile-id",
      "model_revision": "85e237c12c027371202489a0ec509ded67b5e4b5",
      "seed": 42,
      "source_license": "Apache-2.0",
      "approved_by": "user"
    },
    "master": {
      "enabled": true,
      "generate": true,
      "path": "assets/character-masters/lead.png",
      "prompt": "One clean sheet with front, side, and back full-body views of the same person",
      "source_references": [],
      "image_size": "1024x1024",
      "image_quality": "auto"
    }
  }],
  "audio": {
    "mode": "native-dialogue",
    "language": "zh-CN",
    "generate_audio": true,
    "preserve_source_audio": true,
    "duck_source_audio": true,
    "tts_provider": "voicebox",
    "allow_temporary_voices": false,
    "allow_shared_voices": false
  },
  "defaults": {
    "episode_target_seconds": 90,
    "workflow": "character-consistent-story",
    "video_mode": "image-to-video",
    "video_provider": "quickai",
    "video_provider_policy": "automatic",
    "video_size": "1280x720",
    "video_resolution": "480p",
    "video_aspect_ratio": "9:16"
  },
  "limits": {
    "max_character_image_requests": 20,
    "max_episodes": 100
  },
  "retry_policy": {
    "max_total_attempts": 3,
    "max_retries": 2,
    "counts_provider_failover": true
  },
  "episodes": [{
    "id": "ep-001",
    "number": 1,
    "title": "Episode title",
    "synopsis": "Complete episode story beat",
    "continuity_in": "Required starting state",
    "intended_continuity_out": "Planned ending state",
    "character_states": {
      "lead": {"wardrobe": "Episode-specific approved wardrobe"}
    },
    "project": "episodes/ep-001"
  }]
}
```

For image-to-video, a character master is enabled by default and requires a master prompt. For text-to-video, it is disabled by default and no image key is required. Set `master.enabled` explicitly when overriding that rule. Character-master prompts use the same 4096 UTF-8 byte hard limit and full/compact/minimal preflight variants as episode projects.

Series-level `audio` and character `voice` settings are synchronized into every episode project. An owned/licensed/synthetic series voice reference is copied into each episode's `assets/voices/` directory, so episode projects remain self-contained and resumable. Every episode still owns its timed dialogue lines and can be reviewed before generation.

Use `voice-audition` and `voice-approve` at the series root, then run `series-voice-sync`. That command removes stale unapproved voices from episodes and copies only approved voices (or an explicitly allowed `temporary-test`). A normal `series-sync` still copies the whole editable contract for planning and lets preflight report incomplete voice fields. Episode approval remains blocked until every actual dialogue speaker has an approved renderable identity.

## Planning and generation

1. Run `series-init`. It creates the series contract and one standard project skeleton per episode; it does not make a paid request.
2. Fill the season theme, conflict escalation, midpoint, climax, ending hook, every episode title and synopsis, and every episode project's story and shot prompts. Plan each episode dynamically (a two-minute episode typically needs 12-18 shots of 1-15 seconds) and assign each shot a `shot_role`: `establishing`, `wide`, `medium`, `closeup`, `over_shoulder`, `insert`, `reaction`, `transition`, or `ending_hook`. Include environment, action, reaction, and non-dialogue beats; do not make every shot a frontal dialogue closeup. The user can review all of these files before generation.
3. For image-to-video continuity, run `series-generate-characters`. It creates one reusable single-sheet master per enabled character and copies the same bytes into every episode project that references the character.
4. Run `series-preflight --episode ep-001`, then `series-approve ep-001`.
5. Run `series-run --episode ep-001` or `series-run --next`. The episode stops at `needs_review` after generation, assembly, technical QA, and review-frame export.
6. Inspect the generated video, audio track, dialogue intelligibility, mouth timing, clean frame, and all first/key/end review frames. Run `series-accept` with a concise description of the actual end state. That reviewed summary, rather than the planned ending, becomes the next episode's continuity input.
7. Run `series-next` or `series-context --episode ep-002` before writing or generating the next episode.

## Episode lifecycle

| State | Meaning | Allowed next action |
| --- | --- | --- |
| `draft` | Creative contract is still editable | Fill prompts and preflight |
| `approved` | User reviewed this exact episode contract | Generate the episode |
| `generating` | Paid work is active or resumable | Resume; do not create a duplicate project |
| `needs_review` | Media exists but visual continuity is not accepted | Review frames and accept or fix |
| `completed` | Human-reviewed end state is recorded | Proceed to the next episode |
| `failed` | Generation stopped with durable state | Inspect, then explicitly authorize retry |

Only an approved episode can start. Approval stores a digest of the reviewed series canon and episode project; changing story, continuity, characters, or shot prompts after approval blocks all paid generation until `series-preflight` and `series-approve` are run again. Earlier episodes must be completed before a later episode can be approved. `series-run` never generates a whole season implicitly.

## Continuity context

`series-context` returns the whole season outline, compact records for previous episodes, their final video paths and reviewed continuity summaries, plus the full current `project.json`. Calling it also synchronizes the immediately previous reviewed end state into `project.json.series_context`, so a separate `series-sync` step is not required before composing the next episode's image and video prompts.

Do not treat a planned `intended_continuity_out` as fact. Only `series-accept --continuity-summary` records what visibly survived generation and review.

## Route alignment

- Series text-to-video uses the same prompt-only T2V behavior as a standalone project. It needs the QuickAI text-to-video credential but does not require character masters or an image credential. Long-range identity remains best-effort.
- Series image-to-video uses the QuickAI image credential for persistent character masters and per-shot keyframes, then prefers QuickAI for animation. A safely classified QuickAI failure may continue with QuickAI New when configured. The video request receives only the current shot keyframe.
- Neither provider currently supports MP4 video reference/edit/extend or preset/file audio reference; those requests are blocked and reserved for independent future routes.
- A supplied-image animation is not a series workflow. Use a standalone `single-image-animation` project with `generate_image=false` and place the supplied image in `video_references`; no QuickAI image credential is required.
