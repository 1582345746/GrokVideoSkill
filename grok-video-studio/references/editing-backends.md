# Editing Backends

Editing is a separate, resumable stage after clean video generation. The clean `deliverables/final.mp4` is never overwritten; native output is `deliverables/final-edited.mp4`.

## Backend order

1. `auto`: use ChatCut only when the current host exposes and authorizes the required ChatCut MCP tools. A standalone Python process can detect the installed plugin and create a contract, but cannot discover task-scoped tools; its deterministic fallback is native FFmpeg.
2. `native`: use the bundled FFmpeg editor. Edit-plan v2 supports project-relative clip windows, per-shot speed and filters, mixed hard-cut/transition boundaries, loudness normalization, optional music/voice/SRT burn, preview evidence, and resumable `edit-state.json`.
3. `chatcut`: explicit task-scoped adapter target. The CLI emits `integration_contract` with semantic operations, required tools, source media hashes, and a receipt schema. The ChatCut-capable Skill must import the same edit-plan contract, keep the timeline editable, re-read and visually verify it, and return a rendered asset or a clear failure receipt.
4. `jianying-draft`: explicit experimental export target only. It is not a default fallback and cannot be assumed portable across users, Windows paths, Jianying versions, locales, or login state.

## Native workflow

```powershell
python scripts/grok_video_studio.py edit-plan <project> --backend auto --transition dissolve --transition-seconds 0.25 --filter cinematic
python scripts/grok_video_studio.py edit-plan <project> --shot-filter shot-002=warm --shot-speed shot-003=1.25 --boundary shot-001=cut --boundary shot-002=wipe-left:0.30 --normalize-lufs -16
python scripts/grok_video_studio.py edit-validate <project>
python scripts/grok_video_studio.py edit <project>
```

`edit-plan.json` v2 contains timeline inputs, speed, per-shot/global filters, effective windows, one explicit transition per boundary, audio mix, subtitle delivery, preview policy, and clean/edited delivery paths. `edit-state.json` stores the input/plan signature, normalized segment stages, output SHA-256, media metadata, QA, and hash-bound preview frames. Re-running `edit` with the same signature reuses completed output without any provider request. Changing a source clip or the plan produces a new signature.

Edit-plan v2 reads v1 plans through a non-destructive in-memory migration: speed defaults to 1.0, per-shot filters default empty, global filters and boundary transitions retain their meaning, and the existing `-16` LUFS target is preserved. Transition durations must be shorter than both neighboring clips after speed adjustment. A timeline may mix cuts and transitions; continuous hard-cut runs are normalized as deterministic intermediate groups before FFmpeg crossfades are applied. Technical QA and preview extraction are automatic; identity, style, text, watermark, transition taste, and creative quality still require human review.

The native editor deliberately avoids opaque AI transitions, arbitrary third-party plugins, and destructive in-place edits. `--no-normalize` is available when preserving source loudness is intentional; otherwise the plan records a target from -24 to -10 LUFS. Preview frames include the start, transition boundaries, and end up to nine images under `deliverables/edit-preview/`.

## ChatCut receipt gate

The current Codex task must actually expose and authorize ChatCut tools before an agent can submit a handoff. `chatcut-capabilities` reports plugin installation separately from task-tool visibility; a JSON packet is not proof that a timeline was changed. A successful adapter must return `schema_version=1`, `status=completed`, `remote_project_id`, `remote_timeline_id`, `rendered_asset.path` inside the project, matching `output_sha256`, matching `source_plan_sha256`, `verification.structural=true`, `verification.visual=true`, a non-empty `tool_trace`, `confirmed=true`, and an empty `unmapped_features` array. `chatcut-receipt-validate` then applies the same MP4 and technical QA gates as native output and archives the receipt by hash. When tools are absent, `auto` stays native and the Skill must not simulate an integration.

## Jianying boundary

The current contract can produce a portable handoff bundle (media manifest, edit-plan, relink map, and compatibility report), but it does not claim to generate a universally importable Jianying draft. The observed Windows installation exposes an opaque, version-coupled payload rather than a stable public JSON contract. A real draft exporter requires fixtures from a specific Jianying build, a reversible test import on that same machine, and a portable failure fallback. Because every user's operating system, paths, Jianying build, locale, account, and draft format differ, machine-local detection is never enough to enable this by default. MP4 delivery must remain available when draft export is unsupported or fails.
