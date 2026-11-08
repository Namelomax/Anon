// Runs in Chrome's isolated scripting world; keep this function self-contained.
export function pageInspector(action, payload = {}) {
  const key = '__anonInspectorState';
  const state = globalThis[key] || (globalThis[key] = { generation: 0 });
  const makeHost = id => {
    document.getElementById(id)?.remove();
    const host = document.createElement('div');
    host.id = id;
    host.style.cssText = 'all:initial;position:fixed;z-index:2147483647;';
    document.documentElement.appendChild(host);
    return [host, host.attachShadow({ mode: 'closed' })];
  };
  const hideTooltip = () => {
    state.generation++;
    clearTimeout(state.timer);
    state.token = null;
    document.getElementById('__anon_extension_tooltip')?.remove();
  };
  const tokenAt = (x, y) => {
    let node, offset;
    if (document.caretPositionFromPoint) {
      const position = document.caretPositionFromPoint(x, y);
      node = position?.offsetNode; offset = position?.offset;
    } else {
      const range = document.caretRangeFromPoint?.(x, y);
      node = range?.startContainer; offset = range?.startOffset;
    }
    if (!node || node.nodeType !== Node.TEXT_NODE || !node.parentElement || (node.parentElement.isContentEditable || node.parentElement.closest('input,textarea,select,button,script,style,noscript,#__anon_extension_inspector,#__anon_extension_tooltip'))) return null;
    let block = node.parentElement;
    while (block.parentElement && block !== document.body && getComputedStyle(block).display.startsWith('inline')) block = block.parentElement;
    const walker = document.createTreeWalker(block, NodeFilter.SHOW_TEXT, { acceptNode(candidate) {
      return (candidate.parentElement?.isContentEditable || candidate.parentElement?.closest('input,textarea,select,button,script,style,noscript,#__anon_extension_inspector,#__anon_extension_tooltip')) ? NodeFilter.FILTER_REJECT : NodeFilter.FILTER_ACCEPT;
    } });
    let text = '', caret = -1, current, count = 0;
    const nodes = [];
    while ((current = walker.nextNode()) && count++ < 400) {
      if (current === node) caret = text.length + offset;
      nodes.push({ node: current, start: text.length, end: text.length + current.length });
      text += current.data;
      if (text.length > 2000) return null;
    }
    if (caret < 0) return null;
    const re = /\[[A-Z_]+_\d+\]/g;
    for (const match of text.matchAll(re)) {
      if (caret >= match.index && caret <= match.index + match[0].length) {
        const start = nodes.find(part => part.start <= match.index && part.end > match.index);
        const endOffset = match.index + match[0].length;
        const end = nodes.find(part => part.start < endOffset && part.end >= endOffset);
        if (!start || !end) return null;
        const range = document.createRange();
        range.setStart(start.node, match.index - start.start);
        range.setEnd(end.node, endOffset - end.start);
        if ([...range.getClientRects()].some(rect => x >= rect.left && x <= rect.right && y >= rect.top && y <= rect.bottom)) return match[0];
      }
    }
    return null;
  };
  const install = () => {
    if (state.installed) return;
    state.installed = true;
    document.addEventListener('pointermove', event => {
      if (state.closeModal) { hideTooltip(); return; }
      const token = tokenAt(event.clientX, event.clientY);
      if (!token) { hideTooltip(); return; }
      if (state.token === token) return;
      hideTooltip();
      state.token = token;
      const generation = state.generation, x = event.clientX, y = event.clientY;
      state.timer = setTimeout(async () => {
        let result;
        try { result = await chrome.runtime.sendMessage({ type: 'LOOKUP_PLACEHOLDER', token }); } catch { result = null; }
        if (generation !== state.generation || state.token !== token) return;
        const entry = result?.ok && Array.isArray(result.entries) ? result.entries.find(item => item.token === token) : null;
        const [host, root] = makeHost('__anon_extension_tooltip');
        host.style.left = `${Math.max(8, Math.min(x + 12, innerWidth - 280))}px`;
        host.style.top = `${Math.max(8, Math.min(y + 16, innerHeight - 100))}px`;
        host.style.pointerEvents = 'none';
        const box = document.createElement('div');
        box.style.cssText = 'font:14px system-ui;max-width:260px;padding:10px 12px;background:#1d2433;color:white;border-radius:8px;box-shadow:0 4px 18px #0005;white-space:pre-wrap;overflow-wrap:anywhere';
        box.textContent = !result?.ok || !Array.isArray(result.entries) ? `${token}\nОткройте Anon на этой странице ещё раз` : entry?.conflict ? `${token}\nНайдено несколько значений` : entry?.original != null ? `${token}\n${entry.original}` : `${token}\nИсходное значение не найдено`;
        root.appendChild(box);
      }, 300);
    }, true);
    document.addEventListener('scroll', hideTooltip, true);
    document.addEventListener('pointerleave', hideTooltip);
    document.addEventListener('keydown', event => { if (event.key === 'Escape') hideTooltip(); }, true);
    window.addEventListener('blur', hideTooltip);
  };
  if (action === 'install') { install(); return { ok: true }; }
  if (action !== 'show') return { ok: false, error: 'Unknown action' };
  install(); hideTooltip();
  state.closeModal?.();
  const previous = document.activeElement;
  const [host, root] = makeHost('__anon_extension_inspector');
  host.style.inset = '0';
  const backdrop = document.createElement('div');
  backdrop.style.cssText = 'position:fixed;inset:0;background:#0008;display:flex;align-items:center;justify-content:center;padding:20px;font:14px system-ui;box-sizing:border-box';
  const dialog = document.createElement('div');
  dialog.setAttribute('role', 'dialog'); dialog.setAttribute('aria-modal', 'true'); dialog.setAttribute('aria-label', 'Исходные значения');
  dialog.style.cssText = 'background:white;color:#18212f;border-radius:12px;padding:20px;max-width:600px;width:100%;max-height:80vh;overflow:auto;box-shadow:0 8px 35px #0005';
  const header = document.createElement('div'); header.style.cssText = 'display:flex;align-items:center;justify-content:space-between;gap:20px';
  const title = document.createElement('h2'); title.textContent = 'Исходные значения'; title.style.cssText = 'font-size:18px;margin:0';
  const close = document.createElement('button'); close.type = 'button'; close.textContent = 'Закрыть'; close.style.cssText = 'font:inherit;cursor:pointer;padding:6px 10px';
  const dismiss = () => { host.remove(); document.removeEventListener('keydown', onKey, true); if (state.closeModal === dismiss) state.closeModal = null; previous?.focus?.(); };
  const onKey = event => { if (event.key === 'Escape') { event.preventDefault(); event.stopPropagation(); dismiss(); } else if (event.key === 'Tab') { event.preventDefault(); event.stopPropagation(); close.focus(); } };
  close.addEventListener('click', dismiss); document.addEventListener('keydown', onKey, true); state.closeModal = dismiss;
  header.append(title, close); dialog.appendChild(header);
  for (const entry of Array.isArray(payload.entries) ? payload.entries : []) {
    const row = document.createElement('div'); row.style.cssText = 'border-top:1px solid #ddd;padding:12px 0;white-space:pre-wrap;overflow-wrap:anywhere';
    const token = document.createElement('strong'); token.textContent = String(entry.token ?? '');
    const value = document.createElement('div'); value.textContent = entry.conflict ? 'Найдено несколько значений' : entry.original == null ? 'Исходное значение не найдено' : String(entry.original);
    row.append(token, value); dialog.appendChild(row);
  }
  backdrop.appendChild(dialog); root.appendChild(backdrop); close.focus();
  return { ok: true };
}
