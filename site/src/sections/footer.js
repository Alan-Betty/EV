import { PRODUCT, REPO } from '../data/release.js';

const compact = (n) => (n >= 1000 ? `${(n / 1000).toFixed(1)}k` : String(n));

export function initFooter() {
  document.querySelectorAll('[data-repo]').forEach((a) => (a.href = PRODUCT.repo));
  const links = { docs: PRODUCT.docs, quickstart: PRODUCT.quickstart, devlog: PRODUCT.devlog, issues: `${PRODUCT.repo}/issues` };
  document.querySelectorAll('[data-link]').forEach((a) => (a.href = links[a.dataset.link]));
  document.querySelector('[data-year]').textContent = new Date().getFullYear();
  document.querySelectorAll('[data-tests]').forEach((el) => (el.textContent = REPO.tests));

  // Live star count. A private repo, rate limit or offline visitor leaves the badge as "Star".
  const stars = document.querySelector('[data-stars]');
  if (!stars) return;
  stars.hidden = true;
  const api = PRODUCT.repo.replace('https://github.com/', 'https://api.github.com/repos/');
  fetch(api, { headers: { Accept: 'application/vnd.github+json' } })
    .then((r) => (r.ok ? r.json() : Promise.reject(r.status)))
    .then((repo) => {
      stars.textContent = compact(repo.stargazers_count);
      stars.hidden = false;
    })
    .catch(() => {});
}
