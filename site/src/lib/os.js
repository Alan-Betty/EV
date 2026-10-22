import { store } from './store.js';

const KNOWN = new Set(['linux', 'macos', 'windows']);

function sniff() {
  const uaData = navigator.userAgentData;
  const ua = navigator.userAgent;
  const s = `${uaData?.platform ?? navigator.platform ?? ''} ${ua}`.toLowerCase();

  // iPadOS 13+ claims to be a Mac; only touch support gives it away.
  const ipad = /mac/.test(s) && navigator.maxTouchPoints > 1;
  const mobile = Boolean(uaData?.mobile) || ipad || /android|iphone|ipad|ipod|mobile/.test(s);

  let platform = 'linux';
  if (/win/.test(s)) platform = 'windows';
  else if (/mac|iphone|ipad|ipod/.test(s)) platform = 'macos';

  return { platform, mobile };
}

async function sniffArch() {
  // Chromium only. Safari and Firefox never say, so macOS defaults to Apple silicon.
  try {
    const v = await navigator.userAgentData?.getHighEntropyValues?.(['architecture']);
    if (v?.architecture === 'arm') return 'arm64';
    if (v?.architecture === 'x86') return 'x86_64';
  } catch {
    /* permission or API missing: fall through */
  }
  return null;
}

export function detectPlatform() {
  const { platform, mobile } = sniff();

  // ?os=windows lets QA and screenshots force a platform.
  const forced = new URLSearchParams(location.search).get('os');
  store.set('platform', KNOWN.has(forced) ? forced : platform);
  store.set('mobile', forced ? false : mobile);

  sniffArch().then((arch) => store.set('arch', arch));
}
