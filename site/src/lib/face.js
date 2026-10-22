// E.V.'s face on a canvas: a port of ev/face/render.py and a trimmed-down
// ev/face/expression.py. Moods are read from the real ev/face/expressions.json,
// so a mood changed there changes here on the next build.
//
// As in render.py, lids are subtracted rather than drawn: an eye is a rounded
// rectangle, the upper lid is a slanted half-plane cut out of it and the
// smile is an ellipse cut out of the bottom. Canvas has no path subtraction,
// so each cut is a clip: everything below the lid line, everything outside
// the smile ellipse (an even-odd clip against a large rectangle).
import EXPR from '../../../ev/face/expressions.json';

// Geometry constants, copied from render.py.
const SCREEN_INSET = 0.11;
const SCREEN_RADIUS = 0.3;
const EYE_W = 0.2;
const EYE_H = 0.44;
const EYE_SPACING = 0.205;
const GAZE_TRAVEL = 0.085;
const PUPIL_TRAVEL = 0.16;
const ASPECT = 1.25; // the face window is 240 x 192
const REFERENCE_W = 240; // render.py's pen widths are tuned at this size

const D = EXPR.defaults;
const TAU = Math.PI * 2;
const DEG = Math.PI / 180;

const hex = (c) => {
  const n = parseInt(c.slice(1), 16);
  return [(n >> 16) & 255, (n >> 8) & 255, n & 255];
};
const rgba = (c, a) => `rgba(${c[0] | 0},${c[1] | 0},${c[2] | 0},${Math.max(0, Math.min(1, a))})`;
const mix = (c, to, t) => c.map((v, i) => v + (to[i] - v) * t);
const WHITE = [255, 255, 255];
const rand = ([a, b]) => a + Math.random() * (b - a);

function resolve(name) {
  const m = EXPR.moods[name] ?? EXPR.moods.idle;
  const eye = (side) => ({ ...D.eye, ...m.eyes, ...m[side] });
  const face = { ...D.face, ...m.face };
  return {
    name,
    left: eye('left'),
    right: eye('right'),
    face: { ...face, color: hex(face.color) },
    blink: { ...D.blink, ...m.blink },
    saccade: { ...D.saccade, ...m.saccade },
    track: m.track_cursor ?? D.track_cursor,
    transition: m.transition_ms ?? D.transition_ms,
    hold: m.hold_s ?? 0,
    interrupt: Boolean(m.interrupt),
  };
}

const clone = (m) => ({ left: { ...m.left }, right: { ...m.right }, face: { ...m.face, color: [...m.face.color] } });

function approach(cur, target, k) {
  for (const side of ['left', 'right']) {
    for (const key in target[side]) cur[side][key] += (target[side][key] - cur[side][key]) * k;
  }
  for (const key in target.face) {
    if (key === 'color') cur.face.color = cur.face.color.map((v, i) => v + (target.face.color[i] - v) * k);
    else cur.face[key] += (target.face[key] - cur.face[key]) * k;
  }
}

export function createFace(canvas, { onTick } = {}) {
  const ctx = canvas.getContext('2d');
  const reduceMotion = matchMedia('(prefers-reduced-motion: reduce)');

  let base = resolve('idle');
  let target = base;
  let emoteUntil = 0;
  const cur = clone(base);

  const look = { x: 0, y: 0, tx: 0, ty: 0, next: 0 };
  const pointer = { x: 0, y: 0, at: -1e9 };
  const blink = { next: 0, start: -1, dur: 0.16 };
  let ringPhase = 0;
  let reel = null;

  let W = 0, H = 0, dpr = 1;
  let t = 0, last = 0, raf = 0, visible = false, running = false;
  let fpsAcc = 0, fpsN = 0;
  const screenCache = new Map();

  // --- expression state ----------------------------------------------------

  function setTarget(mood) {
    target = mood;
    look.next = 0;
  }

  function emote(name, hold) {
    reel = name === 'demo' ? reel : null;
    const mood = resolve(name);
    setTarget(mood);
    emoteUntil = t + (hold ?? Math.max(mood.hold || 0, 2.4));
    if (!running) settle();
  }

  function setBase(name) {
    base = resolve(name);
    // A transient keeps playing unless the new base insists, as in expression.py.
    if (t >= emoteUntil || base.interrupt) {
      emoteUntil = 0;
      setTarget(base);
    }
    if (!running) settle();
  }

  // Every mood but "demo" from tools/schemas.FACE_MOODS, in turn.
  const REEL = ['happy', 'sad', 'angry', 'surprised', 'confused', 'skeptical', 'excited', 'smug', 'wink', 'worried', 'bored', 'sleepy', 'thinking'];
  function demo() {
    reel = { i: 0, next: t };
  }

  function stepReel() {
    if (!reel || t < reel.next) return;
    if (reel.i >= REEL.length) {
      reel = null;
      emoteUntil = 0;
      setTarget(base);
      return;
    }
    const name = REEL[reel.i];
    const mood = resolve(name);
    setTarget(mood);
    emoteUntil = t + 1.7;
    reel.label = `${name} ${reel.i + 1}/${REEL.length}`;
    reel.i += 1;
    reel.next = t + 1.7;
  }

  function update(dt) {
    stepReel();
    if (emoteUntil && t >= emoteUntil && !reel) {
      emoteUntil = 0;
      setTarget(base);
    }

    const tau = Math.max(0.03, target.transition / 1000 / 3);
    approach(cur, target, 1 - Math.exp(-dt / tau));

    // Where the eyes look: the cursor when the mood tracks it and it moved
    // lately, otherwise small saccades around the mood's own gaze.
    if (target.track && t - pointer.at < 3) {
      look.tx = pointer.x * 0.9;
      look.ty = pointer.y * 0.7;
    } else if (t >= look.next) {
      const r = target.saccade.range;
      look.tx = (Math.random() * 2 - 1) * r;
      look.ty = (Math.random() * 2 - 1) * r * 0.6;
      look.next = t + rand(target.saccade.interval_s);
    }
    const lk = 1 - Math.exp(-dt / 0.06);
    look.x += (look.tx - look.x) * lk;
    look.y += (look.ty - look.y) * lk;

    if (t >= blink.next && blink.start < 0) {
      blink.start = t;
      blink.dur = 0.16 / (target.blink.speed || 1);
    }
    if (blink.start >= 0 && t - blink.start > blink.dur) {
      blink.start = -1;
      blink.next = Math.random() < target.blink.double_chance ? t + 0.12 : t + rand(target.blink.interval_s);
    }
    ringPhase = (ringPhase + cur.face.ring_speed * dt) % 1;
  }

  // Snap straight to the target: used when animation is off.
  function settle() {
    approach(cur, target, 1);
    draw();
  }

  // --- drawing (render.py) -----------------------------------------------

  function screenLayer(rect, color, glow, scale) {
    const cq = color.map((c) => ((c | 0) / 6 | 0) * 6);
    const gq = Math.round(glow * 25) / 25;
    const key = `${W}x${H}@${dpr}:${cq}:${gq}`;
    let layer = screenCache.get(key);
    if (layer) return layer;
    if (screenCache.size > 48) screenCache.clear();
    layer = document.createElement('canvas');
    layer.width = Math.round(W * dpr);
    layer.height = Math.round(H * dpr);
    const c = layer.getContext('2d');
    c.scale(dpr, dpr);
    const { x, y, w, h } = rect;
    const body = new Path2D();
    body.roundRect(x, y, w, h, SCREEN_RADIUS * h);

    // Ambient halo first, so the screen sits on it rather than under it.
    for (const [width, alpha] of [[16, 0.035], [9, 0.06], [4, 0.09]]) {
      c.lineWidth = width * scale;
      c.strokeStyle = rgba(cq, alpha * gq);
      c.stroke(body);
    }
    const fill = c.createLinearGradient(0, y, 0, y + h);
    fill.addColorStop(0, 'rgba(18,24,32,0.965)');
    fill.addColorStop(1, 'rgba(6,8,12,0.965)');
    c.fillStyle = fill;
    c.fill(body);

    // Scanlines, barely there, and the eye colour lighting the glass from below.
    c.save();
    c.clip(body);
    c.strokeStyle = 'rgba(255,255,255,0.028)';
    c.lineWidth = 1;
    c.beginPath();
    for (let ly = y + 1; ly < y + h; ly += 3 * Math.max(1, scale * 0.75)) {
      c.moveTo(x, ly);
      c.lineTo(x + w, ly);
    }
    c.stroke();
    const tint = c.createLinearGradient(0, y, 0, y + h);
    tint.addColorStop(0, rgba(cq, 0));
    tint.addColorStop(1, rgba(cq, 0.07 * gq));
    c.fillStyle = tint;
    c.fillRect(x, y, w, h);
    c.restore();

    c.lineWidth = 1.2 * scale;
    c.strokeStyle = rgba(mix(cq, WHITE, 0.3), 0.18 + 0.2 * gq);
    c.stroke(body);
    screenCache.set(key, layer);
    return layer;
  }

  // Cut the lids out of the current clip, then leave the eye's rounded
  // rectangle as the current path. Mirrors eye_path() in render.py.
  function eyePath(e, side, w, h) {
    if (e.lid_top > 0.001) {
      const y0 = -h / 2 + e.lid_top * h;
      const slope = Math.tan(e.lid_angle * DEG) * -side;
      ctx.beginPath();
      ctx.moveTo(-w, y0 - slope * w);
      ctx.lineTo(w, y0 + slope * w);
      ctx.lineTo(w, h * 3);
      ctx.lineTo(-w, h * 3);
      ctx.closePath();
      ctx.clip();
    }
    if (e.lid_bottom > 0.001) {
      const ew = w * 1.6, eh = h * 1.3;
      const cy = h / 2 + eh / 2 - e.lid_bottom * h * 1.05;
      ctx.beginPath();
      ctx.rect(-w * 3, -h * 4, w * 6, h * 8);
      ctx.ellipse(0, cy, ew / 2, eh / 2, 0, 0, TAU);
      ctx.clip('evenodd');
    }
    const r = Math.min(e.roundness * Math.min(w, h) / 2, w / 2, h / 2);
    ctx.beginPath();
    ctx.roundRect(-w / 2, -h / 2, w, h, Math.max(0, r));
  }

  function drawEye(e, side, baseW, baseH, color, glow, gaze) {
    const w = baseW * e.width;
    const h = Math.max(baseH * e.height * e.open, 1.6);
    if (h < 0.5) return;

    // Soft bloom behind the eye.
    const reach = Math.max(w, h) * 0.95;
    const bloom = ctx.createRadialGradient(0, 0, 0, 0, 0, reach);
    bloom.addColorStop(0, rgba(color, 0.22 * glow));
    bloom.addColorStop(0.55, rgba(color, 0.07 * glow));
    bloom.addColorStop(1, rgba(color, 0));
    ctx.fillStyle = bloom;
    ctx.beginPath();
    ctx.arc(0, 0, reach, 0, TAU);
    ctx.fill();

    // One tight halo: the eye again, a little larger and fainter.
    const grow = 0.05 * baseH;
    ctx.save();
    ctx.scale((w + 2 * grow) / w, (h + 2 * grow) / h);
    eyePath(e, side, w, h);
    ctx.fillStyle = rgba(color, 0.14 * glow);
    ctx.fill();
    ctx.restore();

    ctx.save();
    eyePath(e, side, w, h);
    const fill = ctx.createLinearGradient(0, -h / 2, 0, h / 2);
    fill.addColorStop(0, rgba(mix(color, WHITE, 0.35), 1));
    fill.addColorStop(1, rgba(color, 1));
    ctx.fillStyle = fill;
    ctx.fill();

    if (baseH * e.height * e.open >= baseH * 0.25) {
      ctx.clip();
      if (e.pupil > 0.01) {
        const r = e.pupil * Math.min(w, h) * 0.75;
        const px = gaze.x * w * PUPIL_TRAVEL, py = gaze.y * h * PUPIL_TRAVEL;
        const core = ctx.createRadialGradient(px, py, 0, px, py, r);
        core.addColorStop(0, rgba(mix(color, WHITE, 0.85), 0.95));
        core.addColorStop(0.45, rgba(mix(color, WHITE, 0.5), 0.45));
        core.addColorStop(1, rgba(color, 0));
        ctx.fillStyle = core;
        ctx.beginPath();
        ctx.arc(px, py, r, 0, TAU);
        ctx.fill();
      }
      // A fixed glint: the glass catching a light that does not move.
      const g = Math.min(w, h) * 0.075;
      ctx.fillStyle = 'rgba(255,255,255,0.59)';
      ctx.beginPath();
      ctx.arc(-w * 0.24, -h * 0.26, g, 0, TAU);
      ctx.fill();
    }
    ctx.restore();
  }

  function draw() {
    if (!W || !H) return;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, W, H);

    let rw = W, rh = W / ASPECT;
    if (rh > H) {
      rh = H;
      rw = H * ASPECT;
    }
    const inset = SCREEN_INSET * Math.min(rw, rh);
    const rect = { x: (W - rw) / 2 + inset, y: (H - rh) / 2 + inset, w: rw - 2 * inset, h: rh - 2 * inset };
    const scale = rw / REFERENCE_W;
    const f = cur.face;
    const pulse = 0.5 + 0.5 * Math.sin(TAU * f.pulse_hz * t);
    const glow = f.glow * (f.pulse_hz > 0.01 ? 0.78 + 0.22 * pulse : 1);

    ctx.drawImage(screenLayer(rect, f.color, glow, scale), 0, 0, W, H);

    const body = new Path2D();
    body.roundRect(rect.x, rect.y, rect.w, rect.h, SCREEN_RADIUS * rect.h);

    // The processing ring: a bright arc chasing itself round the screen edge.
    if (f.ring > 0.02 && ctx.createConicGradient) {
      const ring = ctx.createConicGradient(-ringPhase * TAU, rect.x + rect.w / 2, rect.y + rect.h / 2);
      ring.addColorStop(0, rgba(mix(f.color, WHITE, 0.4), f.ring));
      ring.addColorStop(0.18, rgba(f.color, 0.55 * f.ring));
      ring.addColorStop(0.42, rgba(f.color, 0));
      ring.addColorStop(0.96, rgba(f.color, 0));
      ring.addColorStop(1, rgba(f.color, f.ring));
      ctx.lineWidth = 2.6 * scale;
      ctx.strokeStyle = ring;
      ctx.stroke(body);
    }

    const gaze = { x: Math.max(-1, Math.min(1, f.gaze_x + look.x)), y: Math.max(-1, Math.min(1, f.gaze_y + look.y)) };
    const bob = f.bob_hz > 0 ? f.bob * rect.h * Math.sin(TAU * f.bob_hz * t) : 0;
    let blinkMul = 1;
    if (blink.start >= 0) blinkMul = Math.max(0.04, 1 - Math.sin(Math.PI * Math.min(1, (t - blink.start) / blink.dur)));

    ctx.save();
    ctx.clip(body);
    ctx.translate(rect.x + rect.w / 2 + gaze.x * GAZE_TRAVEL * rect.w, rect.y + rect.h / 2 - 0.02 * rect.h + gaze.y * GAZE_TRAVEL * rect.h + bob);
    ctx.rotate(f.roll * DEG);
    for (const [e, side] of [[cur.left, -1], [cur.right, 1]]) {
      ctx.save();
      ctx.translate(side * (EYE_SPACING + e.x) * rect.w, e.y * rect.h);
      ctx.rotate(-side * e.tilt * DEG);
      drawEye({ ...e, open: e.open * blinkMul }, side, EYE_W * rect.w, EYE_H * rect.h, f.color, glow, gaze);
      ctx.restore();
    }
    ctx.restore();
  }

  // --- loop -----------------------------------------------------------------

  function loop(now) {
    const s = now / 1000;
    const dt = last ? Math.min(0.05, s - last) : 0;
    last = s;
    t += dt;
    update(dt);
    draw();
    if (dt > 0) {
      fpsAcc += dt;
      fpsN++;
    }
    if (onTick && fpsAcc >= 0.25) {
      onTick({ mood: reel?.label ?? target.name, fps: fpsN / fpsAcc });
      fpsAcc = 0;
      fpsN = 0;
    }
    raf = requestAnimationFrame(loop);
  }

  function sync() {
    const should = visible && !document.hidden && !reduceMotion.matches;
    if (should === running) return;
    running = should;
    if (running) {
      last = 0;
      raf = requestAnimationFrame(loop);
    } else {
      cancelAnimationFrame(raf);
      settle();
    }
  }

  function resize() {
    const r = canvas.getBoundingClientRect();
    dpr = Math.min(2, window.devicePixelRatio || 1);
    W = r.width;
    H = r.height;
    canvas.width = Math.max(1, Math.round(W * dpr));
    canvas.height = Math.max(1, Math.round(H * dpr));
    screenCache.clear();
    draw();
  }

  window.addEventListener(
    'pointermove',
    (e) => {
      const r = canvas.getBoundingClientRect();
      pointer.x = Math.max(-1, Math.min(1, (e.clientX - (r.left + r.width / 2)) / (r.width * 0.75)));
      pointer.y = Math.max(-1, Math.min(1, (e.clientY - (r.top + r.height / 2)) / (r.height * 0.75)));
      pointer.at = t;
    },
    { passive: true },
  );
  new ResizeObserver(resize).observe(canvas);
  new IntersectionObserver(([e]) => {
    visible = e.isIntersecting;
    sync();
  }).observe(canvas);
  document.addEventListener('visibilitychange', sync);
  reduceMotion.addEventListener('change', sync);
  resize();

  return { emote, setBase, demo: () => (demo(), running || settle()), mood: () => target.name };
}
