(() => {
  if (window.__smartopsInstalled) return;
  window.__smartopsInstalled = true;

  const clean = (value, limit = 160) => String(value || '').replace(/\s+/g, ' ').trim().slice(0, limit);
  const sensitive = el => /password|passwd|secret|token|otp|credit|card|username|email|login/i.test([
    el && el.type, el && el.name, el && el.id, el && el.autocomplete
  ].join(' '));

  const selector = el => {
    if (!el || el.nodeType !== 1) return 'body';
    if (el.id) return '#' + CSS.escape(el.id);
    if (el.getAttribute('data-testid')) return '[data-testid="' + CSS.escape(el.getAttribute('data-testid')) + '"]';
    if (el.name && document.querySelectorAll('[name="' + CSS.escape(el.name) + '"]').length === 1) return '[name="' + CSS.escape(el.name) + '"]';
    const parts = [];
    while (el && el.nodeType === 1 && parts.length < 8) {
      let part = el.tagName.toLowerCase();
      const siblings = el.parentElement ? [...el.parentElement.children].filter(x => x.tagName === el.tagName) : [];
      if (siblings.length > 1) part += ':nth-of-type(' + (siblings.indexOf(el) + 1) + ')';
      parts.unshift(part);
      el = el.parentElement;
    }
    return parts.join(' > ') || 'body';
  };

  const visibleText = el => clean(
    (el && (el.innerText || el.textContent)) ||
    (el && el.getAttribute && (el.getAttribute('aria-label') || el.getAttribute('title') || el.getAttribute('placeholder'))) || ''
  );

  const associatedLabel = el => {
    if (!el) return '';
    if (el.labels && el.labels.length) return clean([...el.labels].map(x => visibleText(x)).filter(Boolean).join(' | '));
    const wrapping = el.closest && el.closest('label');
    if (wrapping) return visibleText(wrapping);
    const labelledBy = el.getAttribute && el.getAttribute('aria-labelledby');
    if (labelledBy) return clean(labelledBy.split(/\s+/).map(id => document.getElementById(id)).filter(Boolean).map(visibleText).join(' | '));
    return '';
  };

  const anchorCandidates = el => {
    const anchors = [];
    const push = (kind, node, text) => {
      text = clean(text || visibleText(node));
      if (!text || anchors.some(x => x.text === text)) return;
      anchors.push({kind, text, selector: node && node.nodeType === 1 ? selector(node) : ''});
    };
    const label = associatedLabel(el);
    if (label) anchors.push({kind: 'label', text: label, selector: ''});
    let current = el;
    for (let depth = 0; current && depth < 4 && anchors.length < 8; depth++, current = current.parentElement) {
      if (current.previousElementSibling) push('previous_sibling', current.previousElementSibling);
      if (current.nextElementSibling) push('next_sibling', current.nextElementSibling);
      if (current.parentElement) {
        const heading = current.parentElement.querySelector(':scope > label, :scope > legend, :scope > h1, :scope > h2, :scope > h3, :scope > h4, :scope > span, :scope > strong');
        if (heading && heading !== el) push('container_text', heading);
      }
    }
    return anchors.slice(0, 8);
  };

  const objectInfo = obj => {
    if (!obj || typeof obj !== 'object') return null;
    const typeName = clean(obj._type_name || obj._class_name || (obj.constructor && obj.constructor.name), 100);
    const id = clean(obj.id || obj._id || obj.name || obj._unique_id, 240);
    if (!typeName && !id) return null;
    const path = [];
    const seen = new Set();
    let current = obj;
    while (current && typeof current === 'object' && path.length < 10 && !seen.has(current)) {
      seen.add(current);
      const part = clean(current.id || current._id || current.name || current._unique_id, 120);
      if (part) path.unshift(part);
      current = current.parent || current.parent_comp || current._parent || current._owner || null;
    }
    return {type: typeName, id, path: path.join('.')};
  };

  const nexacroInfo = el => {
    const available = typeof window.nexacro !== 'undefined';
    const ids = [];
    const candidates = [];
    const seen = new Set();
    let current = el;
    while (current && current.nodeType === 1 && ids.length < 12) {
      if (current.id) ids.push(clean(current.id, 240));
      for (const key of ['_linked_element', '_control_element', '_owner_elem', '_owner_element', '_component', '_owner']) {
        try {
          const info = objectInfo(current[key]);
          if (info && !seen.has(JSON.stringify(info))) {
            seen.add(JSON.stringify(info));
            candidates.push({source: key, ...info});
          }
        } catch (_) {}
      }
      current = current.parentElement;
    }
    let application = false;
    try { application = !!(available && typeof window.nexacro.getApplication === 'function' && window.nexacro.getApplication()); } catch (_) {}
    const pathGuess = ids.find(x => x.includes('.form.')) || ids.find(x => /mainframe|childframe|\.form\./i.test(x)) || '';
    return {
      status: available ? 'ok' : 'unavailable',
      framework_available: available,
      application_available: application,
      dom_id_chain: ids,
      component_candidates: candidates.slice(0, 12),
      accessibility_id_guess: pathGuess
    };
  };

  let lastPointer = null;
  document.addEventListener('pointerdown', event => {
    if (!event.isTrusted) return;
    lastPointer = {target: event.target, screen_x: event.screenX, screen_y: event.screenY, client_x: event.clientX, client_y: event.clientY};
  }, true);

  const discoverySeed = (el, event = null) => {
    const rect = el && el.getBoundingClientRect ? el.getBoundingClientRect() : {x: 0, y: 0, width: 0, height: 0};
    const pointer = event && typeof event.screenX === 'number'
      ? {screen_x: event.screenX, screen_y: event.screenY, client_x: event.clientX, client_y: event.clientY}
      : (lastPointer && lastPointer.target === el ? lastPointer : null);
    const clickX = pointer ? pointer.client_x : rect.x + rect.width / 2;
    const clickY = pointer ? pointer.client_y : rect.y + rect.height / 2;
    const relativeX = rect.width ? (clickX - rect.x) / rect.width : 0.5;
    const relativeY = rect.height ? (clickY - rect.y) / rect.height : 0.5;
    const dom = {
      status: 'ok',
      tag: clean(el && el.tagName, 40).toLowerCase(),
      id: clean(el && el.id, 240),
      name: clean(el && el.getAttribute && el.getAttribute('name'), 160),
      type: clean(el && el.getAttribute && el.getAttribute('type'), 80),
      role: clean(el && el.getAttribute && el.getAttribute('role'), 80),
      aria_label: clean(el && el.getAttribute && el.getAttribute('aria-label')),
      placeholder: clean(el && el.getAttribute && el.getAttribute('placeholder')),
      title: clean(el && el.getAttribute && el.getAttribute('title')),
      text: visibleText(el),
      label: associatedLabel(el),
      test_id: clean(el && el.getAttribute && el.getAttribute('data-testid'), 160),
      selector: selector(el),
      frame_url: clean(location.href, 500),
      frame_name: clean(window.name, 120)
    };
    const anchors = anchorCandidates(el);
    return {
      point: pointer ? {screen_x: pointer.screen_x, screen_y: pointer.screen_y, client_x: pointer.client_x, client_y: pointer.client_y} : {},
      geometry: {
        box: {x: rect.x, y: rect.y, width: rect.width, height: rect.height},
        viewport: {width: innerWidth, height: innerHeight},
        page: {x: scrollX, y: scrollY},
        relative_click: {x: Number(relativeX.toFixed(4)), y: Number(relativeY.toFixed(4))}
      },
      layers: {
        dom,
        nexacro: nexacroInfo(el),
        anchors: {status: anchors.length ? 'ok' : 'miss', candidates: anchors},
        visible_text: {status: (dom.text || dom.label || anchors.length) ? 'ok' : 'miss', text: dom.text, label: dom.label, nearby: anchors.map(x => x.text).slice(0, 6)},
        relative_position: {status: 'ok', box: {x: rect.x, y: rect.y, width: rect.width, height: rect.height}, viewport: {width: innerWidth, height: innerHeight}, relative_click: {x: Number(relativeX.toFixed(4)), y: Number(relativeY.toFixed(4))}}
      }
    };
  };

  const send = step => {
    if (window.__smartopsCapture) window.__smartopsCapture(step).catch(() => {});
  };

  document.addEventListener('click', event => {
    if (!event.isTrusted) return;
    const el = event.target.closest('button,a,input,select,textarea,[role="button"],[role="tab"],[role="menuitem"],[role="option"],[id]');
    if (!el || sensitive(el) || el.matches('input:not([type="submit"]):not([type="button"]):not([type="checkbox"]):not([type="radio"])')) return;
    send({
      action: el.matches('input[type="checkbox"],input[type="radio"]') ? 'check' : 'click',
      selector: selector(el),
      checked: el.matches('input[type="checkbox"],input[type="radio"]') ? !!el.checked : undefined,
      label: visibleText(el) || clean(el.getAttribute('aria-label')) || 'Click',
      discovery_seed: discoverySeed(el, event)
    });
  }, true);

  document.addEventListener('change', event => {
    if (!event.isTrusted) return;
    const el = event.target;
    if (!el || sensitive(el)) return;
    const seed = discoverySeed(el);
    if (el.matches('input[type="checkbox"],input[type="radio"]')) send({action: 'check', selector: selector(el), checked: el.checked, label: associatedLabel(el) || 'Check', discovery_seed: seed});
    else if (el.matches('select')) send({action: 'select', selector: selector(el), value: el.value, label: associatedLabel(el) || 'Select', discovery_seed: seed});
    else if (el.matches('input:not([type="file"]),textarea')) send({action: 'fill', selector: selector(el), value: el.value, label: associatedLabel(el) || clean(el.placeholder) || 'Fill', discovery_seed: seed});
  }, true);

  const safeKeys = new Set(['Tab', 'Escape', 'ArrowUp', 'ArrowDown', 'ArrowLeft', 'ArrowRight', 'Home', 'End', 'PageUp', 'PageDown', 'Insert', 'Delete']);
  document.addEventListener('keydown', event => {
    if (!event.isTrusted || sensitive(event.target)) return;
    const el = event.target && event.target.nodeType === 1 ? event.target : document.body;
    const key = event.key;
    if (key === 'Enter' && el.matches('input,textarea')) {
      send({action: 'fill', selector: selector(el), value: el.value, label: associatedLabel(el) || 'Fill', discovery_seed: discoverySeed(el)});
      send({action: 'press', selector: selector(el), value: 'Enter', label: 'Press Enter', discovery_seed: discoverySeed(el)});
      return;
    }
    const functionKey = /^F([1-9]|1[0-2])$/.test(key);
    const shortcut = (event.ctrlKey || event.altKey || event.metaKey) && key.length === 1;
    if (!safeKeys.has(key) && !functionKey && !shortcut) return;
    const parts = [];
    if (event.ctrlKey) parts.push('Control');
    if (event.altKey) parts.push('Alt');
    if (event.shiftKey) parts.push('Shift');
    if (event.metaKey) parts.push('Meta');
    parts.push(key.length === 1 ? key.toUpperCase() : key);
    send({action: 'press', selector: selector(el), value: parts.join('+'), label: 'Press ' + parts.join('+'), discovery_seed: discoverySeed(el)});
  }, true);
})();
