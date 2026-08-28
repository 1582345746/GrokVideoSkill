# Dialogue And Optional Components

Dialogue is part of the project contract, not prose hidden in a prompt. The same schema works for text-to-video, image-to-video, every series episode, and sourced-news projects.

## Audio modes

| Mode | Local services | Control level | Main risk |
| --- | --- | --- | --- |
| `preserve` | None | Preserve existing audio | Source may be silent |
| `mute` | None | Deterministic silence | No speech |
| `native-dialogue` | None | Fast, provider-generated | Wording, voice, baked captions, and lip sync are not deterministic |
| `local-voice` | CosyVoice | Exact approved text, timing, subtitles, loudness | Mouth motion is not corrected |
| `local-lipsync` | CosyVoice + MuseTalk | Exact text/audio plus mouth correction | Larger install and slower GPU render |

`native-dialogue` sends `generate_audio=true` and injects timed lines into the video prompt. A real QuickAI acceptance test confirmed an audible AAC track, but the provider also burned Chinese dialogue into the image despite the clean-frame instruction. Always inspect and listen.

`local-voice` and `local-lipsync` require an already assembled clean source video. Run:

```text
python scripts/grok_video_studio.py dialogue-render <project> --burn-subtitles
```

The command synthesizes one WAV per line, resumes unchanged lines from `dialogue-state.json`, fits each line to its declared time window, builds `deliverables/dialogue-track.wav`, ducks source audio during speech, normalizes delivery loudness, writes `dialogue.srt`, and creates a separate dialogue video. It never overwrites `final.mp4`.

Subtitle delivery is always a reversible derivative. Keep `final.mp4`, export SRT, then choose `--style clean`, `cinematic`, or `news` when burning. For `native-dialogue`, first inspect the source for provider-baked captions; the CLI requires `--confirm-source-clean` before it will burn another subtitle layer. If the result is not approved, re-burn another style or deliver the clean master; no paid video regeneration is needed.

## Project fields

```json
{
  "audio": {
    "mode": "local-voice",
    "language": "zh-CN",
    "generate_audio": false,
    "preserve_source_audio": true,
    "duck_source_audio": true
  },
  "characters": [{
    "id": "lead",
    "name": "Lead",
    "identity": "Stable visual identity",
    "references": [],
    "voice": {
      "provider": "cosyvoice",
      "voice_id": "optional-model-speaker-id",
      "reference_audio": "assets/voices/lead.wav",
      "reference_text": "The exact words spoken in the reference recording.",
      "consent": "synthetic"
    }
  }],
  "shots": [{
    "id": "shot-001",
    "seconds": 6,
    "dialogue": [{
      "id": "line-001",
      "speaker": "lead",
      "text": "This is the approved spoken and subtitle text.",
      "start": 0.4,
      "end": 3.8,
      "emotion": "calm",
      "subtitle": true,
      "lip_sync": true
    }]
  }]
}
```

Line IDs must be unique across a project. Lines cannot overlap and must fit inside their shot. A line's speaker must be a selected project character. Do not duplicate dialogue under `subtitle` or `subtitles`; dialogue is the single source of truth. Preflight warns when the text is too dense for natural speech.

A zero-shot voice reference is allowed only when `consent` is `synthetic`, `owned`, or `licensed`, and `reference_text` is present. Do not clone public figures or third parties without rights.

## Component profiles

Run `components-plan --profile <profile>` before changing the machine.

- `core`: no local AI service.
- `native-dialogue`: no local AI service; uses the configured video provider.
- `local-voice`: pinned CosyVoice source, isolated Docker runtime, and CosyVoice model weights.
- `full-dialogue`: local-voice plus pinned MuseTalk source/runtime/weights.

After the user approves the profile, storage locations, model downloads, and Docker GPU use:

```text
python scripts/grok_video_studio.py components-configure --profile local-voice --source-root <path> --models-root <path>
python scripts/grok_video_studio.py components-install --profile local-voice --accept-downloads
python scripts/grok_video_studio.py components-setup --profile local-voice --accept-downloads --include-models
python scripts/grok_video_studio.py components-start --profile local-voice
python scripts/grok_video_studio.py components-doctor --profile local-voice
```

Managed host ports bind only to `127.0.0.1`. CosyVoice uses port `9880`; MuseTalk uses `9881`. On an 8 GB card, start one full-dialogue stage at a time with `components-start --profile full-dialogue --component cosyvoice`, then switch with `--component musetalk`; the switch stops the sibling managed container before starting the selected service. `--component all` is an explicit opt-in for machines with enough VRAM. `components-stop` stops and removes only the selected `gvs-*-service` containers; sources, model weights, and generated media remain.

For `local-lipsync` on an 8 GB card, render once with CosyVoice online so `dialogue-state.json` and the per-line WAV files are complete, stop/switch to MuseTalk, then rerun `dialogue-render`; cached lines are reused and the second pass only invokes lip sync.

Source commits are pinned in `assets/components.json`. An existing checkout with local changes or another origin is never overwritten. Docker images isolate CUDA/Python dependencies from the host and the installed Skill directory remains small and updateable.
