import { CHECKSUMS } from './checksums.js';

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
  linux: {
    label: 'Linux',
    long: 'Linux and Arch',
    requires: 'glibc 2.35+, Wayland or X11. Hyprland IPC detected automatically.',
    verify: (a) => `echo "${a.sha256}  ${a.file}" | sha256sum -c`,
  },
  macos: {
    label: 'macOS',
    long: 'macOS 13 or later',
    requires: 'Apple silicon recommended. Screen Recording and Accessibility permissions.',
    verify: (a) => `shasum -a 256 -c <<< "${a.sha256}  ${a.file}"`,
  },
  windows: {
    label: 'Windows',
    long: 'Windows 11',
    requires: 'Windows 11 22H2 or later. GPU with 6 GB VRAM recommended.',
    verify: (a) => `(Get-FileHash .\\${a.file} -Algorithm SHA256).Hash -eq '${a.sha256.toUpperCase()}'`,
  },
};

// Seed values for the live counters. Replace with a real stats endpoint.
export const COUNTERS = { downloads: 1_284_302, nodes: 48_117 };

// Per platform, in display order. `file` and `command` take the channel's version.
const ARTIFACTS = {
  linux: [
    { id: 'appimage', name: 'AppImage', detail: 'Any distro, no install', arch: 'x86_64', size: 48.2, primary: true, file: (v) => `nexus-${v}-x86_64.AppImage` },
    { id: 'aur', name: 'AUR package', detail: 'Arch, Manjaro, EndeavourOS', arch: 'x86_64', command: (_, channel) => `yay -S ${{ stable: 'nexus-bin', nightly: 'nexus-nightly-bin', canary: 'nexus-git' }[channel]}` },
    { id: 'tarball', name: 'Tarball', detail: 'Portable, unpack anywhere', arch: 'x86_64', size: 41.7, file: (v) => `nexus-${v}-linux-x86_64.tar.gz` },
  ],
  macos: [
    { id: 'arm64', name: 'Apple silicon', detail: 'M1 and later', arch: 'arm64', size: 52.9, primary: true, file: (v) => `NEXUS-${v}-arm64.dmg` },
    { id: 'x86_64', name: 'Intel', detail: 'macOS 13 or later', arch: 'x86_64', size: 55.4, file: (v) => `NEXUS-${v}-x86_64.dmg` },
  ],
  windows: [
    { id: 'msi', name: 'Installer', detail: 'Per-user .msi, no admin', arch: 'x64', size: 46.1, primary: true, file: (v) => `NEXUS-${v}-x64.msi` },
    { id: 'portable', name: 'Portable package', detail: 'Zip, runs from a folder', arch: 'x64', size: 44.8, file: (v) => `NEXUS-${v}-x64-portable.zip` },
  ],
};

export function artifactsFor(platform, channel) {
  const { version } = CHANNELS[channel];
  return ARTIFACTS[platform].map((a) => {
    const file = a.file?.(version) ?? null;
    return {
      ...a,
      version,
      file,
      command: a.command?.(version, channel) ?? null,
      sha256: file ? CHECKSUMS[file] : null,
      url: file ? `${PRODUCT.repo}/releases/download/v${version}/${file}` : null,
    };
  });
}

// The build a "Download for X" button should hand over, honouring a sniffed CPU arch.
export function primaryFor(platform, channel, arch) {
  const list = artifactsFor(platform, channel).filter((a) => a.file);
  return list.find((a) => arch && a.arch === arch) ?? list.find((a) => a.primary) ?? list[0];
}
