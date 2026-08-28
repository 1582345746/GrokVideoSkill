# Error Matrix

| Symptom | Layer | Action |
| --- | --- | --- |
| `401` or `403` | Credential or account | Rotate the correct provider key and run `doctor`. |
| Missing unused provider key | Configuration | Allowed. Configure a key when a workflow or explicit provider override needs that provider. |
| QuickAI image request uses the T2V key, or T2V uses the image key | Credential role | Configure `quickai_image_key` and `quickai_video_key` separately. Legacy `quickai_key` intentionally supplies both roles only when role-specific values are absent. |
| Mode/provider mismatch | Project contract | Set `video_mode` and `video_provider` explicitly and verify the matching key. |
| `unknown provider for model` | Upstream model routing | Confirm the exact model appears in that provider's `/v1/models`; do not retry create. |
| `404` on `/v1/videos` | Base path or incompatible upstream | Store only the origin; verify the provider implements the OpenAI video endpoint. |
| `400` with multipart/body validation | Contract mismatch | Inspect field names, model limits, seconds, size, and reference count. |
| Prompt length exceeds 4096 characters | Prompt budget | Shorten the identity/style bibles and shot text; the limit applies after composition and is checked before a paid request. |
| `seconds` above 15 | Video contract | Split the action into more shots; never silently clamp a requested duration. |
| Video receives a multi-view character sheet | Reference selection | Generate a per-shot keyframe from the sheet and send only that keyframe to image-to-video. |
| `429` | Rate or account limit | Wait before a new create; polling may use backoff. |
| `502`, `503`, `context deadline exceeded` | Gateway/upstream | Treat create as ambiguous if no task ID was returned. Do not automatically create again. |
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
| Subtitle timing is inaccurate after adding voice | Post-production | Replace shot-level cues with word/sentence timestamps from the final TTS or voice track, then burn a new subtitled copy. |
| I2V reference field rejected | Provider contract | Verify QuickAI JSON uses `input_reference` for one image or `reference_images` for multiple; QuickAI New uses repeated multipart `input_reference`. |
| `ffprobe` or assembly validation fails | Local media QA | Install FFmpeg/FFprobe, inspect the reported stream metadata, and normalize clips before assembling. |
| Browser history lacks the task | Expected direct mode behavior | The local project is the source of truth; direct Skill tasks do not enter Canvas IndexedDB. |

Always report the provider, endpoint path, HTTP status, sanitized request ID, task ID when known, and whether retrying could duplicate billing. Never print authorization headers or key fragments.
