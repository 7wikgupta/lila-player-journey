// Canvas glyph + trail drawing for run-view.
// Shapes carry the meaning (colorblind-safe); color only reinforces. Each event
// type gets a distinct silhouette so kill/death/loot/storm are never confused.

export const HUMAN_COLOR = '#39d0ff'
export const BOT_COLOR = '#ff9a3c'

// Event -> marker. shape drives the glyph; color reinforces.
export const MARKERS = {
  Loot: { shape: 'diamond', color: '#ffd23f', r: 5 }, // pickup
  BotKill: { shape: 'triangle', color: '#46e06f', r: 5.5 }, // human killed a bot
  BotKilled: { shape: 'cross', color: '#ff5b5b', r: 6 }, // human died to a bot
  KilledByStorm: { shape: 'star', color: '#c07bff', r: 6.5 }, // human died to storm
}

// Draw one marker at already-scaled pixel (x, y).
export function drawMarker(ctx, x, y, m) {
  const { shape, color, r } = m
  ctx.lineJoin = 'round'
  ctx.lineCap = 'round'

  if (shape === 'diamond') {
    ctx.beginPath()
    ctx.moveTo(x, y - r); ctx.lineTo(x + r, y)
    ctx.lineTo(x, y + r); ctx.lineTo(x - r, y)
    ctx.closePath()
    fillStroke(ctx, color)
  } else if (shape === 'triangle') {
    const h = r * 1.15
    ctx.beginPath()
    ctx.moveTo(x, y - h)
    ctx.lineTo(x + r, y + r * 0.85)
    ctx.lineTo(x - r, y + r * 0.85)
    ctx.closePath()
    fillStroke(ctx, color)
  } else if (shape === 'cross') {
    // X: dark halo under a bright stroke so it reads on any terrain.
    const d = r * 0.85
    const strokeX = () => {
      ctx.beginPath()
      ctx.moveTo(x - d, y - d); ctx.lineTo(x + d, y + d)
      ctx.moveTo(x + d, y - d); ctx.lineTo(x - d, y + d)
      ctx.stroke()
    }
    ctx.lineWidth = 4.5; ctx.strokeStyle = 'rgba(0,0,0,0.6)'; strokeX()
    ctx.lineWidth = 2.6; ctx.strokeStyle = color; strokeX()
  } else if (shape === 'star') {
    starPath(ctx, x, y, 5, r, r * 0.46)
    fillStroke(ctx, color)
  }
}

function fillStroke(ctx, color) {
  ctx.fillStyle = color
  ctx.fill()
  ctx.lineWidth = 1.5
  ctx.strokeStyle = 'rgba(0,0,0,0.65)'
  ctx.stroke()
}

function starPath(ctx, cx, cy, spikes, outer, inner) {
  ctx.beginPath()
  let rot = -Math.PI / 2
  const step = Math.PI / spikes
  ctx.moveTo(cx + Math.cos(rot) * outer, cy + Math.sin(rot) * outer)
  for (let i = 0; i < spikes; i++) {
    rot += step
    ctx.lineTo(cx + Math.cos(rot) * inner, cy + Math.sin(rot) * inner)
    rot += step
    ctx.lineTo(cx + Math.cos(rot) * outer, cy + Math.sin(rot) * outer)
  }
  ctx.closePath()
}

// Draw the human journey as a continuous trail. Alpha ramps old -> new so the
// walk direction (time) reads at a glance. pts: [{x, y}] already scaled.
export function drawTrail(ctx, pts, color = HUMAN_COLOR) {
  if (pts.length < 1) return
  ctx.lineJoin = 'round'
  ctx.lineCap = 'round'
  ctx.lineWidth = 2.4
  for (let i = 1; i < pts.length; i++) {
    const t = i / (pts.length - 1) // 0 at start -> 1 at end
    ctx.strokeStyle = withAlpha(color, 0.2 + 0.7 * t)
    ctx.beginPath()
    ctx.moveTo(pts[i - 1].x, pts[i - 1].y)
    ctx.lineTo(pts[i].x, pts[i].y)
    ctx.stroke()
  }

  // spawn (start): white ring
  ring(ctx, pts[0].x, pts[0].y, 5, '#ffffff', 2)
  // end (last known position): solid dot in the trail color
  const end = pts[pts.length - 1]
  ctx.beginPath()
  ctx.arc(end.x, end.y, 3.5, 0, Math.PI * 2)
  ctx.fillStyle = color
  ctx.fill()
  ctx.lineWidth = 1
  ctx.strokeStyle = 'rgba(0,0,0,0.6)'
  ctx.stroke()
}

// Bot positions: small hollow rings — deliberately unlike the human's solid
// trail so the two are never confused.
export function drawBotDot(ctx, x, y) {
  ring(ctx, x, y, 2.2, withAlpha(BOT_COLOR, 0.55), 1.2)
}

function ring(ctx, x, y, r, color, w) {
  ctx.beginPath()
  ctx.arc(x, y, r, 0, Math.PI * 2)
  ctx.lineWidth = w
  ctx.strokeStyle = color
  ctx.stroke()
}

function withAlpha(hex, a) {
  const n = parseInt(hex.slice(1), 16)
  const r = (n >> 16) & 255, g = (n >> 8) & 255, b = n & 255
  return `rgba(${r},${g},${b},${a})`
}
