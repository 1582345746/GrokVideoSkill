# Prompt Contract

## Plan before generating

Write a concise story, then split it into shots that can each be represented by one starting frame and one motion instruction. Choose the number of shots from the target duration and scene changes; never assume eight. Keep every shot between 1 and 15 seconds.

## Prompt budget

The provider limit applies after `character_bible`, `style_bible`, structured shot continuity, series episode continuity, clean-frame policy, audio design, and shot text are composed. The hard boundary is 4096 UTF-8 bytes; keep the selected prompt at or below the 3800-byte working ceiling. Preflight reports full/compact/minimal variants, Unicode character count, UTF-8 byte count, remaining space, selected version, and a compression suggestion. Generation stores the original, all variants, and the final sent version. Prefer concise positive constraints and remove repeated adjectives. Never silently truncate a prompt. If the minimal complete version still exceeds the hard limit, stop and ask for a shorter identity, location, action, dialogue, or ending pose.

## Single-sheet character master

For identity-critical projects, generate one image containing the same character's front, side, and back or full-body views. Keep the sheet neutral, clean, and free of extra people or explanatory text. Treat it as a master reference for image generation only.

For each shot, derive one scene keyframe from that master. Send that keyframe as the sole video reference unless the user explicitly requests another compatible reference. Do not send the multi-view sheet directly to image-to-video: multiple views in one frame can cause identity or subject confusion.

## Identity continuity

Put immutable details in `character_bible`: face, age range, hair, clothing, signature props, and prohibited changes. Reuse one approved master reference throughout related shots. Avoid conflicting identity descriptions in individual shots.

For a series, keep immutable identity in `series.json.characters` and use one persistent master per enabled character. `character_ids` selects the relevant master references for each keyframe. Before a new episode, use the prior accepted episode's reviewed end-state summary as continuity input. Do not assume that a planned wardrobe, prop, or ending survived generation until visual review confirms it.

## Keyframe prompt

Combine character identity, environment, framing, lighting, visual style, and continuity constraints. Describe a single still moment; do not put a sequence of actions into the image prompt.

## Video prompt

Describe one continuous subject motion, one camera motion, environmental motion, pace, and ending pose. Use restrained motion for identity-critical portraits. State that facial features, hairstyle, clothing, body proportions, and background composition remain unchanged when required. Add shot role, location, time, weather, lighting, props, environment sound, sound effects, and emotional delivery when they affect the shot. Unless the script explicitly calls for an interface, keep the frame clean: no app UI, controls, counters, comments, captions, logos, watermarks, or stickers. Native dialogue prompts include approved speaker text plus sound design; local modes ask for natural speaking motion while supplying exact speech and subtitles in post-production. Never ask the video model to draw ordinary subtitles, music controls, or multiple scene cuts.

## Multi-reference use

Order references from most authoritative to least authoritative. Selected `characters[].references` are added automatically before explicit `shot.image_references`. Multiple files may be accepted by the image endpoint, but an upstream model may ignore later references. Both video providers should receive only the current shot keyframe for image-to-video, never the multi-view master sheet.
