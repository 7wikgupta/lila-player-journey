# BUILD BRIEF — LILA BLACK Player Journey Visualization Tool

> This is a handoff document. It captures every decision, data finding, and formula
> worked out during the analysis phase so the build can proceed without re-deriving anything.
> Read it fully before writing code. When something here conflicts with a guess you'd
> otherwise make, this document wins — these decisions came from inspecting the real data.

---

## 0. What we're building & why it's shaped this way

A web tool that lets a **Level Designer** explore player behaviour on 3 maps of LILA BLACK,
an extraction shooter. Built as a **static site** (React + Vite, deployed on Vercel) because
the dataset is tiny and never changes — all heavy work is done **once, offline, in a Python
pipeline** that emits small JSON the frontend just reads and draws.

**Two primary views, toggle between them:**
- **Run view** — one match = one player's journey arc on the minimap, with timeline playback.
- **Map view** — aggregate heatmaps + a best-match ranking + drop-off/aha metrics across all runs.

**Hard requirements (from the assignment):**
parse parquet · plot journeys on correct minimap with correct coord mapping · humans vs bots
visually distinct · distinct markers for kill/death/loot/storm · filter by map/date/match ·
timeline playback · heatmap overlays (kill/death/traffic) · hosted at a shareable URL ·
GitHub repo with README + one-page ARCHITECTURE.md + INSIGHTS.md (3 insights).

---

## 1. THE DATA — ground truth (verified by inspecting all 1,243 files)

- Format: Apache **Parquet**, despite `.nakama-0` extension (no `.parquet`). Any parquet reader opens it by path.
- Filename: `{user_id}_{match_id}.nakama-0`
- **Bot vs human:** `user_id` is a **UUID → human**; `user_id` is a **short numeric string → bot**.
  Detection: `user_id.isdigit()` → bot.
- Schema columns: `user_id` (str), `match_id` (str), `map_id` (str), `x` `y` `z` (float32),
  `ts` (timestamp[ms]), `event` (**binary/bytes** — must `.decode('utf-8')`).
- `y` = elevation. **For 2D minimap use only `x` and `z`.** Ignore `y` for plotting.
- Totals: **89,104 rows**, **1,243 files**, **796 matches**, **245 unique humans**, Feb 10-14 2026 (Feb 14 partial).

### Event types & their REAL counts (whole dataset)
```
Position        51347   human movement sample
BotPosition     21712   bot movement sample
Loot            12885   item pickup
BotKill          2415   human killed a bot
BotKilled         700   bot killed a human
KilledByStorm      39   died to storm   (rare but present)
Kill                3   human killed human   (effectively nonexistent)
Killed              3   human killed by human
```

### TWO CRITICAL DATA FINDINGS — do not miss these

**FINDING 1 — `ts` is SECONDS, not milliseconds.**
The README claims `ts` is "ms elapsed within match." This is WRONG at face value.
The raw int64 magnitudes are tiny: a full 16-player match spans only ~523 raw units, and
consecutive Position samples differ by ~5 units. If those were ms, a whole match would last
<1 second — impossible. Interpreted as **seconds**: matches become ~6-9 minutes (correct for
battle royale) and Position sampling is every ~5s (correct).
→ **Treat raw int64 as seconds.** Get it via `df['ts'].astype('int64')` (gives the raw value;
   it lands in nanoseconds-as-int depending on reader, so verify: the per-match span should be
   roughly 130-890, i.e. seconds). Normalize per match: `t_rel = ts_raw - match_min_ts`.
→ Document this as the headline assumption in ARCHITECTURE.md.

**FINDING 2 — matches are effectively SOLO PvE, not PvP.**
779 of 796 matches have exactly **1 human** (max 2). Human-vs-human kills = 3 in the entire
dataset. The real game loop is one human vs a swarm of bots + a closing storm. The official
"PvPvE" framing is the *design intent*; the telemetry shows the *current reality* is solo PvE —
almost certainly because the early-2026 player population is too low to fill lobbies, so
matchmaking backfills with bots.
→ Consequences for the build:
   - Kill/death heatmaps MUST be built on `BotKill` / `BotKilled` (+ `KilledByStorm`),
     NOT `Kill`/`Killed` (which are empty).
   - "A match" ≈ "a player's journey" (only one human in it).
   - This gap (designed-for-PvPvE, played-as-solo-PvE) is itself a top INSIGHTS.md entry:
     maps designed for contested multiplayer extraction are being experienced as solo corridors.

---

## 2. COORDINATE MAPPING (the part they grade hardest)

Minimaps are **1024×1024 px**. Per-map config (from README):

| Map | Scale | Origin X | Origin Z | Minimap file |
|-----|-------|----------|----------|--------------|
| AmbroseValley | 900 | -370 | -473 | AmbroseValley_Minimap.png |
| GrandRift     | 581 | -290 | -290 | GrandRift_Minimap.png |
| Lockdown      | 1000| -500 | -500 | Lockdown_Minimap.jpg |

Conversion (world (x,z) → pixel):
```
u = (x - origin_x) / scale
v = (z - origin_z) / scale
pixel_x = u * 1024
pixel_y = (1 - v) * 1024     # Y flipped: image origin is top-left
```
Verified example (AmbroseValley): world x=-301.45, z=-355.55 → pixel (78, 890).

**Decision: pipeline emits BOTH pixel coords AND raw world coords** in the JSON.
Pixel makes the frontend trivial; raw world coords are cheap insurance + good for ARCHITECTURE.md
evidence. Validate visually in Gate 3 by overlaying one match and checking points land sensibly.

---

## 3. PIPELINE OUTPUT — four JSON files (this is the contract the frontend reads)

Pipeline = one Python script run once in the `.venv`. Reads raw parquet from the data folder,
writes JSON into `public/data/`. **Commit the JSON into the repo** (it's only a few MB) and
**document the regenerate command** in the README.

### 3a. `matches.json` — the index (drives all filtering + rankings)
Array, one entry per match:
```json
{
  "match_id": "...", "map_id": "AmbroseValley", "date": "February_11",
  "humans": 1, "bots": 12, "duration_s": 523,
  "loot": 24, "botkills": 5, "died_storm": 0, "died_bot": 1,
  "died_before_first_loot": false,
  "best_score": 0.78        // see formula §4c
}
```

### 3b. `match_{match_id}.json` — full event stream for one run (lazy-loaded)
Array of events, time-normalized and pre-mapped:
```json
{ "t": 0, "event": "Position", "px": 78, "py": 890, "x": -301.45, "z": -355.55, "is_bot": false }
```
Include both human and bot events (bots needed to show the swarm). Sort by `t`.

### 3c. `heatmaps_{map_id}.json` — precomputed grids (§4a)
```json
{ "grid": 32,
  "traffic":  [[..32 rows of 32 ints..]],
  "deaths":   [[...]],   // BotKilled + KilledByStorm
  "kills":    [[...]],   // BotKill
  "loot":     [[...]] }
```

### 3d. `players.json` — per-player index (cheap extra, enables a PM "player profile" lens later)
```json
{ "user_id": "...", "matches": 3, "total_loot": 52, "total_botkills": 9,
  "avg_duration_s": 410, "match_ids": ["...","...","..."] }
```

---

## 4. AGGREGATION FORMULAS (precomputed in pipeline — the insight engine)

### 4a. Heatmap grids — resolution = 32×32 (derived, not arbitrary)
Reasoning: a cell needs enough events to read as signal. Death layer is the scarcest important
layer. AmbroseValley deaths = BotKilled(486)+Storm(17)=503, and data occupies ~56% of grid area.
At 64×64: ~0.22 deaths/usable cell → noise. At 32×32: ~0.9/cell → clusters form. Traffic (36k pts)
is dense enough at 32×32 to look smooth. So **32×32 for all layers** — sized for the sparse layer.
Note in ARCHITECTURE.md: "chose 32×32 because death events are sparse; finer grids produced noise."
GrandRift is sparse everywhere (46 deaths) → its heatmaps are inherently weak; note as a limitation.
Binning: map each event's (px,py) to cell = (px//(1024/32), py//(1024/32)); count per cell.
Dead-space = fraction of usable traffic cells with count 0.

### 4b. Survival / drop-off curve (per map)
For each run, end_time = max(t_rel). For minute m in 0..15:
`alive[m] = count(runs where end_time >= m*60) / total_runs`.
Real AmbroseValley shape: min0=100%, min3≈82%, min6≈52%, min9≈4%. Median run ≈ 356s.
Render as line/area chart per map. Reading: where's the cliff.

### 4c. Best-match ranking
Per match, normalize each component by its 95th-percentile (cap at 1) so outliers don't dominate:
```
score = 0.4*norm(duration_s) + 0.3*norm(loot) + 0.3*norm(botkills)
        - 0.5 if died_before_first_loot
```
Weights are a documented judgment call (engagement = survival + progression + combat).
Panel shows ranked list: match_id, map, score, the 3 components as bars, "open in run-view" button.

### 4d. Per-map scoreboard — REAL computed values (humans only)
```
                 AmbroseValley   GrandRift   Lockdown
runs                 554            57          170
median duration      356s          403s        427s
median loot           15            13          12
median botkills        2             2           1
death-by-bot rate     53%           42%         47%
storm-death rate       3%            9%          10%
pre-loot death rate    5%            7%          16%   <-- Lockdown 3x AmbroseValley
```
Lockdown's 16% pre-loot death is a genuine finding: close-quarters map kills players before
their first-loot "aha" moment far more often. INSIGHTS.md candidate.

### 4e. Exit-mode breakdown (overall, humans)
```
died to bot:  403 (52%) · died to storm: 39 (5%) · no death / extracted: 339 (43%)
```
Caveat to document: "no death event" is INFERRED as extraction/survival; may include
disconnects or truncated telemetry. Render as stacked bar / donut, per map + overall.

### Aha-moment stats
Time-to-first-loot: median ~42s, p90 ~102s. **57 humans (7%) never loot at all** = died before
the core progression moment = worst experiences. For a casual mobile game this is the churn risk.

---

## 5. BUILD PLAN — gates (we are entering Gate 2)

- **Gate 1 ✅ DONE** — Vite+React scaffold, GitHub repo (`7wikgupta/lila-player-journey` /
  also pushed under austin316 vercel), deployed empty to Vercel. Deploy pipe proven.
- **Gate 2 — pipeline:** the Python script producing the 4 JSON files above. Verify output by eye
  before trusting. Put script in `pipeline/`, output to `public/data/`.
- **Gate 3 — static minimap + coord proof:** load one map image, plot one match, VISUALLY confirm
  points land sensibly (loot near structures, not in voids). De-risks coordinate mapping early.
- **Gate 4 — filters + markers:** map/date/match selectors; distinct markers for
  kill/death/loot/storm; humans vs bots visually distinct (e.g. colour + shape).
- **Gate 5 — timeline playback:** scrubber animating a run over its t_rel seconds.
- **Gate 6 — heatmaps + docs:** overlay toggles (traffic/death/kill/loot); then write
  README, ARCHITECTURE.md (1 page), INSIGHTS.md (3 insights).

### Suggested project structure
```
lila-player-journey/
├── public/
│   ├── data/         <- the 4 JSON types (committed)
│   └── minimaps/     <- 3 map images (copy from raw data's minimaps/)
├── pipeline/
│   └── build_data.py <- the offline pipeline; documented regenerate command
├── src/              <- React app (canvas rendering, not SVG, for perf)
└── README.md / ARCHITECTURE.md / INSIGHTS.md
```

### Tech notes
- Render points/paths on **HTML canvas**, not SVG — thousands of position points will stutter in SVG.
- Frontend just reads JSON + draws; no math beyond using pre-computed pixel coords.
- Keep the raw parquet OUT of the repo (1,243 files). Only processed JSON + minimaps go in.
- Path between raw data and project: raw parquet currently at
  `C:\Users\user\Downloads\player_data\player_data\` — pipeline reads from there, writes into the project.

---

## 6. THREE STRONG INSIGHTS already surfaced (for INSIGHTS.md)

1. **Designed-as-PvPvE, played-as-solo-PvE.** 779/796 matches have 1 human; 3 PvP kills total.
   Maps built for contested extraction are experienced as solo corridors. Designer should know the
   level is being played differently than designed; multiplayer-tension features may be landing dead.
2. **Lockdown kills players before the aha moment.** Pre-loot death rate 16% vs 5% on AmbroseValley.
   The close-quarters map denies 1-in-6 players their first progression beat. Actionable: soften early
   bot pressure / add early loot near spawn on Lockdown. Metric affected: early-game retention / pre-loot death rate.
3. **Bots are the threat, storm is not.** 52% of runs end in bot death, only 5% in storm. The storm —
   a core extraction-shooter pressure mechanic — is barely doing its job. Either storm timing is too
   loose or players extract well before it matters. Designer should check whether the storm creates the
   intended urgency. (Cross-check with traffic heatmap dead-zones.)

(Tool will likely surface a 4th from the death/kill heatmap geometry — graveyard cells where many
players die but few kill — worth looking for once Gate 6 renders them.)
