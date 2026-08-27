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
  "video_provider": "quickainew",
  "target_duration_seconds": 18,
  "story": "Short screenplay",
  "character_bible": "Concise stable identity, clothing, and props",
  "style_bible": "Concise stable visual language",
  "characters": [{
    "id": "lead",
    "name": "Lead",
    "identity": "Stable face, hair, age, and body description",
    "wardrobe": "Canonical wardrobe",
    "references": []
  }],
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
    "image_size": "1024x1024",
    "image_quality": "auto",
    "video_size": "1280x720",
    "video_seconds": 6,
    "video_resolution": "480p",
    "video_aspect_ratio": "16:9"
  },
  "limits": {
    "max_image_requests": 12,
    "max_video_requests": 8,
    "max_total_video_seconds": 60,
    "max_reference_images": 9,
    "max_prompt_chars": 4096
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
    "scene_id": "office",
    "character_ids": ["lead"],
    "continuity_notes": "Keep wardrobe, eyeline, lighting, and prop position from the previous shot",
    "wardrobe": {},
    "continuity_change": false,
    "image_prompt": "One still keyframe",
    "video_prompt": "One continuous motion",
    "generate_image": true,
    "use_character_master": true,
    "image_references": [],
    "video_references": [],
    "image_size": "1024x1024",
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

## Limits and state

Every clip is 1-15 seconds. Final composed image and video prompts cannot exceed 4096 characters; 3800 is the recommended working ceiling. Limits are hard preflight gates and request counts include a generated character master.

`video_mode` must be `text-to-video` or `image-to-video`; `video_provider` must be `quickai` or `quickainew`. New text-to-video projects default to QuickAI and do not generate keyframes. New image-to-video projects default to QuickAI New. Projects created before these fields existed retain the legacy image-to-video plus QuickAI New interpretation. Resolution is limited to `480p`, `720p`, and `1080p`; aspect ratio is limited to `16:9`, `9:16`, `1:1`, `4:3`, `3:4`, `2:3`, or `3:2`.

Runtime states include `pending`, `submitting`, `queued`, `in_progress`, `completed`, `failed`, `submission_unknown`, and `poll_timeout`. A task ID is sufficient to resume polling without another create request. Paths must be project-relative and stay inside the project.

## Characters, continuity, and budget

`characters` is optional and supports more than one named character. A shot selects participants with `character_ids`; their identity and canonical wardrobe are injected into both keyframe and motion prompts. `scene_id` and `continuity_notes` make adjacent-shot continuity auditable. Set `continuity_change` to true when a deliberate wardrobe or scene discontinuity is part of the story.

Budget rates are operator estimates, not provider invoices. When `max_estimated_cost` is non-null, preflight rejects an over-budget plan and runtime blocks the next create request before its attempt would exceed the ceiling. `state.json.budget_usage` counts actual create attempts, including explicitly authorized retries.
