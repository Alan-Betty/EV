import { toast } from './toast.js';

export async function copy(text, label = 'Copied') {
  try {
    await navigator.clipboard.writeText(text);
  } catch {
    // Insecure context or denied permission: the old selection trick still works.
    const ta = Object.assign(document.createElement('textarea'), { value: text });
    ta.setAttribute('readonly', '');
    ta.style.cssText = 'position:fixed;opacity:0;pointer-events:none';
    document.body.append(ta);
    ta.select();
    const ok = document.execCommand('copy');
    ta.remove();
    if (!ok) {
      toast('Copy failed. Select the text and copy it by hand.', 'error');
      return false;
    }
  }
  toast(label);
  return true;
}

// Swap a button's icon to a check for a moment so the click visibly landed.
export function flashCopied(button) {
  button.dataset.copied = 'true';
  clearTimeout(button._copiedTimer);
  button._copiedTimer = setTimeout(() => delete button.dataset.copied, 1400);
}
