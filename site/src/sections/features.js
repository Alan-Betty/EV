// The duplex lanes are drawn from a seeded generator so they look like speech
// but render identically on every load (no layout or snapshot churn).
function seeded(seed) {
  let s = seed;
  return () => ((s = (s * 16807) % 2147483647) / 2147483647);
}

export function initFeatures() {
  const BARS = 72;
  const CUT = 0.62; // where the user starts talking, as a fraction of the lane

  document.querySelectorAll('.duplex-bars').forEach((lane) => {
    const you = lane.dataset.lane === 'you';
    const rand = seeded(you ? 7 : 3);
    let html = '';
    for (let i = 0; i < BARS; i++) {
      const x = i / BARS;
      // E.V. speaks until the cut, then yields within a few bars. The user starts just before it.
      const active = you ? x > CUT - 0.03 : x < CUT + 0.02;
      const word = 0.35 + 0.65 * Math.abs(Math.sin(i * 0.55 + (you ? 1.3 : 0)));
      const h = active ? Math.max(0.12, word * (0.4 + rand() * 0.6)) : 0.06;
      html += `<span style="--h:${h.toFixed(2)};--i:${i}"${active ? '' : ' class="is-off"'}></span>`;
    }
    lane.innerHTML = html;
  });
}
