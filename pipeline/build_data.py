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
import os
from collections import Counter, defaultdict

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
        r.match_id = cols["match_id"][i]
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


if __name__ == "__main__":
    matches, files_read = ingest()
    verify(matches, files_read)
