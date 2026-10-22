import { CHANNELS, PLATFORMS, REPO, primaryFor } from '../data/release.js';
import { createFace } from '../lib/face.js';
import { platformIcon } from '../lib/icons.js';
import { store } from '../lib/store.js';

const MOODS = ['happy', 'angry', 'surprised', 'wink', 'sleepy', 'demo'];

export function initHero() {
  const cta = document.getElementById('hero-download');
  const tip = document.getElementById('hero-sha');
  const alt = document.getElementById('hero-alt');

  const render = () => {
    const platform = store.get('platform');
    const channel = store.get('channel');
    const build = primaryFor(platform, channel);

    cta.href = store.get('mobile') ? '#download' : build.url;
    cta.querySelector('[data-icon]').innerHTML = platformIcon(platform, 'size-[18px]');
    cta.querySelector('[data-label]').textContent = `Download for ${PLATFORMS[platform].label}`;
    cta.querySelector('[data-version]').textContent = CHANNELS[channel].tag;

    // A hosted file is verified by its SHA-256; a source archive by its commit.
    tip.querySelector('[data-tip-kind]').textContent = build.sha256 ? build.name : 'Source archive';
    tip.querySelector('[data-tip-label]').textContent = build.sha256 ? 'SHA-256' : 'commit';
    tip.querySelector('[data-commit]').textContent = build.sha256 ?? build.commit;
    tip.querySelector('[data-file]').textContent = build.size ? `${build.file}  ${build.size}` : build.file;

    const others = Object.keys(PLATFORMS).filter((p) => p !== platform);
    alt.innerHTML = `Also for ${others
      .map((p) => `<a href="#download" data-pick="${p}" class="text-slate-300 underline decoration-white/20 underline-offset-4 transition-colors hover:text-white hover:decoration-cyan-400/60">${PLATFORMS[p].label}</a>`)
      .join(' and ')}.`;
  };
  ['platform', 'channel', 'mobile', 'content'].forEach((k) => store.on(k, render));

  alt.addEventListener('click', (e) => {
    const pick = e.target.closest('[data-pick]')?.dataset.pick;
    if (pick) store.set('platform', pick);
  });

  document.querySelectorAll('[data-iteration-tag]').forEach((el) => (el.textContent = `[ BUILD: ITERATION ${REPO.iteration.number} ]`));

  initFace(cta);
}

function initFace(cta) {
  const canvas = document.getElementById('face');
  const stage = document.getElementById('face-stage');
  const face = createFace(canvas, { boot: true });

  const moods = document.getElementById('face-moods');
  moods.innerHTML = MOODS.map((m) => `<button type="button" class="mood-chip" data-mood="${m}" aria-pressed="false">${m}</button>`).join('');
  moods.addEventListener('click', (e) => {
    const btn = e.target.closest('[data-mood]');
    if (!btn) return;
    moods.querySelectorAll('[data-mood]').forEach((b) => b.setAttribute('aria-pressed', String(b === btn)));
    btn.dataset.mood === 'demo' ? face.demo() : face.emote(btn.dataset.mood);
  });

  canvas.addEventListener('click', () => face.emote('surprised'));
  cta.addEventListener('pointerenter', () => face.emote('happy'));

  // The whole screen leans a few degrees toward the pointer, whatever the mood,
  // so the face always answers the mouse even when the eyes are busy emoting.
  const fine = matchMedia('(hover: hover) and (pointer: fine)').matches;
  const still = matchMedia('(prefers-reduced-motion: reduce)').matches;
  let tiltFrame = 0;
  const tilt = (x, y) => {
    cancelAnimationFrame(tiltFrame);
    tiltFrame = requestAnimationFrame(() => {
      stage.style.setProperty('--tx', `${(y * -5).toFixed(2)}deg`);
      stage.style.setProperty('--ty', `${(x * 7).toFixed(2)}deg`);
    });
  };

  // A real flick startles it; ordinary mouse movement must not. Speed is the
  // distance covered over the last ~100ms, not between two events: pointer
  // events can be 1ms apart, and a 5px hop over 1ms reads as 5000px/s.
  // It also has to happen near the face; a flick across the nav bar is not
  // something E.V. would see.
  const trail = [];
  let lastStartle = -1e9, idleTimer = 0, asleep = false;
  const doze = () => {
    asleep = true;
    face.setBase('sleepy');
  };
  window.addEventListener('pointermove', (e) => {
    const now = e.timeStamp;
    trail.push({ t: now, x: e.clientX, y: e.clientY });
    while (trail.length && now - trail[0].t > 110) trail.shift();

    const r = canvas.getBoundingClientRect();
    if (fine && !still) {
      const nx = Math.max(-1, Math.min(1, (e.clientX - (r.left + r.width / 2)) / (window.innerWidth / 2)));
      const ny = Math.max(-1, Math.min(1, (e.clientY - (r.top + r.height / 2)) / (window.innerHeight / 2)));
      tilt(nx, ny);
    }

    clearTimeout(idleTimer);
    idleTimer = setTimeout(doze, 15000);
    if (asleep) {
      asleep = false;
      face.setBase('idle');
      face.emote('listening', 1.4);
      return;
    }

    const first = trail[0];
    const dist = Math.hypot(e.clientX - first.x, e.clientY - first.y);
    const speed = (dist / Math.max(16, now - first.t)) * 1000;
    const near = e.clientX > r.left - 160 && e.clientX < r.right + 160 && e.clientY > r.top - 160 && e.clientY < r.bottom + 160;
    if (near && speed > 2400 && dist > 180 && now - lastStartle > 5000) {
      lastStartle = now;
      trail.length = 0;
      face.emote('surprised');
    }
  }, { passive: true });
  document.documentElement.addEventListener('pointerleave', () => tilt(0, 0));
  idleTimer = setTimeout(doze, 15000);
}
