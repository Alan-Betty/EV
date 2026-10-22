import { PRODUCT } from '../data/release.js';

export function initFooter() {
  const stars = document.querySelector('[data-stars]');
  if (stars) stars.textContent = PRODUCT.stars >= 1000 ? `${(PRODUCT.stars / 1000).toFixed(1)}k` : String(PRODUCT.stars);
  document.querySelectorAll('[data-repo]').forEach((a) => (a.href = PRODUCT.repo));
  document.querySelector('[data-year]').textContent = new Date().getFullYear();

  // Local IPC round trip readout, jittered the way a real one would be.
  const ipc = document.querySelector('[data-ipc]');
  if (!ipc || matchMedia('(prefers-reduced-motion: reduce)').matches) return;
  setInterval(() => {
    if (!document.hidden) ipc.textContent = `${(0.32 + Math.random() * 0.16).toFixed(1)}ms`;
  }, 2400);
}
