// Everything the page says that is not read from git lives in content/site.json,
// edited by hand or through the site manager (npm run admin). This fills every
// [data-content="path.to.text"] slot from it, and swaps edits in live while the
// dev server runs, so the manager's preview updates without a reload.
import CONTENT from '../../content/site.json';
import REPO from '../data/repo.json';
import { splitWords } from './motion.js';
import { store } from './store.js';

// Placeholders that fill themselves from the repo: "{iteration} iterations".
const TOKENS = { iteration: REPO.iteration.number, tests: REPO.tests, commits: REPO.commitCount };
export const fill = (text) => String(text ?? '').replace(/\{(\w+)\}/g, (m, k) => (k in TOKENS ? TOKENS[k] : m));

const at = (obj, path) => path.split('.').reduce((o, k) => o?.[k], obj);

function apply(content) {
  document.querySelectorAll('[data-content]').forEach((el) => {
    const value = at(content, el.dataset.content);
    // An empty field keeps the text already in the HTML rather than blanking it.
    if (typeof value !== 'string' || !value.trim()) return;
    const text = fill(value);
    if ((el.getAttribute('aria-label') ?? el.textContent.trim()) === text) return;
    el.textContent = text;
    if (el.classList.contains('is-split')) splitWords(el);
  });
  const tag = document.getElementById('hero-tag');
  if (tag && content.hero?.statusLink) tag.href = content.hero.statusLink;
}

export function initContent() {
  store.set('content', CONTENT);
  apply(CONTENT);
  if (import.meta.hot) {
    import.meta.hot.accept('../../content/site.json', (mod) => {
      if (!mod) return;
      store.set('content', mod.default);
      apply(mod.default);
    });
  }
}

export { CONTENT };
