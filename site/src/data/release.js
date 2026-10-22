// Single source of truth for everything the page states about a release.
// Placeholder values: CI should generate this file (or a JSON manifest it
// replaces) from the real build artifacts before the site is published.

export const PRODUCT = {
  name: 'NEXUS',
  repo: 'https://github.com/nexus-assistant/nexus',
  docs: '#',
};

export const CHANNELS = {
  stable: { label: 'Stable', version: '0.9.4-beta', date: '2026-10-20', note: 'Recommended. Tested across every supported desktop.' },
  nightly: { label: 'Nightly', version: '0.9.5-nightly.20261022', date: '2026-10-22', note: 'Built from main every night. Passing tests, rough edges.' },
  canary: { label: 'Canary', version: '0.10.0-canary.7', date: '2026-10-22', note: 'Every merged commit. Expect breakage. For contributors.' },
};

export const PLATFORMS = {
  linux: { label: 'Linux', long: 'Linux and Arch' },
  macos: { label: 'macOS', long: 'macOS 13 or later' },
  windows: { label: 'Windows', long: 'Windows 11' },
};
