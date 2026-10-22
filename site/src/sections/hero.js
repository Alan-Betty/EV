import { CHANNELS, PLATFORMS, REPO, primaryFor } from '../data/release.js';
import { platformIcon } from '../lib/icons.js';
import { createFace } from '../lib/face.js';
import { store } from '../lib/store.js';

export function initHero() {
  const cta = document.getElementById('hero-download');
  const tip = document.getElementById('hero-sha');
  const alt = document.getElementById('hero-alt');

  const render = () => {
    const platform = store.get('platform');
    const channel = store.get('channel');
    const build = primaryFor(platform, channel);

    cta.href = build.url;
    cta.querySelector('[data-icon]').innerHTML = platformIcon(platform, 'size-[18px]');
    cta.querySelector('[data-label]').textContent = `Download for ${PLATFORMS[platform].label}`;
    cta.querySelector('[data-version]').textContent = CHANNELS[channel].tag;

    tip.querySelector('[data-file]').textContent = build.file;
    tip.querySelector('[data-commit]').textContent = build.commit;

    const others = Object.keys(PLATFORMS).filter((p) => p !== platform);
    alt.innerHTML = `Also for ${others
      .map((p) => `<a href="#download" data-pick="${p}" class="text-slate-300 underline decoration-white/20 underline-offset-4 transition-colors hover:text-white hover:decoration-cyan-400/60">${PLATFORMS[p].label}</a>`)
      .join(' and ')}. Iteration ${REPO.iteration.number}, ${REPO.iteration.date}.`;
  };
  ['platform', 'channel', 'arch'].forEach((k) => store.on(k, render));

  document.querySelectorAll('[data-iteration-tag]').forEach((el) => (el.textContent = `[ BUILD: ITERATION ${REPO.iteration.number} ]`));

  // Phones get the matrix instead of a desktop binary they cannot run.
  store.on('mobile', (mobile) => {
    if (!mobile) return;
    cta.href = '#download';
  });

  alt.addEventListener('click', (e) => {
    const pick = e.target.closest('[data-pick]')?.dataset.pick;
    if (pick) store.set('platform', pick);
  });

  // E.V.'s face: follows the pointer, startles at a fast flick, dozes off when
  // left alone, and wears any mood the chat tool can ask for.
  const hud = {
    mood: document.querySelector('[data-hud="mood"]'),
    event: document.querySelector('[data-hud="event"]'),
    fps: document.querySelector('[data-hud="fps"]'),
  };
  const canvas = document.getElementById('face');
  const face = createFace(canvas, {
    onTick({ mood, fps }) {
      hud.mood.textContent = mood;
      hud.fps.textContent = `${Math.round(fps)} fps`;
    },
  });
  const event = (name) => (hud.event.textContent = name);

  const moods = document.getElementById('face-moods');
  moods.innerHTML = ['happy', 'angry', 'surprised', 'wink', 'skeptical', 'smug', 'sleepy', 'thinking', 'demo']
    .map((m) => `<button type="button" class="mood-chip" data-mood="${m}">${m}</button>`)
    .join('');
  moods.addEventListener('click', (e) => {
    const mood = e.target.closest('[data-mood]')?.dataset.mood;
    if (!mood) return;
    event(`chat mood=${mood}`);
    mood === 'demo' ? face.demo() : face.emote(mood);
  });

  canvas.addEventListener('click', () => {
    event('poke');
    face.emote('surprised');
  });
  cta.addEventListener('pointerenter', () => {
    event('success');
    face.emote('happy');
  });

  // A real flick startles it; ordinary mouse movement must not. Speed is the
  // distance covered over the last ~100ms, not between two events: pointer
  // events can be 1ms apart, and a 5px hop over 1ms reads as 5000px/s.
  // It also has to happen near the face; a flick across the nav bar is not
  // something E.V. would see.
  const trail = [];
  let lastStartle = -1e9, idleTimer = 0, asleep = false;
  const doze = () => {
    asleep = true;
    event('standby');
    face.setBase('sleepy');
  };
  window.addEventListener('pointermove', (e) => {
    const now = e.timeStamp;
    trail.push({ t: now, x: e.clientX, y: e.clientY });
    while (trail.length && now - trail[0].t > 110) trail.shift();

    clearTimeout(idleTimer);
    idleTimer = setTimeout(doze, 15000);
    if (asleep) {
      asleep = false;
      event('wake');
      face.setBase('idle');
      face.emote('listening', 1.4);
      return;
    }

    const first = trail[0];
    const dist = Math.hypot(e.clientX - first.x, e.clientY - first.y);
    const speed = (dist / Math.max(16, now - first.t)) * 1000;
    const r = canvas.getBoundingClientRect();
    const near = e.clientX > r.left - 160 && e.clientX < r.right + 160 && e.clientY > r.top - 160 && e.clientY < r.bottom + 160;
    if (near && speed > 2400 && dist > 180 && now - lastStartle > 5000) {
      lastStartle = now;
      trail.length = 0;
      event('barge_in');
      face.emote('surprised');
    } else if (hud.event.textContent === 'idle' || hud.event.textContent === 'standby') {
      event('cursor');
    }
  }, { passive: true });
  idleTimer = setTimeout(doze, 15000);
}
