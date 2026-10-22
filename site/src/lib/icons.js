import { siApple, siArchlinux, siGithub, siLinux } from 'simple-icons';

const brand = (path) => (cls = '') =>
  `<svg viewBox="0 0 24 24" fill="currentColor" aria-hidden="true" class="${cls}"><path d="${path}"/></svg>`;

// 24px stroke glyphs, 1.75 weight to sit beside the 13-15px UI text.
const line = (d) => (cls = '') =>
  `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" class="${cls}">${d}</svg>`;

export const icon = {
  apple: brand(siApple.path),
  linux: brand(siLinux.path),
  arch: brand(siArchlinux.path),
  github: brand(siGithub.path),
  windows: brand('M3 3h8.4v8.4H3zm9.6 0H21v8.4h-8.4zM3 12.6h8.4V21H3zm9.6 0H21V21h-8.4z'),

  download: line('<path d="M12 4v11"/><path d="m7 10 5 5 5-5"/><path d="M5 20h14"/>'),
  copy: line('<rect x="8" y="8" width="12" height="12" rx="2.5"/><path d="M16 8V6.5A2.5 2.5 0 0 0 13.5 4h-7A2.5 2.5 0 0 0 4 6.5v7A2.5 2.5 0 0 0 6.5 16H8"/>'),
  check: line('<path d="m5 12.5 4.5 4.5L19 7.5"/>'),
  search: line('<circle cx="11" cy="11" r="6.5"/><path d="m20 20-4.2-4.2"/>'),
  terminal: line('<path d="m5 7 5 5-5 5"/><path d="M12.5 17H19"/>'),
  menu: line('<path d="M4 8h16"/><path d="M4 16h16"/>'),
  close: line('<path d="m6 6 12 12"/><path d="M18 6 6 18"/>'),
  shield: line('<path d="M12 3.5 5 6v5.5c0 4.3 3 7.8 7 9 4-1.2 7-4.7 7-9V6z"/><path d="m9 12 2.2 2.2L15.5 10"/>'),
  play: line('<path d="M8 5.5v13l10.5-6.5z"/>'),
  pause: line('<path d="M8.5 5v14"/><path d="M15.5 5v14"/>'),
  star: line('<path d="m12 4 2.47 5.01 5.53.8-4 3.9.94 5.5L12 16.6l-4.94 2.6.94-5.5-4-3.9 5.53-.8z"/>'),
};

export const platformIcon = (platform, cls) =>
  ({ linux: icon.linux, macos: icon.apple, windows: icon.windows })[platform]?.(cls) ?? '';
