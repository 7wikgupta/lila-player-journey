"""
build_data.py — LILA BLACK player-journey offline pipeline.

Reads raw parquet telemetry from ./raw_data/February_*/*.nakama-0 and emits the
small JSON files the static frontend reads from public/data/.

See BUILD_BRIEF.md for every decision/formula this implements. Key assumptions:
  - ts int64 magnitude is SECONDS, not ms (BUILD_BRIEF Finding 1).
  - user_id.isdigit() => bot; UUID => human (Finding, §1).
  - 2D minimap uses (x, z); y is elevation, ignored.

Run from the project root with the repo's interpreter:  python pipeline/build_data.py

This file is being built in reviewable pieces. Piece A = ingestion + shared core
+ a verification gate that checks dataset totals against BUILD_BRIEF ground truth.
"""

import glob
import json
import os
from collections import Counter, defaultdict

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

# --------------------------------------------------------------------------- #
# Paths & constants
# --------------------------------------------------------------------------- #

# Resolve paths relative to the project root (this file lives in pipeline/).
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RAW_GLOB = os.path.join(ROOT, "raw_data", "February_*", "*.nakama-0")
DATA_OUT = os.path.join(ROOT, "public", "data")

MINIMAP_PX = 1024  # minimaps are 1024x1024

# Per-map coordinate config (BUILD_BRIEF §2).
MAP_CONFIG = {
    "AmbroseValley": {"scale": 900,  "origin_x": -370, "origin_z": -473},
    "GrandRift":     {"scale": 581,  "origin_x": -290, "origin_z": -290},
    "Lockdown":      {"scale": 1000, "origin_x": -500, "origin_z": -500},
}

# Event-name groupings used across the pipeline.
DEATH_EVENTS = {"BotKilled", "KilledByStorm"}  # ways a human dies
KILL_EVENT = "BotKill"                          # human kills a bot
LOOT_EVENT = "Loot"
POSITION_EVENTS = {"Position", "BotPosition"}


# --------------------------------------------------------------------------- #
# Shared helpers
# --------------------------------------------------------------------------- #

def is_bot(user_id: str) -> bool:
    """Short numeric user_id => bot; UUID => human (BUILD_BRIEF §1).

    This is the SOLE source of truth for actor type. We deliberately do NOT
    infer actor from the event name: 636 'Position' rows (from 3 specific bot
    ids) are emitted by bots, and 'BotPosition' is 100% bots. Keying on the
    event name would misclassify those 636 rows. user_id always wins.
    """
    return user_id.isdigit()


# --------------------------------------------------------------------------- #
# ATTRIBUTION POLICY — the single most important decision in this pipeline.
# --------------------------------------------------------------------------- #
#
# A telemetry row's `user_id` identifies the ACTOR whose event it is. BOTH
# humans and bots emit Loot / BotKill / BotKilled / Position rows (a bot loots,
# a bot kills another bot, etc.). So every aggregation must choose an actor
# scope, and there are exactly two — used in two different places, on purpose:
#
#   (1) HUMAN-ONLY  -> per-player & per-match metrics:
#       matches.json, players.json, best_score, the §4d scoreboard, the
#       survival curve, exit-mode. These describe a PLAYER's journey, so they
#       count only rows where `not is_bot`. Use human_rows() / human_event_count().
#       Evidence this is correct: human-attributed BotKilled = 403 and
#       KilledByStorm = 39 reproduce BUILD_BRIEF §4d/§4e exactly (53%/3% Ambrose,
#       47%/10%/15% Lockdown, exit-mode 399/39/341). All-actor would give 700
#       BotKilled and break every one of those numbers.
#
#   (2) ALL-ACTOR   -> heatmap layers (§4a):
#       traffic / kills / deaths / loot grids count EVERY actor, because a
#       heatmap answers "where on the map does X happen" (incl. the bot
#       "graveyard" cells of insight #4), not "what did the human do". Use the
#       raw match rows directly — no is_bot filter. Evidence: all-actor
#       BotKilled on AmbroseValley = 486 and Storm = 17, which is exactly the
#       "486 + 17 = 503" the §4a 32x32 grid sizing was derived from. Human-only
#       (297) would contradict the brief's own grid-resolution reasoning.
#
# Rule of thumb for reviewers: if the number describes a PLAYER, filter to
# humans; if it describes a PLACE on the map, include all actors.
# --------------------------------------------------------------------------- #

def human_rows(rows: list) -> list:
    """ATTRIBUTION mode (1): keep only human-actor rows, for per-player/per-match
    metrics. Bots are dropped here ON PURPOSE — see ATTRIBUTION POLICY above."""
    return [r for r in rows if not r.is_bot]


def human_event_count(rows: list, event: str) -> int:
    """Count human-actor rows of `event` — the metric-side counterpart used by
    matches.json / players.json / scoreboard (ATTRIBUTION mode (1))."""
    return sum(1 for r in rows if r.event == event and not r.is_bot)


# Heatmaps (ATTRIBUTION mode (2)) intentionally have NO helper here: they
# iterate the match's raw rows with no is_bot filter, and the call site is
# commented to say so. Adding a "human" helper there would invite the wrong
# default. The absence is deliberate.


def world_to_pixel(x: float, z: float, cfg: dict) -> tuple[int, int]:
    """World (x, z) -> minimap pixel (px, py). Y is flipped (image origin top-left)."""
    u = (x - cfg["origin_x"]) / cfg["scale"]
    v = (z - cfg["origin_z"]) / cfg["scale"]
    px = u * MINIMAP_PX
    py = (1 - v) * MINIMAP_PX
    return round(px), round(py)


def date_from_path(path: str) -> str:
    """The day-folder name (e.g. 'February_11') is the run's date."""
    return os.path.basename(os.path.dirname(path))


# --------------------------------------------------------------------------- #
# Ingestion
# --------------------------------------------------------------------------- #

class Row:
    """One telemetry sample, normalized."""
    __slots__ = ("user_id", "match_id", "map_id", "x", "z", "event",
                 "ts_raw", "t", "is_bot", "px", "py", "date")


def read_file(path: str) -> list:
    """Read one parquet file into a list of Row objects (ts not yet normalized)."""
    table = pq.read_table(path)
    cols = table.to_pydict()
    n = table.num_rows
    ts_raw = table.column("ts").cast(pa.int64()).to_pylist()  # magnitude == seconds
    date = date_from_path(path)
    rows = []
    for i in range(n):
        r = Row()
        r.user_id = cols["user_id"][i]
        # The raw match_id column carries the '.nakama-0' storage-shard suffix
        # (Nakama backend); the true match identifier is the UUID. Strip it so
        # match_id is clean & URL-safe across matches.json / match_{id}.json /
        # players.json.
        r.match_id = cols["match_id"][i].removesuffix(".nakama-0")
        r.map_id = cols["map_id"][i]
        r.x = cols["x"][i]
        r.z = cols["z"][i]
        ev = cols["event"][i]
        r.event = ev.decode("utf-8") if isinstance(ev, (bytes, bytearray)) else ev
        r.ts_raw = ts_raw[i]
        r.is_bot = is_bot(r.user_id)
        r.date = date
        rows.append(r)
    return rows


def ingest(verbose: bool = True):
    """
    Read every raw file, group rows by match, normalize ts per match, and
    precompute pixel coords. Returns (matches, files_read) where matches is a
    dict: match_id -> {map_id, date, rows:[Row,...]}.
    """
    files = sorted(glob.glob(RAW_GLOB))
    matches = defaultdict(lambda: {"map_id": None, "date": None, "rows": []})

    for path in files:
        for r in read_file(path):
            m = matches[r.match_id]
            m["map_id"] = r.map_id
            m["date"] = r.date
            m["rows"].append(r)

    # Normalize ts per match and map coords.
    for mid, m in matches.items():
        cfg = MAP_CONFIG.get(m["map_id"])
        min_ts = min(r.ts_raw for r in m["rows"])
        for r in m["rows"]:
            r.t = r.ts_raw - min_ts
            if cfg is not None:
                r.px, r.py = world_to_pixel(r.x, r.z, cfg)
            else:
                r.px, r.py = None, None

    if verbose:
        print(f"Read {len(files)} files into {len(matches)} matches.")
    return matches, len(files)


# --------------------------------------------------------------------------- #
# Verification gate (Piece A) — check totals against BUILD_BRIEF ground truth
# --------------------------------------------------------------------------- #

BRIEF_EVENT_COUNTS = {
    "Position": 51347, "BotPosition": 21712, "Loot": 12885,
    "BotKill": 2415, "BotKilled": 700, "KilledByStorm": 39,
    "Kill": 3, "Killed": 3,
}
BRIEF_TOTALS = {"rows": 89104, "files": 1243, "matches": 796, "humans": 245}


def verify(matches, files_read):
    all_rows = [r for m in matches.values() for r in m["rows"]]
    ev_counts = Counter(r.event for r in all_rows)
    humans = {r.user_id for r in all_rows if not r.is_bot}
    maps = Counter(m["map_id"] for m in matches.values())

    print("\n=== TOTALS vs BUILD_BRIEF ===")
    got = {
        "rows": len(all_rows),
        "files": files_read,
        "matches": len(matches),
        "humans": len(humans),
    }
    for k, want in BRIEF_TOTALS.items():
        mark = "OK" if got[k] == want else "DIFF"
        print(f"  {k:8} got {got[k]:>6}  brief {want:>6}  [{mark}]")

    print("\n=== EVENT COUNTS vs BUILD_BRIEF ===")
    for ev, want in BRIEF_EVENT_COUNTS.items():
        g = ev_counts.get(ev, 0)
        mark = "OK" if g == want else "DIFF"
        print(f"  {ev:14} got {g:>6}  brief {want:>6}  [{mark}]")
    extra = set(ev_counts) - set(BRIEF_EVENT_COUNTS)
    if extra:
        print("  UNEXPECTED EVENTS:", {e: ev_counts[e] for e in extra})

    print("\n=== MATCHES PER MAP ===")
    for mp, c in maps.most_common():
        print(f"  {mp:14} {c}")

    # Human-count-per-match distribution (Finding 2: should be ~all 1).
    hppm = Counter(len({r.user_id for r in m["rows"] if not r.is_bot})
                   for m in matches.values())
    print("\n=== HUMANS-PER-MATCH DISTRIBUTION (Finding 2) ===")
    for h in sorted(hppm):
        print(f"  {h} human(s): {hppm[h]} matches")

    # --- The 3 attribution questions -------------------------------------- #
    print("\n=== ATTRIBUTION QUESTIONS ===")

    # Q1: do Loot / BotKill rows carry the HUMAN's user_id or the BOT's?
    for ev in (LOOT_EVENT, KILL_EVENT, "BotKilled", "KilledByStorm"):
        rows = [r for r in all_rows if r.event == ev]
        bot_share = sum(1 for r in rows if r.is_bot)
        human_share = len(rows) - bot_share
        print(f"  Q1 {ev:14}: {len(rows):>6} rows | "
              f"user_id human={human_share} bot={bot_share}")

    # Q2: are BotPosition rows numeric-user_id bots? Position rows humans?
    for ev in ("Position", "BotPosition"):
        rows = [r for r in all_rows if r.event == ev]
        bot_share = sum(1 for r in rows if r.is_bot)
        print(f"  Q2 {ev:14}: {len(rows):>6} rows | "
              f"bot user_id={bot_share} human user_id={len(rows)-bot_share}")

    # Q3: is is_bot (from user_id) consistent with the event name's implied actor?
    #     Position should be human-id; BotPosition should be bot-id. Report mismatches.
    mismatches = Counter()
    for r in all_rows:
        if r.event == "Position" and r.is_bot:
            mismatches["Position-but-bot-id"] += 1
        if r.event == "BotPosition" and not r.is_bot:
            mismatches["BotPosition-but-human-id"] += 1
    print(f"  Q3 position/id consistency mismatches: {dict(mismatches) or 'NONE'}")


# --------------------------------------------------------------------------- #
# Piece B — matches.json (the index; drives filtering + rankings)
# --------------------------------------------------------------------------- #
#
# One entry per PLAYER-JOURNEY match. The 16 bot-only (zero-human) matches are
# EXCLUDED by design: they have no player to follow, can't open in run-view, and
# contribute nothing to scores/survival. They are kept as evidence for the
# solo-PvE / low-population insight (documented in README), not as index rows.
# Result: 780 matches (779 solo + 1 two-human). All per-match metrics use the
# HUMAN-only attribution mode — see ATTRIBUTION POLICY above.

# best_score weights (§4c) — a documented judgment call: engagement is
# survival (duration) + progression (loot) + combat (botkills), minus a penalty
# for dying before the first-loot "aha" moment.
SCORE_W_DURATION = 0.4
SCORE_W_LOOT = 0.3
SCORE_W_BOTKILLS = 0.3
SCORE_PRELOOT_PENALTY = 0.5


def _match_metrics(m: dict) -> dict | None:
    """Compute the human-centric metrics for one match. Returns None for
    bot-only matches (excluded from the index by decision)."""
    rows = m["rows"]
    hrows = human_rows(rows)  # ATTRIBUTION mode (1): metrics are human-only
    if not hrows:
        return None  # bot-only match -> excluded

    human_ids = {r.user_id for r in rows if not r.is_bot}
    bot_ids = {r.user_id for r in rows if r.is_bot}

    # duration_s = the human's own last event time (verified: human-only medians
    # 357/403/427 reproduce §4d 356/403/427; all-rows overshoots).
    duration_s = max(r.t for r in hrows)

    loot = sum(1 for r in hrows if r.event == LOOT_EVENT)
    botkills = sum(1 for r in hrows if r.event == KILL_EVENT)
    died_bot = sum(1 for r in hrows if r.event == "BotKilled")
    died_storm = sum(1 for r in hrows if r.event == "KilledByStorm")

    # died_before_first_loot: the human's first death precedes their first loot
    # (or they died having never looted). The aha-moment denial signal (§4d/§4e).
    # We use a STRICT '<': a same-timestamp tie (death and loot at the same t)
    # means the player DID loot, so it must NOT count as a pre-loot death. This
    # is the definition that's correct on its own merits; it differs from the
    # brief's §4d figures by <=1 match on the sparse maps (rounding noise), and
    # we deliberately do not tune the operator to chase the brief's numbers.
    first_loot = min((r.t for r in hrows if r.event == LOOT_EVENT), default=None)
    first_death = min((r.t for r in hrows if r.event in DEATH_EVENTS), default=None)
    died_before_first_loot = (
        first_death is not None and (first_loot is None or first_death < first_loot)
    )

    return {
        "match_id": next(iter({r.match_id for r in rows})),
        "map_id": m["map_id"],
        "date": m["date"],
        "humans": len(human_ids),
        "bots": len(bot_ids),
        "duration_s": int(duration_s),
        "loot": loot,
        "botkills": botkills,
        "died_storm": died_storm,
        "died_bot": died_bot,
        "died_before_first_loot": died_before_first_loot,
        # best_score filled in by the second pass (needs global percentiles).
    }


def build_matches(matches: dict) -> list:
    """Build matches.json: per-match human metrics + best_score (§3a, §4c)."""
    # Pass 1: per-match metrics (drops bot-only matches).
    entries = [e for e in (_match_metrics(m) for m in matches.values()) if e]

    # Pass 2: normalize each score component by its GLOBAL 95th percentile
    # (cap at 1) so outliers don't dominate the ranking (§4c).
    def p95(key):
        vals = np.array([e[key] for e in entries], dtype=float)
        return float(np.percentile(vals, 95))

    p95_dur, p95_loot, p95_bk = p95("duration_s"), p95("loot"), p95("botkills")

    def norm(v, p):
        return min(v / p, 1.0) if p > 0 else 0.0

    for e in entries:
        score = (
            SCORE_W_DURATION * norm(e["duration_s"], p95_dur)
            + SCORE_W_LOOT * norm(e["loot"], p95_loot)
            + SCORE_W_BOTKILLS * norm(e["botkills"], p95_bk)
        )
        if e["died_before_first_loot"]:
            score -= SCORE_PRELOOT_PENALTY
        e["best_score"] = round(score, 2)

    # Pre-rank the index by best_score (helps the ranking panel; filters don't
    # care about order).
    entries.sort(key=lambda e: e["best_score"], reverse=True)
    return entries


def write_json(name: str, obj):
    os.makedirs(DATA_OUT, exist_ok=True)
    path = os.path.join(DATA_OUT, name)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, separators=(",", ":"))
    return path


def verify_matches(entries: list):
    """Piece B verification: cross-check the generated index against §4d/§4e."""
    import statistics as st
    print("\n=== matches.json — Piece B verification ===")
    print(f"  entries (player-journey matches): {len(entries)}  "
          f"(expect 780 = 779 solo + 1 two-human)")
    hcount = Counter(e["humans"] for e in entries)
    print(f"  humans-per-entry: {dict(sorted(hcount.items()))}")

    maps = ("AmbroseValley", "GrandRift", "Lockdown")
    print("\n  per-map medians vs §4d (dur 356/403/427, loot 15/13/12, botk 2/2/1):")
    for mp in maps:
        es = [e for e in entries if e["map_id"] == mp]
        md = st.median([e["duration_s"] for e in es])
        ml = st.median([e["loot"] for e in es])
        mb = st.median([e["botkills"] for e in es])
        print(f"    {mp:14} n={len(es):4} med_dur={md:.0f} med_loot={ml:.0f} med_botk={mb:.0f}")

    print("\n  per-map rates vs §4d (bot/storm/preloot):")
    print("    Ambrose 53%/3%/5% · GrandRift 42%/9%/7% · Lockdown 47%/10%/16%")
    for mp in maps:
        es = [e for e in entries if e["map_id"] == mp]
        n = len(es)
        b = sum(1 for e in es if e["died_bot"] > 0) / n
        s = sum(1 for e in es if e["died_storm"] > 0) / n
        pl = sum(1 for e in es if e["died_before_first_loot"]) / n
        print(f"    {mp:14} bot={b:.0%} storm={s:.0%} preloot={pl:.0%}")

    # Exit-mode overall (§4e: bot 403 / storm 39 / none 339).
    bot = sum(1 for e in entries if e["died_bot"] > 0)
    storm = sum(1 for e in entries if e["died_storm"] > 0 and e["died_bot"] == 0)
    none = len(entries) - bot - storm
    print(f"\n  exit-mode overall vs §4e (403/39/339): "
          f"bot={bot} storm_only={storm} no_death={none}")

    print("\n  best_score: "
          f"max={max(e['best_score'] for e in entries):.2f} "
          f"min={min(e['best_score'] for e in entries):.2f} "
          f"penalized(<0)={sum(1 for e in entries if e['best_score'] < 0)}")
    print("\n  top 3 by score:")
    for e in entries[:3]:
        print(f"    {e['best_score']:.2f} {e['map_id']:14} "
              f"dur={e['duration_s']}s loot={e['loot']} botk={e['botkills']} "
              f"{e['match_id'][:8]}")
    print("\n  sample entry (JSON):")
    print("   ", json.dumps(entries[0]))


# --------------------------------------------------------------------------- #
# Piece C — match_{match_id}.json (full event stream per run, lazy-loaded)
# --------------------------------------------------------------------------- #
#
# One file per player-journey match (bot-only matches get no file). Each event
# is time-normalized (t = t_rel seconds) and pre-mapped to minimap pixels, so
# the frontend just draws. Includes BOTH human and bot rows — the bots are the
# swarm the player fights, needed for run-view (§3b). Sorted by t.

def _event_record(r) -> dict:
    """One event in a match stream (§3b shape). Emits both pixel coords (for
    trivial drawing) and raw world x/z (cheap insurance + coord-proof evidence)."""
    return {
        "t": int(r.t),
        "event": r.event,
        "px": r.px,
        "py": r.py,
        "x": round(r.x, 2),
        "z": round(r.z, 2),
        "is_bot": r.is_bot,
    }


def build_match_files(matches: dict) -> dict:
    """Write match_{id}.json for every player-journey match. Returns stats."""
    # Clear stale per-match files so a re-run can't leave orphans behind.
    for old in glob.glob(os.path.join(DATA_OUT, "match_*.json")):
        os.remove(old)

    written, total_events = 0, 0
    for m in matches.values():
        if not any(not r.is_bot for r in m["rows"]):
            continue  # bot-only match -> no run-view file (excluded by decision)
        match_id = m["rows"][0].match_id
        stream = [_event_record(r) for r in sorted(m["rows"], key=lambda r: r.t)]
        write_json(f"match_{match_id}.json", stream)
        written += 1
        total_events += len(stream)
    return {"files": written, "events": total_events}


def verify_match_files(matches: dict, stats: dict):
    print("\n=== match_{id}.json — Piece C verification ===")
    print(f"  files written: {stats['files']}  (expect 780)")
    print(f"  total events across files: {stats['events']}")

    # Coord proof: the brief's verified example
    # AmbroseValley world (-301.45, -355.55) -> pixel (78, 890).
    px, py = world_to_pixel(-301.45, -355.55, MAP_CONFIG["AmbroseValley"])
    print(f"  coord proof: world(-301.45,-355.55) -> pixel({px},{py})  "
          f"[brief (78,890)] {'OK' if (px, py) == (78, 890) else 'DIFF'}")

    # Inspect one real file: sort order, pixel bounds, human+bot presence.
    sample_id = [m["rows"][0].match_id for m in matches.values()
                 if any(not r.is_bot for r in m["rows"])][0]
    stream = json.load(open(os.path.join(DATA_OUT, f"match_{sample_id}.json")))
    ts = [e["t"] for e in stream]
    pxs = [e["px"] for e in stream]
    pys = [e["py"] for e in stream]
    print(f"\n  sample file: match_{sample_id}.json")
    print(f"    events={len(stream)}  t range {min(ts)}..{max(ts)}  "
          f"sorted={ts == sorted(ts)}")
    print(f"    px range {min(pxs)}..{max(pxs)}  py range {min(pys)}..{max(pys)} "
          f"(0..1024 expected)")
    print(f"    is_bot mix: {Counter(e['is_bot'] for e in stream)}")
    print(f"    event mix: {dict(Counter(e['event'] for e in stream))}")
    print(f"    first event: {json.dumps(stream[0])}")

    # Sanity: a Loot/death event from some file, to eyeball coords land in-bounds.
    for e in stream:
        if e["event"] != "Position":
            print(f"    a non-Position event: {json.dumps(e)}")
            break


# --------------------------------------------------------------------------- #
# Piece D — heatmaps_{map_id}.json (precomputed 32x32 grids per map)
# --------------------------------------------------------------------------- #
#
# Grid resolution = 32x32 (BUILD_BRIEF §4a): sized for the SPARSEST important
# layer (deaths). Finer grids turned death signal into noise; 32x32 gives ~0.9
# deaths/usable-cell on AmbroseValley so zones form. All four layers share it.
#
# Binning happens in the SAME 0..1024 pixel space the verified world_to_pixel
# mapping produces, so every cell aligns to a real region of the map:
#     cell_x = px // (coord_space / grid)
#     cell_y = py // (coord_space / grid)
# Grid is indexed [row=cell_y][col=cell_x]; the frontend recovers a cell's
# pixel origin as cell * (coord_space / grid), then scales 1024 -> display size.
# We emit grid + coord_space in every file so the frontend never has to guess.

GRID = 32
COORD_SPACE = MINIMAP_PX  # 1024 — same space as the pixel coords in match files

# Layer -> the event set that feeds it. ALL-ACTOR by design (ATTRIBUTION mode
# (2)): a heatmap describes a PLACE on the map, not a player, so NO is_bot
# filter is applied to any layer (see ATTRIBUTION POLICY above).
HEATMAP_LAYERS = {
    "traffic": POSITION_EVENTS,        # Position + BotPosition
    "deaths": DEATH_EVENTS,            # BotKilled + KilledByStorm
    "kills": {KILL_EVENT},             # BotKill
    "loot": {LOOT_EVENT},              # Loot
}


def _empty_grid():
    return [[0] * GRID for _ in range(GRID)]


def build_heatmaps(matches: dict) -> tuple[dict, dict]:
    """Build one set of 32x32 grids per map. Returns (heatmaps, stats).

    Heatmaps include EVERY match (bot-only ones too) and EVERY actor — this is
    the 'describes a PLACE' case, matching how §4a's counts were derived.
    """
    cell = COORD_SPACE / GRID
    heatmaps = {mp: {"grid": GRID, "coord_space": COORD_SPACE,
                     **{layer: _empty_grid() for layer in HEATMAP_LAYERS}}
                for mp in MAP_CONFIG}
    oob = Counter()  # off-grid pixels per map (should be ~0 if mapping is sound)

    for m in matches.values():
        mp = m["map_id"]
        hm = heatmaps[mp]
        for r in m["rows"]:               # all actors — no is_bot filter, on purpose
            for layer, events in HEATMAP_LAYERS.items():
                if r.event in events:
                    cx, cy = int(r.px // cell), int(r.py // cell)
                    if 0 <= cx < GRID and 0 <= cy < GRID:
                        hm[layer][cy][cx] += 1
                    else:
                        oob[mp] += 1
                    break

    stats = {"oob": dict(oob)}
    return heatmaps, stats


def _grid_total(grid):
    return sum(sum(row) for row in grid)


def _clustering(grid):
    """Describe how concentrated a grid is: nonzero cells, max, and the share
    of all events held by the top-5 cells. High top-5 share => real clustering."""
    flat = sorted((v for row in grid for v in row), reverse=True)
    total = sum(flat)
    nonzero = sum(1 for v in flat if v)
    top5 = sum(flat[:5])
    top5_share = (top5 / total) if total else 0.0
    return {"total": total, "nonzero": nonzero, "max": flat[0] if flat else 0,
            "top5": top5, "top5_share": top5_share}


def verify_heatmaps(heatmaps: dict, stats: dict):
    print("\n=== heatmaps_{map_id}.json — Piece D verification ===")

    print("  per-map, per-layer totals (all-actor) — reconcile vs event totals:")
    for mp in ("AmbroseValley", "Lockdown", "GrandRift"):
        hm = heatmaps[mp]
        tot = {layer: _grid_total(hm[layer]) for layer in HEATMAP_LAYERS}
        print(f"    {mp:14} traffic={tot['traffic']:6} deaths={tot['deaths']:4} "
              f"kills={tot['kills']:5} loot={tot['loot']:5}")
    print("    (§4a check: AmbroseValley deaths should be BotKilled 486 + Storm 17 = 503)")

    print(f"\n  off-grid pixels (skipped, expect ~0): {stats['oob'] or 'NONE'}")

    print("\n  CLUSTERING (AmbroseValley — expect a few hot cells, not flat scatter):")
    for layer in ("deaths", "traffic"):
        c = _clustering(heatmaps["AmbroseValley"][layer])
        mean_nz = c["total"] / c["nonzero"] if c["nonzero"] else 0
        print(f"    {layer:8} total={c['total']:6} nonzero_cells={c['nonzero']:4} "
              f"max_cell={c['max']:5} mean_nonzero={mean_nz:5.1f} "
              f"top5={c['top5']} ({c['top5_share']:.0%} of all events)")
    print("    -> max_cell >> mean and a high top-5 share = clusters are real, not uniform.")

    gr = _clustering(heatmaps["GrandRift"]["deaths"])
    print(f"\n  LIMITATION: GrandRift deaths total={gr['total']} across "
          f"{gr['nonzero']} cells — inherently sparse (§4a). Expected thin data, "
          f"NOT a bug; document as a limitation (its death heatmap reads weak).")


# --------------------------------------------------------------------------- #
# Piece E — players.json (per-human index; enables a "player profile" lens)
# --------------------------------------------------------------------------- #
#
# Per-player stats are HUMAN-only by definition (ATTRIBUTION mode (1)). Stats
# are aggregated per user_id from THAT user's own rows, so the two-human match
# splits its loot/kills/duration correctly between its two players.

def build_players(matches: dict) -> list:
    """Build players.json: per-human totals across the matches they appear in."""
    # user_id -> match_id -> their rows in that match
    per_user = defaultdict(lambda: defaultdict(list))
    for m in matches.values():
        for r in m["rows"]:
            if not r.is_bot:
                per_user[r.user_id][r.match_id].append(r)

    players = []
    for uid, by_match in per_user.items():
        durations = [max(r.t for r in rows) for rows in by_match.values()]
        total_loot = sum(1 for rows in by_match.values()
                         for r in rows if r.event == LOOT_EVENT)
        total_botkills = sum(1 for rows in by_match.values()
                             for r in rows if r.event == KILL_EVENT)
        players.append({
            "user_id": uid,
            "matches": len(by_match),
            "total_loot": total_loot,
            "total_botkills": total_botkills,
            "avg_duration_s": round(sum(durations) / len(durations)),
            "match_ids": sorted(by_match.keys()),
        })

    players.sort(key=lambda p: p["matches"], reverse=True)
    return players


def verify_players(players: list):
    print("\n=== players.json — Piece E verification ===")
    print(f"  players (unique humans): {len(players)}  (expect 245)")
    memberships = sum(p["matches"] for p in players)
    print(f"  total human-match memberships: {memberships}  "
          f"(expect 781 = 779 solo + 1 two-human match counted for 2 players)")
    print(f"  total loot across players: {sum(p['total_loot'] for p in players)}  "
          f"(expect 12770 human Loot)")
    print(f"  total botkills across players: {sum(p['total_botkills'] for p in players)}  "
          f"(expect 2232 human BotKill)")
    mc = Counter(p["matches"] for p in players)
    print(f"  matches-per-player distribution: {dict(sorted(mc.items()))}")
    print(f"  most active player: {json.dumps(players[0])}")


if __name__ == "__main__":
    matches, files_read = ingest()
    verify(matches, files_read)

    entries = build_matches(matches)
    write_json("matches.json", entries)
    verify_matches(entries)

    stats = build_match_files(matches)
    verify_match_files(matches, stats)

    heatmaps, hstats = build_heatmaps(matches)
    for mp, hm in heatmaps.items():
        write_json(f"heatmaps_{mp}.json", hm)
    verify_heatmaps(heatmaps, hstats)

    players = build_players(matches)
    write_json("players.json", players)
    verify_players(players)
