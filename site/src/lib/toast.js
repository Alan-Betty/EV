let host;
let timer = 0;

export function toast(message, tone = 'ok') {
  if (!host) {
    host = document.createElement('div');
    host.className = 'toast';
    host.setAttribute('role', 'status');
    host.setAttribute('aria-live', 'polite');
    document.body.append(host);
  }
  host.dataset.tone = tone;
  host.innerHTML = `<span class="toast-dot"></span><span></span>`;
  host.lastElementChild.textContent = message;
  host.dataset.show = 'true';
  clearTimeout(timer);
  timer = setTimeout(() => (host.dataset.show = 'false'), 2200);
}
