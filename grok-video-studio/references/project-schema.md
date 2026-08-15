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
  "target_duration_seconds": 18,
  "story": "Short screenplay",
  "character_bible": "Concise stable identity, clothing, and props",
  "style_bible": "Concise stable visual language",
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
    "video_seconds": 6
  },
  "limits": {
    "max_image_requests": 12,
    "max_video_requests": 8,
    "max_total_video_seconds": 60,
    "max_reference_images": 9,
    "max_prompt_chars": 4096
  },
  "shots": [{
    "id": "shot-001",
    "summary": "What happens",
    "image_prompt": "One still keyframe",
    "video_prompt": "One continuous motion",
    "generate_image": true,
    "use_character_master": true,
    "image_references": [],
    "video_references": [],
    "image_size": "1024x1024",
    "video_size": "1280x720",
    "seconds": 6
  }]
}
```

## Character master

`mode` must be `single-sheet`. The file contains all character views on one canvas and counts as one image request and one image reference. When `generate` is false, `path` must already exist. When `generate` is true, `generate-character` writes the actual output path to `state.json`.

`use_character_master` prepends the single sheet to a shot's image references. It does not add that sheet to video references. When `video_references` is empty and a generated keyframe exists, only that keyframe becomes the video reference.

## Limits and state

Every clip is 1-15 seconds. Final composed image and video prompts cannot exceed 4096 characters; 3800 is the recommended working ceiling. Limits are hard preflight gates and request counts include a generated character master.

Runtime states include `pending`, `submitting`, `queued`, `in_progress`, `completed`, `failed`, `submission_unknown`, and `poll_timeout`. A task ID is sufficient to resume polling without another create request. Paths must be project-relative and stay inside the project.
