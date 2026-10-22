// The site manager: edits content/site.json through the dev server's
// /__admin API (scripts/admin-api.mjs). Every change saves itself, and the
// preview beside it is the real page, which picks the edit up live.
import '../styles.css';
import './admin.css';
import { copy } from '../lib/clipboard.js';
import { icon, platformIcon } from '../lib/icons.js';
import { toast } from '../lib/toast.js';

const PLATFORMS = { linux: 'Linux', macos: 'macOS', windows: 'Windows' };
const STATES = { done: 'Shipped', active: 'In progress', todo: 'Next' };

// Page text, in the order it appears on the page.
const TEXT = [
  {
    group: 'Demo',
    preview: '#demo',
    fields: [['demo.title', 'Title'], ['demo.body', 'Intro', true]],
  },
  {
    group: 'Features',
    preview: '#features',
    fields: [
      ['features.title', 'Title'],
      ['features.body', 'Intro', true],
      ...['voice', 'apps', 'light', 'memory', 'guard', 'web'].flatMap((k) => [
        [`features.${k}.title`, `${k[0].toUpperCase()}${k.slice(1)} tile, title`],
        [`features.${k}.body`, `${k[0].toUpperCase()}${k.slice(1)} tile, text`, true],
      ]),
    ],
  },
  {
    group: 'System logs',
    preview: '#activity',
    fields: [['activity.title', 'Title'], ['activity.body', 'Intro', true]],
  },
  {
    group: 'Download',
    preview: '#download',
    fields: [['download.title', 'Title'], ['download.body', 'Intro', true]],
  },
  { group: 'Footer', preview: 'footer', fields: [['footer.privacy', 'Privacy line', true]] },
];

const $ = (sel, root = document) => root.querySelector(sel);
const esc = (s) => String(s ?? '').replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' })[c]);
const bytes = (n) => (n >= 1e9 ? `${(n / 1e9).toFixed(2)} GB` : n >= 1e6 ? `${(n / 1e6).toFixed(1)} MB` : `${Math.max(1, Math.round(n / 1e3))} KB`);
const get = (obj, path) => path.split('.').reduce((o, k) => o?.[k], obj);
function set(obj, path, value) {
  const keys = path.split('.');
  const last = keys.pop();
  const parent = keys.reduce((o, k) => (o[k] ??= {}), obj);
  parent[last] = value;
}

async function api(path, options = {}) {
  const res = await fetch(`/__admin${path}`, { ...options, headers: { 'x-ev-admin': '1', ...options.headers } });
  const body = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(body.error || `request failed (${res.status})`);
  return body;
}

let state = null; // { content, repo, uploads, uncommitted }
const uploading = {}; // platform -> percent; kept out of the content so it is never saved
const editor = $('#editor');

// --- saving ------------------------------------------------------------------

const saveEl = $('#save-state');
let saveTimer = 0;
let saving = false;
let again = false;
let dirty = false;

function setSaveState(kind, text) {
  saveEl.dataset.state = kind;
  saveEl.textContent = text;
}

function queueSave() {
  dirty = true;
  setSaveState('dirty', 'Editing');
  clearTimeout(saveTimer);
  saveTimer = setTimeout(save, 450);
}

async function save() {
  clearTimeout(saveTimer);
  if (saving) {
    again = true;
    return;
  }
  saving = true;
  setSaveState('saving', 'Saving');
  try {
    await api('/content', { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(state.content) });
    dirty = false;
    setSaveState('saved', 'All changes saved');
    refreshGit();
  } catch (err) {
    setSaveState('error', `Not saved: ${err.message}`);
  }
  saving = false;
  if (again) {
    again = false;
    save();
  }
}

async function refreshGit() {
  try {
    const s = await api('/state');
    state.uncommitted = s.uncommitted;
    state.uploads = s.uploads;
  } catch {
    /* the save itself worked; a stale count is fine */
  }
  const el = $('#git-state');
  if (state.uncommitted == null) return (el.textContent = '');
  el.dataset.clean = String(state.uncommitted === 0);
  el.textContent = state.uncommitted === 0 ? 'Committed' : `${state.uncommitted} file${state.uncommitted === 1 ? '' : 's'} to commit`;
  el.title = 'Changes live in site/content and site/public/downloads. Commit them with git when you are happy.';
}

window.addEventListener('beforeunload', (e) => {
  if (dirty || saving) e.preventDefault();
});
window.addEventListener('keydown', (e) => {
  if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 's') {
    e.preventDefault();
    save();
  }
});

// --- cards -------------------------------------------------------------------

function field(path, label, { multiline = false, placeholder = '', hint = '', type = 'text' } = {}) {
  const value = esc(get(state.content, path) ?? '');
  const control = multiline
    ? `<textarea data-path="${path}" rows="3" placeholder="${esc(placeholder)}">${value}</textarea>`
    : `<input type="${type}" data-path="${path}" value="${value}" placeholder="${esc(placeholder)}" spellcheck="${type === 'text'}" />`;
  return `<label class="field"><span class="field-label">${label}</span>${control}${hint ? `<small>${hint}</small>` : ''}</label>`;
}

function card(id, title, intro, preview, body, aside = '') {
  return `<section class="card" id="card-${id}" data-preview="${preview}">
    <header class="card-head"><div><h2>${title}</h2><p>${intro}</p></div>${aside}</header>
    <div class="card-body">${body}</div>
  </section>`;
}

function releaseCard() {
  const { repo, content } = state;
  const it = repo.iteration;
  const pin = content.release.pin;
  const options = [
    `<option value="auto"${pin === 'auto' ? ' selected' : ''}>Newest iteration, automatically (iteration ${it.number}, ${it.hash.slice(0, 7)})</option>`,
    ...repo.commits.map(
      (c) => `<option value="${c.hash.slice(0, 12)}"${pin !== 'auto' && c.hash.startsWith(pin) ? ' selected' : ''}>${c.date}  ${c.hash.slice(0, 7)}  ${esc(c.subject.slice(0, 60))}</option>`,
    ),
  ].join('');
  const facts = [
    ['Iteration', it.number],
    ['Commit', it.hash.slice(0, 7)],
    ['Date', new Date(`${it.date}T12:00:00`).toLocaleDateString('en-US', { month: 'short', day: 'numeric' })],
    ['Tests', repo.tests],
    ['Commits', repo.commitCount],
  ]
    .map(([k, v]) => `<div class="fact"><dt>${k}</dt><dd>${esc(v)}</dd></div>`)
    .join('');
  return card(
    'release',
    'Release',
    'Read from git automatically. Only change these to hold a release back or rename it.',
    '#download',
    `<dl class="facts">${facts}</dl>
     <label class="field"><span class="field-label">Stable points at</span><select data-path="release.pin">${options}</select>
       <small>Automatic follows the newest commit with "Iteration" in its message.</small></label>
     ${field('release.label', 'Version label', { placeholder: `iteration ${it.number} (automatic)`, hint: 'Shown on the download buttons. Leave empty for the automatic one.' })}`,
  );
}

function downloadBody(platform) {
  const d = state.content.release.downloads[platform];
  const base = `release.downloads.${platform}`;
  if (d.source === 'github') {
    return `<p class="hint">Zip of the pinned commit, straight from GitHub. Nothing to upload.</p>`;
  }
  if (d.source === 'url') {
    return `${field(`${base}.url`, 'Link', { type: 'url', placeholder: 'https://github.com/Alan-Betty/EV/releases/download/…' })}
      ${field(`${base}.sha256`, 'SHA-256 (optional)', { placeholder: '64 hex characters', hint: 'Without it, visitors are told to check the commit instead.' })}
      ${field(`${base}.title`, 'Button label', { placeholder: 'Download' })}`;
  }
  if (uploading[platform]) {
    const pct = uploading[platform];
    return `<div class="upload-progress" role="progressbar" aria-valuenow="${pct}" aria-valuemin="0" aria-valuemax="100" aria-label="Uploading">
      <span style="width:${pct}%"></span></div><p class="hint">Uploading and hashing, ${pct}%</p>`;
  }
  if (d.file) {
    return `<div class="file-row">
        <span class="file-icon">${icon.download('size-4')}</span>
        <div class="min-w-0 flex-1"><p class="truncate text-[13.5px] text-white">${esc(d.file)}</p>
          <p class="font-mono text-[11px] text-slate-500">${bytes(d.size || 0)}  sha256 ${esc((d.sha256 || '').slice(0, 16))}…</p></div>
        <button type="button" class="icon-btn" data-action="copy" data-value="${esc(d.sha256)}" aria-label="Copy SHA-256">${icon.copy('size-4')}</button>
        <label class="icon-btn" title="Replace the file">${icon.download('size-4 rotate-180')}<input type="file" class="sr-only" data-upload="${platform}" aria-label="Replace the ${PLATFORMS[platform]} file" /></label>
        <button type="button" class="icon-btn" data-action="remove-file" data-platform="${platform}" aria-label="Remove the file">${icon.close('size-4')}</button>
      </div>
      ${field(`${base}.title`, 'Button label', { placeholder: 'Installer' })}`;
  }
  return `<label class="dropzone" data-drop="${platform}">
      <input type="file" class="sr-only" data-upload="${platform}" />
      <span class="text-[13.5px] text-slate-200">Drop a file here, or click to choose one</span>
      <span class="text-[12px] text-slate-500">It is copied into public/downloads and hashed automatically.</span>
    </label>`;
}

function downloadsCard() {
  const rows = Object.entries(PLATFORMS)
    .map(([p, label]) => {
      const source = state.content.release.downloads[p].source;
      const seg = [['github', 'GitHub zip'], ['file', 'Upload'], ['url', 'Link']]
        .map(([v, l]) => `<button type="button" role="radio" aria-checked="${source === v}" data-action="source" data-platform="${p}" data-value="${v}">${l}</button>`)
        .join('');
      return `<div class="platform" data-platform="${p}">
        <div class="flex flex-wrap items-center justify-between gap-3">
          <p class="flex items-center gap-2.5 text-[14px] font-medium text-white"><span class="platform-icon">${platformIcon(p, 'size-4')}</span>${label}</p>
          <div class="seg" role="radiogroup" aria-label="${label} download">${seg}</div>
        </div>
        <div class="platform-body" data-body="${p}">${downloadBody(p)}</div>
      </div>`;
    })
    .join('');
  return card('downloads', 'Downloads', 'What the Stable buttons hand out on each system. Nightly and Canary always use GitHub.', '#download', rows);
}

function heroCard() {
  return card(
    'hero',
    'Headline',
    'The first thing anyone reads.',
    '#top',
    `${field('hero.headline', 'Headline')}
     ${field('hero.sub', 'Intro', { multiline: true })}
     <div class="grid gap-4 sm:grid-cols-[1fr_9rem]">
       ${field('hero.status', 'Status line', { hint: 'Next to the iteration badge above the headline.' })}
       ${field('hero.statusLink', 'Links to', { placeholder: '#features' })}
     </div>`,
  );
}

function roadmapPercent() {
  const items = state.content.roadmap;
  const done = items.filter((m) => m.state === 'done').length;
  const partial = items.reduce((n, m) => n + (m.state === 'active' ? (Number(m.progress) || 0) / 100 : 0), 0);
  return items.length ? Math.round(((done + partial) / items.length) * 100) : 0;
}

function roadmapRows() {
  return state.content.roadmap
    .map((m, i) => {
      const states = Object.entries(STATES).map(([v, l]) => `<option value="${v}"${m.state === v ? ' selected' : ''}>${l}</option>`).join('');
      return `<li class="road-row" data-state="${m.state}">
        <input data-path="roadmap.${i}.label" value="${esc(m.label)}" aria-label="Milestone ${i + 1}" />
        <select data-path="roadmap.${i}.state" data-rerender="roadmap" aria-label="State of milestone ${i + 1}">${states}</select>
        <div class="road-tools">
          <button type="button" class="icon-btn" data-action="road-up" data-i="${i}" aria-label="Move up"${i === 0 ? ' disabled' : ''}>↑</button>
          <button type="button" class="icon-btn" data-action="road-down" data-i="${i}" aria-label="Move down"${i === state.content.roadmap.length - 1 ? ' disabled' : ''}>↓</button>
          <button type="button" class="icon-btn" data-action="road-del" data-i="${i}" aria-label="Delete">${icon.close('size-4')}</button>
        </div>
        ${m.state === 'active' ? `<label class="road-progress"><span class="sr-only">Progress</span><input type="range" min="0" max="100" step="5" data-path="roadmap.${i}.progress" data-type="number" value="${Number(m.progress) || 0}" /><output>${Number(m.progress) || 0}%</output></label>` : ''}
      </li>`;
    })
    .join('');
}

function roadmapCard() {
  return card(
    'roadmap',
    'Road to 1.0',
    'The checklist beside the system logs. The percentage works itself out.',
    '#activity',
    `<ol class="roadmap" id="roadmap">${roadmapRows()}</ol>
     <button type="button" class="admin-btn mt-3" data-action="road-add">Add a milestone</button>`,
    `<span class="pct" id="road-pct">${roadmapPercent()}%</span>`,
  );
}

function textCard() {
  const groups = TEXT.map(
    (g) => `<details class="text-group" data-preview="${g.preview}">
      <summary>${g.group}</summary>
      <div class="space-y-4 pt-3">${g.fields.map(([path, label, multi]) => field(path, label, { multiline: multi })).join('')}</div>
    </details>`,
  ).join('');
  return card('text', 'Everything else on the page', 'Write {iteration}, {tests} or {commits} and the number fills itself in.', '#demo', groups);
}

function render() {
  editor.innerHTML = [releaseCard(), downloadsCard(), heroCard(), roadmapCard(), textCard()].join('');
  editor.querySelectorAll('textarea').forEach(autoGrow);
}

function rerender(which, keepFocusPath) {
  if (which === 'roadmap') {
    $('#roadmap').innerHTML = roadmapRows();
    $('#road-pct').textContent = `${roadmapPercent()}%`;
  } else if (which.startsWith('download:')) {
    const p = which.split(':')[1];
    $(`[data-body="${p}"]`).innerHTML = downloadBody(p);
    const src = state.content.release.downloads[p].source;
    editor.querySelectorAll(`[data-action="source"][data-platform="${p}"]`).forEach((b) => b.setAttribute('aria-checked', String(b.dataset.value === src)));
  }
  if (keepFocusPath) editor.querySelector(`[data-path="${keepFocusPath}"]`)?.focus();
}

function autoGrow(ta) {
  ta.style.height = 'auto';
  ta.style.height = `${ta.scrollHeight + 2}px`;
}

// --- uploads -----------------------------------------------------------------

function upload(platform, file) {
  const d = state.content.release.downloads[platform];
  uploading[platform] = 1;
  rerender(`download:${platform}`);

  // XHR rather than fetch: fetch cannot report upload progress.
  const xhr = new XMLHttpRequest();
  xhr.open('PUT', `/__admin/upload?name=${encodeURIComponent(file.name)}`);
  xhr.setRequestHeader('x-ev-admin', '1');
  xhr.upload.onprogress = (e) => {
    if (!e.lengthComputable) return;
    const pct = Math.max(1, Math.round((e.loaded / e.total) * 100));
    uploading[platform] = pct;
    const bar = $(`[data-body="${platform}"] .upload-progress span`);
    if (bar) bar.style.width = `${pct}%`;
    const hint = $(`[data-body="${platform}"] .hint`);
    if (hint) hint.textContent = `Uploading and hashing, ${pct}%`;
  };
  xhr.onload = () => {
    delete uploading[platform];
    let body = {};
    try {
      body = JSON.parse(xhr.responseText);
    } catch {}
    if (xhr.status !== 200) {
      toast(`Upload failed: ${body.error || xhr.status}`, 'error');
      rerender(`download:${platform}`);
      return;
    }
    Object.assign(d, { source: 'file', file: body.file, size: body.size, sha256: body.sha256 });
    rerender(`download:${platform}`);
    toast(`${body.file} uploaded and hashed`);
    save();
  };
  xhr.onerror = () => {
    delete uploading[platform];
    toast('Upload failed: the dev server did not answer', 'error');
    rerender(`download:${platform}`);
  };
  xhr.send(file);
}

// --- events ------------------------------------------------------------------

editor.addEventListener('input', (e) => {
  const el = e.target.closest('[data-path]');
  if (!el) return;
  const value = el.dataset.type === 'number' ? Number(el.value) : el.value;
  set(state.content, el.dataset.path, value);
  if (el.tagName === 'TEXTAREA') autoGrow(el);
  if (el.type === 'range') {
    el.nextElementSibling.textContent = `${value}%`;
    $('#road-pct').textContent = `${roadmapPercent()}%`;
  }
  if (el.dataset.rerender) rerender(el.dataset.rerender, el.dataset.path);
  queueSave();
});

editor.addEventListener('change', (e) => {
  const input = e.target.closest('[data-upload]');
  if (input?.files?.[0]) upload(input.dataset.upload, input.files[0]);
});

editor.addEventListener('click', async (e) => {
  const btn = e.target.closest('[data-action]');
  if (!btn) return;
  const { action, platform } = btn.dataset;
  const road = state.content.roadmap;
  const i = Number(btn.dataset.i);

  if (action === 'source') {
    state.content.release.downloads[platform].source = btn.dataset.value;
    rerender(`download:${platform}`);
    queueSave();
  } else if (action === 'remove-file') {
    const d = state.content.release.downloads[platform];
    const name = d.file;
    state.content.release.downloads[platform] = { source: 'github' };
    rerender(`download:${platform}`);
    await save();
    // Only delete the file itself once no platform points at it any more.
    const used = Object.values(state.content.release.downloads).some((x) => x.file === name);
    if (name && !used) await api(`/upload?name=${encodeURIComponent(name)}`, { method: 'DELETE' }).catch(() => {});
    refreshGit();
  } else if (action === 'copy') {
    copy(btn.dataset.value, 'Copied SHA-256');
  } else if (action === 'road-add') {
    road.push({ label: '', state: 'todo' });
    rerender('roadmap', `roadmap.${road.length - 1}.label`);
    queueSave();
  } else if (action === 'road-del') {
    road.splice(i, 1);
    rerender('roadmap');
    queueSave();
  } else if (action === 'road-up' || action === 'road-down') {
    const j = action === 'road-up' ? i - 1 : i + 1;
    [road[i], road[j]] = [road[j], road[i]];
    rerender('roadmap');
    editor.querySelector(`[data-action="${action}"][data-i="${j}"]`)?.focus();
    queueSave();
  }
});

// Drag a file onto a platform's drop zone.
editor.addEventListener('dragover', (e) => {
  const zone = e.target.closest('[data-drop]');
  if (!zone) return;
  e.preventDefault();
  zone.dataset.over = 'true';
});
editor.addEventListener('dragleave', (e) => e.target.closest('[data-drop]')?.removeAttribute('data-over'));
editor.addEventListener('drop', (e) => {
  const zone = e.target.closest('[data-drop]');
  if (!zone) return;
  e.preventDefault();
  zone.removeAttribute('data-over');
  const file = e.dataTransfer.files?.[0];
  if (file) upload(zone.dataset.drop, file);
});

// Focusing a field scrolls the preview to the part of the page it changes.
const frame = $('#preview');
let lastPreview = '';
editor.addEventListener('focusin', (e) => {
  const sel = e.target.closest('[data-preview]')?.dataset.preview;
  if (!sel || sel === lastPreview) return;
  lastPreview = sel;
  try {
    frame.contentDocument?.querySelector(sel)?.scrollIntoView({ behavior: 'smooth', block: 'start' });
  } catch {
    /* preview still loading */
  }
});
editor.addEventListener('toggle', (e) => {
  if (e.target.open) e.target.querySelector('input, textarea')?.focus({ preventScroll: true });
}, true);

// Preview size
document.querySelector('.admin-preview .seg').addEventListener('click', (e) => {
  const b = e.target.closest('[data-device]');
  if (!b) return;
  $('#frame-wrap').dataset.device = b.dataset.device;
  b.parentElement.querySelectorAll('[data-device]').forEach((x) => x.setAttribute('aria-checked', String(x === b)));
});

// Top bar
document.querySelector('.admin-top').addEventListener('click', async (e) => {
  const btn = e.target.closest('[data-action]');
  if (!btn) return;
  btn.disabled = true;
  try {
    if (btn.dataset.action === 'sync') {
      const res = await api('/sync', { method: 'POST' });
      state.repo = res.repo;
      $('#card-release').outerHTML = releaseCard();
      toast(`Read git: iteration ${res.repo.iteration.number}, ${res.repo.commitCount} commits, ${res.repo.tests} tests`);
    } else if (btn.dataset.action === 'build') {
      if (dirty) await save();
      btn.textContent = 'Building';
      const res = await api('/build', { method: 'POST' }).catch((err) => ({ ok: false, output: err.message }));
      $('#build-output').textContent = res.output;
      $('#build-title').textContent = res.ok ? 'Built. The site is in site/dist.' : 'The build failed';
      $('#build-dialog').showModal();
    }
  } catch (err) {
    toast(err.message, 'error');
  } finally {
    btn.disabled = false;
    if (btn.dataset.action === 'build') btn.textContent = 'Build';
  }
});

// --- start -------------------------------------------------------------------

api('/state')
  .then((s) => {
    state = s;
    render();
    setSaveState('saved', 'All changes saved');
    refreshGit();
  })
  .catch((err) => {
    editor.innerHTML = `<div class="p-8"><p class="text-[15px] text-white">The manager could not reach its API.</p>
      <p class="mt-2 text-[13.5px] text-slate-400">${esc(err.message)}. Start it with <code class="font-mono text-slate-200">npm run admin</code> inside the site folder.</p></div>`;
    setSaveState('error', 'Offline');
  });
