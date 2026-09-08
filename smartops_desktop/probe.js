// Element radar, page side. Arms on demand, swallows the next click so the page does not react,
// and reports one element through as many independent layers as it can see.
// It reports identity only: no field values, so a fingerprint never carries business data.
(() => {
  if (window.__smartopsProbeInstalled) { window.__smartopsProbeArmed = true; return; }
  window.__smartopsProbeInstalled = true;
  window.__smartopsProbeArmed = true;

  const FOUND = 'found', MISSING = 'missing', UNAVAILABLE = 'unavailable';
  const layer = (status, detail, data) => ({status, detail: String(detail || '').slice(0, 400), data: data || {}});
  const text = el => ((el && (el.innerText || el.textContent)) || '').replace(/\s+/g, ' ').trim().slice(0, 120);
  const attr = (el, name) => (el && el.getAttribute && el.getAttribute(name)) || '';
  const visible = el => {
    const box = el.getBoundingClientRect();
    return box.width > 0 && box.height > 0 && getComputedStyle(el).visibility !== 'hidden';
  };
  const box = el => { const r = el.getBoundingClientRect(); return {x: Math.round(r.left), y: Math.round(r.top), width: Math.round(r.width), height: Math.round(r.height)}; };

  const cssPath = el => {
    if (el.id && !/\./.test(el.id)) return '#' + CSS.escape(el.id);
    if (attr(el, 'data-testid')) return '[data-testid="' + CSS.escape(attr(el, 'data-testid')) + '"]';
    if (el.name && document.querySelectorAll('[name="' + CSS.escape(el.name) + '"]').length === 1) return '[name="' + CSS.escape(el.name) + '"]';
    const parts = [];
    let node = el;
    while (node && node.nodeType === 1 && parts.length < 8) {
      let part = node.tagName.toLowerCase();
      const siblings = node.parentElement ? [...node.parentElement.children].filter(x => x.tagName === node.tagName) : [];
      if (siblings.length > 1) part += ':nth-of-type(' + (siblings.indexOf(node) + 1) + ')';
      parts.unshift(part);
      node = node.parentElement;
    }
    return parts.join(' > ');
  };

  // Credential fields are identified but never described by their content.
  const sensitive = el => /password|passwd|secret|token|otp|credit|card/i.test([el.type, el.name, el.id, el.autocomplete].join(' '));
  const unique = selector => { try { return document.querySelectorAll(selector).length === 1; } catch (error) { return null; } };

  // --- Layer: web ------------------------------------------------------------------------
  const webLayer = el => {
    const tag = el.tagName.toLowerCase();
    const secret = sensitive(el);
    const data = {
      tag, id: el.id || '', name: el.name || '', type: attr(el, 'type'), role: attr(el, 'role'),
      testid: attr(el, 'data-testid'), title: attr(el, 'title'), placeholder: attr(el, 'placeholder'),
      classes: (el.className && typeof el.className === 'string' ? el.className.trim().split(/\s+/).slice(0, 6) : []),
      text: secret ? '' : text(el), href: attr(el, 'href'), selector: cssPath(el), sensitive: secret,
      in_iframe: window !== window.top,
      kind: tag === 'a' ? 'link' : tag === 'select' ? 'list' : tag === 'textarea' ? 'field'
            : tag === 'button' ? 'button' : tag === 'iframe' ? 'inner frame'
            : tag === 'input' ? ((/^(button|submit|reset)$/i.test(attr(el, 'type')) ? 'button' : /^(checkbox|radio)$/i.test(attr(el, 'type')) ? 'choice' : 'field'))
            : attr(el, 'role') || 'text',
    };
    data.unique_selector = data.selector ? unique(data.selector) : null;
    data.unique_name = el.name ? document.querySelectorAll('[name="' + CSS.escape(el.name) + '"]').length === 1 : false;
    if (!(data.id || data.name || data.testid || data.text || data.title) && !data.selector) {
      return layer(MISSING, 'The element carries no id, name or text.', data);
    }
    return layer(FOUND, 'identified in the page', data);
  };

  // --- Layer: frame and tab context --------------------------------------------------------
  const frameLayer = el => {
    const chain = [];
    let view = window, depth = 0;
    while (view !== view.parent && depth < 8) {
      let holder = null;
      try { holder = view.frameElement; } catch (error) { holder = null; }
      chain.unshift(holder
        ? {id: holder.id || '', name: holder.getAttribute('name') || '', src: (holder.getAttribute('src') || '').slice(0, 200), selector: cssPath(holder)}
        : {cross_origin: true});
      view = view.parent;
      depth++;
    }
    return layer(FOUND, 'frame context resolved', {
      depth, top_level: depth === 0, url: location.href, frame_name: window.name || '', chain,
      target: attr(el, 'target'),
      opens_new_tab: attr(el, 'target') === '_blank' || (el.tagName === 'A' && /\bnoopener\b/.test(attr(el, 'rel'))),
    });
  };

  // --- Layer: nexacro --------------------------------------------------------------------
  // Two independent routes. The object route reads the live component; the id route reads the
  // dotted path Nexacro renders into the DOM, which keeps working when internals are hidden.
  const nexacroObject = el => {
    for (let node = el; node && node.nodeType === 1; node = node.parentElement) {
      for (const key of ['_comp', 'comp', '_control', '__comp', '_pseudo_comp']) {
        const value = node[key];
        if (value && typeof value === 'object' && (value._type_name || value.name)) return value;
      }
    }
    return null;
  };
  const nexacroChain = component => {
    const chain = [];
    for (let node = component; node && chain.length < 12; node = node.parent) chain.push(node);
    return chain;
  };
  const nexacroIdPath = el => {
    for (let node = el; node && node.nodeType === 1; node = node.parentElement) {
      const id = node.id || '';
      if (id.includes('.') && /(^|\.)(form|mainframe|frameset)(\.|$)/i.test(id)) return {node, path: id};
    }
    return null;
  };
  const nexacroLayer = el => {
    const present = typeof nexacro !== 'undefined';
    const found = nexacroIdPath(el);
    if (!present && !found) return layer(UNAVAILABLE, 'This page is not a Nexacro screen.', {});
    const data = {framework: present, application: present && typeof nexacro.getApplication === 'function'};
    if (found) {
      const parts = found.path.split('.');
      data.path = found.path;
      let at = parts.length - 1;
      // A grid body cell renders as <grid>.body.<row>.<column>: the component is the grid.
      const body = parts.lastIndexOf('body');
      if (body > 0 && /^\d+$/.test(parts[body + 1] || '')) {
        data.grid = parts[body - 1];
        data.row = Number(parts[body + 1]);
        if (/^\d+$/.test(parts[body + 2] || '')) data.column = Number(parts[body + 2]);
        at = body - 1;
      }
      data.component = parts[at];
      const form = parts.lastIndexOf('form');
      if (form > 0) data.form = parts[form - 1];
      // Skip the structural segments Nexacro inserts, so the parent is a real component.
      const structural = new Set(['form', 'body', 'mainframe']);
      let up = at - 1;
      while (up >= 0 && (structural.has(parts[up]) || /^\d+$/.test(parts[up]))) up--;
      data.parent = up >= 0 ? parts[up] : '';
    }
    const component = nexacroObject(el);
    if (component) {
      data.type = component._type_name || '';
      data.name = component.name || component.id || data.component || '';
      const chain = nexacroChain(component);
      const grid = chain.find(c => c._type_name === 'Grid');
      const form = chain.find(c => c._type_name === 'Form');
      if (grid) { data.grid = grid.name || data.grid; if (typeof grid._getCurRow === 'function') { try { data.row = grid._getCurRow(); } catch (e) {} } }
      if (form) data.form = form.name || data.form;
      if (component.parent) data.parent = component.parent.name || data.parent || '';
      data.chain = chain.map(c => (c._type_name || '?') + ':' + (c.name || '')).slice(0, 8);
    }
    if (!data.component && !data.name) return layer(MISSING, 'Nexacro is loaded but this element is not inside a component.', data);
    const label = [data.type, data.name || data.component].filter(Boolean).join(' ');
    const where = [data.form && 'form ' + data.form, data.grid && 'grid ' + data.grid,
                   data.row !== undefined && 'row ' + data.row, data.column !== undefined && 'column ' + data.column].filter(Boolean).join(' · ');
    return layer(FOUND, label + (where ? ' · ' + where : ''), data);
  };

  // --- Layer: anchor ---------------------------------------------------------------------
  const anchorLayer = el => {
    const target = el.getBoundingClientRect();
    const centre = {x: target.left + target.width / 2, y: target.top + target.height / 2};
    let best = null;
    for (const candidate of document.querySelectorAll('label,legend,caption,th,h1,h2,h3,h4,h5,h6,[role="heading"],strong,b,a,button')) {
      if (candidate === el || candidate.contains(el) || el.contains(candidate)) continue;
      const words = text(candidate);
      if (!words || words.length > 80 || !visible(candidate)) continue;
      const rect = candidate.getBoundingClientRect();
      const distance = Math.hypot(rect.left + rect.width / 2 - centre.x, rect.top + rect.height / 2 - centre.y);
      // A <label for> that points straight at the element beats anything measured by distance.
      const bound = candidate.tagName === 'LABEL' && el.id && candidate.htmlFor === el.id;
      const ranked = bound ? -1 : distance;
      if (!best || ranked < best.ranked) best = {node: candidate, words, ranked, distance, bound, rect};
    }
    if (!best) return layer(MISSING, 'No stable neighbouring text was found.', {});
    const rect = best.rect;
    const side = rect.bottom <= target.top ? 'above' : rect.top >= target.bottom ? 'below' : rect.right <= target.left ? 'left of' : rect.left >= target.right ? 'right of' : 'overlapping';
    return layer(FOUND, 'anchor resolved', {
      text: best.words, tag: best.node.tagName.toLowerCase(), selector: cssPath(best.node),
      side, bound: best.bound, distance: Math.round(best.distance), anchor_box: box(best.node),
    });
  };

  // --- Layer: relative position ----------------------------------------------------------
  const relativeLayer = el => {
    const target = el.getBoundingClientRect();
    let container = el.closest('form,dialog,[role="dialog"],section,main,article') || document.body;
    const outer = container.getBoundingClientRect();
    if (!outer.width || !outer.height) return layer(MISSING, 'The surrounding container has no measurable size.', {});
    const data = {
      container: container.tagName.toLowerCase() + (container.id ? '#' + container.id : ''),
      container_box: box(container), element_box: box(el),
      offset_x: Math.round(target.left - outer.left), offset_y: Math.round(target.top - outer.top),
      fraction_x: Number(((target.left + target.width / 2 - outer.left) / outer.width).toFixed(4)),
      fraction_y: Number(((target.top + target.height / 2 - outer.top) / outer.height).toFixed(4)),
      viewport: {width: window.innerWidth, height: window.innerHeight},
    };
    return layer(FOUND, Math.round(data.fraction_x * 100) + '% across, ' + Math.round(data.fraction_y * 100) + '% down its ' + data.container, data);
  };

  // --- Layer: keyboard route -------------------------------------------------------------
  const FOCUSABLE = 'a[href],button,input,select,textarea,[tabindex]:not([tabindex="-1"]),[contenteditable="true"]';
  const keyboardLayer = el => {
    const focusable = [...document.querySelectorAll(FOCUSABLE)].filter(x => !x.disabled && visible(x));
    const index = focusable.indexOf(el);
    const data = {tab_index: attr(el, 'tabindex'), access_key: attr(el, 'accesskey'), focus_order: index, focus_total: focusable.length,
                  reachable: index >= 0, is_active: document.activeElement === el};
    if (index < 0) return layer(MISSING, 'The element cannot be reached with the Tab key.', data);
    return layer(FOUND, 'Tab stop ' + (index + 1) + ' of ' + focusable.length + (data.access_key ? ' · shortcut ' + data.access_key : ''), data);
  };

  let counter = 0;
  const capture = (el, event) => {
    // Tag the element so the worker can resolve the very same node over CDP for the
    // accessibility tree, then hand the tag back for removal.
    const token = 'e' + (++counter) + '-' + Math.random().toString(36).slice(2, 10);
    try { el.setAttribute('data-smartops-probe', token); } catch (error) { /* read-only nodes */ }
    const layers = {};
    for (const [key, build] of [['web', webLayer], ['frame', frameLayer], ['nexacro', nexacroLayer], ['anchor', anchorLayer], ['relative', relativeLayer], ['keyboard', keyboardLayer]]) {
      try { layers[key] = build(el); }
      catch (error) { layers[key] = layer(MISSING, 'This layer failed: ' + (error && error.message ? error.message : error)); }
    }
    return {source: location.href, page_title: document.title, layers, token, box: box(el),
            screen: event ? {x: Math.round(event.screenX), y: Math.round(event.screenY)} : null,
            device_pixel_ratio: window.devicePixelRatio || 1};
  };

  // Swallow the whole press so the page never acts on a click that was only meant to point.
  const swallow = event => { event.preventDefault(); event.stopImmediatePropagation(); };
  for (const name of ['mouseup', 'click', 'dblclick', 'contextmenu', 'submit']) {
    document.addEventListener(name, event => { if (window.__smartopsProbeArmed && event.isTrusted) swallow(event); }, true);
  }
  document.addEventListener('mousedown', event => {
    if (!window.__smartopsProbeArmed || !event.isTrusted) return;
    swallow(event);
    const element = event.target;
    if (!element || element.nodeType !== 1) return;
    try {
      if (window.__smartopsInspectCapture) window.__smartopsInspectCapture(capture(element, event)).catch(() => {});
    } catch (error) { /* pointing at an element must never break the page */ }
  }, true);
})();
