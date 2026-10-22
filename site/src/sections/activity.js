import { POOLS, READINESS } from '../data/activity.js';
import REPO from '../data/repo.json';
import { copy, flashCopied } from '../lib/clipboard.js';
import { icon } from '../lib/icons.js';

const MAX_ROWS = 240;
const TYPES = ['commit', ...Object.keys(POOLS)];

const pick = (arr) => arr[Math.floor(Math.random() * arr.length)];
const clock = (d) => d.toTimeString().slice(0, 8);
const day = (iso) => new Date(`${iso}T12:00:00`).toLocaleDateString('en-US', { month: 'short', day: '2-digit' });
const escape = (s) => s.replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' })[c]);

function makeEntry(type, at = new Date()) {
  const [msg, status] = pick(POOLS[type]);
  return { type, at, msg, status };
}

export function initActivity() {
  const root = document.getElementById('activity');
  if (!root) return;

  const list = root.querySelector('#log-list');
  const chips = root.querySelector('#log-filters');
  const search = root.querySelector('#log-search');
  const pauseBtn = root.querySelector('#log-pause');
  const counter = root.querySelector('#log-count');
  const empty = root.querySelector('#log-empty');

  const counts = Object.fromEntries(TYPES.map((t) => [t, 0]));
  let filter = 'all';
  let query = '';
  let paused = false;
  let onScreen = false;
  let follow = true; // stick to the bottom unless the reader scrolled up
  let timer = 0;

  function matches(li) {
    return (filter === 'all' || li.dataset.type === filter) && (!query || li.dataset.text.includes(query));
  }

  function render(entry) {
    const li = document.createElement('li');
    li.className = 'log-row';
    li.dataset.type = entry.type;
    li.dataset.text = `${entry.type} ${entry.msg} ${entry.hash ?? ''}`.toLowerCase();
    li.innerHTML = `
      <time class="log-time">${entry.date ? day(entry.date) : clock(entry.at)}</time>
      <span class="log-type" data-type="${entry.type}">${entry.type}</span>
      <span class="log-msg" data-status="${entry.status}">${escape(entry.msg)}</span>
      ${
        entry.hash
          ? `<button type="button" class="log-hash" data-hash="${entry.full ?? entry.hash}" aria-label="Copy commit hash ${entry.hash}"><span>${entry.hash}</span><span class="copy-icon">${icon.copy('size-3')}</span><span class="check-icon">${icon.check('size-3')}</span></button>`
          : `<span class="log-status" data-status="${entry.status}">${entry.status === 'info' ? '' : entry.status}</span>`
      }`;
    li.hidden = !matches(li);
    return li;
  }

  function append(entry) {
    counts[entry.type]++;
    list.append(render(entry));
    while (list.childElementCount > MAX_ROWS) {
      counts[list.firstElementChild.dataset.type]--;
      list.firstElementChild.remove();
    }
    refreshCounts();
    if (follow) list.scrollTop = list.scrollHeight;
  }

  function refreshCounts() {
    chips.querySelectorAll('[data-filter]').forEach((b) => {
      const n = b.dataset.filter === 'all' ? list.childElementCount : counts[b.dataset.filter];
      b.querySelector('[data-n]').textContent = n;
    });
    const shown = [...list.children].filter((li) => !li.hidden).length;
    counter.textContent = `${shown} shown`;
    empty.hidden = shown > 0;
  }

  function applyFilters() {
    for (const li of list.children) li.hidden = !matches(li);
    refreshCounts();
    list.scrollTop = list.scrollHeight;
  }

  // E.V.'s real history first, oldest at the top, then a short replayed session.
  [...REPO.commits].reverse().forEach((c) =>
    append({ type: 'commit', date: c.date, msg: c.subject, hash: c.hash.slice(0, 7), full: c.hash, status: 'ok' }),
  );
  const now = Date.now();
  const seedTypes = ['voice', 'tool', 'test', 'guard', 'voice', 'tool'];
  seedTypes.forEach((t, i) => append(makeEntry(t, new Date(now - (seedTypes.length - i) * 9_000))));

  // Stream
  function schedule() {
    clearTimeout(timer);
    if (paused || !onScreen || document.hidden) return;
    timer = setTimeout(() => {
      const weights = ['tool', 'tool', 'tool', 'voice', 'voice', 'test', 'test', 'guard'];
      append(makeEntry(pick(weights)));
      schedule();
    }, 900 + Math.random() * 1600);
  }
  new IntersectionObserver(([e]) => {
    onScreen = e.isIntersecting;
    schedule();
  }).observe(list);
  document.addEventListener('visibilitychange', schedule);

  function setPaused(value) {
    paused = value;
    pauseBtn.setAttribute('aria-pressed', String(paused));
    pauseBtn.querySelector('[data-label]').textContent = paused ? 'Resume' : 'Pause';
    pauseBtn.querySelector('[data-icon]').innerHTML = paused ? icon.play('size-3.5') : icon.pause('size-3.5');
    root.querySelector('#log-live').dataset.live = String(!paused);
    schedule();
  }
  pauseBtn.addEventListener('click', () => setPaused(!paused));
  setPaused(false);

  list.addEventListener('scroll', () => {
    follow = list.scrollHeight - list.scrollTop - list.clientHeight < 24;
  }, { passive: true });

  chips.addEventListener('click', (e) => {
    const btn = e.target.closest('[data-filter]');
    if (!btn) return;
    filter = btn.dataset.filter;
    chips.querySelectorAll('[data-filter]').forEach((b) => b.setAttribute('aria-pressed', String(b === btn)));
    applyFilters();
  });

  search.addEventListener('input', () => {
    query = search.value.trim().toLowerCase();
    applyFilters();
  });

  list.addEventListener('click', (e) => {
    const btn = e.target.closest('[data-hash]');
    if (!btn) return;
    copy(btn.dataset.hash, `Copied ${btn.dataset.hash.slice(0, 7)}`).then((ok) => ok && flashCopied(btn));
  });

  // Readiness: fill the bars once, when the panel first comes into view.
  const panel = root.querySelector('#readiness');
  const done = READINESS.milestones.filter((m) => m.state === 'done').length;
  const partial = READINESS.milestones.reduce((n, m) => n + (m.state === 'active' ? m.progress / 100 : 0), 0);
  const percent = Math.round(((done + partial) / READINESS.milestones.length) * 100);
  panel.querySelector('[data-percent]').textContent = `${percent}%`;
  panel.querySelector('[data-closed]').textContent = `${done} of ${READINESS.milestones.length} shipped`;
  panel.querySelector('#readiness-bar').setAttribute('aria-valuenow', percent);
  panel.querySelector('#milestones').innerHTML = READINESS.milestones
    .map(
      (m) => `<li class="milestone" data-state="${m.state}">
        <span class="milestone-mark" aria-hidden="true">${m.state === 'done' ? icon.check('size-3') : ''}</span>
        <span class="flex-1">${escape(m.label)}</span>
        <span class="mono-meta">${m.state === 'done' ? 'shipped' : m.state === 'active' ? `${m.progress}%` : 'next'}</span>
        <span class="sr-only">${m.state === 'done' ? 'shipped' : m.state === 'active' ? 'in progress' : 'not started'}</span>
      </li>`,
    )
    .join('');
  const io = new IntersectionObserver(([e]) => {
    if (!e.isIntersecting) return;
    io.disconnect();
    panel.style.setProperty('--fill', `${percent}%`);
  }, { threshold: 0.3 });
  io.observe(panel);
}
