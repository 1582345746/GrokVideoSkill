# Fast continuous-event dialogue

Use for brisk character exchanges about one unfolding event, not as a universal rhythm for silent film, music, ads, scenery or slow performance. A large cast can exist across an episode; one native-generation shot defaults to one clear speaker.

First state the protagonist's goal, opposing purpose, stakes and deadline. Each advancing beat has a trigger, choice/action, result and reason for what follows. Ask an independent Codex critic to check motivations, physical plausibility, unexplained knowledge, unresolved goals and redundant dialogue before spending. Field completeness is not a semantic review. A beat can span several speaker/listener/object shots without a new event at every cut.

For roughly one-minute episodes, 1.5–3 second edit shots are a useful reference, not a quota. Plan lengths from understandable speech and visible actions. Start with a meaningful exchange already underway. Replies must address the preceding turn. Establishing/object/reaction coverage may carry motivated off-screen character speech. Do not substitute a narrator without the user's choice.

## Contract

`init --workflow fast-continuous-event-dialogue` sets `prompt_compiler=scoped-v1`, `event_flow.required=true`, and declares `continuity.handoff_policy=reviewed-clip-exit`. Every shot after the first is initialized with `handoff_source=reviewed-clip-exit`; video generation blocks until the preceding clip is content-approved, hash unchanged, and its actual exit frame is exported. Fill the scaffold before preflight.

For a mixed-mode fast dialogue project, keep the project-level `video_mode` as `image-to-video`, set the first shot's `video_mode` to `text-to-video`, and leave later shots as `image-to-video` (explicitly or by inheritance). The first T2V request does not need a generated keyframe and must not include image references. Every later I2V shot keeps the reviewed-clip-exit gate and uses the approved preceding clip's actual exit frame as its handoff reference; a missing, changed, or unapproved exit still blocks generation.

```json
{
  "event_flow": {
    "required": true,
    "event_id": "shipment-price",
    "initial_state": {"price": 3, "load_allowed": false},
    "pace": {"max_shot_seconds": 3.5, "max_silent_seconds": 0.6,
      "max_dialogue_lead_seconds": 0.4, "max_dialogue_tail_seconds": 0.5,
      "max_no_change_seconds": 6, "max_function_repeat": 2,
      "max_visible_characters": 3, "max_speakers_per_shot": 1,
      "max_speech_units_per_second": 8, "max_timeline_silent_seconds": 0.8,
      "max_task_silent_seconds": 1.2}
  },
  "shots": [{
    "id": "shot-001", "video_mode": "text-to-video",
    "seconds": 4, "edit_in": 0, "edit_out": 2.4,
    "timeline_duration": 2.4, "character_ids": ["broker"],
    "listener_position": "offscreen-left",
    "event": {
      "event_id": "shipment-price", "mode": "advance", "beat_id": "low-offer",
      "function": "obstacle", "trigger": "The seller is about to load the shipment.",
      "requires": {"price": 3}, "effects": {"price": 1},
      "viewer_learns": "The broker changes the agreed price.",
      "bridge_to_next": "The seller challenges the offer."
    },
    "dialogue": [{
      "id": "line-001", "speaker": "broker", "listener": "seller",
      "text": "一块一斤，现在就装车。", "start": 0.1, "end": 2.2,
      "intent": "request", "new_information": "The offer is one yuan.",
      "effect_key": "price", "on_screen": true
    }]
  }]
}
```

State dictionaries have scalar values and flat keys (dotted names allowed). Later shots name the preceding `cause_shot_id`; `requires` is checked against accumulated state and `effects` changes it. Each `advance` changes state. `coverage` references the active advancing `beat_id`, has `coverage_role=speaker|listener|object|action` and may have no effects; its dialogue effect key can refer to that active beat. Coverage still has a configurable no-change time budget. This reducer checks declared contradictions, not whether the video realized them.

Speech times are source-clip times and must fit the retained edit window. Chinese characters/Latin words estimate speech units, not measured speed. Known listeners may be outside the frame with `listener_position=offscreen-left|offscreen-right`. Off-screen speakers need `on_screen=false`, motivated `audio_source=offscreen|phone|speaker`, and must be absent from visible cast. Do not invent speech merely to fill gaps.

This workflow requires `event_flow.required=true`; disabling it blocks preflight. `event_flow.dialogue_timeline` joins every retained no-dialogue interval across cuts, so a 0.5-second tail followed by a 0.4-second lead cannot hide as two independently short pauses. Ordinary gaps default to at most `max_timeline_silent_seconds=0.8`. A whole silent insert may have a longer budget, `max_task_silent_seconds=1.2`, only with declared listener/object/action coverage of the active beat, an event state effect, or a nonempty `entry_action`/`exit_action`. Adjacent silent inserts and their neighboring lead/tail pads share this budget; unmotivated padding within that span must still fit the ordinary 0.8-second budget. Existing per-line lead, tail and internal-gap limits still apply; a task in a speaking shot cannot excuse its padded dialogue tail. The report lists final-timeline start/end, affected shot IDs, source-clip ranges, and declared task reasons. These are editable planning limits, not reference measurements or proof of audible dialogue: listen to actual clips and verify the edited master before approving content. Legacy workflows retain their existing single-shot silence rules.

Use independent character masters and a stable scene map. Keyframes describe only the opening still. Reverse shots use the spatial map and identity references. Fast event dialogue uses `handoff_source=reviewed-clip-exit` for every non-first shot; it exports the approved previous video exit, adds it to the motion request, and rejects changed edit windows. When a later shot sets `generate_image=false`, leave `video_references` empty: the request is handoff-only and the reviewed previous exit is the sole I2V reference, even if stale current-shot image state exists. Ordinary previous keyframes are appearance aids, not end-state evidence. A missing or unapproved exit never falls back to a static opening frame.

An explicit shot mode overrides `project.video_mode`; an omitted mode inherits it. T2V shots must not send image references or be blocked for lacking a generated keyframe. Set `generate_image=false` on a T2V shot unless a separate still is intentionally needed; workflow templates may otherwise submit an unnecessary image request before video generation. This exception applies to the T2V request only: it does not relax the following I2V shot's reviewed-exit requirement.

Inspect keyframes before paid video requests. Generate a small meaningful exchange first when provider behavior is uncertain. Native time windows are requests, not guarantees; do not cut words to meet a planned duration. Technical QA and actual content findings stay separate. Answer final understanding questions from the rendered master. Repair affected shots/edits with audited retries; do not scale a failed pilot to a season.
