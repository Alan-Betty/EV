import './styles.css';
import { initContent } from './lib/content.js';
import { initMotion } from './lib/motion.js';
import { initSheen } from './lib/sheen.js';
import { detectPlatform } from './lib/os.js';
import { initActivity } from './sections/activity.js';
import { initDownloads } from './sections/downloads.js';
import { initFeatures } from './sections/features.js';
import { initFooter } from './sections/footer.js';
import { initHeader } from './sections/header.js';
import { initHero } from './sections/hero.js';
import { initPalette } from './sections/palette.js';
import { initSimulator } from './sections/simulator.js';

detectPlatform();

// Each section owns its DOM and exports one init(). Order matters only where a
// section reads state another publishes (OS detection feeds header, hero, downloads).
const sections = [initContent, initMotion, initHeader, initHero, initSimulator, initFeatures, initActivity, initDownloads, initFooter, initPalette, initSheen];

for (const init of sections) {
  try {
    init();
  } catch (err) {
    // One broken widget must never take the page down with it.
    console.error('[ev] section failed to init', err);
  }
}
