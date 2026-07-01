import { useEffect, useMemo, useRef, useState } from 'react'
import './App.css'

// --- Gate 3: static minimap + coordinate proof -----------------------------
// Load a minimap, plot ONE match's pre-mapped pixel coords on a canvas, and
// visually confirm points land sensibly (loot near structures, not in voids).
// The pipeline already emitted px/py in 1024-space; here we only scale 1024 ->
// display size. No coordinate math beyond that, by design (BUILD_BRIEF §5).

const MAPS = [
  { id: 'AmbroseValley', img: '/minimaps/AmbroseValley_Minimap.png' },
  { id: 'GrandRift', img: '/minimaps/GrandRift_Minimap.png' },
  { id: 'Lockdown', img: '/minimaps/Lockdown_Minimap.jpg' }, // note: .jpg
]
const COORD_SPACE = 1024 // px space the pipeline binned/mapped into
const DISPLAY = 760 // on-screen canvas size (square; maps are 1024x1024)

// Event -> how it draws. Colors are distinct now; Gate 4 adds distinct shapes.
const STYLE = {
  Position: { color: '#39d0ff', r: 2 }, // human movement
  BotPosition: { color: 'rgba(255,150,60,0.45)', r: 1.6 }, // bot swarm
  Loot: { color: '#ffd23f', r: 4 }, // gold pickups
  BotKill: { color: '#46e06f', r: 4.5 }, // human killed a bot
  BotKilled: { color: '#ff4d4d', r: 6 }, // human died to a bot
  KilledByStorm: { color: '#b06bff', r: 6 }, // human died to storm
}

// Pick a few visually rich demo matches for a map; adapt to sparse maps so
// GrandRift (no swarm, few deaths) still yields good examples to eyeball.
function pickDemos(all, mapId) {
  const es = all.filter((m) => m.map_id === mapId)
  const strict = es.filter((m) => m.bots >= 3 && m.loot >= 15 &&
    m.botkills >= 2 && m.died_bot > 0)
  const loose = es.filter((m) => m.loot >= 8 && m.botkills >= 1)
  const pool = strict.length >= 3 ? strict : loose.length ? loose : es
  return [...pool].sort((a, b) => b.best_score - a.best_score).slice(0, 6)
}

function App() {
  const canvasRef = useRef(null)
  const imgRef = useRef(null)
  const [imgReady, setImgReady] = useState(false)
  const [allMatches, setAllMatches] = useState([])
  const [mapId, setMapId] = useState('AmbroseValley')
  const [selected, setSelected] = useState(null)
  const [events, setEvents] = useState(null)

  const mapCfg = MAPS.find((m) => m.id === mapId)

  // Load the match index once.
  useEffect(() => {
    fetch('/data/matches.json').then((r) => r.json()).then(setAllMatches)
  }, [])

  // Demo candidates for the current map.
  const demos = useMemo(
    () => (allMatches.length ? pickDemos(allMatches, mapId) : []),
    [allMatches, mapId],
  )

  // When the map (and thus its demo list) changes, select its top match.
  useEffect(() => {
    if (demos.length) setSelected(demos[0].match_id)
  }, [demos])

  // Load the current map's minimap image; clear stale events while it swaps.
  useEffect(() => {
    setImgReady(false)
    setEvents(null)
    const img = new Image()
    img.onload = () => { imgRef.current = img; setImgReady(true) }
    img.src = mapCfg.img
  }, [mapCfg.img])

  // Load the selected match's event stream.
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

    const dot = (e, st) => {
      ctx.beginPath()
      ctx.arc(X(e.px), Y(e.py), st.r, 0, Math.PI * 2)
      ctx.fillStyle = st.color
      ctx.fill()
    }

    // 1) bot positions (background swarm)
    for (const e of events)
      if (e.event === 'BotPosition') dot(e, STYLE.BotPosition)

    // 2) the human journey path (Position points in time order)
    const path = events.filter((e) => e.event === 'Position' && !e.is_bot)
    ctx.strokeStyle = 'rgba(57,208,255,0.55)'
    ctx.lineWidth = 1.5
    ctx.beginPath()
    path.forEach((e, i) => {
      const x = X(e.px), y = Y(e.py)
      i ? ctx.lineTo(x, y) : ctx.moveTo(x, y)
    })
    ctx.stroke()
    for (const e of path) dot(e, STYLE.Position)

    // 3) event markers on top (loot, kills, deaths)
    const ordered = ['Loot', 'BotKill', 'BotKilled', 'KilledByStorm']
    for (const type of ordered)
      for (const e of events)
        if (e.event === type) {
          const st = STYLE[type]
          dot(e, st)
          ctx.lineWidth = 1
          ctx.strokeStyle = 'rgba(0,0,0,0.6)'
          ctx.stroke()
        }

    // mark the spawn (first human position)
    if (path[0]) {
      ctx.beginPath()
      ctx.arc(X(path[0].px), Y(path[0].py), 5, 0, Math.PI * 2)
      ctx.strokeStyle = '#ffffff'
      ctx.lineWidth = 2
      ctx.stroke()
    }
  }, [imgReady, events])

  const meta = demos.find((d) => d.match_id === selected)
  const counts = events
    ? events.reduce((a, e) => ((a[e.event] = (a[e.event] || 0) + 1), a), {})
    : {}

  return (
    <div className="app">
      <header>
        <h1>LILA BLACK — Player Journey</h1>
        <span className="gate">Gate 3 · coordinate proof</span>
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
            Match
            <select
              value={selected ?? ''}
              onChange={(e) => setSelected(e.target.value)}
            >
              {demos.map((d) => (
                <option key={d.match_id} value={d.match_id}>
                  {d.match_id.slice(0, 8)} · {d.duration_s}s · score {d.best_score}
                </option>
              ))}
            </select>
          </label>

          {meta && (
            <ul className="stats">
              <li><b>{meta.duration_s}s</b> duration</li>
              <li><b>{meta.loot}</b> loot · <b>{meta.botkills}</b> botkills</li>
              <li><b>{meta.bots}</b> bots · died to bot: {meta.died_bot} · storm: {meta.died_storm}</li>
            </ul>
          )}

          <div className="legend">
            <Item c={STYLE.Position.color} label="human path / position" />
            <Item c="#ff9a3c" label={`bot position (${counts.BotPosition || 0})`} />
            <Item c={STYLE.Loot.color} label={`loot (${counts.Loot || 0})`} />
            <Item c={STYLE.BotKill.color} label={`botkill (${counts.BotKill || 0})`} />
            <Item c={STYLE.BotKilled.color} label={`death by bot (${counts.BotKilled || 0})`} />
            <Item c={STYLE.KilledByStorm.color} label={`death by storm (${counts.KilledByStorm || 0})`} />
            <Item c="#ffffff" ring label="spawn (first position)" />
          </div>

          <p className="note">
            Eyeball test: loot &amp; the path should hug structures/roads, not sit
            in empty voids. Points are the pipeline's pre-mapped px/py scaled
            {` ${COORD_SPACE}→${DISPLAY}`}.
          </p>
        </aside>
      </div>
    </div>
  )
}

function Item({ c, label, ring }) {
  return (
    <div className="item">
      <span
        className="swatch"
        style={ring
          ? { border: `2px solid ${c}`, background: 'transparent' }
          : { background: c }}
      />
      {label}
    </div>
  )
}

export default App
