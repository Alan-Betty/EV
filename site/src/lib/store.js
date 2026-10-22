// Tiny pub/sub for the few facts several sections share: detected platform,
// CPU arch, and the release channel picked in the download matrix.
const state = {
  platform: 'linux',
  mobile: false,
  arch: null,
  channel: 'stable',
  content: null,
};
const subscribers = new Map();

export const store = {
  get: (key) => state[key],

  set(key, value) {
    if (state[key] === value) return;
    state[key] = value;
    subscribers.get(key)?.forEach((fn) => fn(value));
  },

  // Calls back immediately with the current value, then on every change.
  on(key, fn) {
    if (!subscribers.has(key)) subscribers.set(key, new Set());
    subscribers.get(key).add(fn);
    fn(state[key]);
    return () => subscribers.get(key).delete(fn);
  },
};
