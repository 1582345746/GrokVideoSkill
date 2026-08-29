# Grok Video Studio Productization Checklist

This checklist is the release contract for the two supported distribution paths.

Current decision (2026-08-29): v2.0.0 `upstream-first` is the active contract. Codex-managed installation from the repository is the priority distribution route; the standalone installer remains transparent and backward compatible. The unchecked signing, fresh-machine, and public-release gates below are intentionally deferred rather than treated as complete.

## Distribution Contracts

- [x] Codex-managed installation is documented in `grok-video-studio/SKILL.md`.
- [x] Provider keys are accepted through one stdin JSON payload and stored with Windows DPAPI.
- [x] The Skill directory and project contracts reject credential-like fields.
- [x] `install-plan` is side-effect free and reports profile, dependencies, key roles, GPU requirement, model storage, and consent.
- [x] User-facing profiles are `basic`, `upstream-dialogue`, `precise-subtitles`, `precise-voice`, and `lip-sync`.
- [x] Legacy component names remain accepted as aliases.
- [x] Standalone `install.ps1 -Interactive` selects a profile, prompts for keys without echo, and asks before local model downloads.
- [x] `install.ps1 -Check`, `-Repair -Force`, and `-Uninstall` have explicit, bounded behavior.
- [x] Skill upgrades preserve a timestamped previous copy and restore it when the copy/validation phase fails.

## Media Capability Contracts

- [x] Text-to-video and image-to-video remain separate provider routes.
- [x] Supplied-image animation is an image-to-video project, not a third route.
- [x] Character master sheets and per-shot keyframes are reusable in standalone and episodic projects.
- [x] Series projects keep season canon, episode approval, and accepted continuity state.
- [x] News projects require sources and claim mappings before paid generation.
- [x] `audio.subtitle_source=upstream|project|none` is independent from audio mode.
- [x] Local subtitle burn creates a derivative and never overwrites `final.mp4`.
- [x] Native provider dialogue is explicitly labeled generative and requires visual/listening review.
- [x] Local voice and lip-sync are optional and can resume from cached line audio.
- [x] Clean-frame prompts default to no UI, comments, captions, logos, watermarks, or stickers.
- [x] QuickAI and QuickAI New capabilities report T2V and I2V independently, including model-default versus explicit native audio.
- [x] Video reference/edit/extend and preset/file audio reference are explicitly unsupported and blocked in preflight.
- [x] New projects default to native upstream dialogue (`generate_audio=true`, `subtitle_source=none`); local subtitles remain opt-in.
- [x] Prompt limits are counted as UTF-8 bytes with full/compact/minimal variants and no silent truncation.
- [x] Image, video, and character-master create attempts share a three-total-attempt budget; failover counts and `submission_unknown` is not recreated.
- [x] Series contracts include theme, escalation, midpoint, climax, ending hook, shot-role taxonomy, continuity ledger inputs, and dynamic shot planning.
- [x] Technical QA separates clean-frame, embedded subtitle, audio-track, and visual/manual review gates; review frames identify first/key/end.

## Security And Permissions

- [x] No provider key is present in source, documentation examples, project JSON, command-line arguments, or Git history introduced by this release.
- [x] Optional service URLs are restricted to host loopback.
- [x] Component sources and model revisions are pinned.
- [x] Docker/model downloads require explicit `--accept-downloads` approval.
- [x] Uninstall preserves credentials, projects, component checkouts, and model weights.
- [ ] Release artifacts are code-signed and published with checksums.
- [x] A secret-pattern scanning job runs in GitHub Actions for every push and pull request.

## Dependency And Upgrade Work

- [x] FFmpeg/ffprobe, Docker, and NVIDIA availability are reported before optional setup.
- [x] CosyVoice and MuseTalk Docker builds are isolated from the host Python environment.
- [x] 8 GB GPU staged startup is documented and enforced for the full-dialogue profile.
- [x] Optional FFmpeg/Docker system dependency installation through a separately approved winget step; NVIDIA drivers remain manual.
- [x] Disk-space preflight, resumable model downloads, and per-file integrity verification.
- [x] Component source/runtime migration and rollback after a failed upgrade.
- [x] Standalone release builder emits a version manifest and SHA-256 checksum and supports optional Authenticode signing.
- [ ] A signed standalone release bundle that includes the installer and version manifest.

## Release Acceptance

- [x] Python unit/integration suite covers the v2 contracts and passes on the Windows development machine; the same suite is configured for Windows and Ubuntu CI.
- [x] Installed copy reports version `2.0.0` and passes `-Check`.
- [x] Real QuickAI/QuickAI New T2V and I2V acceptance artifacts are recorded for this v2 release in `docs/paid-acceptance-2026-08-29.md`.
- [x] No local service is started by the default `upstream-dialogue` installation.
- [x] Interactive installer end-to-end test with redacted test credentials in an isolated Windows profile.
- [x] All five profiles install, persist their selection, and pass `-Check` in isolated directories.
- [ ] Fresh-machine acceptance for each of the five profiles.
- [x] Reinstall/repair/uninstall acceptance with a locked file and a simulated failed component download.
- [ ] Final release on `main` plus GitHub Release notes, installation wording, and user usage wording.

The unchecked items are release-blocking for a signed public installer, but they do not prevent Codex-managed use of the current Skill or the transparent PowerShell installer.
