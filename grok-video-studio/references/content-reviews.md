# Rendered content reviews

`review-shot` approves an asset's bytes, not every semantic/audio layer. New scoped/event projects require independent findings before formal delivery.

1. Run `qa <project>` for technical signals and retained-window sample frames.
2. Run `content-review-template <project>`; every layer starts `unverified`.
3. Inspect actual video/pixels and listen when capabilities allow. In a separate receipt, fill only observed layers with `status=approved|rejected`, supported `method`, and concrete `evidence` strings with times/findings. Keep unavailable checks unverified. Do not approve from intended prompts.
4. Run `content-review-apply <project> <receipt.json>`, then QA again. No new paid request is created.

The receipt binds `project.json` SHA-256, project-local asset paths, video hashes and retained clip windows. Changed project/media/windows invalidate approval. Partial receipts leave other layers/assets unknown. Malformed/stale receipts cannot replace prior review. The final master is reviewed separately and narrative approval requires `understanding.goal`, `obstacle`, `choice`, `consequence`.

Layers: narrative, visual, audio, continuity, clean_frame. Pixel layers support `codex-vision`, `human-video`, or actual `model-audio-video`. Audio requires `human-listening`, `asr-and-human`, or actual `model-audio-video`; frame methods are rejected. Clean-frame also supports `ocr-and-vision`. Method names declare provenance, not available capabilities: record only a method really used. An unexpected voice can reject a supposedly silent insert; ASR alone cannot approve speaker identity/lip timing.

When Tesseract is absent, Windows may use its installed Windows.Media.Ocr language pack with no download. OCR is sampled and `verified_full_video=false`. Unavailable checks remain pending; sufficiently observed pixel review can supply the clean layer. Detected unwanted text remains a failure until fixed.

QA writes layer results to state; status revalidates receipts/final bytes before displaying delivery completed. Required assets/layers must pass; any rejection blocks even when metadata passes. Preview assembly may remain available while content is pending. Describe it as a validation sample, not a fully reviewed delivery.
