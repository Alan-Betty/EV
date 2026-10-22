import { CHANNELS, EXTRAS, PLATFORMS, REPO, artifactsFor, primaryFor } from '../data/release.js';
import { copy, flashCopied } from '../lib/clipboard.js';
import { icon, platformIcon } from '../lib/icons.js';
import { store } from '../lib/store.js';

const fmt = new Intl.NumberFormat('en-US');
const escape = (s) => s.replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' })[c]);

function copyButton(value, label) {
  return `<button type="button" class="copy-btn" data-copy="${escape(value)}" data-copy-label="${escape(label)}" aria-label="${escape(label)}">
    <span class="copy-icon">${icon.copy('size-3.5')}</span><span class="check-icon">${icon.check('size-3.5')}</span>
  </button>`;
}

function artifactRow(a) {
  if (a.command) {
    return `<li class="artifact">
      <p class="artifact-name">${a.name}</p><p class="artifact-detail">${a.detail}</p>
      <div class="artifact-code"><code class="truncate" title="${escape(a.command)}"><span class="text-slate-600">$</span> ${escape(a.command)}</code>${copyButton(a.command, `Copy ${a.name.toLowerCase()} command`)}</div>
    </li>`;
  }
  return `<li class="artifact">
    <div class="flex items-start justify-between gap-3">
      <div class="min-w-0"><p class="artifact-name">${a.name}</p><p class="artifact-detail">${a.detail}</p></div>
      <a href="${a.url}" class="artifact-dl" aria-label="Download ${a.file}">${icon.download('size-4')}<span>.zip</span></a>
    </div>
    <div class="artifact-code">
      <span class="shrink-0 text-slate-600">commit</span>
      <code class="truncate" title="${a.commit}">${a.commit.slice(0, 12)}…${a.commit.slice(-6)}</code>
      ${copyButton(a.commit, `Copy commit id ${a.commit.slice(0, 7)}`)}
    </div>
  </li>`;
}

export function initDownloads() {
  const root = document.getElementById('download');
  if (!root) return;

  const grid = root.querySelector('#dl-grid');
  const channelGroup = root.querySelector('#dl-channels');
  const note = root.querySelector('#dl-note');
  const verify = root.querySelector('#dl-verify');

  function render() {
    const channel = store.get('channel');
    const detected = store.get('mobile') ? null : store.get('platform');

    grid.innerHTML = Object.entries(PLATFORMS)
      .map(([id, p]) => {
        const here = id === detected;
        return `<article class="dl-card sheen ${here ? 'is-detected max-lg:order-first' : ''}" aria-labelledby="dl-${id}">
          <header class="flex items-center gap-3">
            <span class="dl-icon">${platformIcon(id, 'size-5')}</span>
            <div class="min-w-0 flex-1">
              <h3 id="dl-${id}" class="text-[16px] font-semibold text-white">${p.label}</h3>
              <p class="text-[12.5px] text-slate-500">${p.long}</p>
            </div>
            ${here ? '<span class="dl-detected"><span class="size-1.5 rounded-full bg-pulse"></span>Your system</span>' : ''}
          </header>
          <ul class="mt-5 space-y-2">${artifactsFor(id, channel).map(artifactRow).join('')}</ul>
          <p class="mt-auto pt-5 text-[12px] leading-5 text-slate-500">${p.requires}</p>
        </article>`;
      })
      .join('');

    const c = CHANNELS[channel];
    note.innerHTML = `<span class="font-mono text-slate-200">${c.version}</span><span class="text-slate-600">${c.date}</span><span>${c.note}</span>`;

    channelGroup.querySelectorAll('[data-channel]').forEach((b) => {
      const on = b.dataset.channel === channel;
      b.setAttribute('aria-checked', String(on));
      b.tabIndex = on ? 0 : -1;
    });
    channelGroup.style.setProperty('--seg', Object.keys(CHANNELS).indexOf(channel));

    // How to check which commit you actually got, for this visitor's system.
    const platform = store.get('platform');
    const cmd = PLATFORMS[platform].verify(primaryFor(platform, channel));
    verify.querySelector('[data-verify-os]').textContent = `${PLATFORMS[platform].label}. Optional extras: ${EXTRAS.split('&&')[0].replace('python -m pip install ', '').trim()}`;
    verify.querySelector('code').textContent = cmd;
    verify.querySelector('[data-copy]').dataset.copy = cmd;
  }
  ['channel', 'platform', 'mobile'].forEach((k) => store.on(k, render));

  // Channel radiogroup: click, or arrow keys per the ARIA pattern.
  const ids = Object.keys(CHANNELS);
  channelGroup.addEventListener('click', (e) => {
    const b = e.target.closest('[data-channel]');
    if (b) store.set('channel', b.dataset.channel);
  });
  channelGroup.addEventListener('keydown', (e) => {
    const step = { ArrowRight: 1, ArrowDown: 1, ArrowLeft: -1, ArrowUp: -1 }[e.key];
    if (!step) return;
    e.preventDefault();
    const next = ids[(ids.indexOf(store.get('channel')) + step + ids.length) % ids.length];
    store.set('channel', next);
    channelGroup.querySelector(`[data-channel="${next}"]`).focus();
  });

  root.addEventListener('click', (e) => {
    const b = e.target.closest('[data-copy]');
    if (!b) return;
    copy(b.dataset.copy, b.dataset.copyLabel?.replace(/^Copy /, 'Copied ') ?? 'Copied').then((ok) => ok && flashCopied(b));
  });

  // Real numbers from the repo, counted up once on first view.
  const counters = [
    [root.querySelector('[data-counter="tests"]'), REPO.tests],
    [root.querySelector('[data-counter="commits"]'), REPO.commitCount],
  ];
  counters.forEach(([el, n]) => (el.textContent = fmt.format(n)));
  const io = new IntersectionObserver(([e]) => {
    if (!e.isIntersecting || matchMedia('(prefers-reduced-motion: reduce)').matches) return;
    io.disconnect();
    const start = performance.now();
    const frame = (now) => {
      const t = Math.min(1, (now - start) / 1100);
      const ease = 1 - Math.pow(1 - t, 3);
      counters.forEach(([el, n]) => (el.textContent = fmt.format(Math.round(n * ease))));
      if (t < 1) requestAnimationFrame(frame);
    };
    requestAnimationFrame(frame);
  });
  io.observe(root.querySelector('#dl-stats'));
}
