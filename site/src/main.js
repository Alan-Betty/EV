import './styles.css';

// Each section owns its DOM and exports one init(). Order matters only where a
// section reads state another publishes (OS detection feeds header, hero, downloads).
const sections = [];

for (const init of sections) {
  try {
    init();
  } catch (err) {
    // One broken widget must never take the page down with it.
    console.error('[nexus] section failed to init', err);
  }
}
