import './styles.css';
import { detectPlatform } from './lib/os.js';
import { initHeader } from './sections/header.js';

detectPlatform();

// Each section owns its DOM and exports one init(). Order matters only where a
// section reads state another publishes (OS detection feeds header, hero, downloads).
const sections = [initHeader];

for (const init of sections) {
  try {
    init();
  } catch (err) {
    // One broken widget must never take the page down with it.
    console.error('[nexus] section failed to init', err);
  }
}
