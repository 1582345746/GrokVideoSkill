# Error Matrix

| Symptom | Layer | Action |
| --- | --- | --- |
| `401` or `403` | Credential or account | Rotate the correct provider key and run `doctor`. |
| Missing unused provider key | Configuration | Allowed. Configure a key when a workflow or explicit provider override needs that provider. |
| QuickAI image request uses the T2V key, or T2V uses the image key | Credential role | Configure `quickai_image_key` and `quickai_video_key` separately. Legacy `quickai_key` intentionally supplies both roles only when role-specific values are absent. |
| Mode/provider mismatch | Project contract | Set `video_mode` and `video_provider` explicitly and verify the matching key. |
| `unknown provider for model` | Upstream model routing | Confirm the exact model appears in that provider's `/v1/models`; do not retry create. |
| `404` on `/v1/videos` | Base path or incompatible upstream | Store only the origin; verify the provider implements the OpenAI video endpoint. |
| Intermittent `404 Video request not found` while polling a known task ID | Provider read path | Keep the known task ID, back off, and poll it again. The HTTP response `request_id` is a trace ID, not a replacement video task ID. Do not create or fail over to a second paid task. If repeated polling times out and the operator explicitly authorizes replacement, use `resume --shot <id> --replace-lost-task --retry-failed --retry-reason "..."`; the command checks both status and content endpoints before any replacement create. |
| `project is busy in another mutating command` | Local project state | Wait for the active generation, assembly, QA, or review command to finish, then retry. State-mutating commands are serialized so parallel CLI processes cannot overwrite each other's review or task state. |
| `400` with multipart/body validation | Contract mismatch | Inspect field names, model limits, seconds, size, and reference count. |
| Prompt length exceeds 4096 UTF-8 bytes | Prompt budget | Inspect preflight full/compact/minimal variants and remaining bytes. Keep identity, location, core action, dialogue, and ending pose; remove season-wide or unrelated scene detail. If minimal still exceeds the hard limit, shorten the source fields. |
| `400` prompt-too-long | Prompt repair | The runtime may move from full to compact to minimal within the configured total-attempt budget. It stores every version; it never silently truncates. |
| `400` size conflict | Provider parameter repair | QuickAI New may omit a conflicting `size` while retaining `resolution` and `aspect_ratio`; inspect the recorded provider attempt. |
| `400`/`422` reference error | Reference contract | Verify I2V uses still images and only the current shot keyframe. MP4/WAV references are blocked; use future video-edit/video-extend or audio-reference routes when implemented. |
| `seconds` above 15 | Video contract | Split the action into more shots; never silently clamp a requested duration. |
| Video receives a multi-view character sheet | Reference selection | Generate a per-shot keyframe from the sheet and send only that keyframe to image-to-video. |
| `429` | Rate or account limit | Wait before a new create; polling may use backoff. |
| `502`, `503`, `context deadline exceeded` | Gateway/upstream | Treat create as ambiguous if no task ID was returned. Do not automatically create again. |
| Retry count exceeded | Cost control | Default is three total attempts (initial plus two retries), including provider failover. A known task is resumed/polled/downloaded only; `submission_unknown` is not recreated automatically. |
| QuickAI task reaches a confirmed provider failure | Provider task | Record the failed QuickAI attempt, then automatically continue with QuickAI New only when its video key is configured and the failure is not content/account/input related. |
| QuickAI create is rejected as unsupported or rate-limited before a task exists | Provider routing | It is safe to record a separate QuickAI New attempt. Keep the same internal request ID and a new attempt ID. |
| `provider circuit is open` | Repeated idempotent reads failed | Wait for the reported cooldown, verify provider health, then resume. No create request was retried. |
| Budget gate blocked | Project cost control | Raise the ceiling, lower request counts/rates, or stop. The blocked request was not sent. |
| Completed status but no playable file | Result retrieval | Retry `/content`, then an advertised HTTPS result URL; verify MP4 bytes. |
| QA orientation or dimensions mismatch | Provider output contract | Regenerate the affected shot or deliberately normalize it during assembly; review cropping before delivery. |
| Resolution downgraded by upstream | Provider output contract | Record requested and observed resolution in state/QA; do not report it as an exact match. |
| Final assembly has no audio | Local assembly policy | Keep `defaults.audio_policy=preserve`; assembly retains source audio and adds silent AAC to clips without audio. Use `mute` only intentionally. |
| App controls, likes, comments, captions, or watermarks appear | Generative visual artifact | Keep `allow_ui_elements=false`, regenerate the affected shot, and inspect every exported QA review frame before delivery. Do not crop blindly when overlays cover story content. |
| Character appearance changes between T2V shots | Model continuity limit | Use concise identity locks for best effort. For strict continuity, switch to image-to-video with one character master and a per-shot keyframe. |
| Later series episode is blocked | Series lifecycle | Finish visual review and `series-accept` the earlier episode, then preflight and approve the next draft. |
| Series episode remains `needs_review` | Series lifecycle | Inspect the final and every review frame, then record the actual ending with `series-accept --continuity-summary`. |
| Supplied-image animation asks for an image key | Project contract | Use `single-image-animation`, keep `generate_image=false`, and place the supplied image in `video_references`. Only the I2V key is required. |
| News project says `news.json` is incomplete | News evidence gate | Browse current exact source pages, map claims and narration, resolve conflicts, set verified timestamps, then run `news-validate`. No paid request was sent. |
| News claim lacks support | News evidence gate | Add one primary source or two independent sources from distinct publishers; remove unsupported wording from the script. |
| Subtitle text is garbled in generated footage | Generation prompt | Keep upstream clean-frame rules enabled. Export SRT and use local `subtitles --burn` instead of asking the model to draw text. |
| Native video has no audible dialogue or has burned captions | Native audio QA | Treat sound, exact wording, mouth timing, and model-baked captions as generated results requiring human review. Keep `final.mp4` clean; use `subtitle_source=project` only for an explicitly reviewed local derivative. |
| Subtitle timing is inaccurate after adding voice | Post-production | Replace shot-level cues with word/sentence timestamps from the final TTS or voice track, then burn a new subtitled copy. |
| I2V reference field rejected | Provider contract | Verify QuickAI JSON uses `image.url` for one first-frame image or `reference_images[].url` for multiple guidance images; QuickAI New uses repeated multipart `input_reference`. |
| `ffprobe` or assembly validation fails | Local media QA | Install FFmpeg/FFprobe, inspect the reported stream metadata, and normalize clips before assembling. |
| Browser history lacks the task | Expected direct mode behavior | The local project is the source of truth; direct Skill tasks do not enter Canvas IndexedDB. |

Always report the provider, endpoint path, HTTP status, sanitized request ID, task ID when known, and whether retrying could duplicate billing. Never print authorization headers or key fragments.
