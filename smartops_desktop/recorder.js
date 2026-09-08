(() => {
  if (window.__smartopsInstalled) return;
  window.__smartopsInstalled = true;
  const sensitive = el => /password|passwd|secret|token|otp|credit|card|username|email|login/i.test([el.type,el.name,el.id,el.autocomplete].join(' '));
  const selector = el => {
    if (el.id) return '#' + CSS.escape(el.id);
    if (el.getAttribute('data-testid')) return '[data-testid="' + CSS.escape(el.getAttribute('data-testid')) + '"]';
    if (el.name && document.querySelectorAll('[name="' + CSS.escape(el.name) + '"]').length === 1) return '[name="' + CSS.escape(el.name) + '"]';
    let parts = [];
    while (el && el.nodeType === 1 && parts.length < 8) {
      let part = el.tagName.toLowerCase();
      const siblings = el.parentElement ? [...el.parentElement.children].filter(x => x.tagName === el.tagName) : [];
      if (siblings.length > 1) part += ':nth-of-type(' + (siblings.indexOf(el)+1) + ')';
      parts.unshift(part); el = el.parentElement;
    }
    return parts.join(' > ');
  };
  const text = el => (el.innerText || el.value || el.getAttribute('aria-label') || el.getAttribute('title') || '').trim().slice(0,120);
  const evidence = el => {
    const r = el.getBoundingClientRect();
    const web = {tag:el.tagName.toLowerCase(), id:el.id||'', name:el.getAttribute('name')||'', role:el.getAttribute('role')||'', ariaLabel:el.getAttribute('aria-label')||'', text:text(el), selector:selector(el)};
    const ax = {role:el.getAttribute('role') || (el.tagName.toLowerCase()==='button'?'button':''), name:el.getAttribute('aria-label') || text(el)};
    const ev = [
      {layer:'web',available:true,confidence:(el.id||el.getAttribute('data-testid'))?0.95:0.8,identity:web},
      {layer:'accessibility',available:!!(ax.role||ax.name),confidence:ax.role&&ax.name?0.8:0.55,identity:ax},
      {layer:'relative_position',available:true,confidence:0.35,identity:{x:r.x/window.innerWidth,y:r.y/window.innerHeight,w:r.width/window.innerWidth,h:r.height/window.innerHeight}}
    ];
    const parent = el.parentElement;
    if (parent) {
      const candidates=[...parent.children].filter(x=>x!==el && text(x));
      if(candidates.length) ev.push({layer:'anchor',available:true,confidence:0.55,identity:{text:text(candidates[0]),relation:'sibling',targetSelector:selector(el)}});
    }
    try {
      const nx = window.nexacro;
      if (nx) {
        const hints = {};
        for (const node of [el, el.parentElement, el.parentElement && el.parentElement.parentElement].filter(Boolean)) {
          for (const key of ['id','name','class']) if (node.getAttribute && node.getAttribute(key)) hints[key]=node.getAttribute(key);
        }
        ev.push({layer:'nexacro',available:true,confidence:0.4,identity:{framework:true,domHints:hints}});
      }
    } catch (_) {}
    return ev;
  };
  const packet = (action,el,extra={}) => ({action,selector:selector(el),label:text(el)||action,evidence:evidence(el),...extra});
  const send = step => { if (window.__smartopsCapture) window.__smartopsCapture(step).catch(() => {}); };
  document.addEventListener('click', event => {
    if (!event.isTrusted) return;
    const el = event.target.closest('button,a,input,select,textarea,[role="button"],[role="tab"],[role="menuitem"],[id]');
    if (!el || sensitive(el) || el.matches('input:not([type="submit"]):not([type="button"]):not([type="checkbox"]):not([type="radio"])')) return;
    send(packet('click',el));
  }, true);
  document.addEventListener('change', event => {
    if (!event.isTrusted) return;
    const el = event.target;
    if (!el || sensitive(el)) return;
    if (el.matches('input[type="checkbox"],input[type="radio"]')) send(packet('check',el,{checked:el.checked}));
    else if (el.matches('select')) send(packet('select',el,{value:el.value}));
    else if (el.matches('input:not([type="file"]),textarea')) send(packet('fill',el,{value:el.value}));
  }, true);
  document.addEventListener('keydown', event => {
    if (!event.isTrusted || sensitive(event.target)) return;
    if (event.key === 'Enter' && event.target.matches('input,textarea')) {
      send(packet('fill',event.target,{value:event.target.value}));
      send(packet('press',event.target,{value:'Enter',evidence:[...evidence(event.target),{layer:'keyboard',available:true,confidence:1,identity:{key:'Enter'}}]}));
    }
  }, true);
})();
