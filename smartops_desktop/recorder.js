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
  const send = step => { if (window.__smartopsCapture) window.__smartopsCapture(step).catch(() => {}); };
  document.addEventListener('click', event => {
    if (!event.isTrusted) return;
    const el = event.target.closest('button,a,input,[role="button"],[role="tab"], [id]');
    if (!el || sensitive(el) || el.matches('input:not([type="submit"]):not([type="button"])')) return;
    send({action:'click',selector:selector(el),label:(el.innerText || el.getAttribute('aria-label') || 'Click').trim().slice(0,80)});
  }, true);
  document.addEventListener('change', event => {
    if (!event.isTrusted) return;
    const el = event.target;
    if (sensitive(el)) return;
    if (el.matches('input[type="checkbox"],input[type="radio"]')) send({action:'check',selector:selector(el),checked:el.checked});
    else if (el.matches('select')) send({action:'select',selector:selector(el),value:el.value});
    else if (el.matches('input:not([type="file"]),textarea')) send({action:'fill',selector:selector(el),value:el.value});
  }, true);
  document.addEventListener('keydown', event => {
    if (event.isTrusted && event.key === 'Enter' && !sensitive(event.target) && event.target.matches('input,textarea')) {
      send({action:'fill',selector:selector(event.target),value:event.target.value});
      send({action:'press',selector:selector(event.target),value:'Enter'});
    }
  }, true);
})();
