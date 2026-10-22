// Page motion: one entrance sequence for the hero, and a single reveal as each
// section first scrolls into view. Everything here is CSS-driven; this module
// only flips classes. With reduced motion the CSS collapses it all to instant.

const escape = (s) => s.replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' })[c]);

// Wrap each word so the headline can rise in word by word. The element keeps
// its full text as an aria-label; the word spans are hidden from screen readers.
export function splitWords(el) {
  const text = el.textContent.trim().replace(/\s+/g, ' ');
  el.setAttribute('aria-label', text);
  el.classList.add('is-split');
  el.innerHTML = text
    .split(' ')
    .map((w, i) => `<span class="word" aria-hidden="true" style="--w:${i}"><span>${escape(w)}</span></span>`)
    .join(' ');
}

export function initMotion() {
  document.querySelectorAll('[data-split]').forEach(splitWords);

  // Two frames so the hidden starting styles are painted before they change.
  requestAnimationFrame(() => requestAnimationFrame(() => document.documentElement.classList.add('is-loaded')));

  const io = new IntersectionObserver(
    (entries) => {
      for (const e of entries) {
        if (!e.isIntersecting) continue;
        e.target.classList.add('is-in');
        io.unobserve(e.target);
        // Groups whose children get re-rendered (the download cards) should not
        // replay the stagger every time; mark them done once it has played.
        if (e.target.hasAttribute('data-reveal-group')) setTimeout(() => e.target.classList.add('is-done'), 1400);
      }
    },
    { rootMargin: '0px 0px -10% 0px', threshold: 0.06 },
  );
  document.querySelectorAll('[data-reveal], [data-reveal-group]').forEach((el) => io.observe(el));
}
