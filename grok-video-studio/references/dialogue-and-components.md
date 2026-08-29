# Dialogue And Optional Components

Dialogue is part of the project contract, not prose hidden in a prompt. The same schema works for text-to-video, image-to-video, every series episode, and sourced-news projects.

## Audio modes

| Mode | Local services | Control level | Main risk |
| --- | --- | --- | --- |
| `preserve` | None | Preserve existing audio | Source may be silent |
| `mute` | None | Deterministic silence | No speech |
| `native-dialogue` | None | Fast, provider-generated | Wording, voice, baked captions, and lip sync are not deterministic |
| `local-voice` | Approved Voicebox or CosyVoice provider | Exact approved text, timing, subtitles, loudness | Mouth motion is not corrected |
| `local-lipsync` | Approved TTS provider + MuseTalk | Exact text/audio plus mouth correction | Larger install and slower GPU render |

`native-dialogue` injects timed lines into the video prompt. QuickAI uses its current JSON video contract without a non-standard `generate_audio` field; QuickAI New sends `generate_audio=true` in its multipart contract. A real QuickAI acceptance test confirmed an audible AAC track, but the provider also burned Chinese dialogue into the image despite the clean-frame instruction. Always inspect and listen.

`qa` exposes a `blocking_review_items` entry for native-dialogue clips and deliverables. A human must inspect the exported first/key/end frames; any model-baked captions or dialogue text blocks clean delivery even when the audio track is present and technically healthy.

`local-voice` and `local-lipsync` require an already assembled clean source video. Run:

```text
python scripts/grok_video_studio.py dialogue-render <project> --burn-subtitles
```

The command synthesizes one WAV per line, resumes unchanged lines from `dialogue-state.json`, fits each line to its declared time window, builds `deliverables/dialogue-track.wav`, ducks source audio during speech, normalizes delivery loudness, writes `dialogue.srt`, and creates a separate dialogue video. It never overwrites `final.mp4`.

Subtitle delivery is always a reversible derivative. Keep `final.mp4`, export SRT, then choose `--style clean`, `cinematic`, or `news` when burning. For `native-dialogue`, first inspect the source for provider-baked captions; the CLI requires `--confirm-source-clean` before it will burn another subtitle layer. If the result is not approved, re-burn another style or deliver the clean master; no paid video regeneration is needed.

`audio.subtitle_source` controls local subtitle artifacts: `upstream` keeps provider/source pixels and creates no local SRT, `project` uses the approved dialogue/cue contract, and `none` suppresses subtitle delivery. New projects use `none`; use `subtitles --source project` to deliberately create a reviewed local subtitle derivative. This choice does not change the audio track or lip-sync route.

## Project fields

```json
{
  "audio": {
    "mode": "native-dialogue",
    "language": "zh-CN",
    "generate_audio": true,
    "preserve_source_audio": true,
    "duck_source_audio": true,
    "subtitle_source": "project",
    "tts_provider": "voicebox",
    "allow_temporary_voices": false,
    "allow_shared_voices": false
  },
  "characters": [{
    "id": "lead",
    "name": "Lead",
    "identity": "Stable visual identity",
    "references": [],
    "voice": {
      "provider": "voicebox",
      "voice_type": "preset",
      "voice_status": "approved",
      "preset_engine": "qwen_custom_voice",
      "preset_voice_id": "Dylan",
      "provider_profile_id": "voicebox-profile-id",
      "model": "Qwen/Qwen3-TTS-12Hz-0.6B-CustomVoice",
      "model_revision": "85e237c12c027371202489a0ec509ded67b5e4b5",
      "seed": 42,
      "source_license": "Apache-2.0",
      "approved_by": "user"
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

## Voice casting and providers

`audio.mode` chooses the production behavior; `audio.tts_provider` chooses the default synthesis provider. A character's `voice.provider` can override the default. Existing projects with a valid legacy `voice_id` or rights-cleared reference and no explicit status remain compatible and are treated as already approved.

Run `voice-list`, then `voice-audition`. The audition is stored under `assets/voice-auditions/<character>/`, technically analyzed, and recorded in `voice-catalog.json`; it does not change the active character voice. Only `voice-approve` changes the contract. `series-voice-sync` copies only approved voices into episodes. `temporary-test` is blocked unless the series explicitly sets `audio.allow_temporary_voices=true`.

Voicebox is accessed only through its loopback REST API. The supported production slice is Qwen CustomVoice preset speech (0.6B and 1.7B) and Voicebox's Kokoro presets. Voicebox output is downloaded into the video project, so dialogue recovery never depends on Voicebox database paths. CosyVoice remains supported through the same provider registry. VoxCPM is experimental: a design prompt is not a stable production identity and must first produce a reviewed master audio.

The cache signature contains text, speaker, provider, model revision, preset/profile identity or reference hash, seed, and performance controls. Recasting one character rebuilds only that character's dialogue and subsequent lip-sync derivative. It does not recreate clean video clips, keyframes, or subtitle text.

## Component profiles

Run `install-plan --profile <basic|upstream-dialogue|precise-subtitles|precise-voice|lip-sync>` before changing the machine. It is side-effect free and is the user-facing capability plan. Run `components-plan --profile <profile>` for lower-level source checkout and model details.

The user-facing profiles map to component profiles as follows:

| Install profile | Local service | Subtitle default | Alias |
| --- | --- | --- | --- |
| `basic` | none | `upstream` | `core` |
| `upstream-dialogue` | none | `none` | `native-dialogue` |
| `precise-subtitles` | none | `project` | none |
| `precise-voice` | CosyVoice | `project` | `local-voice` |
| `lip-sync` | CosyVoice + MuseTalk | `project` | `full-dialogue` |

- `core`: no local AI service.
- `native-dialogue`: no local AI service; uses the configured video provider.
- `local-voice`: pinned CosyVoice source/runtime when reference cloning is selected; Voicebox/Qwen preset casting uses its own isolated Python 3.12 service plan.
- `full-dialogue`: local-voice plus pinned MuseTalk source/runtime/weights.

After the user approves the profile, storage locations, model downloads, and Docker GPU use:

```text
python scripts/grok_video_studio.py components-configure --profile local-voice --source-root <path> --models-root <path>
python scripts/grok_video_studio.py components-install --profile local-voice --accept-downloads
python scripts/grok_video_studio.py components-setup --profile local-voice --accept-downloads --include-models
python scripts/grok_video_studio.py components-start --profile local-voice
python scripts/grok_video_studio.py components-doctor --profile local-voice
```

For Voicebox, run the read-only plan first:

```text
python scripts/grok_video_studio.py voicebox-setup-plan --source <voicebox-repo> --models-root <models-root> --data-root <data-root>
```

The plan pins the Voicebox source commit and Qwen model revision, checks `uv`, isolated Python 3.12, GPU memory, E-drive storage, model cache, and loopback health. The managed service uses `HF_HUB_OFFLINE=1` after the pinned snapshot is present, and the adapter compares Voicebox's actual cache `refs/main` with the approved revision before generation. Codex may execute the approved setup stages for the user, but no environment creation, dependency install, model download, service start, or audition is implicit.

Managed host ports bind only to `127.0.0.1`. CosyVoice uses port `9880`; MuseTalk uses `9881`. On an 8 GB card, start one full-dialogue stage at a time with `components-start --profile full-dialogue --component cosyvoice`, then switch with `--component musetalk`; the switch stops the sibling managed container before starting the selected service. `--component all` is an explicit opt-in for machines with enough VRAM. `components-stop` stops and removes only the selected `gvs-*-service` containers; sources, model weights, and generated media remain.

For `local-lipsync` on an 8 GB card, render once with CosyVoice online so `dialogue-state.json` and the per-line WAV files are complete, stop/switch to MuseTalk, then rerun `dialogue-render`; cached lines are reused and the second pass only invokes lip sync.

Source commits are pinned in `assets/components.json`. An existing checkout with local changes or another origin is never overwritten. New checkouts are staged in a temporary sibling and published only after the pinned commit is verified; failed updates restore the prior commit. Docker images use commit-specific tags, so a failed build does not replace the prior runtime image. Docker isolates CUDA/Python dependencies from the host and the installed Skill directory remains small and updateable.

Model setup is resumable and auditable. Each manifest entry declares an estimated size and required file patterns. Before a download, the manager checks free space with a 512 MiB safety margin. After a successful download, `.gvs-model-state.json` records the component, repository revision, destination, required-file sizes, and SHA-256 digests. A complete existing directory can be adopted without a second download; a partial directory resumes through Hugging Face's cache, and a later digest/size mismatch causes that model to be repaired. The service launcher checks required files before mounting them.

The standalone installer lifecycle is recoverable: `install.ps1 -Check` is read-only, `-Repair -Force` performs a transactional replacement, and `-Uninstall` removes only Skill files. A timestamped previous-copy backup is retained after an upgrade; user credentials, projects, component sources, and model weights are outside that directory and are preserved. Missing FFmpeg/Docker can be installed only with explicit `-InstallSystemDependencies -AcceptSystemDependencyChanges` through winget; NVIDIA drivers are never replaced automatically.
