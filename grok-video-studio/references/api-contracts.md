# Direct API Contracts

## Endpoint ownership

| Purpose | Base URL | Credential | Path |
| --- | --- | --- | --- |
| Model discovery and images | `https://quickai.hn.takin.cc` | QuickAI image key | `/v1/models`, `/v1/images/*` |
| QuickAI JSON videos | `https://quickai.hn.takin.cc` | QuickAI text-to-video key | `/v1/models`, `/v1/videos/generations*` |
| Model discovery and QuickAI New videos | `https://quickainew.hn.takin.cc` | QuickAI New video key | `/v1/models`, `/v1/videos/*` |

Store the origin only. The client appends `/v1`; a configured trailing `/v1` is normalized away. Never send one provider's key to the other provider.

The two QuickAI roles may use different keys even though they share one origin. Configuration stores `quickai_image_key`, `quickai_video_key`, and `quickainew_video_key` separately. Legacy `quickai_key` supplies both QuickAI roles only when no role-specific value exists; legacy `quickainew_key` supplies the QuickAI New video role. Environment equivalents are `GVS_QUICKAI_IMAGE_KEY`, `GVS_QUICKAI_VIDEO_KEY`, and `GVS_QUICKAINEW_VIDEO_KEY`; the handoff-compatible aliases `QUICKAI_IMAGE_API_KEY`, `QUICKAI_VIDEO_API_KEY`, and `QUICKAI_NEW_VIDEO_API_KEY` are also accepted.

## Text to image

`POST /v1/images/generations` with JSON:

```json
{"model":"configured image model","prompt":"...","n":1,"size":"1024x1024","quality":"auto","response_format":"b64_json","output_format":"png"}
```

## Image to image

`POST /v1/images/edits` as multipart. Send `model`, `prompt`, `n`, `size`, `quality`, `response_format`, `output_format`, and one repeated `image` file part per reference image.

Accept image results from `data[].b64_json`, data URLs, or HTTPS URLs. Never forward the API key when downloading a returned public URL.

## Video create

Text-to-video defaults to QuickAI's JSON contract:

`POST /v1/videos/generations` with `model`, `prompt`, `duration`, `resolution`, and `aspect_ratio`. Text-to-video sends no image fields. Image-to-video sends one first-frame data URL as `image.url`; multiple guidance images use `reference_images[].url`. Query uses `/v1/videos/{request_id}`. Completed responses normally expose the media URL in `video.url`; the adapter also accepts the gateway's `/v1/videos/{request_id}/content` download route when present.

QuickAI New uses this multipart contract for either video mode when selected as an explicit provider or safe automatic fallback:

`POST /v1/videos` as multipart with `model`, `prompt`, `seconds`, optional `size`, and zero or more repeated `input_reference` file parts. This Skill enforces 1-15 seconds and a final composed prompt of at most 4096 characters. For image-to-video, send the current shot keyframe; do not send a multi-view character sheet directly.

Both adapters send `resolution` (`480p`, `720p`, or `1080p`) and `aspect_ratio`. Both video modes default to QuickAI. QuickAI New is considered only when its key is configured and the QuickAI failure is safe to fail over, or when the project explicitly fixes `video_provider=quickainew`; providers are never selected implicitly from image presence.

Automatic fallback is allowed only when a create was definitively rejected before a task ID (unsupported endpoint/capability or exhausted rate limit), or when a known QuickAI task reaches a provider terminal failure. A create timeout, network loss, or HTTP 5xx without a task ID is `submission_unknown`; a polling timeout keeps the known task. Neither condition creates a second paid task automatically.

The expected model is configured, with `grok-imagine-video-1.5` as the default. Do not silently rename it to a preview model. The adapter searches common `data`, `result`, `output`, `response`, `task`, and `video` wrappers; it accepts snake_case and camelCase task IDs, status, progress, errors, and result URLs.

## Video status and content

- Query: `GET /v1/videos/{task_id}`
- Content: `GET /v1/videos/{task_id}/content`
- Completed: `completed`, `complete`, `succeeded`, `success`, `done`
- Failed: `failed`, `failure`, `error`, `cancelled`, `expired`

If a completed response exposes `url`, `result_url`, `video_url`, or a nested content/video/metadata URL, use it only if the authenticated content endpoint is unavailable. Validate bytes, not only HTTP status or MIME type.

Model discovery, status queries, and downloads are idempotent and may use bounded exponential backoff. Repeated transient failures open a short process-local circuit breaker. `POST /v1/videos` and all image creation endpoints are billable writes and are never automatically retried.
