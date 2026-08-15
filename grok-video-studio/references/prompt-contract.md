# Prompt Contract

## Plan before generating

Write a concise story, then split it into shots that can each be represented by one starting frame and one motion instruction. Choose the number of shots from the target duration and scene changes; never assume eight. Keep every shot between 1 and 15 seconds.

## Prompt budget

The provider limit applies after `character_bible`, `style_bible`, and shot text are composed. Keep each final prompt at or below 4096 characters, with 3800 as the working ceiling. Prefer concise positive constraints and remove repeated adjectives. Never silently truncate a prompt.

## Single-sheet character master

For identity-critical projects, generate one image containing the same character's front, side, and back or full-body views. Keep the sheet neutral, clean, and free of extra people or explanatory text. Treat it as a master reference for image generation only.

For each shot, derive one scene keyframe from that master. Send that keyframe as the sole video reference unless the user explicitly requests another compatible reference. Do not send the multi-view sheet directly to image-to-video: multiple views in one frame can cause identity or subject confusion.

## Identity continuity

Put immutable details in `character_bible`: face, age range, hair, clothing, signature props, and prohibited changes. Reuse one approved master reference throughout related shots. Avoid conflicting identity descriptions in individual shots.

## Keyframe prompt

Combine character identity, environment, framing, lighting, visual style, and continuity constraints. Describe a single still moment; do not put a sequence of actions into the image prompt.

## Video prompt

Describe one continuous subject motion, one camera motion, environmental motion, pace, and ending pose. Use restrained motion for identity-critical portraits. State that facial features, hairstyle, clothing, body proportions, and background composition remain unchanged when required. Do not put dialogue, subtitles, music, or multiple scene cuts into a single generation prompt unless the provider explicitly supports them.

## Multi-reference use

Order references from most authoritative to least authoritative. Multiple files may be accepted by the image endpoint, but an upstream model may ignore later references. QuickAI New workflows should prefer the current shot keyframe as one video reference.
