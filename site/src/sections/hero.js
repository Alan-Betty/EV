import { CHANNELS, PLATFORMS, REPO, primaryFor } from '../data/release.js';
import { platformIcon } from '../lib/icons.js';
import { createOrb } from '../lib/orb.js';
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

  // Orb and its readouts
  const canvas = document.getElementById('orb');
  const hud = {
    level: document.querySelector('[data-hud="level"]'),
    state: document.querySelector('[data-hud="state"]'),
    fps: document.querySelector('[data-hud="fps"]'),
  };
  const orb = createOrb(canvas, {
    count: matchMedia('(max-width: 640px)').matches ? 900 : 1400,
    onTick({ energy, fps }) {
      hud.level.textContent = `${(-58 + energy * 46).toFixed(1)} dB`;
      hud.state.textContent = energy > 0.62 ? 'speaking' : energy > 0.3 ? 'listening' : 'idle';
      hud.fps.textContent = `${Math.round(fps)} fps`;
    },
  });
  cta.addEventListener('pointerenter', () => orb.pulse(0.6));
  cta.addEventListener('focus', () => orb.pulse(0.6));
}
