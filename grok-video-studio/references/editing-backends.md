# Editing Backends

Editing is a separate, resumable stage after clean video generation. The clean `deliverables/final.mp4` is never overwritten; native output is `deliverables/final-edited.mp4`.

## Backend order

1. `auto`: use ChatCut only when ChatCut MCP tools are loaded and authorized in the current Codex task. A standalone Python process cannot discover task-scoped MCP tools, so its deterministic fallback is native FFmpeg.
2. `native`: use the bundled FFmpeg editor. It supports project-relative clip windows, hard cuts, dissolve/fade/slide/wipe transitions, a small filter preset set, optional music/voice/SRT burn, and resumable `edit-state.json`.
3. `chatcut`: explicit handoff target. The CLI records the plan but does not pretend that ChatCut is installed, logged in, or available in another user's task. The ChatCut-capable Skill must import the same edit-plan contract and return a rendered asset or a clear failure receipt.
4. `jianying-draft`: explicit experimental export target only. It is not a default fallback and cannot be assumed portable across users, Windows paths, Jianying versions, locales, or login state.

## Native workflow

```powershell
python scripts/grok_video_studio.py edit-plan <project> --backend auto --transition dissolve --transition-seconds 0.25 --filter cinematic
python scripts/grok_video_studio.py edit-validate <project>
python scripts/grok_video_studio.py edit <project>
```

`edit-plan.json` contains timeline inputs, effective windows, explicit transition reasons, filter scope, audio mix, subtitle delivery, and clean/edited delivery paths. `edit-state.json` stores the input/plan signature, normalized segment stages, output SHA-256, media metadata, and QA. Re-running `edit` with the same signature reuses completed output without any provider request. Changing a source clip or the plan produces a new signature.

The native v1 editor deliberately avoids opaque AI transitions, arbitrary third-party plugins, and destructive in-place edits. Transition durations must be shorter than both neighboring clips. Technical QA is automatic; identity, style, text, watermark, and creative quality still require human review.

## Jianying boundary

The current contract can produce a portable handoff bundle (media manifest, edit-plan, relink map, and compatibility report), but it does not claim to generate a universally importable Jianying draft. A real draft exporter requires fixtures from a specific Jianying build and a reversible test import on that same machine. MP4 delivery must remain available when draft export is unsupported or fails.
