// Everything the page says about getting E.V. E.V. ships as source: there are
// no binaries, so a "build" is a GitHub source archive pinned to a commit, and
// the thing to verify is that commit id. Repo facts come from repo.json, which
// scripts/sync-repo.mjs regenerates from git before every dev and build run.
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

export const CHANNELS = {
  stable: {
    label: 'Stable',
    version: `iteration ${REPO.iteration.number}`,
    tag: `iter ${REPO.iteration.number}`,
    ref: REPO.iteration.hash,
    date: REPO.iteration.date,
    note: `Pinned to the iteration ${REPO.iteration.number} commit. What the devlog describes.`,
  },
  nightly: {
    label: 'Nightly',
    version: 'main',
    tag: 'main',
    ref: 'main',
    date: REPO.head.date,
    note: 'Whatever is on main right now. Usually fine, sometimes mid-change.',
  },
  canary: {
    label: 'Canary',
    version: 'main + dev tools',
    tag: 'dev',
    ref: 'main',
    date: REPO.head.date,
    note: `For contributors: a clone with the ${REPO.tests} offline tests and the live browser suite.`,
  },
};

export const PLATFORMS = {
  linux: {
    label: 'Linux',
    long: 'Ubuntu first-class, other distros work',
    requires: 'Python 3.11+. On GNOME Wayland, install the window extension and log in again.',
    setup: 'sudo apt install libportaudio2 && python3 -m pip install -r requirements.txt && cp .env.example .env',
    extra: { name: 'Wayland window support', detail: 'GNOME Shell extension for exact windows and focus', command: 'python3 ev_core.py --install-gnome-extension' },
    verify: () => 'unzip -z EV-*.zip | tail -1   # prints the commit the archive was made from',
  },
  macos: {
    label: 'macOS',
    long: 'Experimental',
    requires: 'The voice loop, browser errands and files work. Desktop control is partial, through System Events.',
    setup: 'python3 -m pip install -r requirements.txt && cp .env.example .env',
    extra: null,
    verify: () => 'unzip -z EV-*.zip | tail -1   # prints the commit the archive was made from',
  },
  windows: {
    label: 'Windows',
    long: 'Windows 10 and 11, the primary target',
    requires: 'Python 3.11+. Audio plays through winmm and needs no extra package.',
    setup: 'py -m pip install -r requirements.txt; copy .env.example .env',
    extra: null,
    verify: () => 'git rev-parse HEAD   # in a clone; or open the zip and read its comment',
  },
};

export const EXTRAS = 'python -m pip install playwright PySide6-Essentials send2trash && python -m playwright install chromium';

// Rows for one platform card, in display order.
export function artifactsFor(platform, channel) {
  const c = CHANNELS[channel];
  const p = PLATFORMS[platform];
  const commit = c.ref === 'main' ? REPO.head.hash : c.ref;
  const zip = c.ref === 'main' ? `${REPO.repo}/archive/refs/heads/main.zip` : `${REPO.repo}/archive/${c.ref}.zip`;
  const rows = [];

  if (channel === 'canary') {
    rows.push({ id: 'clone', name: 'Clone', detail: 'Full history, ready for branches', command: `git clone ${REPO.repo}.git && cd EV` });
    rows.push({ id: 'tests', name: 'Run the tests', detail: `${REPO.tests} tests, offline, no API key`, command: 'python -m pip install pytest && python -m pytest tests/ -q' });
    rows.push({ id: 'live', name: 'Live browser suite', detail: 'Real Chromium against real sites', command: 'EV_LIVE_BROWSER=1 python -m pytest tests/test_web_agent_live.py -q' });
    return rows;
  }

  rows.push({
    id: 'archive',
    name: 'Source archive',
    detail: `${c.ref === 'main' ? 'main' : `iteration ${REPO.iteration.number}`}, zip from GitHub`,
    primary: true,
    url: zip,
    file: `EV-${c.ref === 'main' ? 'main' : short(c.ref)}.zip`,
    commit,
  });
  rows.push({ id: 'setup', name: 'Set up', detail: 'Then add a free Groq or Gemini key to .env', command: p.setup });
  if (p.extra) rows.push({ id: 'extra', ...p.extra });
  return rows;
}

// What a "Download for X" button hands over.
export function primaryFor(platform, channel) {
  return artifactsFor(platform, channel === 'canary' ? 'nightly' : channel).find((a) => a.primary);
}
