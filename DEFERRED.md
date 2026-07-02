# Deferred items (revisit at Gate 6 / docs)

Non-blocking polish and doc tasks parked during earlier gates. None block the
required features (filters, markers, toggles, playback, heatmaps).

> **Running log.** This file is the running log of caveats + deferrals. At
> Gate 6 it converges with `BUILD_BRIEF_ADDENDUM.md` into the final
> ARCHITECTURE.md / INSIGHTS.md — they merge, they don't compete.

## Gate 6 — polish
- **Constant-size markers on zoom (Google Maps pin behavior).** Markers
  currently scale with the map when zooming; desired is fixed *screen* size,
  anchored to their map location, while only the map zooms/pans underneath.
  Medium effort: draw markers on a separate layer at fixed pixel size,
  counter-scaled and repositioned on each zoom/pan. Deferred until after
  toggles/playback/heatmaps exist.
- **Minimap image weight.** `AmbroseValley_Minimap.png` is ~9.7 MB. Compress /
  convert (WebP or optimized PNG/JPG) so the deployed site loads fast.

## Docs (Gate 6)
- **README:** document that the 16 bot-only (zero-human) matches are excluded
  from `matches.json`; cite them as supporting evidence for the solo-PvE /
  low-population insight.
- **README:** document the pipeline regenerate command
  (`python pipeline/build_data.py`).
- **ARCHITECTURE.md / INSIGHTS.md:** GrandRift heatmaps are inherently sparse
  (~51 deaths) — note as a documented limitation, not a bug.
- **Data caveat — `bots` field is NOT swarm size.** `bots` counts distinct bot
  `user_id`s that emitted `BotPosition` telemetry, which is a different (often
  smaller) population than the bots the player fought. `BotKill` rows carry only
  the killer's id, never the victim's, so kills can't be mapped to distinct
  bots. Result: `botkills > bots` in 563/780 matches (72%), and some matches
  have `bots = 0` with `botkills = 38`. Treat `bots` only as "bots with logged
  movement" (i.e. how many bot-position rings render); use `botkills` for
  combat. Document this so nobody reads `bots` as opponents-faced.
- **Data caveat — `BotPosition` telemetry is incomplete.** The
  `bots=0` / `botkills=38` cases prove many bots logged no position at all.
  Consequence: any bot-position-based layer (run-view bot rings, map-view
  traffic/heatmap bot contribution) **undercounts actual bot presence**. Note
  this wherever bot-position data is visualized so it isn't read as complete.
