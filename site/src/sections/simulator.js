import { SCRIPTS, freeform } from '../data/scripts.js';
import { createWave } from '../lib/wave.js';

const STATES = {
  idle: { pill: 'IDLE', title: 'Idle' },
  listening: { pill: 'LISTENING', title: 'Listening...' },
  thinking: { pill: 'THINKING', title: 'Thinking...' },
  executing: { pill: 'EXECUTING LOGIC', title: 'Executing...' },
  done: { pill: 'DONE', title: 'Done' },
};
const ORDER = Object.keys(STATES);
const ABORT = Symbol('abort');

const escape = (s) => s.replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' })[c]);
const fmtMs = (ms) => (ms >= 1000 ? `${(ms / 1000).toFixed(1)} s` : `${ms} ms`);

export function initSimulator() {
  const root = document.getElementById('demo');
  if (!root) return;

  const el = {
    title: root.querySelector('#sim-title'),
    pill: root.querySelector('#sim-state'),
    steps: [...root.querySelectorAll('#sim-steps [data-step]')],
    transcript: root.querySelector('#sim-transcript'),
    empty: root.querySelector('#sim-empty'),
    term: root.querySelector('#sim-term'),
    progress: root.querySelector('#sim-progress'),
    prompts: root.querySelector('#sim-prompts'),
    form: root.querySelector('#sim-form'),
    input: root.querySelector('#sim-input'),
    status: root.querySelector('#sim-status'),
  };
  const wave = createWave(root.querySelector('#sim-wave'));
  const reduceMotion = matchMedia('(prefers-reduced-motion: reduce)');

  el.prompts.innerHTML = SCRIPTS.map(
    (s) => `<button type="button" class="prompt-pill" data-script="${s.id}" aria-pressed="false">${escape(s.label)}</button>`,
  ).join('');

  let runId = 0;
  let idleTimer = 0;

  function setState(name) {
    const s = STATES[name];
    el.pill.dataset.state = name;
    el.pill.textContent = `[${s.pill}]`;
    el.title.textContent = `Nexus Assistant v0.9.4 - ${s.title}`;
    const at = ORDER.indexOf(name);
    el.steps.forEach((li, i) => {
      li.dataset.reached = String(i <= at);
      li.toggleAttribute('aria-current', i === at);
    });
    wave.setMode(name);
  }

  // Each run gets an id; anything awaiting from an older run throws ABORT on wake.
  function runner(id) {
    const alive = () => id === runId;
    const speed = () => (reduceMotion.matches ? 0.15 : 1);
    const wait = (ms) =>
      new Promise((r) => setTimeout(r, ms * speed())).then(() => {
        if (!alive()) throw ABORT;
      });
    async function type(node, text, perChar, onChar) {
      if (reduceMotion.matches) {
        node.textContent = text;
        return;
      }
      for (let i = 0; i < text.length; i++) {
        node.textContent += text[i];
        onChar?.(text[i]);
        await wait(perChar + (/[,.]/.test(text[i]) ? perChar * 4 : 0));
      }
    }
    return { wait, type };
  }

  function message(who, cls = '') {
    el.empty?.remove();
    const node = document.createElement('div');
    node.className = `sim-msg ${cls}`;
    node.dataset.who = who;
    node.innerHTML = `<span class="sim-who">${who}</span><p class="sim-text"></p>`;
    el.transcript.append(node);
    el.transcript.scrollTop = el.transcript.scrollHeight;
    return node.querySelector('.sim-text');
  }

  function termLine(cls, html) {
    const li = document.createElement('li');
    li.className = cls;
    li.innerHTML = html;
    el.term.append(li);
    el.term.scrollTop = el.term.scrollHeight;
    return li;
  }

  async function play(script) {
    const id = ++runId;
    const { wait, type } = runner(id);
    clearTimeout(idleTimer);
    el.prompts.querySelectorAll('[data-script]').forEach((b) => b.setAttribute('aria-pressed', String(b.dataset.script === script.id)));
    el.transcript.querySelectorAll('.sim-msg').forEach((n) => n.remove());
    el.term.replaceChildren();
    el.progress.textContent = `0/${script.steps.length}`;
    el.status.textContent = `Running: ${script.heard}`;

    try {
      setState('listening');
      await wait(260);
      const heard = message('you');
      heard.classList.add('is-typing');
      await type(heard, script.heard, 26, (c) => wave.kick(c));
      heard.classList.remove('is-typing');

      setState('thinking');
      const plan = message('plan', 'sim-plan');
      plan.innerHTML = `<span class="shimmer">${escape(script.plan)}</span>`;
      await wait(1100);
      plan.textContent = script.plan;

      if (script.confirm) {
        const ask = message('nexus');
        setState('executing');
        wave.setMode('speaking');
        await type(ask, script.confirm, 22, (c) => wave.kick(c));
        wave.setMode('listening');
        await wait(650);
        const yes = message('you');
        await type(yes, 'Yes.', 60, (c) => wave.kick(c));
        await wait(250);
      }

      setState('executing');
      for (const [i, step] of script.steps.entries()) {
        const line = termLine(
          'term-cmd',
          `<span class="text-electric" aria-hidden="true">❯</span><span class="term-text"></span><span class="term-meta is-running">running</span>`,
        );
        await type(line.querySelector('.term-text'), step.cmd, 9);
        await wait(Math.min(1000, Math.max(260, step.ms / 3)));
        for (const out of step.out) {
          termLine(`term-out${step.tone === 'ok' ? ' is-ok' : ''}`, escape(out));
          await wait(90);
        }
        const meta = line.querySelector('.term-meta');
        meta.classList.remove('is-running');
        meta.innerHTML = `<span class="text-pulse">✓</span> ${fmtMs(step.ms)}`;
        el.progress.textContent = `${i + 1}/${script.steps.length}`;
        await wait(140);
      }

      wave.setMode('speaking');
      el.title.textContent = 'Nexus Assistant v0.9.4 - Speaking...';
      const reply = message('nexus');
      reply.classList.add('is-typing');
      await type(reply, script.reply, 17, (c) => wave.kick(c));
      reply.classList.remove('is-typing');

      setState('done');
      el.status.textContent = `NEXUS: ${script.reply}`;
      idleTimer = setTimeout(() => id === runId && setState('idle'), 7000);
    } catch (err) {
      if (err !== ABORT) throw err;
    }
  }

  el.prompts.addEventListener('click', (e) => {
    const id = e.target.closest('[data-script]')?.dataset.script;
    const script = SCRIPTS.find((s) => s.id === id);
    if (script) play(script);
  });

  el.form.addEventListener('submit', (e) => {
    e.preventDefault();
    const text = el.input.value.trim();
    if (!text) return;
    el.input.value = '';
    const q = text.toLowerCase();
    const match = SCRIPTS.find((s) => s.label.toLowerCase() === q || s.heard.toLowerCase() === q);
    play(match ?? freeform(text));
  });

  setState('idle');

  // Run the first errand once, the first time the demo is properly on screen.
  const io = new IntersectionObserver(
    ([entry]) => {
      if (!entry.isIntersecting) return;
      io.disconnect();
      if (runId === 0) setTimeout(() => runId === 0 && play(SCRIPTS[0]), 500);
    },
    { threshold: 0.45 },
  );
  io.observe(root.querySelector('.sim-window'));

  return { play: (id) => play(SCRIPTS.find((s) => s.id === id) ?? SCRIPTS[0]) };
}
