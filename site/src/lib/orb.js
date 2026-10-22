// Particle "voice orb": a Fibonacci sphere whose surface ripples like a
// spectrum. Pointer velocity feeds energy, energy drives ripple depth, spin,
// point size and the cyan-to-violet shift. One 2D canvas, no allocations
// inside the frame loop, paused whenever it is off screen or the tab is hidden.

const RAMP_STEPS = 32;
const CYAN = [0, 242, 254];
const VIOLET = [127, 0, 255];

const ramp = Array.from({ length: RAMP_STEPS }, (_, i) => {
  const t = i / (RAMP_STEPS - 1);
  const c = CYAN.map((v, k) => Math.round(v + (VIOLET[k] - v) * t));
  return `rgb(${c[0]},${c[1]},${c[2]})`;
});

export function createOrb(canvas, { count = 1400, onTick } = {}) {
  const ctx = canvas.getContext('2d');
  const reduceMotion = matchMedia('(prefers-reduced-motion: reduce)');

  // x, y, z on the unit sphere plus a per-point phase seed.
  const pts = new Float32Array(count * 4);
  const golden = Math.PI * (3 - Math.sqrt(5));
  for (let i = 0; i < count; i++) {
    const y = 1 - ((i + 0.5) / count) * 2;
    const r = Math.sqrt(1 - y * y);
    const th = golden * i;
    pts[i * 4] = Math.cos(th) * r;
    pts[i * 4 + 1] = y;
    pts[i * 4 + 2] = Math.sin(th) * r;
    pts[i * 4 + 3] = Math.random() * Math.PI * 2;
  }

  let w = 0, h = 0, dpr = 1, radius = 0;
  let rect = canvas.getBoundingClientRect();
  const pointer = { nx: 0, ny: 0, speed: 0, inside: false, lx: 0, ly: 0, lt: 0 };
  let energy = 0.2, boost = 0, spin = 0, tiltX = 0.32, tiltY = 0;
  let running = false, visible = false, raf = 0, last = 0, fpsAcc = 0, fpsN = 0;

  function resize() {
    rect = canvas.getBoundingClientRect();
    dpr = Math.min(2, window.devicePixelRatio || 1);
    w = Math.max(1, Math.round(rect.width * dpr));
    h = Math.max(1, Math.round(rect.height * dpr));
    canvas.width = w;
    canvas.height = h;
    radius = Math.min(w, h) * 0.34;
    if (!running) draw(performance.now() / 1000, 0);
  }

  function onPointerMove(e) {
    const now = e.timeStamp || performance.now();
    const dt = Math.max(1, now - pointer.lt);
    const v = (Math.hypot(e.clientX - pointer.lx, e.clientY - pointer.ly) / dt) * 1000; // px/s
    if (pointer.lt) pointer.speed = Math.max(pointer.speed, Math.min(v, 6000));
    pointer.lx = e.clientX;
    pointer.ly = e.clientY;
    pointer.lt = now;
    pointer.nx = ((e.clientX - rect.left) / rect.width) * 2 - 1;
    pointer.ny = ((e.clientY - rect.top) / rect.height) * 2 - 1;
    pointer.inside = Math.abs(pointer.nx) < 0.8 && Math.abs(pointer.ny) < 0.8;
  }

  function draw(t, dt) {
    // Exponential decay/approach so behaviour is frame-rate independent.
    pointer.speed *= Math.pow(0.03, dt);
    boost *= Math.pow(0.15, dt);
    const target = 0.16 + Math.min(1, pointer.speed / 2200) * 0.72 + (pointer.inside ? 0.14 : 0) + boost;
    energy += (Math.min(1.2, target) - energy) * (1 - Math.pow(0.004, dt));
    spin += dt * (0.14 + energy * 0.95);
    tiltX += (0.32 + pointer.ny * 0.28 - tiltX) * (1 - Math.pow(0.05, dt));
    tiltY += (pointer.nx * 0.45 - tiltY) * (1 - Math.pow(0.05, dt));

    const cx = w / 2, cy = h / 2;
    const sinS = Math.sin(spin + tiltY), cosS = Math.cos(spin + tiltY);
    const sinT = Math.sin(tiltX), cosT = Math.cos(tiltX);
    const ripple = 0.035 + energy * 0.17;
    const grow = 1 + energy * 0.45;

    ctx.globalCompositeOperation = 'source-over';
    ctx.clearRect(0, 0, w, h);

    // Core bloom, scaled by energy.
    const bloom = ctx.createRadialGradient(cx, cy, 0, cx, cy, radius * 1.45);
    bloom.addColorStop(0, `rgba(0,242,254,${0.09 + energy * 0.12})`);
    bloom.addColorStop(0.45, `rgba(127,0,255,${0.05 + energy * 0.07})`);
    bloom.addColorStop(1, 'rgba(9,10,15,0)');
    ctx.fillStyle = bloom;
    ctx.fillRect(0, 0, w, h);

    // Orbit ring, behind the points.
    ctx.globalAlpha = 0.16 + energy * 0.2;
    ctx.strokeStyle = '#00F2FE';
    ctx.lineWidth = dpr;
    ctx.beginPath();
    ctx.ellipse(cx, cy, radius * 1.32, radius * 1.32 * Math.abs(Math.sin(tiltX + 0.18)), -0.18, 0, Math.PI * 2);
    ctx.stroke();

    ctx.globalCompositeOperation = 'lighter';
    for (let i = 0; i < count; i++) {
      const o = i * 4;
      const x = pts[o], y = pts[o + 1], z = pts[o + 2], seed = pts[o + 3];

      // Layered sines read as a spectrum moving across the surface.
      const d = Math.sin(y * 5 + t * 2.1 + seed) * Math.sin(x * 4 - t * 1.6) + Math.sin(z * 7 + t * 3.3) * 0.45;
      const k = 1 + d * ripple;

      const x1 = x * cosS - z * sinS;
      const z1 = x * sinS + z * cosS;
      const y2 = y * cosT - z1 * sinT;
      const z2 = y * sinT + z1 * cosT;

      const persp = 2.8 / (2.8 - z2);
      const sx = cx + x1 * radius * k * persp;
      const sy = cy + y2 * radius * k * persp;
      const depth = (z2 + 1) * 0.5;

      const size = (0.7 + depth * 1.6) * dpr * grow;
      let c = ((y2 + 1) * 0.5 * 0.75 + d * 0.12 + energy * 0.3) * (RAMP_STEPS - 1);
      c = c < 0 ? 0 : c > RAMP_STEPS - 1 ? RAMP_STEPS - 1 : c | 0;

      ctx.globalAlpha = 0.1 + depth * 0.85;
      ctx.fillStyle = ramp[c];
      ctx.fillRect(sx - size / 2, sy - size / 2, size, size);
    }
    ctx.globalAlpha = 1;

    if (dt > 0) {
      fpsAcc += dt;
      fpsN++;
    }
    if (onTick && fpsAcc >= 0.25) {
      onTick({ energy, fps: fpsN / fpsAcc });
      fpsAcc = 0;
      fpsN = 0;
    }
  }

  function loop(now) {
    const t = now / 1000;
    const dt = last ? Math.min(0.05, t - last) : 0;
    last = t;
    draw(t, dt);
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
      draw(performance.now() / 1000, 0); // leave a still frame behind
    }
  }

  new ResizeObserver(resize).observe(canvas);
  new IntersectionObserver(([e]) => {
    visible = e.isIntersecting;
    sync();
  }).observe(canvas);
  window.addEventListener('pointermove', onPointerMove, { passive: true });
  window.addEventListener('scroll', () => (rect = canvas.getBoundingClientRect()), { passive: true });
  document.addEventListener('visibilitychange', sync);
  reduceMotion.addEventListener('change', sync);
  resize();

  return {
    // Let other UI (CTA hover, the demo) kick the orb.
    pulse(amount = 0.5) {
      boost = Math.min(1, boost + amount);
    },
  };
}
