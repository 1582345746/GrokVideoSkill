# v2.0 Upstream Acceptance

Date: 2026-08-29

The test credentials were supplied at runtime through the Windows DPAPI-backed configuration flow. No key is stored in this repository, project JSON, event log, or acceptance record.

## Matrix

| Route | Provider | Result | Artifact |
| --- | --- | --- | --- |
| Text-to-image | QuickAI | completed | `E:\TEMP\gvs-paid-v2-20260829-01\quickai-image\assets\keyframes\shot-001.png` |
| T2V | QuickAI | completed, task `98b7cf55-54b7-9ea1-bc4b-9163b2c6ea1e` | `quickai-t2v/clips/shot-001.mp4` |
| I2V | QuickAI | completed after gateway-compatible JPEG normalization, task `e787fa9e-bc4f-928b-b455-25d89a53a367` | `quickai-i2v-fixed-2/clips/shot-001.mp4` |
| T2V | QuickAI New | completed, task `task_P3x3pPaSNLslHg8eqJs7ByGnuI7hF1Ew` | `quickainew-t2v/clips/shot-001.mp4` |
| I2V | QuickAI New | completed, task `task_1VeKNqY095nfwiVTxKyyIcodL8KMVj8S` | `quickainew-i2v/clips/shot-001.mp4` |
| Native dialogue T2V | QuickAI New | completed with audible AAC, task `task_sJ0yPnPmhrHHD2KuNOlsVUMLHdZfK6Ep` | `quickainew-dialogue/clips/shot-001.mp4` |

QuickAI rejected PNG data-URI I2V requests with HTTP 400 and no task ID. A public image URL and a JPEG data URI both created tasks successfully. The adapter now validates stills during preflight and sends valid references as a temporary center-cropped JPEG; the user's source image is untouched. `quickai-i2v` retains the three failed attempts as an audit record, while `quickai-i2v-fixed-2` is the clean end-to-end regression.

## QA

- All five completed project artifacts passed technical QA: MP4, H.264/yuv420p, expected orientation, and AAC audio track.
- No artifact contained a subtitle stream. The four no-dialogue samples had effectively silent audio, which is expected for prompts without spoken lines and remains a warning.
- The native-dialogue sample had mean volume `-18.4 dB`, no silence interval, and passed the audio-track gate.
- Visual review of first/key/end frames found no UI, logo, watermark, or unrelated overlay in the no-dialogue samples.
- The native-dialogue sample visibly burned the Chinese dialogue into pixels despite the negative prompt. `qa` now exposes `blocking_review_items`; this sample is not a clean-delivery approval until a human chooses a remediation path.

## Request accounting

The controlled run submitted one image-generation request and ten video-create calls, including three definitively rejected QuickAI I2V calls with no task ID and two isolated protocol probes. Successful task IDs are retained above for resumable audit; no task with a known ID was recreated.
