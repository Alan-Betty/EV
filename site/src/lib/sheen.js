// Pointer-following highlight for glass surfaces. One delegated listener,
// rAF-throttled, writes two custom properties on the hovered card only.
export function initSheen() {
  if (!matchMedia('(hover: hover) and (pointer: fine)').matches) return;
  let pending = null;
  document.addEventListener(
    'pointermove',
    (e) => {
      const card = e.target.closest?.('.sheen');
      if (!card) return;
      if (!pending) requestAnimationFrame(flush);
      pending = { card, x: e.clientX, y: e.clientY };
    },
    { passive: true },
  );
  function flush() {
    const { card, x, y } = pending;
    pending = null;
    const r = card.getBoundingClientRect();
    card.style.setProperty('--mx', `${x - r.left}px`);
    card.style.setProperty('--my', `${y - r.top}px`);
  }
}
