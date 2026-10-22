import { CHANNELS, PLATFORMS } from '../data/release.js';
import { platformIcon } from '../lib/icons.js';
import { store } from '../lib/store.js';

export function initHeader() {
  const header = document.getElementById('site-header');
  if (!header) return;

  // Glass only once content scrolls beneath it; transparent over the hero.
  const onScroll = () => (header.dataset.scrolled = String(window.scrollY > 8));
  onScroll();
  window.addEventListener('scroll', onScroll, { passive: true });

  // Platform badge
  const badge = document.getElementById('os-badge');
  const render = () => {
    const platform = store.get('platform');
    const mobile = store.get('mobile');
    badge.querySelector('[data-os-icon]').innerHTML = mobile ? '' : platformIcon(platform, 'size-4');
    badge.querySelector('[data-os-label]').textContent = mobile ? 'Get the desktop app' : `Download for ${PLATFORMS[platform].label}`;
    badge.querySelector('[data-os-version]').textContent = CHANNELS[store.get('channel')].tag;
  };
  ['platform', 'mobile', 'channel'].forEach((k) => store.on(k, render));

  // Mobile disclosure
  const toggle = document.getElementById('menu-toggle');
  const panel = document.getElementById('mobile-nav');
  const setOpen = (open) => {
    toggle.setAttribute('aria-expanded', String(open));
    panel.hidden = !open;
    header.dataset.menu = String(open);
  };
  toggle.addEventListener('click', () => setOpen(panel.hidden));
  panel.addEventListener('click', (e) => e.target.closest('a') && setOpen(false));
  window.addEventListener('keydown', (e) => e.key === 'Escape' && !panel.hidden && setOpen(false));
  matchMedia('(min-width: 768px)').addEventListener('change', (e) => e.matches && setOpen(false));

  // Current-section highlight: the section crossing the band under the header wins.
  const links = [...document.querySelectorAll('[data-nav]')];
  const targets = links.map((a) => document.querySelector(a.getAttribute('href'))).filter(Boolean);
  if (!targets.length) return;
  const io = new IntersectionObserver(
    (entries) => {
      for (const entry of entries) {
        if (!entry.isIntersecting) continue;
        const id = `#${entry.target.id}`;
        links.forEach((a) => a.toggleAttribute('aria-current', a.getAttribute('href') === id));
      }
    },
    { rootMargin: '-45% 0px -50% 0px' },
  );
  targets.forEach((t) => io.observe(t));
}
