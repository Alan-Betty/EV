// Mirrored bar waveform for the simulator. Modes set the resting shape;
// kick() adds energy the way a syllable would, so typed speech visibly drives it.

const MODES = {
  idle: { floor: 0.04, gain: 0.0, travel: 0 },
  listening: { floor: 0.08, gain: 0.55, travel: 0 },
  thinking: { floor: 0.06, gain: 0.0, travel: 1 },
  executing: { floor: 0.05, gain: 0.25, travel: 0.4 },
  speaking: { floor: 0.1, gain: 1.0, travel: 0 },
  done: { floor: 0.04, gain: 0.0, travel: 0 },
};

export function createWave(canvas, { bars = 64 } = {}) {
  const ctx = canvas.getContext('2d');
  const reduceMotion = matchMedia('(prefers-reduced-motion: reduce)');
  const level = new Float32Array(bars);
  const target = new Float32Array(bars);
  let mode = MODES.idle;
  let energy = 0;
  let w = 0, h = 0, dpr = 1, grad = null;
  let raf = 0, last = 0, visible = false, running = false;

  function resize() {
    const r = canvas.getBoundingClientRect();
    dpr = Math.min(2, window.devicePixelRatio || 1);
    w = canvas.width = Math.max(1, Math.round(r.width * dpr));
    h = canvas.height = Math.max(1, Math.round(r.height * dpr));
    grad = ctx.createLinearGradient(0, 0, w, 0);
    grad.addColorStop(0, '#00F2FE');
    grad.addColorStop(0.55, '#5B8CFF');
    grad.addColorStop(1, '#7F00FF');
    draw(performance.now() / 1000, 0);
  }

  function draw(t, dt) {
    energy *= Math.pow(0.02, dt);
    for (let i = 0; i < bars; i++) {
      const x = i / (bars - 1);
      const envelope = Math.sin(Math.PI * x) ** 0.6; // quieter at the edges
      const noise = 0.5 + 0.5 * Math.sin(t * 9 + i * 1.7) * Math.sin(t * 5.3 + i * 0.6);
      const sweep = mode.travel * Math.max(0, Math.sin(x * 9 - t * 4)) * 0.35;
      target[i] = envelope * (mode.floor + sweep + mode.gain * energy * noise);
      level[i] += (target[i] - level[i]) * (1 - Math.pow(0.0005, dt || 0.016));
    }

    ctx.clearRect(0, 0, w, h);
    ctx.fillStyle = grad;
    const gap = 3 * dpr;
    const bw = (w - gap * (bars - 1)) / bars;
    const mid = h / 2;
    for (let i = 0; i < bars; i++) {
      const bh = Math.max(2 * dpr, level[i] * h * 0.92);
      ctx.globalAlpha = 0.35 + Math.min(0.65, level[i] * 1.4);
      const x = i * (bw + gap);
      ctx.beginPath();
      ctx.roundRect(x, mid - bh / 2, bw, bh, bw / 2);
      ctx.fill();
    }
    ctx.globalAlpha = 1;
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
    }
  }

  new ResizeObserver(resize).observe(canvas);
  new IntersectionObserver(([e]) => {
    visible = e.isIntersecting;
    sync();
  }).observe(canvas);
  document.addEventListener('visibilitychange', sync);
  reduceMotion.addEventListener('change', sync);

  return {
    setMode(name) {
      mode = MODES[name] ?? MODES.idle;
    },
    // Vowels hit hard, consonants less, spaces let it fall: reads like speech.
    kick(char = 'a') {
      const strength = /[aeiouy]/i.test(char) ? 0.55 : /\s/.test(char) ? 0 : /[.,!?]/.test(char) ? -0.4 : 0.3;
      energy = Math.max(0, Math.min(1.4, energy + strength));
    },
  };
}
