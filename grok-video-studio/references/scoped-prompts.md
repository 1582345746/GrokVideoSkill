# Scoped prompt compiler

Set `project.prompt_compiler="scoped-v1"` to opt into request-scoped compilation. Legacy projects keep their original compiler.

```json
{
  "prompt_compiler": "scoped-v1",
  "prompt_style": {"medium": "photoreal", "rendering": "natural handheld camera", "palette": "warm natural colors"},
  "character_master": {"character_id": "lead", "prompt": "Clean neutral turnaround."},
  "shots": [{
    "id": "shot-001", "seconds": 5,
    "character_ids": ["lead", "friend"],
    "background_character_ids": [],
    "image_prompt": "Opening still: lead holds a crate; friend stands right of the table.",
    "video_prompt": "Lead slides the crate onto the table while speaking.",
    "event": {"requires": {"crate.owner": "lead"}, "effects": {"crate.owner": "friend"}},
    "dialogue": [{"speaker": "lead", "listener": "friend", "on_screen": true, "text": "先收这筐！", "start": 0.15, "end": 1.25}],
    "exit_behavior": "cut-on-action", "exit_action": "the crate slides onto the table"
  }]
}
```

`character_ids` and `background_character_ids` describe only the current visible cast. Both empty means no characters in the image; the compiler never falls back to the full cast. A named background character must exist in `characters`. The speaker defaults to on-screen and must be in the visible cast. Declare `on_screen=false` for a PA, telephone, or a voice from outside the frame; that speaker cannot also be listed as visible. A listener is a known character id or `audience`. For a single-speaker closeup, a listener outside the picture needs `listener_position="offscreen-left"` or `"offscreen-right"` on the line or shot; their identity is not added to the visible cast. Every native/local dialogue shot has one speaker. Split multi-speaker exchanges into separately controlled shots.

`prompt_style` accepts only `medium`, `rendering`, `palette`, `lighting`, and `texture`; a shot can override the same fields. `visual_profile.medium` supplies a medium when explicitly configured. `character_bible`, `style_bible`, full-episode payoff, global genre story instructions, and season canon are planning context and are never emitted into these requests. Put pure visual values in `prompt_style`; author `image_prompt` as the current opening still and `video_prompt` as the current action, without retelling future events.

An image prompt contains the cast, camera, current scene/asset locks, and `event.requires`. It excludes dialogue, `event.effects`, and the motion/exit plan. A video prompt additionally contains the current action, `event.effects`, explicit line windows, speaker/listener and closed-mouth rules, and the cut exit. No five-stage micro-performance or deliberate pause is injected.

`scene_state` and `asset_state` retain the full world state for continuity, including entities outside the camera view. For closeups or inserts, set optional shot-level `prompt_state` to the state actually visible in this view:

```json
{
  "scene_state": {"wall": "white plaster", "truck": "parked beside the yard"},
  "asset_state": {"crate_count": 12, "phone": "in buyer's hand"},
  "prompt_state": {
    "scene": {"wall": "white plaster"},
    "assets": {"phone": "in buyer's hand"}
  }
}
```

When present, `prompt_state.scene` and `.assets` replace the scene/asset locks sent to both image and video requests; hidden trucks and crates remain in world state but are not injected by those locks. The projection and its optional sections must be objects, and only `scene` and `assets` are accepted. Missing sections or `{}` mean no locks for that section; `prompt_state={}` suppresses both world-lock sections. Without `prompt_state`, the existing full scene/asset locks remain the default. This projection never removes `event.requires`, `event.effects`, dialogue timing, or speaker rules. Keep event fields local and other free-form camera/action/prop descriptions consistent with the visible view; a projection does not filter those fields or prove actual visibility in generated pixels.

For a standalone master, `character_master.character_id` selects one identity. With no id, only its explicit `prompt` is used; global character bibles are excluded. Masters use a neutral background.

All three compatibility variants are the same compiled contract. A prompt exceeding `limits.max_prompt_bytes` (maximum 4096 UTF-8 bytes) fails locally. Rewrite or split the shot instead of removing identity, state, dialogue timing, or exit rules. These are generation intentions; exact wording, speaker assignment, geometry and lip sync still require evidence from the resulting clip before delivery.
