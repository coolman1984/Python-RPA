(() => {
  if (window.__smartopsInstalled) return;
  window.__smartopsInstalled = true;

  const clean = (value, limit = 240) => String(value || '').replace(/\s+/g, ' ').trim().slice(0, limit);
  const sensitive = el => {
    if (!el || el.nodeType !== 1) return false;
    const hay = [el.type, el.name, el.id, el.autocomplete, el.getAttribute('aria-label'), el.placeholder].join(' ');
    return /password|passwd|secret|token|otp|one.?time|credit.?card|cvv|cvc|login|sign.?in|username/i.test(hay);
  };
  const escAttr = value => String(value || '').replace(/\\/g, '\\\\').replace(/"/g, '\\"');
  const structuralSelector = el => {
    let parts = [];
    let current = el;
    while (current && current.nodeType === 1 && parts.length < 8) {
      let part = current.tagName.toLowerCase();
      if (current.id) {
        parts.unshift('#' + CSS.escape(current.id));
        break;
      }
      const siblings = current.parentElement ? [...current.parentElement.children].filter(x => x.tagName === current.tagName) : [];
      if (siblings.length > 1) part += ':nth-of-type(' + (siblings.indexOf(current) + 1) + ')';
      parts.unshift(part);
      current = current.parentElement;
    }
    return parts.join(' > ');
  };
  const selectorCandidates = el => {
    const values = [];
    const add = (kind, value, score) => { if (value && !values.some(x => x.value === value)) values.push({kind, value, score}); };
    if (el.id) add('id', '#' + CSS.escape(el.id), 100);
    const testid = el.getAttribute('data-testid') || el.getAttribute('data-test') || el.getAttribute('data-qa');
    if (testid) add('testid', '[data-testid="' + escAttr(testid) + '"]', 95);
    if (el.name && document.querySelectorAll('[name="' + CSS.escape(el.name) + '"]').length === 1)
      add('name', '[name="' + escAttr(el.name) + '"]', 88);
    const role = el.getAttribute('role');
    const aria = el.getAttribute('aria-label');
    if (role && aria) add('aria', '[role="' + escAttr(role) + '"][aria-label="' + escAttr(aria) + '"]', 92);
    if (el.placeholder && ['INPUT', 'TEXTAREA'].includes(el.tagName)) add('placeholder', '[placeholder="' + escAttr(el.placeholder) + '"]', 82);
    add('structural_css', structuralSelector(el), 45);
    return values.sort((a,b) => b.score - a.score);
  };
  const selector = el => selectorCandidates(el)[0]?.value || structuralSelector(el);
  const implicitRole = el => {
    if (el.getAttribute('role')) return el.getAttribute('role');
    const tag = el.tagName.toLowerCase();
    if (tag === 'button') return 'button';
    if (tag === 'a' && el.hasAttribute('href')) return 'link';
    if (tag === 'textarea') return 'textbox';
    if (tag === 'select') return 'combobox';
    if (tag === 'input') {
      const type = (el.type || 'text').toLowerCase();
      if (['button','submit','reset'].includes(type)) return 'button';
      if (type === 'checkbox') return 'checkbox';
      if (type === 'radio') return 'radio';
      return 'textbox';
    }
    return '';
  };
  const accessibleName = el => {
    const labelled = el.getAttribute('aria-labelledby');
    if (labelled) {
      const text = labelled.split(/\s+/).map(id => document.getElementById(id)?.innerText || '').join(' ');
      if (clean(text)) return clean(text);
    }
    if (el.getAttribute('aria-label')) return clean(el.getAttribute('aria-label'));
    if (el.id) {
      const label = document.querySelector('label[for="' + CSS.escape(el.id) + '"]');
      if (label && clean(label.innerText)) return clean(label.innerText);
    }
    const wrapping = el.closest('label');
    if (wrapping && clean(wrapping.innerText)) return clean(wrapping.innerText);
    return clean(el.innerText || el.placeholder || el.title || el.name || '');
  };
  const anchors = el => {
    const result = [];
    const add = (kind, node) => {
      if (!node || result.length >= 5) return;
      const text = clean(node.innerText || node.getAttribute?.('aria-label') || node.title || '', 160);
      if (text && !result.some(x => x.text === text)) result.push({kind, text, tag: (node.tagName || '').toLowerCase(), id: clean(node.id, 80)});
    };
    if (el.id) add('label_for', document.querySelector('label[for="' + CSS.escape(el.id) + '"]'));
    add('wrapping_label', el.closest('label'));
    let sibling = el.previousElementSibling;
    for (let i = 0; sibling && i < 3; i++, sibling = sibling.previousElementSibling) add('previous', sibling);
    let parent = el.parentElement;
    for (let i = 0; parent && i < 3; i++, parent = parent.parentElement) add('container', parent);
    return result;
  };
  const componentType = comp => {
    try { return clean(comp._type_name || comp.constructor?.name || String(comp).replace(/^\[object\s+|\]$/g,''), 100); } catch { return ''; }
  };
  const nexacroProbe = () => {
    try {
      if (typeof nexacro === 'undefined' || typeof nexacro.getApplication !== 'function') return {available:false};
      const app = nexacro.getApplication();
      const form = typeof app.getActiveForm === 'function' ? app.getActiveForm() : null;
      const focus = form && typeof form.getFocus === 'function' ? form.getFocus() : null;
      const path = [];
      let current = focus;
      for (let i = 0; current && i < 8; i++) {
        path.push({name: clean(current.name || current.id, 120), type: componentType(current)});
        current = current.parent && current.parent !== current ? current.parent : null;
      }
      const component = focus ? {
        name: clean(focus.name || focus.id, 120),
        type: componentType(focus),
        text: clean(typeof focus.getDisplayText === 'function' ? focus.getDisplayText() : (focus.text || ''), 180),
        cell: typeof focus.getCellPos === 'function' ? Number(focus.getCellPos()) : null,
        dataset_row: typeof focus.getBindDataset === 'function' && focus.getBindDataset() ? Number(focus.getBindDataset().rowposition) : null,
        path
      } : null;
      const components = [];
      if (form && form.components && typeof form.components.length === 'number') {
        for (let i = 0; i < Math.min(form.components.length, 80); i++) {
          const comp = form.components[i];
          components.push({name: clean(comp?.name || comp?.id, 100), type: componentType(comp)});
        }
      }
      return {available:true, application: clean(app.id || app.name, 100), form: form ? clean(form.name || form.id, 120) : '', component, component_count: components.length, components};
    } catch (error) {
      return {available:true, error: clean(error?.name || 'probe_error', 80)};
    }
  };
  const geometry = (el, event) => {
    const r = el.getBoundingClientRect();
    return {
      x: Math.round(r.x), y: Math.round(r.y), width: Math.round(r.width), height: Math.round(r.height),
      center_x_ratio: window.innerWidth ? Number(((r.left + r.width / 2) / window.innerWidth).toFixed(5)) : null,
      center_y_ratio: window.innerHeight ? Number(((r.top + r.height / 2) / window.innerHeight).toFixed(5)) : null,
      viewport_width: window.innerWidth, viewport_height: window.innerHeight,
      screen_x: event && Number.isFinite(event.screenX) ? event.screenX : null,
      screen_y: event && Number.isFinite(event.screenY) ? event.screenY : null,
      click_x_ratio: event && r.width ? Number(((event.clientX - r.left) / r.width).toFixed(5)) : null,
      click_y_ratio: event && r.height ? Number(((event.clientY - r.top) / r.height).toFixed(5)) : null,
    };
  };
  const fingerprint = (el, event) => ({
    schema_version: 1,
    web: {
      tag: el.tagName.toLowerCase(), id: clean(el.id, 120), name: clean(el.name, 120), type: clean(el.type, 80),
      role: clean(implicitRole(el), 80), accessible_name: accessibleName(el), text: clean(el.innerText, 180),
      placeholder: clean(el.placeholder, 140), title: clean(el.title, 140), candidates: selectorCandidates(el)
    },
    anchors: anchors(el),
    geometry: geometry(el, event),
    canvas: el.tagName === 'CANVAS' ? {relative_x: geometry(el,event).click_x_ratio, relative_y: geometry(el,event).click_y_ratio} : null,
    nexacro: nexacroProbe()
  });
  const send = step => { if (window.__smartopsCapture) window.__smartopsCapture(step).catch(() => {}); };

  document.addEventListener('click', event => {
    if (!event.isTrusted) return;
    const raw = event.target?.nodeType === 1 ? event.target : event.target?.parentElement;
    if (!raw) return;
    const el = raw.closest?.('button,a,input,textarea,select,canvas,[role],[id],[name]') || raw;
    if (!el || sensitive(el) || el.matches?.('input:not([type="submit"]):not([type="button"]):not([type="checkbox"]):not([type="radio"])')) return;
    send({action:'click', selector:selector(el), label:accessibleName(el) || clean(el.innerText) || 'Click', fingerprint:fingerprint(el,event)});
  }, true);

  document.addEventListener('change', event => {
    if (!event.isTrusted) return;
    const el = event.target;
    if (!el || sensitive(el)) return;
    const fp = fingerprint(el,event);
    if (el.matches('input[type="checkbox"],input[type="radio"]')) send({action:'check',selector:selector(el),checked:el.checked,label:accessibleName(el)||'Check',fingerprint:fp});
    else if (el.matches('select')) send({action:'select',selector:selector(el),value:el.value,label:accessibleName(el)||'Select',fingerprint:fp});
    else if (el.matches('input:not([type="file"]),textarea')) send({action:'fill',selector:selector(el),value:el.value,label:accessibleName(el)||'Fill',fingerprint:fp});
  }, true);

  document.addEventListener('keydown', event => {
    if (!event.isTrusted || sensitive(event.target)) return;
    const el = event.target?.nodeType === 1 ? event.target : document.activeElement;
    if (!el) return;
    const special = event.key.length > 1 || event.ctrlKey || event.altKey || event.metaKey;
    if (!special) return;
    const parts = [];
    if (event.ctrlKey) parts.push('Control');
    if (event.altKey) parts.push('Alt');
    if (event.shiftKey) parts.push('Shift');
    if (event.metaKey) parts.push('Meta');
    if (!['Control','Alt','Shift','Meta'].includes(event.key)) parts.push(event.key);
    if (!parts.length) return;
    if (event.key === 'Enter' && el.matches('input,textarea')) send({action:'fill',selector:selector(el),value:el.value,label:accessibleName(el)||'Fill',fingerprint:fingerprint(el,event)});
    send({action:'press',selector:selector(el),value:parts.join('+'),label:'Press '+parts.join('+'),fingerprint:fingerprint(el,event)});
  }, true);
})();
