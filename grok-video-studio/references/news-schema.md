# Sourced News Video Contract

News video is a research layer over a standard video project. Codex performs current web research, fills `news.json`, and then uses the normal T2V or I2V pipeline. The Python CLI validates evidence but does not crawl arbitrary websites by itself.

## Product flow

1. Run `news-init` to create `project.json`, `state.json`, and `news.json` without paid media requests.
2. Codex searches the live web within the requested region and time window. Compare publication time with event time and prefer primary or authoritative sources.
3. Select a current topic, record the selection rationale and actual search queries, and save at least two sources from distinct publishers.
4. Write atomic factual claims and map each claim to one primary source or at least two independent sources.
5. Map every video shot to narration and sourced claim IDs. Fill the standard project story and prompts from those verified segments.
6. Set editorial status to `verified` only after checking dates, names, numbers, source disagreement, and misleading visual implications.
7. Run `news-validate`, then standard `preflight` and `run`. A `news-video` project cannot generate while its evidence contract is incomplete or blocked.

## `news.json`

```json
{
  "version": 1,
  "topic": "Selected current topic",
  "region": "CN",
  "language": "zh-CN",
  "as_of": "2026-08-28T12:00:00+08:00",
  "created_at": 0,
  "selection": {
    "mode": "hot-topic-research",
    "window_hours": 24,
    "rationale": "Why this topic is current, relevant, and supportable",
    "search_queries": ["queries actually used"]
  },
  "sources": [{
    "id": "official-release",
    "title": "Exact page title",
    "publisher": "Publisher name",
    "url": "https://example.com/exact-page",
    "published_at": "2026-08-28T08:00:00+08:00",
    "accessed_at": "2026-08-28T12:00:00+08:00",
    "source_type": "primary",
    "visual_rights": "facts-only"
  }, {
    "id": "independent-report",
    "title": "Exact report title",
    "publisher": "Another publisher",
    "url": "https://news.example.com/exact-report",
    "published_at": "2026-08-28T09:00:00+08:00",
    "accessed_at": "2026-08-28T12:00:00+08:00",
    "source_type": "secondary",
    "visual_rights": "facts-only"
  }],
  "claims": [{
    "id": "claim-001",
    "text": "One precise factual statement",
    "source_ids": ["official-release"]
  }],
  "script_segments": [{
    "shot_id": "shot-001",
    "narration": "Narration limited to verified facts and clearly marked analysis",
    "claim_ids": ["claim-001"]
  }],
  "editorial": {
    "status": "verified",
    "fact_checked_at": "2026-08-28T12:05:00+08:00",
    "unresolved_conflicts": [],
    "corrections": []
  }
}
```

## Evidence rules

- Sources must be exact HTTPS pages, not search-result URLs. Do not store credentials in URLs.
- Use at least two distinct publishers. A claim needs one primary source or two independent sources.
- `primary` means an official filing, release, statement, public dataset, court document, standards body, or direct accountable source. A repost is not primary.
- Keep disputed facts out of narration until resolved. If disagreement is itself newsworthy, describe the disagreement and cite each side; keep editorial status blocked until the script is accurately qualified.
- Separate fact from analysis. Do not invent quotations, eyewitness detail, motives, numbers, or causal claims.
- `visual_rights=facts-only` means use facts but do not copy that source's images or video. Use generated explanatory visuals unless an asset is licensed, public domain, or user-provided.
- Never present an AI reconstruction as authentic event footage. When realistic reconstruction could mislead, add a post-produced label such as “AI生成示意画面”.

## Subtitles and sources

News `script_segments[].narration` is a fallback source for `subtitles`; explicit shot subtitle cues take priority. Export a sidecar SRT, then burn a separate subtitled video if needed. Keep `news.json` with the delivery as the machine-readable source manifest.
