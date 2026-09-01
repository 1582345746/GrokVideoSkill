# Visual Profile Contract

`visual_profile` is optional for old projects. When absent, the CLI resolves an automatic profile and emits a review warning; it never treats a missing field as proof that the subject is a real person.

```json
{
  "visual_profile": {
    "version": 1,
    "mode": "auto",
    "medium": "auto",
    "subject": "auto",
    "subject_nature": "auto",
    "realism": "auto",
    "identity_strictness": "auto",
    "performance_complexity": "auto",
    "confirmed": false
  }
}
```

## Fields

- `medium`: `photoreal`, `2d-anime`, `3d-anime`, `stylized-3d`, `motion-graphics`, or `hybrid`. This describes rendering language, not whether a person is real.
- `subject`: the visible subject class: `human`, `fictional-character`, `mascot`, `product`, `environment`, `graphic`, or `mixed`.
- `subject_nature`: the reality boundary: `real-person`, `synthetic-human`, `human-like-fictional`, `non-human-fictional`, `object`, `environment`, `graphic`, `mixed`, or `unknown`.
- `real-person` is reserved for a supplied/captured actual human identity. A generic request for a photoreal person is not proof of this value.
- `synthetic-human` is an AI-generated or virtual human presented with live-action/photoreal visual grammar, without claiming an actual-person identity.
- `human-like-fictional` covers anime, toon, and CG characters that resemble humans, including near-photoreal 3D renders.
- `realism`: perceived realism (`high`, `medium`, `low`, or `not-applicable`). Both `synthetic-human` and `human-like-fictional` can use `realism=high`; realism never establishes actual-person provenance.
- `identity_strictness`: how strongly face, costume, model, or object identity must persist.
- `performance_complexity`: how much acting, dialogue, lip movement, or coordinated motion is required.
- `confirmed`: a user or multimodal reviewer has confirmed the explicit fields. It does not silently confirm fields left as `auto`; an unknown or low-confidence automatic subject/source axis still requires review. Automatic text/filename analysis is not pixel inspection.

The bundled heuristic uses project text and reference filenames only. It is deliberately conservative: low medium confidence, low subject confidence, unknown nature, or mixed evidence sets `review_required=true`. Generic terms such as `真人` select photoreal rendering but do not prove an actual identity. A multimodal review must inspect the actual image/video through the evidence workflow before persisting a manual profile. Folder labels such as `真人`, `动漫`, and `动画` are weak dataset labels and must not override the visual evidence. In particular, a near-real anime or CG render remains `human-like-fictional` even when it resembles a photographed person.

Use:

```powershell
python scripts/grok_video_studio.py visual-classify --text "3D anime heroine with realistic rendering"
python scripts/grok_video_studio.py visual-profile <project>
python scripts/grok_video_studio.py visual-evidence <project> <media.mp4> --frames 5
python scripts/grok_video_studio.py visual-review-record <manifest.json> --reviewer-id codex-multimodal --medium 3d-anime --subject fictional-character --subject-nature human-like-fictional --realism high --confidence 0.92 --identity-origin fictional-design --frame-evidence "frame-01=Rendered fictional face and modeled hair" --confirm
python scripts/grok_video_studio.py visual-review-apply <project> <manifest.json> <review-receipt.json> --confirm
```

The resolved profile injects a medium-specific generation policy into image and video prompts. `photoreal + real-person` preserves a confirmed supplied identity. `photoreal + synthetic-human` uses live-action grammar while prohibiting real-person claims and prioritizing hands, props, contact physics, liquids, and cloth. `human-like-fictional` remains a rendered/animated character regardless of realism.

## Pixel evidence and receipt

`visual-evidence` accepts JPG/PNG/WebP/BMP images and MP4/MOV/MKV/WebM/AVI videos. An image yields one normalized evidence frame; a video yields 1-9 evenly spaced interior frames. The source bytes, evidence frames, sampling contract, and manifest are SHA-256 bound. Repeating the same request resumes the existing evidence only when all hashes still match.

`visual-review-record` is intentionally separate from extraction. Codex or a human reviewer must cite visible observations against frame IDs, declare confidence, and record identity provenance. It produces `review-receipt.json`; replacing a prior review archives the old receipt. `visual-review-apply` rejects changed frames, changed manifests, pending receipts, missing `--confirm`, or unsupported fields.

Pixels can support `medium`, visible `subject`, perceived `realism`, and many fictional-design judgments. Pixels alone cannot establish that a face belongs to an actual person. A receipt may use `real-person` only when `identity_origin=captured-real-person`, provenance is explicitly confirmed, and a non-empty provenance explanation is recorded. Without that evidence, use `synthetic-human`, `human-like-fictional`, `mixed`, or `unknown` as appropriate.

## External benchmark

`visual-benchmark-build <dataset> <output> --per-group 3 --frames 3` selects cases by a stable relative-path hash and writes only derived evidence and local receipts to the chosen output. It does not copy source images/videos into the Skill. `visual-benchmark-score <benchmark.json>` validates every receipt and reports weak-label agreement. Directory categories are never treated as identity ground truth; for example, a `真人` folder allows both `synthetic-human` and provenance-confirmed `real-person`, while a near-real `动漫` case must not become `real-person`.
