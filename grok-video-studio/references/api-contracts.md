# Direct API Contracts (v2.4.0 chatcut-adapter)

## Endpoint ownership

| Purpose | Base URL | Credential | Path |
| --- | --- | --- | --- |
| Model discovery and images | User-provided `image_base_url` | Sub2Api image key | `/v1/models`, `/v1/images/*` |
| Sub2Api JSON videos | User-provided `sub2api_video_base_url` | Sub2Api video key (T2V/I2V) | `/v1/models`, `/v1/videos/generations*` |
| Model discovery and NewApi videos | User-provided `newapi_video_base_url` | NewApi video key | `/v1/models`, `/v1/videos/*` |

There is no built-in production provider URL. The user must supply every required Base URL and API Key. Store the origin or service path only; the client appends `/v1`, and a configured trailing `/v1` is normalized away. Never send one provider's key to the other provider.

The two Sub2Api roles may use different URLs and keys. Configuration stores `sub2api_image_key`, `sub2api_video_key`, and `newapi_video_key` separately. A role-specific key never fills another role; unused roles may be omitted. Legacy role-agnostic fields are read only during local migration; new configuration never writes them. Environment equivalents are `GVS_IMAGE_API_KEY`, `GVS_VIDEO_API_KEY`, and `GVS_NEWAPI_VIDEO_KEY`.

## Text to image

`POST /v1/images/generations` with JSON:

```json
{"model":"configured image model","prompt":"...","n":1,"size":"1024x1024","quality":"auto","response_format":"b64_json","output_format":"png"}
```

## Image to image

`POST /v1/images/edits` as multipart. Send `model`, `prompt`, `n`, `size`, `quality`, `response_format`, `output_format`, and one repeated `image` file part per reference image.

Accept image results from `data[].b64_json`, data URLs, or HTTPS URLs. Never forward the API key when downloading a returned public URL.

## Capability matrix

| Provider | T2V | I2V | Video reference/edit/extend | Audio generation | Preset/file audio reference |
| --- | --- | --- | --- | --- | --- |
| Sub2Api | yes | yes | no / no / no | `model_default` | no / no |
| NewApi | yes | yes | no / no / no | `explicit_generate_audio` | no / no |

Video reference means an existing MP4 used for edit or extension, not a still-image I2V keyframe. Unsupported MP4/WAV references are rejected during validation and are reserved for future independent routes.

## Video create

Text-to-video defaults to Sub2Api's JSON contract:

`POST /v1/videos/generations` with `model`, `prompt`, `duration`, `resolution`, and `aspect_ratio`. Sub2Api has no explicit audio flag in this contract; `audio_generation=model_default` is reported as a capability, not a guarantee. Text-to-video sends no image fields. Image-to-video sends one first-frame data URL as `image.url`; multiple guidance images use `reference_images[].url`. Query uses `/v1/videos/{request_id}`. Completed responses normally expose the media URL in `video.url`; the adapter also accepts the gateway's `/v1/videos/{request_id}/content` download route when present.

The configured NewApi multipart adapter uses this contract when it is selected for the canonical model, either by a fixed route or an eligible same-model fallback:

`POST /v1/videos` as multipart with `model`, `prompt`, `seconds`, optional `size`, `resolution`, `aspect_ratio`, `generate_audio`, and zero or more repeated `input_reference` file parts. `generate_audio=true` is the v2 default for native dialogue. This Skill enforces 1-15 seconds and a final composed prompt of at most 4096 UTF-8 bytes. For image-to-video, send only the current shot keyframe.

The new-project canonical model default is seedance-2.0-mini-720p. Its built-in route prefers configured Sub2Api and may continue to configured NewApi for the same model only after an eligible failure. The selected model/upstream adapter controls the request contract; model catalog presence alone does not prove create, status, download, or parameter compatibility. Both video modes use the same resolved project/shot route; image presence never selects an upstream implicitly.

## Native audio and frame layout

The v2.1 project contract requests native upstream audio by default (`audio.mode=native-dialogue`, `generate_audio=true`, `subtitle_source=none`). Sub2Api has no explicit audio field in this gateway contract, so its capability is reported as `model_default`; NewApi receives `generate_audio=true`. The provider may still return an inaudible or captioned result, so audio and pixels require human QA.

`frame_layout=single-full-frame` is the default and means one uninterrupted physical camera view. `split-screen`, `triptych`, and `comic-panel` are opt-in layouts and require `allow_multi_panel=true`. A vertical multi-character T2V single-frame request is a known repeated-panel risk and is blocked for new projects unless the operator explicitly sets `layout_risk_policy=allow`; the recommended route is one approved keyframe followed by I2V. Layout selection is a creative contract and does not change the upstream endpoint.

The current Sub2Api gateway rejects PNG data-URI I2V references even though the upstream xAI contract documents PNG/JPEG/WebP data URIs. The adapter therefore validates each still during preflight and, for valid references, center-crops a temporary JPEG canvas matching the requested aspect ratio before placing it in `image.url` or `reference_images[].url`. The source image is never modified. NewApi keeps the original image file in multipart `input_reference` form.

Automatic fallback is allowed only when a create was definitively rejected before a task ID (unsupported endpoint/capability or exhausted rate limit), or when a known Sub2Api task reaches a provider terminal failure. A create timeout, network loss, HTTP 5xx without a task ID, or successful response without an identifiable task is `submission_unknown`; a polling timeout keeps the known task. These conditions do not create a second paid task automatically. Input/account errors require diagnosis and do not automatically select NewApi.

For `submission_unknown`, first check the provider dashboard or support using its HTTP trace ID. A trace ID and the skill's local `request_id` are diagnostic identifiers, not provider task IDs. After verified evidence is available, `submission-resolve` verifies a supplied provider task ID through its read-only status endpoint before binding it for normal resume, or records confirmed rejection. The command never creates media. A confirmed rejection still requires an explicit, shot-scoped retry authorization.

The default canonical model for new projects is seedance-2.0-mini-720p. Grok grok-imagine-video-1.5 remains available when explicitly selected, and old projects without video_model keep that legacy model for compatibility. Each upstream adapter still has its own verified request and response contract; do not infer XingAPI compatibility from its Sub2Api-shaped adapter wiring or model directory listing.

## Video status and content

- Query: `GET /v1/videos/{task_id}`
- Content: `GET /v1/videos/{task_id}/content`
- Completed: `completed`, `complete`, `succeeded`, `success`, `done`
- Failed: `failed`, `failure`, `error`, `cancelled`, `expired`

If a completed response exposes `url`, `result_url`, `video_url`, or a nested content/video/metadata URL, use it only if the authenticated content endpoint is unavailable. Validate bytes, not only HTTP status or MIME type.

Model discovery, status queries, and downloads are idempotent and may use bounded exponential backoff. Image/video creates use three total attempts by default (initial request plus two retries); provider failover counts toward this total. Only classified prompt/parameter errors and explicit transient failures can be repaired. A `submission_unknown` create is never recreated automatically; a known task is only resumed, polled, or downloaded. `submission-resolve` records an operator-verified provider outcome without contacting the provider. The separate `--replace-lost-task` path is restricted to timed-out known-task lookups and verifies that both the status and content endpoints return `404` before preserving the old task ID and issuing one audited replacement create. If content still exists, it is recovered instead of creating.
