import { useEffect, useMemo, useRef, useState } from 'react'
import { MARKERS, HUMAN_COLOR, BOT_COLOR, drawMarker, drawTrail, drawBotDot } from './draw'
import './App.css'

// --- Gate 4: run-view ("this player's story") ------------------------------
// Filters (map · date · match) drive the view off matches.json; picking a match
// lazy-loads its event stream and renders that ONE player's journey. The full
// event stream (human + bot) is kept in state and rendered via layer selectors
// — NOT stripped to human-only at load — so a future "the human's interacting
// bots" layer (the bot that killed them, bots they killed) is just another
// selector over data already present, no reload/rewrite needed.
// Coordinates: pipeline emitted px/py in 1024-space; we only scale 1024 -> display.

const MAPS = [
  { id: 'AmbroseValley', img: '/minimaps/AmbroseValley_Minimap.png' },
  { id: 'GrandRift', img: '/minimaps/GrandRift_Minimap.png' },
  { id: 'Lockdown', img: '/minimaps/Lockdown_Minimap.jpg' }, // note: .jpg
]
const COORD_SPACE = 1024 // px space the pipeline binned/mapped into
const DISPLAY = 760 // on-screen canvas size (square; maps are 1024x1024)

// Toggleable layers (also the legend). Order = draw order top→bottom. Defaults:
// human path + event types ON; bot positions OFF (noise in run-view).
const LAYER_ROWS = [
  { key: 'path', shape: 'line', color: HUMAN_COLOR, label: 'human trail', on: true },
  { key: 'Loot', shape: 'diamond', color: MARKERS.Loot.color, label: 'loot', on: true },
  { key: 'BotKill', shape: 'triangle', color: MARKERS.BotKill.color, label: 'botkill', on: true },
  { key: 'BotKilled', shape: 'cross', color: MARKERS.BotKilled.color, label: 'death by bot', on: true },
  { key: 'KilledByStorm', shape: 'star', color: MARKERS.KilledByStorm.color, label: 'storm death', on: true },
  { key: 'bots', shape: 'ring', color: BOT_COLOR, label: 'bot positions', on: false },
]
const DEFAULT_LAYERS = Object.fromEntries(LAYER_ROWS.map((r) => [r.key, r.on]))
const EVENT_KEYS = ['Loot', 'BotKill', 'BotKilled', 'KilledByStorm']

// Date helpers: matches.json carries "February_10".."February_14".
const dayNum = (d) => parseInt(d.split('_')[1], 10)
const dateLabel = (d) => d.replace('February_', 'Feb ')

function App() {
  const canvasRef = useRef(null)
  const imgRef = useRef(null)
  const [imgReady, setImgReady] = useState(false)
  const [allMatches, setAllMatches] = useState([])
  const [mapId, setMapId] = useState('AmbroseValley')
  const [date, setDate] = useState('All')
  const [selected, setSelected] = useState(null)
  const [events, setEvents] = useState(null)
  const [layers, setLayers] = useState(DEFAULT_LAYERS)

  const mapCfg = MAPS.find((m) => m.id === mapId)
  const toggle = (key) => setLayers((l) => ({ ...l, [key]: !l[key] }))

  // Load the match index once.
  useEffect(() => {
    fetch('/data/matches.json').then((r) => r.json()).then(setAllMatches)
  }, [])

  // Dates offered by the current map, in calendar order.
  const dates = useMemo(() => {
    const ds = [...new Set(
      allMatches.filter((m) => m.map_id === mapId).map((m) => m.date),
    )]
    return ds.sort((a, b) => dayNum(a) - dayNum(b))
  }, [allMatches, mapId])

  // If the selected date isn't offered by the current map, fall back to All.
  useEffect(() => {
    if (date !== 'All' && !dates.includes(date)) setDate('All')
  }, [dates]) // eslint-disable-line react-hooks/exhaustive-deps

  // Matches passing the map + date filter, best score first.
  const filtered = useMemo(
    () => allMatches
      .filter((m) => m.map_id === mapId && (date === 'All' || m.date === date))
      .sort((a, b) => b.best_score - a.best_score),
    [allMatches, mapId, date],
  )

  // Keep the selection valid: if the current match fell outside the new filter
  // set, re-default to the highest-score match in it; never leave it empty.
  useEffect(() => {
    if (!filtered.length) { setSelected(null); return }
    if (!filtered.some((m) => m.match_id === selected)) {
      setSelected(filtered[0].match_id)
    }
  }, [filtered]) // eslint-disable-line react-hooks/exhaustive-deps

  // Load the current map's minimap image; clear stale events while it swaps.
  useEffect(() => {
    setImgReady(false)
    setEvents(null)
    const img = new Image()
    img.onload = () => { imgRef.current = img; setImgReady(true) }
    img.src = mapCfg.img
  }, [mapCfg.img])

  // Load the selected match's FULL event stream (human + bot). Kept whole on
  // purpose (see top-of-file note): layers select subsets at render time.
  useEffect(() => {
    if (!selected) return
    let alive = true
    fetch(`/data/match_${selected}.json`)
      .then((r) => r.json())
      .then((d) => { if (alive) setEvents(d) })
    return () => { alive = false }
  }, [selected])

  // Draw whenever the image + events are ready.
  useEffect(() => {
    if (!imgReady || !events) return
    const canvas = canvasRef.current
    const dpr = window.devicePixelRatio || 1
    canvas.width = DISPLAY * dpr
    canvas.height = DISPLAY * dpr
    const ctx = canvas.getContext('2d')
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0)

    const s = DISPLAY / COORD_SPACE
    const X = (px) => px * s
    const Y = (py) => py * s

    ctx.clearRect(0, 0, DISPLAY, DISPLAY)
    ctx.drawImage(imgRef.current, 0, 0, DISPLAY, DISPLAY)
    // Dim the map so overlay points read clearly.
    ctx.fillStyle = 'rgba(10,12,20,0.35)'
    ctx.fillRect(0, 0, DISPLAY, DISPLAY)

    // 1) bot positions (off by default) — hollow orange rings, deliberately
    //    unlike the human's solid trail.
    if (layers.bots)
      for (const e of events)
        if (e.event === 'BotPosition') drawBotDot(ctx, X(e.px), Y(e.py))

    // 2) the human journey as a continuous, time-ramped trail (a walk, not dots)
    if (layers.path) {
      const path = events
        .filter((e) => e.event === 'Position' && !e.is_bot)
        .map((e) => ({ x: X(e.px), y: Y(e.py) }))
      drawTrail(ctx, path, HUMAN_COLOR)
    }

    // 3) THIS human's own events as distinct shapes, each independently
    //    toggleable (run-view = one player's story; bot events -> map-view).
    for (const type of EVENT_KEYS)
      if (layers[type])
        for (const e of events)
          if (e.event === type && !e.is_bot)
            drawMarker(ctx, X(e.px), Y(e.py), MARKERS[type])
  }, [imgReady, events, layers])

  const meta = filtered.find((d) => d.match_id === selected)
  // Counts for the legend: human-only event tallies (run-view foregrounds THIS
  // player), plus the bot-position total. These now match matches.json.
  const hcount = {}
  let botPos = 0
  if (events)
    for (const e of events) {
      if (e.event === 'BotPosition') botPos++
      else if (!e.is_bot) hcount[e.event] = (hcount[e.event] || 0) + 1
    }

  // This player's own outcome (human-only, from matches.json). No all-actor
  // death counts here — those are a map-view concern. "no death recorded" is an
  // inference (may be extraction, disconnect, or truncated telemetry; §4e).
  const outcome = !meta
    ? null
    : meta.died_bot > 0
      ? { text: 'died to a bot', cls: 'bad' }
      : meta.died_storm > 0
        ? { text: 'died to the storm', cls: 'storm' }
        : { text: 'no death recorded (extracted / survived)', cls: 'good' }

  return (
    <div className="app">
      <header>
        <h1>LILA BLACK — Player Journey</h1>
        <span className="gate">Gate 4 · run-view</span>
      </header>

      <div className="layout">
        <div className="stage">
          <canvas ref={canvasRef} style={{ width: DISPLAY, height: DISPLAY }} />
          {(!events || !imgReady) && <div className="loading">loading…</div>}
        </div>

        <aside className="panel">
          <label>
            Map
            <div className="maps">
              {MAPS.map((m) => (
                <button
                  key={m.id}
                  className={m.id === mapId ? 'map on' : 'map'}
                  onClick={() => setMapId(m.id)}
                >
                  {m.id}
                </button>
              ))}
            </div>
          </label>

          <label>
            Date
            <select value={date} onChange={(e) => setDate(e.target.value)}>
              <option value="All">All dates</option>
              {dates.map((d) => (
                <option key={d} value={d}>{dateLabel(d)}</option>
              ))}
            </select>
          </label>

          <label>
            Match <span className="count">({filtered.length})</span>
            <select
              value={selected ?? ''}
              onChange={(e) => setSelected(e.target.value)}
            >
              {filtered.map((d) => (
                <option key={d.match_id} value={d.match_id}>
                  {d.match_id.slice(0, 8)} · {dateLabel(d.date)} · {d.duration_s}s · score {d.best_score}
                </option>
              ))}
            </select>
          </label>

          {meta && (
            <div className="stats">
              <div className="stats-head">This run · <span>player</span></div>
              <ul>
                <li><b>{meta.duration_s}s</b> survived</li>
                <li><b>{meta.loot}</b> loot picked up</li>
                <li><b>{meta.botkills}</b> bots killed</li>
                <li className={`outcome ${outcome.cls}`}>
                  {outcome.text}
                  {meta.died_before_first_loot && ' · before first loot'}
                </li>
              </ul>
              <div className="stats-foot">
                Aggregate / all-actor stats live in map-view (Gate 6).
              </div>
            </div>
          )}

          <div className="layers-head">Layers</div>
          <div className="layers">
            {LAYER_ROWS.map((row) => (
              <Toggle
                key={row.key}
                shape={row.shape}
                c={row.color}
                label={row.label}
                count={layerCount(row.key, hcount, botPos)}
                on={layers[row.key]}
                onClick={() => toggle(row.key)}
              />
            ))}
          </div>

          <p className="note">
            Shapes carry meaning (colorblind-safe): ◆ loot · ▲ botkill · ✕ death
            · ★ storm. The human is a continuous trail (dim→bright = start→end);
            bots are hollow rings. Click a layer to toggle it.
          </p>
        </aside>
      </div>
    </div>
  )
}

// Count shown next to each layer row.
function layerCount(key, hcount, botPos) {
  if (key === 'bots') return botPos
  if (key === 'path') return hcount.Position || 0
  return hcount[key] || 0
}

// A layer row that doubles as legend + on/off switch. Its glyph is the actual
// canvas glyph, so the shapes stay self-documenting.
function Toggle({ shape, c, label, count, on, onClick }) {
  return (
    <button type="button" className={on ? 'row on' : 'row'} onClick={onClick}>
      <Glyph shape={shape} c={c} />
      <span className="row-label">{label}</span>
      <span className="row-count">{count}</span>
      <span className="switch" aria-hidden="true" />
    </button>
  )
}

function Glyph({ shape, c }) {
  const stroke = 'rgba(0,0,0,0.65)'
  return (
    <svg className="glyph" viewBox="0 0 16 16" width="16" height="16">
      {shape === 'line' && (
        <line x1="2" y1="8" x2="14" y2="8" stroke={c} strokeWidth="2.5" strokeLinecap="round" />
      )}
      {shape === 'ring' && (
        <circle cx="8" cy="8" r="4" fill="none" stroke={c} strokeWidth="1.6" />
      )}
      {shape === 'diamond' && (
        <polygon points="8,2 14,8 8,14 2,8" fill={c} stroke={stroke} strokeWidth="1" />
      )}
      {shape === 'triangle' && (
        <polygon points="8,2 14,13 2,13" fill={c} stroke={stroke} strokeWidth="1" />
      )}
      {shape === 'cross' && (
        <g stroke={c} strokeWidth="2.6" strokeLinecap="round">
          <line x1="3" y1="3" x2="13" y2="13" />
          <line x1="13" y1="3" x2="3" y2="13" />
        </g>
      )}
      {shape === 'star' && (
        <polygon
          points="8,1 9.8,6 15,6 10.8,9.3 12.5,14.5 8,11.3 3.5,14.5 5.2,9.3 1,6 6.2,6"
          fill={c} stroke={stroke} strokeWidth="0.8"
        />
      )}
    </svg>
  )
}

export default App
