// Serialized by chrome.scripting.executeScript: keep this function self-contained.
export function pageSelection(action, payload = {}) {
  const snapshots = globalThis.__anonSelectionSnapshots ||= new Map();
  const now = Date.now();
  for (const [id, item] of snapshots) {
    if (now - item.created > 15 * 60 * 1000) snapshots.delete(id);
  }
  const fail = message => ({ error: message });
  const selectionOf = root => root && typeof root.getSelection === 'function'
    ? root.getSelection() : window.getSelection();
  const isControl = node => node.nodeType === Node.ELEMENT_NODE && (
    node.matches('input, textarea, select, button, [role="toolbar"], [role="button"], [role="menu"], [role="menuitem"], [role="checkbox"], [role="radio"], [role="switch"], [role="slider"], [role="spinbutton"]') ||
    (node.matches('[role="combobox"], [role="textbox"]') && !node.isContentEditable)
  );
  function captureDom(range, owner) {
    let text = '';
    const segments = [];
    const blocks = new Set(['P', 'DIV', 'H1', 'H2', 'H3', 'H4', 'H5', 'H6', 'LI', 'UL', 'OL', 'TR', 'TD', 'TH', 'BLOCKQUOTE', 'PRE', 'SECTION', 'ARTICLE']);
    function newline(force = false) {
      if (!text || (!force && text.endsWith('\n'))) return;
      segments.push({ virtual: true, start: text.length, end: text.length + 1 });
      text += '\n';
    }
    function walk(node) {
      if (!range.intersectsNode(node)) return;
      if (node.nodeType === Node.TEXT_NODE) {
        const from = node === range.startContainer ? range.startOffset : 0;
        const to = node === range.endContainer ? range.endOffset : node.data.length;
        if (to > from) {
          const value = node.data.slice(from, to);
          segments.push({ node, from, to, start: text.length, end: text.length + value.length, expected: node.data });
          text += value;
        }
        return;
      }
      if (node.nodeType !== Node.ELEMENT_NODE || ['SCRIPT', 'STYLE', 'NOSCRIPT'].includes(node.tagName)) return;
      // Controls are not document text. Keep a virtual boundary so a single
      // replacement cannot join text on opposite sides of a skipped control.
      if (isControl(node)) {
        newline(true);
        return;
      }
      if (node.tagName === 'BR') { newline(true); return; }
      const block = blocks.has(node.tagName);
      if (block) newline();
      const before = text.length;
      for (const child of node.childNodes) walk(child);
      if (block) newline(text.length === before);
    }
    walk(owner);
    // A closing block boundary is layout, not an extra trailing text character.
    if (segments.at(-1)?.virtual) { text = text.slice(0, -1); segments.pop(); }
    return { text, segments };
  }
  try {
    if (action === 'release') {
      snapshots.delete(payload.id);
      return { ok: true };
    }
    if (action === 'notify') {
      const old = globalThis.__anonStatus;
      if (old) old.remove();
      const phases = {
        sending: 'Отправка', processing: 'Обработка', restoring: 'Восстановление',
        retrying: 'Ожидание сервера', success: 'Готово', warning: 'Внимание', error: 'Ошибка'
      };
      const phase = Object.hasOwn(phases, payload.phase) ? payload.phase : payload.error ? 'error' : 'success';
      const busy = ['sending', 'processing', 'restoring', 'retrying'].includes(phase);
      const host = document.createElement('div');
      host.id = '__anon_extension_status';
      host.dataset.anonPhase = phase;
      globalThis.__anonStatus = host;
      const shadow = host.attachShadow({ mode: 'closed' });
      const style = document.createElement('style');
      style.textContent = `
        :host{all:initial;position:fixed;z-index:2147483647;right:16px;bottom:16px;pointer-events:none}
        .box{box-sizing:border-box;display:flex;align-items:center;gap:10px;max-width:340px;padding:12px 14px;border-radius:14px;background:#263238;color:#fff;box-shadow:0 4px 18px #0004;font:14px/1.35 sans-serif;animation:appear .2s ease-out}
        .box.dismiss{opacity:0;transition:opacity .2s ease}.box.success{background:#17633f}.box.warning{background:#805600}.box.error{background:#982e2e}
        .icon{display:grid;place-items:center;flex:none;width:20px;height:20px;font-size:17px;font-weight:bold}
        .spinner{box-sizing:border-box;border:2px solid #ffffff66;border-top-color:#fff;border-radius:50%;animation:spin .8s linear infinite}
        .content{min-width:0;overflow-wrap:anywhere}.title{font-weight:bold}.message{margin-top:2px}
        button{all:unset;box-sizing:border-box;align-self:flex-start;cursor:pointer;pointer-events:auto;padding:0 4px;font-size:20px;line-height:1}button:focus-visible{outline:2px solid white;outline-offset:2px}
        @keyframes spin{to{transform:rotate(360deg)}}@keyframes appear{from{opacity:0;transform:translateY(6px)}to{opacity:1;transform:translateY(0)}}
        @media(prefers-reduced-motion:reduce){.box,.spinner{animation:none;transition:none}}
      `;
      const box = document.createElement('div');
      box.className = `box ${phase}`;
      box.setAttribute('role', phase === 'error' ? 'alert' : 'status');
      box.setAttribute('aria-live', phase === 'error' ? 'assertive' : 'polite');
      const icon = document.createElement('span');
      icon.className = busy ? 'icon spinner' : 'icon';
      icon.setAttribute('aria-hidden', 'true');
      icon.textContent = busy ? '' : phase === 'success' ? '✓' : '!';
      const content = document.createElement('div');
      content.className = 'content';
      const title = document.createElement('div');
      title.className = 'title';
      title.textContent = `Anon · ${phases[phase]}`;
      const message = document.createElement('div');
      message.className = 'message';
      message.textContent = typeof payload.message === 'string' ? payload.message.slice(0, 300) : '';
      content.append(title, message);
      const close = document.createElement('button');
      close.type = 'button';
      close.setAttribute('aria-label', 'Скрыть уведомление');
      close.title = busy ? 'Скрыть уведомление. Обработка продолжится.' : 'Скрыть уведомление';
      close.textContent = '×';
      close.addEventListener('click', () => {
        host.remove();
        if (globalThis.__anonStatus === host) delete globalThis.__anonStatus;
      });
      box.append(icon, content, close);
      shadow.append(style, box);
      // Outside body: a status toast must not invalidate a saved body selection.
      document.documentElement.append(host);
      if (phase === 'success' || phase === 'warning') {
        const duration = phase === 'success' ? 6000 : 10000;
        setTimeout(() => box.classList.add('dismiss'), duration - 200);
        setTimeout(() => {
          host.remove();
          if (globalThis.__anonStatus === host) delete globalThis.__anonStatus;
        }, duration);
      }
      return { ok: true };
    }
    if (action === 'install') {
      if (!globalThis.__anonSelectionInstalled) {
        globalThis.__anonSelectionInstalled = true;
        const remember = () => pageSelection('observe');
        for (const event of ['selectionchange', 'select', 'mouseup', 'keyup']) document.addEventListener(event, remember, true);
      }
      return { ok: true };
    }
    if (action === 'capture' || action === 'observe') {
      if (!document.hasFocus()) return fail('Окно страницы не активно.');
      let active = document.activeElement;
      while (active?.shadowRoot?.activeElement) active = active.shadowRoot.activeElement;
      if (active?.closest?.('#__anon_extension_status, #__anon_extension_inspector, #__anon_extension_tooltip')) return fail('Это выделение не поддерживается.');
      if (active instanceof HTMLIFrameElement) return fail('Выделение находится во вложенном окне.');
      let item;
      let empty = false;
      if (active instanceof HTMLInputElement || active instanceof HTMLTextAreaElement) {
        if (active.disabled || active.readOnly || (active instanceof HTMLInputElement && active.type.toLowerCase() === 'password')) {
          return fail('Это поле недоступно для замены.');
        }
        const start = active.selectionStart, end = active.selectionEnd;
        if (start == null || end == null) return fail('Выделение в этом поле не поддерживается.');
        empty = start === end;
        item = { kind: 'input', node: active, start, end, expected: active.value, source: active.value.slice(start, end) };
      } else {
        const root = active?.getRootNode() || document;
        const selection = selectionOf(root);
        empty = !selection || selection.rangeCount === 0 || selection.isCollapsed;
        if (!empty && selection.rangeCount !== 1) return fail('Это выделение не поддерживается.');
        if (empty) {
          item = null;
        } else {
        const range = selection.getRangeAt(0).cloneRange();
        if (range.startContainer.getRootNode() !== range.endContainer.getRootNode()) return fail('Это выделение не поддерживается.');
        let element = range.commonAncestorContainer;
        if (element.nodeType !== Node.ELEMENT_NODE) element = element.parentElement;
        if (!element) return fail('Это выделение не поддерживается.');
        for (let ancestor = element; ancestor; ancestor = ancestor.parentElement) {
          if (isControl(ancestor)) return fail('Выделение содержит элементы управления.');
        }
        if (element.closest('#__anon_extension_status, #__anon_extension_inspector, #__anon_extension_tooltip, [data-anon-extension]')) return fail('Это выделение не поддерживается.');
        const editable = element.closest('[contenteditable]');
        const editableRoot = editable?.isContentEditable ? editable.closest('[contenteditable="true"], [contenteditable=""]') || editable : null;
        const owner = editableRoot || element;
        const captured = captureDom(range, owner);
        item = { kind: editableRoot ? 'editable' : 'range', node: owner, range, expected: owner.innerHTML, source: captured.text, segments: captured.segments };
        }
      }
      if (empty && action === 'capture' && payload.usePrevious) {
        item = globalThis.__anonPreviousSelection;
        if (!item || now - item.created > 15 * 60 * 1000) { delete globalThis.__anonPreviousSelection; return fail('Предыдущее выделение устарело. Выделите текст заново.'); }
        const node = item.node;
        if (!node.isConnected || node.ownerDocument !== document ||
            (item.kind === 'input' ? node.disabled || node.readOnly || (node instanceof HTMLInputElement && node.type.toLowerCase() === 'password') || node.value !== item.expected || node.value.slice(item.start, item.end) !== item.source :
              node.innerHTML !== item.expected || !item.range.startContainer.isConnected || !item.range.endContainer.isConnected || (item.kind === 'editable' && !node.isContentEditable) || item.segments.some(segment => !segment.virtual && (!segment.node.isConnected || segment.node.data !== segment.expected)))) {
          delete globalThis.__anonPreviousSelection;
          return fail('Текст изменился. Выделите его заново.');
        }
      }
      if (!item || !item.source.trim()) return fail('Выделите текст на странице.');
      if (item.source.length > 100000) return fail('Выберите фрагмент до 100 000 символов.');
      item = { ...item, created: now };
      globalThis.__anonPreviousSelection = item;
      if (action === 'observe') return { ok: true };
      // randomUUID is unavailable on ordinary HTTP pages; getRandomValues is
      // also available in non-secure contexts and keeps snapshot IDs unguessable.
      const id = Array.from(crypto.getRandomValues(new Uint32Array(4)), value => value.toString(16).padStart(8, '0')).join('');
      while (snapshots.size >= 10) snapshots.delete(snapshots.keys().next().value);
      snapshots.set(id, item);
      return { id, text: item.source };
    }
    if (action === 'apply' || action === 'restore') {
      const item = snapshots.get(payload.id);
      if (!item) return fail('Выделение устарело. Выделите текст заново.');
      if (typeof payload.text !== 'string') return fail('Не удалось получить текст для замены.');
      const node = item.node;
      if (!node.isConnected || node.ownerDocument !== document) return fail('Страница изменилась. Выделите текст заново.');
      if (item.kind === 'input') {
        if (node.disabled || node.readOnly || (node instanceof HTMLInputElement && node.type.toLowerCase() === 'password') || node.value !== item.expected || node.value.slice(item.start, item.end) !== item.source) return fail('Текст изменился. Выделите его заново.');
        const prototype = node instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
        Object.getOwnPropertyDescriptor(prototype, 'value').set.call(node, node.value.slice(0, item.start) + payload.text + node.value.slice(item.end));
        node.focus();
        node.setSelectionRange(item.start, item.start + payload.text.length);
        node.dispatchEvent(new InputEvent('input', { bubbles: true, inputType: 'insertReplacementText', data: payload.text }));
      } else {
        const range = item.range;
        if (node.innerHTML !== item.expected || !range.startContainer.isConnected || !range.endContainer.isConnected || range.startContainer.ownerDocument !== document || range.endContainer.ownerDocument !== document) return fail('Текст изменился. Выделите его заново.');
        const selection = selectionOf(node.getRootNode());
        if (!selection) return fail('Не удалось восстановить выделение.');
        if (item.kind === 'editable' && !node.isContentEditable) return fail('Поле больше недоступно для редактирования.');
        const segments = item.segments;
        if (segments.some(segment => !segment.virtual && (!segment.node.isConnected || segment.node.data !== segment.expected))) return fail('Текст изменился. Выделите его заново.');
        let edits = payload.edits;
        if (!Array.isArray(edits)) {
          // Legacy/simple callers: a single changed run is safe only when it
          // does not span paragraph boundaries. Never flatten the whole range.
          let start = 0, end = item.source.length, replacementEnd = payload.text.length;
          while (start < end && start < replacementEnd && item.source[start] === payload.text[start]) start++;
          while (end > start && replacementEnd > start && item.source[end - 1] === payload.text[replacementEnd - 1]) { end--; replacementEnd--; }
          edits = start === end && start === replacementEnd ? [] : [{ start, end, text: payload.text.slice(start, replacementEnd) }];
        }
        let previous = 0, expected = '';
        const plans = [];
        for (const edit of edits) {
          if (!Number.isInteger(edit.start) || !Number.isInteger(edit.end) || edit.start < previous || edit.end < edit.start || edit.end > item.source.length || typeof edit.text !== 'string') return fail('Некорректные границы замены.');
          expected += item.source.slice(previous, edit.start) + edit.text;
          previous = edit.end;
          if (segments.some(segment => segment.virtual && edit.start < segment.end && edit.end > segment.start)) return fail('Не удалось сохранить разметку этой замены. Результат доступен в панели Anon.');
          const affected = segments.filter(segment => !segment.virtual && edit.start < segment.end && edit.end > segment.start);
          if (!affected.length) return fail('Не удалось определить текст для замены.');
          plans.push({ edit, affected });
        }
        if (expected + item.source.slice(previous) !== payload.text) return fail('Замены не соответствуют выделенному тексту.');
        const selectedText = segments.filter(segment => !segment.virtual);
        if (!selectedText.length) return fail('Выделите текст на странице.');
        const first = selectedText[0], last = selectedText.at(-1);
        let endOffset = last.to;
        // Text nodes alone are changed, right-to-left. Paragraphs, font/style
        // elements, lists, tables and surrounding non-selected DOM stay intact.
        for (const { edit, affected } of plans.reverse()) {
          for (let index = 0; index < affected.length; index++) {
            const segment = affected[index];
            const from = segment.from + Math.max(0, edit.start - segment.start);
            const length = Math.min(edit.end, segment.end) - Math.max(edit.start, segment.start);
            const inserted = index === 0 ? edit.text : '';
            segment.node.replaceData(from, length, inserted);
            if (segment.node === last.node) endOffset += inserted.length - length;
          }
        }
        if (item.kind === 'editable') node.focus();
        const selected = document.createRange();
        selected.setStart(first.node, first.from);
        selected.setEnd(last.node, endOffset);
        selection.removeAllRanges(); selection.addRange(selected);
        if (item.kind === 'editable' && plans.length) node.dispatchEvent(new InputEvent('input', { bubbles: true, composed: true, inputType: 'insertReplacementText', data: null }));
      }
      // Capture the resulting range immediately; queued selection events may run later.
      if (item.kind === 'input') {
        globalThis.__anonPreviousSelection = { ...item, end: item.start + payload.text.length, expected: node.value, source: payload.text, created: Date.now() };
      } else {
        const selected = selectionOf(node.getRootNode());
        if (selected?.rangeCount === 1) {
          const range = selected.getRangeAt(0).cloneRange();
          const captured = captureDom(range, node);
          globalThis.__anonPreviousSelection = { ...item, range, expected: node.innerHTML, source: captured.text, segments: captured.segments, created: Date.now() };
        }
      }
      snapshots.delete(payload.id);
      return { ok: true };
    }
    return fail('Неизвестное действие расширения.');
  } catch {
    return fail('Не удалось обработать выделение на странице.');
  }
}
