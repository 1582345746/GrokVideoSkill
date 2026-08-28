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
  "id": "city-story",
  "title": "City Story",
  "premise": "The series premise",
  "season_arc": "The beginning, escalation, midpoint, and ending of the season",
  "style_bible": "Stable visual language for every episode",
  "locations": [],
  "props": [],
  "characters": [{
    "id": "lead",
    "name": "Lead",
    "identity": "Stable age, face, hair, build, and distinguishing details",
    "wardrobe": "Canonical wardrobe and signature props",
    "voice": {
      "provider": "cosyvoice",
      "reference_audio": "assets/voices/lead.wav",
      "reference_text": "Exact transcript of the owned series voice reference",
      "consent": "owned"
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
    "mode": "local-voice",
    "language": "zh-CN",
    "generate_audio": false,
    "preserve_source_audio": true,
    "duck_source_audio": true
  },
  "defaults": {
    "episode_target_seconds": 90,
    "workflow": "character-consistent-story",
    "video_mode": "image-to-video",
    "video_provider": "quickainew",
    "video_size": "1280x720",
    "video_resolution": "480p",
    "video_aspect_ratio": "9:16"
  },
  "limits": {
    "max_character_image_requests": 20,
    "max_episodes": 100
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

For image-to-video, a character master is enabled by default and requires a master prompt. For text-to-video, it is disabled by default and no image key is required. Set `master.enabled` explicitly when overriding that rule.

Series-level `audio` and character `voice` settings are synchronized into every episode project. An owned/licensed/synthetic series voice reference is copied into each episode's `assets/voices/` directory, so episode projects remain self-contained and resumable. Every episode still owns its timed dialogue lines and can be reviewed before generation.

## Planning and generation

1. Run `series-init`. It creates the series contract and one standard project skeleton per episode; it does not make a paid request.
2. Fill the entire season arc, every episode title and synopsis, and every episode project's story and shot prompts. The user can review all of these files before generation.
3. For image-to-video continuity, run `series-generate-characters`. It creates one reusable single-sheet master per enabled character and copies the same bytes into every episode project that references the character.
4. Run `series-preflight --episode ep-001`, then `series-approve ep-001`.
5. Run `series-run --episode ep-001` or `series-run --next`. The episode stops at `needs_review` after generation, assembly, technical QA, and review-frame export.
6. Inspect the generated video and all review frames. Run `series-accept` with a concise description of the actual end state. That reviewed summary, rather than the planned ending, becomes the next episode's continuity input.
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

`series-context` returns the whole season outline, compact records for previous episodes, their final video paths and reviewed continuity summaries, plus the full current `project.json`. The immediately previous reviewed end state is also synchronized into `project.json.series_context` and injected into current image and video prompts.

Do not treat a planned `intended_continuity_out` as fact. Only `series-accept --continuity-summary` records what visibly survived generation and review.

## Route alignment

- Series text-to-video uses the same prompt-only T2V behavior as a standalone project. It needs the QuickAI text-to-video credential but does not require character masters or an image credential. Long-range identity remains best-effort.
- Series image-to-video uses the QuickAI image credential for persistent character masters and per-shot keyframes, then the QuickAI New image-to-video credential for animation. The video request receives only the current shot keyframe.
- A supplied-image animation is not a series workflow. Use a standalone `single-image-animation` project with `generate_image=false` and place the supplied image in `video_references`; no QuickAI image credential is required.
