// Everything the page says about getting E.V. Repo facts come from repo.json
// (regenerated from git before every dev and build run); the choices a human
// makes - which commit Stable points at, its label, and what each platform's
// download is - come from content/site.json, usually through the site manager.
//
// Out of the box E.V. ships as source, so a download is a GitHub archive
// pinned to a commit and the thing to verify is that commit id. Upload a real
// file in the manager and that platform switches to the file and its SHA-256.
// content.js owns the JSON import; it alone accepts hot updates, so an edit
// swaps in live instead of reloading the page.
import { CONTENT } from '../lib/content.js';
import { store } from '../lib/store.js';
import REPO from './repo.json';

export { REPO };

export const PRODUCT = {
  name: 'E.V.',
  repo: REPO.repo,
  docs: `${REPO.repo}/blob/main/OVERVIEW.md`,
  quickstart: `${REPO.repo}/blob/main/quickstart.md`,
  devlog: `${REPO.repo}/blob/main/devlog.md`,
};

const short = (sha) => sha.slice(0, 7);
const iterationOf = (commit) =>
  commit === REPO.iteration ? REPO.iteration.number : Number(commit.subject.match(/(\d+)\s*(?:st|nd|rd|th|h)?\s+iteration/i)?.[1]) || null;

export const formatBytes = (n) => (n >= 1e9 ? `${(n / 1e9).toFixed(2)} GB` : n >= 1e6 ? `${(n / 1e6).toFixed(1)} MB` : `${Math.max(1, Math.round(n / 1e3))} KB`);

// The commit Stable points at: "auto" follows the latest iteration commit.
export function pinnedCommit(content = store.get('content') ?? CONTENT) {
  const pin = content.release?.pin;
  if (!pin || pin === 'auto') return REPO.iteration;
  return REPO.commits.find((c) => c.hash.startsWith(pin)) ?? REPO.iteration;
}

// Rebuilt in place whenever the content changes, so importers keep one object.
export const CHANNELS = {};

function rebuild(content) {
  const commit = pinnedCommit(content);
  const n = iterationOf(commit);
  const label = content.release?.label?.trim();
  CHANNELS.stable = {
    label: 'Stable',
    version: label || (n ? `iteration ${n}` : short(commit.hash)),
    tag: label || (n ? `iter ${n}` : short(commit.hash)),
    ref: commit.hash,
    date: commit.date,
    note: `${label || (n ? `Iteration ${n}` : `Commit ${short(commit.hash)}`)}, ${commit.date}. What the devlog describes.`,
  };
  CHANNELS.nightly = {
    label: 'Nightly',
    version: 'main',
    tag: 'main',
    ref: 'main',
    date: REPO.head.date,
    note: 'Whatever is on main right now. Usually fine, sometimes mid-change.',
  };
  CHANNELS.canary = {
    label: 'Canary',
    version: 'main + dev tools',
    tag: 'dev',
    ref: 'main',
    date: REPO.head.date,
    note: `For contributors: a clone with the ${REPO.tests} offline tests and the live browser suite.`,
  };
}
rebuild(CONTENT);
// Subscribed at import time, so it runs before any section re-renders.
store.on('content', (content) => content && rebuild(content));

export const PLATFORMS = {
  linux: {
    label: 'Linux',
    long: 'Ubuntu first-class, other distros work',
    requires: 'Python 3.11+. On GNOME Wayland, install the window extension and log in again.',
    setup: 'sudo apt install libportaudio2 && python3 -m pip install -r requirements.txt && cp .env.example .env',
    extra: { name: 'Wayland window support', detail: 'GNOME Shell extension for exact windows and focus', command: 'python3 ev_core.py --install-gnome-extension' },
    verifyFile: (a) => `echo "${a.sha256}  ${a.file}" | sha256sum -c`,
  },
  macos: {
    label: 'macOS',
    long: 'Experimental',
    requires: 'The voice loop, browser errands and files work. Desktop control is partial, through System Events.',
    setup: 'python3 -m pip install -r requirements.txt && cp .env.example .env',
    extra: null,
    verifyFile: (a) => `shasum -a 256 -c <<< "${a.sha256}  ${a.file}"`,
  },
  windows: {
    label: 'Windows',
    long: 'Windows 10 and 11, the primary target',
    requires: 'Python 3.11+. Audio plays through winmm and needs no extra package.',
    setup: 'py -m pip install -r requirements.txt; copy .env.example .env',
    extra: null,
    verifyFile: (a) => `(Get-FileHash .\\${a.file} -Algorithm SHA256).Hash -eq '${a.sha256.toUpperCase()}'`,
  },
};

// How to check a download: its SHA-256 when there is one, else its commit.
export function verifyCommand(platform, build) {
  if (build.sha256) return PLATFORMS[platform].verifyFile(build);
  return platform === 'windows'
    ? 'git rev-parse HEAD   # in a clone; or open the zip and read its comment'
    : 'unzip -z EV-*.zip | tail -1   # prints the commit the archive was made from';
}

// The Stable download for one platform, from the manager's settings.
function stableDownload(platform, content) {
  const d = content.release?.downloads?.[platform] ?? { source: 'github' };
  const commit = CHANNELS.stable.ref;
  if (d.source === 'file' && d.file) {
    const file = d.file.split('/').pop();
    return { id: 'file', name: d.title || 'Installer', detail: `${file}, ${formatBytes(d.size || 0)}`, primary: true, url: `downloads/${file}`, file, size: formatBytes(d.size || 0), sha256: d.sha256, commit };
  }
  if (d.source === 'url' && d.url) {
    const file = decodeURIComponent(d.url.split('?')[0].split('/').pop() || 'download');
    return { id: 'url', name: d.title || 'Download', detail: file, primary: true, url: d.url, file, sha256: d.sha256 || null, commit };
  }
  return null;
}

// Rows for one platform card, in display order.
export function artifactsFor(platform, channel) {
  const content = store.get('content') ?? CONTENT;
  const c = CHANNELS[channel];
  const p = PLATFORMS[platform];
  const commit = c.ref === 'main' ? REPO.head.hash : c.ref;
  const rows = [];

  if (channel === 'canary') {
    rows.push({ id: 'clone', name: 'Clone', detail: 'Full history, ready for branches', command: `git clone ${REPO.repo}.git && cd EV` });
    rows.push({ id: 'tests', name: 'Run the tests', detail: `${REPO.tests} tests, offline, no API key`, command: 'python -m pip install pytest && python -m pytest tests/ -q' });
    rows.push({ id: 'live', name: 'Live browser suite', detail: 'Real Chromium against real sites', command: 'EV_LIVE_BROWSER=1 python -m pytest tests/test_web_agent_live.py -q' });
    return rows;
  }

  const custom = channel === 'stable' ? stableDownload(platform, content) : null;
  rows.push(
    custom ?? {
      id: 'archive',
      name: 'Source archive',
      detail: `${c.ref === 'main' ? 'main' : c.version}, zip from GitHub`,
      primary: true,
      url: c.ref === 'main' ? `${REPO.repo}/archive/refs/heads/main.zip` : `${REPO.repo}/archive/${c.ref}.zip`,
      file: `EV-${c.ref === 'main' ? 'main' : short(c.ref)}.zip`,
      commit,
    },
  );
  rows.push({ id: 'setup', name: 'Set up', detail: 'Then add a free Groq or Gemini key to .env', command: p.setup });
  if (p.extra) rows.push({ id: 'extra', ...p.extra });
  return rows;
}

// What a "Download for X" button hands over.
export function primaryFor(platform, channel) {
  return artifactsFor(platform, channel === 'canary' ? 'nightly' : channel).find((a) => a.primary);
}
