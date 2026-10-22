import { CHANNELS, PLATFORMS, primaryFor } from '../data/release.js';
import { SCRIPTS } from '../data/scripts.js';
import { copy } from '../lib/clipboard.js';
import { icon } from '../lib/icons.js';
import { store } from '../lib/store.js';

const escape = (s) => s.replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' })[c]);

function jump(selector) {
  const target = document.querySelector(selector);
  if (!target) return;
  const smooth = !matchMedia('(prefers-reduced-motion: reduce)').matches;
  target.scrollIntoView({ behavior: smooth ? 'smooth' : 'auto', block: 'start' });
  // Move focus too, so keyboard users land where they jumped.
  target.setAttribute('tabindex', '-1');
  target.focus({ preventScroll: true });
}

function commands() {
  const platform = store.get('platform');
  const channel = store.get('channel');
  const build = primaryFor(platform, channel, store.get('arch'));
  return [
    { group: 'Go to', label: 'Features', hint: 'Capabilities grid', run: () => jump('#features') },
    { group: 'Go to', label: 'Live demo', hint: 'Assistant simulator', run: () => jump('#demo') },
    { group: 'Go to', label: 'System logs', hint: 'Build and commit stream', run: () => jump('#activity') },
    { group: 'Go to', label: 'Download specs', hint: 'Every platform and channel', run: () => jump('#download') },
    {
      group: 'Go to', label: 'Search system logs', keys: ['/'],
      run: () => { jump('#activity'); setTimeout(() => document.getElementById('log-search')?.focus({ preventScroll: true }), 350); },
    },
    ...SCRIPTS.map((s) => ({
      group: 'Run in demo', label: s.label,
      run: () => { jump('#demo'); document.dispatchEvent(new CustomEvent('nexus:demo', { detail: s.id })); },
    })),
    { group: 'Download', label: `Download for ${PLATFORMS[platform].label}`, hint: build.file, run: () => { location.href = build.url; } },
    { group: 'Download', label: 'Copy SHA-256 of your build', hint: `${build.sha256.slice(0, 16)}…`, run: () => copy(build.sha256, 'Copied SHA-256') },
    ...Object.entries(CHANNELS).map(([id, c]) => ({
      group: 'Download', label: `Switch to ${c.label} channel`, hint: id === channel ? 'current' : `v${c.version}`,
      run: () => { store.set('channel', id); jump('#download'); },
    })),
  ];
}

export function initPalette() {
  const dialog = document.getElementById('cmdk');
  const input = document.getElementById('cmdk-input');
  const list = document.getElementById('cmdk-list');
  if (!dialog) return;

  const mac = store.get('platform') === 'macos';
  document.querySelectorAll('[data-mod-key]').forEach((k) => (k.textContent = mac ? '⌘' : 'Ctrl'));

  let items = [];
  let shown = [];
  let active = 0;
  let opener = null;

  function render() {
    const q = input.value.trim().toLowerCase();
    shown = items.filter((c) => !q || `${c.group} ${c.label} ${c.hint ?? ''}`.toLowerCase().includes(q));
    active = Math.min(active, Math.max(0, shown.length - 1));
    if (!shown.length) {
      list.innerHTML = `<li class="px-3 py-8 text-center text-[13px] text-slate-500" role="presentation">Nothing matches "${escape(input.value)}".</li>`;
      input.removeAttribute('aria-activedescendant');
      return;
    }
    let group = '';
    list.innerHTML = shown
      .map((c, i) => {
        const head = c.group !== group ? `<li class="cmdk-group" role="presentation">${(group = c.group)}</li>` : '';
        return `${head}<li id="cmdk-${i}" class="cmdk-item" role="option" data-i="${i}" aria-selected="${i === active}">
          <span class="flex-1 truncate">${escape(c.label)}</span>
          ${c.hint ? `<span class="cmdk-hint">${escape(c.hint)}</span>` : ''}
          ${c.keys ? c.keys.map((k) => `<kbd class="kbd">${k}</kbd>`).join('') : ''}
        </li>`;
      })
      .join('');
    input.setAttribute('aria-activedescendant', `cmdk-${active}`);
  }

  function move(step) {
    if (!shown.length) return;
    active = (active + step + shown.length) % shown.length;
    list.querySelectorAll('[role=option]').forEach((li) => li.setAttribute('aria-selected', String(+li.dataset.i === active)));
    input.setAttribute('aria-activedescendant', `cmdk-${active}`);
    document.getElementById(`cmdk-${active}`)?.scrollIntoView({ block: 'nearest' });
  }

  function open() {
    if (dialog.open) return;
    opener = document.activeElement;
    items = commands();
    input.value = '';
    active = 0;
    render();
    dialog.showModal();
    input.focus();
  }

  function close(restoreFocus = true) {
    if (!dialog.open) return;
    dialog.close();
    if (restoreFocus) opener?.focus?.({ preventScroll: true });
  }

  function choose(i) {
    const cmd = shown[i];
    if (!cmd) return;
    close(false);
    cmd.run();
  }

  input.addEventListener('input', () => { active = 0; render(); });
  input.addEventListener('keydown', (e) => {
    if (e.key === 'ArrowDown') { e.preventDefault(); move(1); }
    else if (e.key === 'ArrowUp') { e.preventDefault(); move(-1); }
    else if (e.key === 'Enter') { e.preventDefault(); choose(active); }
  });
  list.addEventListener('click', (e) => {
    const li = e.target.closest('[role=option]');
    if (li) choose(+li.dataset.i);
  });
  list.addEventListener('pointermove', (e) => {
    const li = e.target.closest('[role=option]');
    if (li && +li.dataset.i !== active) move(+li.dataset.i - active);
  });
  // Click on the backdrop (the dialog element itself, outside the panel) closes.
  dialog.addEventListener('click', (e) => e.target === dialog && close());

  document.querySelectorAll('[data-open-cmdk]').forEach((b) => b.addEventListener('click', open));

  // Global hotkeys
  window.addEventListener('keydown', (e) => {
    if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'k') {
      e.preventDefault();
      dialog.open ? close() : open();
      return;
    }
    const typing = e.target.closest?.('input, textarea, [contenteditable="true"]');
    if (e.key === '/' && !typing && !dialog.open && !e.metaKey && !e.ctrlKey && !e.altKey) {
      e.preventDefault();
      const search = document.getElementById('log-search');
      search.scrollIntoView({ behavior: 'smooth', block: 'center' });
      search.focus({ preventScroll: true });
    }
  });
}
