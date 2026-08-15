# Direct API Contracts

## Endpoint ownership

| Purpose | Base URL | Credential | Path |
| --- | --- | --- | --- |
| Model discovery and images | `https://quickai.hn.takin.cc` | QuickAI key | `/v1/models`, `/v1/images/*` |
| Model discovery and videos | `https://quickainew.hn.takin.cc` | QuickAI New key | `/v1/models`, `/v1/videos/*` |

Store the origin only. The client appends `/v1`; a configured trailing `/v1` is normalized away. Never send one provider's key to the other provider.

## Text to image

`POST /v1/images/generations` with JSON:

```json
{"model":"configured image model","prompt":"...","n":1,"size":"1024x1024","quality":"auto","response_format":"b64_json","output_format":"png"}
```

## Image to image

`POST /v1/images/edits` as multipart. Send `model`, `prompt`, `n`, `size`, `quality`, `response_format`, `output_format`, and one repeated `image` file part per reference image.

Accept image results from `data[].b64_json`, data URLs, or HTTPS URLs. Never forward the API key when downloading a returned public URL.

## Video create

`POST /v1/videos` as multipart with `model`, `prompt`, `seconds`, optional `size`, and zero or more repeated `input_reference` file parts. This Skill enforces 1-15 seconds and a final composed prompt of at most 4096 characters. For image-to-video, send the current shot keyframe; do not send a multi-view character sheet directly.

The expected model is configured, with `grok-imagine-video-1.5` as the default. Do not silently rename it to a preview model. Normalize a task ID from `id`, `request_id`, or `task_id`, including a nested `data` object.

## Video status and content

- Query: `GET /v1/videos/{task_id}`
- Content: `GET /v1/videos/{task_id}/content`
- Completed: `completed`, `complete`, `succeeded`, `success`, `done`
- Failed: `failed`, `failure`, `error`, `cancelled`, `expired`

If a completed response exposes `url`, `result_url`, `video_url`, or a nested content/video/metadata URL, use it only if the authenticated content endpoint is unavailable. Validate bytes, not only HTTP status or MIME type.
