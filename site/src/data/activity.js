// Pools the activity stream draws from. Placeholder content until the page is
// pointed at a real CI / event feed.

export const POOLS = {
  commit: [
    'fix(voice): hold barge-in until the echo level is learned',
    'feat(vision): crop before downscale for small dialogs',
    'perf(stt): end the utterance 600 ms after a full sentence',
    'fix(guard): strip confirmed from model-supplied arguments',
    'feat(ipc): reconnect to hyprland socket2 with backoff',
    'refactor(memory): atomic writes through os.replace',
    'feat(plugins): hot reload on inotify close_write',
    'fix(overlay): never take focus on XWayland',
    'test(web): live log-in and basket flow',
    'perf(orb): cache bloom per quantised energy',
    'feat(a11y): number AT-SPI elements for the planner',
    'fix(audio): pin a real microphone, never a monitor source',
  ],
  build: [
    ['linux-x86_64 AppImage packed, 48.2 MB', 'ok'],
    ['macos-arm64 dmg notarized', 'ok'],
    ['windows-x64 msi signed', 'ok'],
    ['tarball reproducible, digest matches previous run', 'ok'],
    ['cache hit 94%, build 1m 52s', 'ok'],
    ['macos-x86_64 notarization queued', 'warn'],
  ],
  test: [
    ['tests/test_voice.py 41 passed', 'ok'],
    ['suite 752 passed, 0 failed in 41.8 s', 'ok'],
    ['e2e/hyprland 12 passed', 'ok'],
    ['test_overlay_focus passed on retry 2', 'warn'],
    ['tests/test_web_agent.py 88 passed', 'ok'],
    ['bench: first word 0.87 s (p50), 1.21 s (p95)', 'ok'],
  ],
  ipc: [
    ['socket2 connected', 'info'],
    ['workspace>>3', 'info'],
    ['activewindow>>kitty,dev', 'info'],
    ['openwindow>>5a1c,3,code,nexus', 'info'],
    ['monitoradded>>DP-2', 'info'],
    ['mpris spotify PlaybackStatus=Playing', 'info'],
    ['focuswindow 4 ms', 'ok'],
  ],
};

export const READINESS = {
  target: 'v1.0.0 Gold',
  percent: 78,
  closed: 142,
  total: 182,
  milestones: [
    { label: 'Full-duplex voice', state: 'done' },
    { label: 'Vision loop with verification', state: 'done' },
    { label: 'Hyprland and X11 IPC', state: 'done' },
    { label: 'Plugin API freeze', state: 'active', progress: 60 },
    { label: 'Signed builds on every platform', state: 'todo' },
    { label: 'External security audit', state: 'todo' },
  ],
};
