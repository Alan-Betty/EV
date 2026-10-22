import { CHANNELS, PLATFORMS, artifactsFor, primaryFor, verifyCommand } from '../data/release.js';
import { copy, flashCopied } from '../lib/clipboard.js';
import { icon, platformIcon } from '../lib/icons.js';
import { store } from '../lib/store.js';

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
  // A hosted file is checked by its SHA-256, a source archive by its commit.
  const [kind, value] = a.sha256 ? ['sha256', a.sha256] : ['commit', a.commit];
  const ext = a.file.match(/\.[a-z0-9]+$/i)?.[0] ?? '';
  return `<li class="artifact">
    <div class="flex items-start justify-between gap-3">
      <div class="min-w-0"><p class="artifact-name">${escape(a.name)}</p><p class="artifact-detail truncate">${escape(a.detail)}</p></div>
      <a href="${escape(a.url)}" class="artifact-dl" aria-label="Download ${escape(a.file)}"${a.sha256 ? ' download' : ''}>${icon.download('size-4')}<span>${a.size ?? ext}</span></a>
    </div>
    <div class="artifact-code">
      <span class="shrink-0 text-slate-600">${kind}</span>
      <code class="truncate" title="${value}">${value.slice(0, 12)}…${value.slice(-6)}</code>
      ${copyButton(value, kind === 'sha256' ? `Copy SHA-256 for ${a.file}` : `Copy commit id ${value.slice(0, 7)}`)}
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
    note.textContent = c.note;

    channelGroup.querySelectorAll('[data-channel]').forEach((b) => {
      const on = b.dataset.channel === channel;
      b.setAttribute('aria-checked', String(on));
      b.tabIndex = on ? 0 : -1;
    });
    channelGroup.style.setProperty('--seg', Object.keys(CHANNELS).indexOf(channel));

    // How to check which commit you actually got, for this visitor's system.
    const platform = store.get('platform');
    const cmd = verifyCommand(platform, primaryFor(platform, channel));
    verify.querySelector('code').textContent = cmd;
    verify.querySelector('[data-copy]').dataset.copy = cmd;
  }
  // Channel switches cross-fade the cards where the browser supports it.
  let ready = false;
  const update = () => {
    if (!ready || !document.startViewTransition || matchMedia('(prefers-reduced-motion: reduce)').matches) render();
    else document.startViewTransition(render);
  };
  ['channel', 'platform', 'mobile', 'content'].forEach((k) => store.on(k, update));
  ready = true;

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
}
