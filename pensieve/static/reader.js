// Reader behaviours: Google Reader keyboard map, selection, HTMX state sync, panes, toasts, dialogs, menus.
// Registered once per page load; survives hx-boost body swaps because handlers live on document.
(function () {
  if (window.__pensieveReader) return;
  window.__pensieveReader = true;

  const $ = (sel, root) => (root || document).querySelector(sel);
  const $$ = (sel, root) => Array.from((root || document).querySelectorAll(sel));
  const app = () => $('#app');

  // ---- Breakpoints: one source of truth in app.css (--bp-mobile / --bp-wide) ----
  function bp(name, fallback) {
    const v = parseFloat(getComputedStyle(document.documentElement).getPropertyValue(name));
    return isNaN(v) ? fallback : v;
  }
  const mqMobile = window.matchMedia('(max-width: ' + (bp('--bp-mobile', 900) - 0.02) + 'px)');
  const mqWide = window.matchMedia('(min-width: ' + bp('--bp-wide', 1160) + 'px)');
  const isMobile = () => mqMobile.matches;
  const isMid = () => !mqMobile.matches && !mqWide.matches;

  // ---- Panes (mobile: one at a time; mid: nav is a drawer) ----
  const paneY = { app: null, at: {} };
  // Phones: the incoming pane slides in from the side it sits on (folders | list | article); web.css keys the
  // entrance off data-enter, which is cleared again so a later list refresh does not slide in too.
  const PANE_ORDER = { nav: 0, list: 1, article: 2 };
  let enterTimer = null;
  function setPane(name, opts) {
    const a = app();
    if (!a) return;
    const was = a.dataset.pane;
    const phoneSwitch = isMobile() && was !== name;
    if (phoneSwitch) {
      // One document scroll serves every pane on phones: keep each pane's place (a new article starts at the top).
      if (paneY.app !== a) { paneY.app = a; paneY.at = {}; }
      paneY.at[was] = window.scrollY;
      a.dataset.enter = PANE_ORDER[name] > PANE_ORDER[was] ? 'right' : 'left';
      clearTimeout(enterTimer);
      enterTimer = setTimeout(() => { delete a.dataset.enter; }, 450);
      // Push the pane's history entry while the list is still showing: the browser records the scroll of the entry
      // being left and puts it back on Back, over ours. Pushed after the switch, hiding the list had already
      // clamped the page to the top, so Back reopened the list at its first row.
      if (!(opts && opts.fromHistory)) paneHistory(was, name);
    }
    a.dataset.pane = name;
    showBars(true);
    measureBars();
    if (phoneSwitch) scrollPageTo(name === 'article' ? 0 : paneY.at[name] || 0);
    if (name === 'nav') { const first = $('#nav .nav-item'); if (first && !isMobile()) first.focus({ preventScroll: true }); }
  }

  // ---- Phones: the system Back (Android's button or gesture, Safari's edge swipe) closes the article or the
  // folders instead of leaving the page. Opening one from the list pushes a same-URL entry; leaving it any other way
  // pops that entry again. htmx owns window.onpopstate for boosted pages and would reload the list from its history
  // cache, so this listener (added before htmx sets its handler at DOMContentLoaded) stops the event whenever it is
  // only a pane change on this page. ----
  const DOC = Math.random().toString(36).slice(2);
  let paneEntry = null, popping = false;
  function paneHistory(was, name) {
    if (was === 'list' && name !== 'list') {
      try { history.pushState({ pensievePane: name, doc: DOC }, '', location.href); paneEntry = { url: location.href }; } catch (_) {}
    } else if (name === 'list' && paneEntry) {
      paneEntry = null; popping = true; history.back();
    }
  }
  window.addEventListener('popstate', (e) => {
    const st = e.state || {};
    if (popping) { popping = false; e.stopImmediatePropagation(); return; }
    if (st.pensievePane && st.doc === DOC) {
      // Forward again onto an entry this page pushed.
      e.stopImmediatePropagation();
      paneEntry = { url: location.href };
      setPane(st.pensievePane === 'article' && !currentArticle() ? 'list' : st.pensievePane, { fromHistory: true });
      return;
    }
    if (paneEntry) {
      const same = location.href === paneEntry.url;
      paneEntry = null;
      if (same) { e.stopImmediatePropagation(); leaveDrag(); setPane('list', { fromHistory: true }); }
    }
  });
  // A boosted link followed from the article or the folders (the feed name in an article, a folder in the drawer):
  // the entry htmx is about to leave now stands for the page itself, and its snapshot should reopen on the list.
  document.body.addEventListener('htmx:beforeHistorySave', () => {
    if (!paneEntry) return;
    paneEntry = null;
    try { history.replaceState({ htmx: true }, '', location.href); } catch (_) {}
    const a = app(); if (a) a.dataset.pane = 'list';
  });
  // A reload keeps the old entry's state; this page did not push it.
  if (history.state && history.state.pensievePane) { try { history.replaceState(null, '', location.href); } catch (_) {} }
  function closeDrawer() { const a = app(); if (a && a.dataset.pane === 'nav') setPane('list'); }
  mqWide.addEventListener('change', (e) => { if (e.matches) closeDrawer(); });

  document.addEventListener('click', (e) => {
    const sw = e.target.closest('[data-pane-switch]');
    if (sw) {
      const target = sw.dataset.paneSwitch;
      if (sw.tagName === 'A' && !isMobile()) return; // let the link navigate on desktop
      if (sw.tagName === 'A' && isMobile() && app() && app().dataset.view && app().dataset.view !== 'search' && target === 'list' && location.pathname !== '/manage/feeds' && location.pathname !== '/insights') {
        e.preventDefault();
      } else if (sw.tagName !== 'A') {
        e.preventDefault();
      }
      if (target === 'nav' && !isMobile() && app() && app().dataset.pane === 'nav') { setPane('list'); return; }
      setPane(target);
      return;
    }
    const navLink = e.target.closest('.pane-nav a.nav-item');
    if (navLink && (isMobile() || isMid())) setPane('list');
    const toggle = e.target.closest('[data-toggle]');
    if (toggle && toggle.tagName !== 'INPUT') {
      e.preventDefault();
      const el = $(toggle.dataset.toggle);
      if (el) { el.classList.toggle('hidden'); const f = el.querySelector('textarea, input'); if (f && !el.classList.contains('hidden')) f.focus(); }
    }
    // Close any open <details class="menu"> when clicking outside it.
    $$('details.menu[open]').forEach((d) => { if (!d.contains(e.target) || e.target === d) d.removeAttribute('open'); });
  });
  const THEME_COLORS = { light: '#F3F1EA', sepia: '#EFE6D2', dark: '#171614', black: '#000000' };
  function setThemeColor(theme) {
    const metas = $$('meta[name="theme-color"]');
    if (!metas.length) return;
    if (THEME_COLORS[theme]) { metas.forEach((m, i) => { if (i === 0) { m.removeAttribute('media'); m.content = THEME_COLORS[theme]; } else m.remove(); }); return; }
    // Auto: one meta per scheme, so the OS switch is honoured without a reload.
    const first = metas[0]; first.setAttribute('media', '(prefers-color-scheme: light)'); first.content = THEME_COLORS.light;
    if (metas.length < 2) { const m = document.createElement('meta'); m.name = 'theme-color'; m.setAttribute('media', '(prefers-color-scheme: dark)'); m.content = THEME_COLORS.dark; first.after(m); }
    else { metas[1].setAttribute('media', '(prefers-color-scheme: dark)'); metas[1].content = THEME_COLORS.dark; }
  }
  function applyTheme(value) {
    const html = document.documentElement;
    if (value === 'auto') html.removeAttribute('data-theme'); else html.dataset.theme = value;
    html.setAttribute('data-theme-src', value);
    setThemeColor(value);
    try { const uid = html.dataset.uid || ''; localStorage.setItem('pensieve.theme:' + uid, value); localStorage.setItem('pensieve.theme:', value); } catch (_) {}
  }

  // ---- Reading preferences: data-* on <html> drive the typography in app.css; every change is applied at once
  // and persisted through /manage/account/font. Controls carry data-pref (attribute name) + data-value (buttons)
  // or are <select>s whose value is the preference. Mirrors READING_PREF_ATTRS in templating.py. ----
  const PREF_FORM_KEYS = { font: 'font_size', measure: 'measure', face: 'font_family', leading: 'line_height', align: 'text_align', theme: 'theme' };
  const PREF_LABELS = { font: 'Text size', face: 'Font', leading: 'Line height', measure: 'Line width', align: 'Alignment', theme: 'Theme' };
  function prefValue(name) {
    const h = document.documentElement;
    return name === 'theme' ? (h.getAttribute('data-theme-src') || 'auto') : (h.dataset[name] || '');
  }
  function syncPrefControls(root) {
    $$('[data-pref]', root).forEach((el) => {
      const cur = prefValue(el.dataset.pref);
      if (el.tagName === 'SELECT') { if (el.value !== cur) el.value = cur; return; }
      const on = el.dataset.value === cur;
      el.classList.toggle('on', on);
      el.setAttribute('aria-checked', on ? 'true' : 'false');
    });
  }
  function setPref(name, value, opts) {
    if (!PREF_FORM_KEYS[name] || prefValue(name) === value) return;
    if (name === 'theme') applyTheme(value); else document.documentElement.dataset[name] = value;
    syncPrefControls();
    const body = new URLSearchParams({ [PREF_FORM_KEYS[name]]: value });
    fetch('/manage/account/font', { method: 'POST', credentials: 'same-origin', body, headers: { 'X-CSRF-Token': csrfToken() } }).catch(() => {});
    if (opts && opts.announce) announce(PREF_LABELS[name] + ': ' + value);
  }
  document.addEventListener('click', (e) => {
    const b = e.target.closest('button[data-pref]');
    if (b) { e.preventDefault(); setPref(b.dataset.pref, b.dataset.value, { announce: true }); }
  });
  document.addEventListener('change', (e) => {
    const t = e.target;
    if (t.matches('[data-toggle]')) { const el = $(t.dataset.toggle); if (el) el.classList.toggle('hidden', !t.checked); if (el && t.checked) { const f = el.querySelector('textarea'); if (f) f.focus(); } }
    if (t.matches('select[data-pref]')) setPref(t.dataset.pref, t.value);
    // File drop zones echo the chosen file name.
    if (t.matches('.file input[type="file"]')) { const name = t.closest('.file').querySelector('[data-file-name]'); if (name) name.textContent = t.files && t.files[0] ? t.files[0].name : ''; }
  });
  document.addEventListener('dragover', (e) => { const z = e.target.closest && e.target.closest('.file'); if (z) { e.preventDefault(); z.classList.add('drag-over'); } });
  document.addEventListener('dragleave', (e) => { const z = e.target.closest && e.target.closest('.file'); if (z) z.classList.remove('drag-over'); });
  document.addEventListener('drop', (e) => {
    const z = e.target.closest && e.target.closest('.file'); if (!z) return;
    e.preventDefault(); z.classList.remove('drag-over');
    const input = z.querySelector('input[type="file"]');
    if (input && e.dataTransfer && e.dataTransfer.files.length) { try { input.files = e.dataTransfer.files; } catch (_) {} input.dispatchEvent(new Event('change', { bubbles: true })); }
  });
  document.addEventListener('click', (e) => { const b = e.target.closest('[data-action="share"]'); if (b) { e.preventDefault(); share(b); } });
  // Signing out: tell the service worker to drop its cached reader pages and offline queue.
  document.addEventListener('submit', (e) => {
    const f = e.target;
    if (f && f.getAttribute && f.getAttribute('action') === '/logout' && navigator.serviceWorker && navigator.serviceWorker.controller) {
      navigator.serviceWorker.controller.postMessage({ type: 'clear' });
    }
  });

  // ---- Progress bar bound to HTMX requests ----
  let inflight = 0, progressTimer = null;
  function progressEl() { return $('#progress'); }
  function progressStart() {
    inflight++;
    const p = progressEl(); if (!p) return;
    clearTimeout(progressTimer);
    p.classList.remove('done'); void p.offsetWidth; p.classList.add('on');
  }
  function progressEnd() {
    inflight = Math.max(0, inflight - 1);
    if (inflight) return;
    const p = progressEl(); if (!p) return;
    p.classList.remove('on'); p.classList.add('done');
    progressTimer = setTimeout(() => p.classList.remove('done'), 500);
  }
  let autoOpenAt = 0;
  document.body.addEventListener('htmx:beforeRequest', (e) => {
    progressStart();
    const src = e.detail && e.detail.elt;
    if (src && src.getAttribute && /\/open$/.test(src.getAttribute('hx-post') || '')) autoOpenAt = Date.now();
    const t = e.detail && e.detail.target;
    if (t && (t.id === 'list' || t.id === 'article' || t.id === 'nav')) t.setAttribute('aria-busy', 'true');
    const elt = e.detail && e.detail.elt;
    if (elt && elt.classList && elt.classList.contains('btn')) elt.setAttribute('aria-busy', 'true');
  });
  document.body.addEventListener('htmx:afterRequest', (e) => {
    progressEnd();
    const t = e.detail && e.detail.target;
    if (t && t.removeAttribute) t.removeAttribute('aria-busy');
    const elt = e.detail && e.detail.elt;
    if (elt && elt.removeAttribute) elt.removeAttribute('aria-busy');
  });
  document.body.addEventListener('htmx:sendAbort', () => progressEnd());
  // Skeleton rows while a list pane reloads (only for full list swaps, not paging).
  document.body.addEventListener('htmx:beforeRequest', (e) => {
    const t = e.detail && e.detail.target;
    if (t && t.id === 'list' && e.detail.elt && e.detail.elt.id !== 'list-body') {
      const body = $('#list-body'); if (!body || body.children.length === 0) return;
      const sk = document.createElement('div'); sk.className = 'skeleton'; sk.setAttribute('aria-hidden', 'true');
      sk.innerHTML = '<div class="skeleton-row"><div class="lines"><span class="bar w1"></span><span class="bar w2"></span><span class="bar w3"></span></div></div>'.repeat(6);
      body.replaceChildren(sk);
    }
  });

  // ---- Toasts ----
  function announce(text) { const l = $('#live'); if (l) { l.textContent = ''; setTimeout(() => { l.textContent = text; }, 30); } }
  function toast(text, opts) {
    opts = opts || {};
    const stack = $('#toasts'); if (!stack) return null;
    const el = document.createElement('div');
    el.className = 'toast' + (opts.kind ? ' toast-' + opts.kind : '');
    const span = document.createElement('span'); span.className = 'toast-text'; span.textContent = text; el.appendChild(span);
    if (opts.node) el.appendChild(opts.node);
    if (opts.action) { const b = document.createElement('button'); b.type = 'button'; b.className = 'btn btn-sm'; b.textContent = opts.action.label; b.addEventListener('click', () => { opts.action.run(); dismiss(el); }); el.appendChild(b); }
    const x = document.createElement('button'); x.type = 'button'; x.className = 'btn btn-sm btn-icon'; x.setAttribute('aria-label', 'Dismiss');
    x.innerHTML = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" aria-hidden="true"><path d="M6 6l12 12M18 6L6 18"/></svg>';
    x.addEventListener('click', () => dismiss(el)); el.appendChild(x);
    stack.appendChild(el);
    // Keep three: drop the oldest at once, fading ones included (dismiss() skips those, so counting children and
    // dismissing the first one looped forever and froze the page once a fading toast was the oldest).
    const live = [...stack.children];
    live.slice(0, Math.max(0, live.length - 3)).forEach((t) => { t._gone = true; clearTimeout(t._timer); t.remove(); });
    if (window.htmx && opts.node) window.htmx.process(el);
    const ttl = opts.ttl || (opts.node || opts.action ? 8000 : 3500);
    el._timer = setTimeout(() => dismiss(el), ttl);
    el.addEventListener('mouseenter', () => clearTimeout(el._timer));
    el.addEventListener('mouseleave', () => { el._timer = setTimeout(() => dismiss(el), 2500); });
    return el;
  }
  function dismiss(el, now) {
    if (!el || el._gone) return; el._gone = true; clearTimeout(el._timer);
    if (now) { el.remove(); return; }
    el.classList.add('leaving'); setTimeout(() => el.remove(), 200);
  }
  window.pensieveToast = toast;
  // Server-rendered toast seeds (mark-all-read undo) become toasts after the swap.
  function consumeSeeds(root) {
    $$('.toast-seed', root).forEach((seed) => {
      const form = seed.querySelector('form');
      if (form) form.classList.add('row');
      toast(seed.dataset.toast || '', { node: form || null, ttl: form ? 10000 : 3500 });
      seed.remove();
    });
  }

  // ---- Confirm dialog (replaces hx-confirm and window.confirm) ----
  let confirmResolve = null, confirmOpener = null;
  function confirmDialog(opts) {
    const d = $('#confirm');
    if (!d || typeof d.showModal !== 'function') return Promise.resolve(window.confirm(opts.body || opts.title || 'Are you sure?'));
    return new Promise((resolve) => {
      confirmResolve = resolve; confirmOpener = document.activeElement;
      $('#confirm-title', d).textContent = opts.title || 'Are you sure?';
      $('#confirm-body', d).textContent = opts.body || '';
      const ok = $('[data-dialog-confirm]', d);
      ok.textContent = opts.label || 'Confirm';
      ok.classList.toggle('btn-danger', !!opts.danger);
      ok.classList.toggle('btn-primary', !opts.danger);
      // Prompt mode: `input` (a string, possibly empty) shows a text field; the promise resolves with its
      // trimmed value, or false on cancel.
      const inp = $('#confirm-input', d);
      const prompt = typeof opts.input === 'string';
      if (inp) { inp.classList.toggle('hidden', !prompt); inp.value = prompt ? opts.input : ''; inp.placeholder = opts.placeholder || ''; }
      d.dataset.prompt = prompt ? '1' : '';
      d.showModal();
      if (prompt && inp) { inp.focus(); inp.select(); } else ok.focus();
    });
  }
  function settleConfirm(value) {
    const d = $('#confirm');
    if (value === true && d && d.dataset.prompt === '1') { const inp = $('#confirm-input', d); value = inp ? inp.value.trim() : ''; }
    if (d && d.open) d.close();
    const r = confirmResolve; confirmResolve = null;
    const o = confirmOpener; confirmOpener = null;
    if (o && o.focus) try { o.focus({ preventScroll: true }); } catch (_) {}
    if (r) r(value);
  }
  window.pensieveConfirm = confirmDialog;
  document.addEventListener('click', (e) => {
    if (e.target.closest('[data-dialog-confirm]')) settleConfirm(true);
    else if (e.target.closest('[data-dialog-cancel], [data-dialog-close]')) settleConfirm(false);
    else { const d = e.target.closest('dialog#confirm'); if (d && e.target === d) settleConfirm(false); }
  });
  document.addEventListener('cancel', (e) => { if (e.target && e.target.id === 'confirm') { e.preventDefault(); settleConfirm(false); } }, true);
  document.addEventListener('keydown', (e) => { if (e.key === 'Enter' && e.target && e.target.id === 'confirm-input') { e.preventDefault(); settleConfirm(true); } });
  document.body.addEventListener('htmx:confirm', (e) => {
    const elt = e.detail && e.detail.elt; if (!elt) return;
    const src = elt.hasAttribute('hx-confirm') ? elt : elt.closest('[hx-confirm]');
    if (!src) return;
    const question = e.detail.question || src.getAttribute('hx-confirm');
    if (!question) return;
    e.preventDefault();
    // data-confirm-prompt adds an optional text field; its value goes out as the form's hidden "note".
    const prompt = src.dataset.confirmPrompt;
    confirmDialog({ title: src.dataset.confirmTitle, body: question, label: src.dataset.confirmLabel, danger: src.hasAttribute('data-confirm-danger'),
      input: prompt === undefined ? undefined : '', placeholder: prompt })
      .then((ok) => {
        if (ok === false || ok === undefined || ok === null) return;
        if (prompt !== undefined) { const note = src.querySelector('input[name="note"]'); if (note) note.value = typeof ok === 'string' ? ok : ''; }
        e.detail.issueRequest(true);
      });
  });

  // ---- Menus: aria-expanded, arrow keys, Escape restores focus ----
  document.addEventListener('toggle', (e) => {
    const d = e.target; if (!d.matches || !d.matches('details.menu')) return;
    const s = d.querySelector(':scope > summary'); if (s) s.setAttribute('aria-expanded', d.open ? 'true' : 'false');
    if (d.open) { $$('details.menu[open]').forEach((o) => { if (o !== d) o.removeAttribute('open'); }); }
  }, true);
  function menuItems(d) { return $$('.menu-item, .menu-form .input, .menu-form .btn, .reading-row .seg-btn', d).filter((el) => el.offsetParent !== null); }
  document.addEventListener('keydown', (e) => {
    const d = e.target.closest && e.target.closest('details.menu'); if (!d) return;
    const items = menuItems(d);
    if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); d.removeAttribute('open'); const s = d.querySelector(':scope > summary'); if (s) s.focus(); return; }
    if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
      if (!d.open) { d.setAttribute('open', ''); }
      if (!items.length) return;
      e.preventDefault();
      const idx = items.indexOf(document.activeElement);
      const next = e.key === 'ArrowDown' ? (idx + 1) % items.length : (idx - 1 + items.length) % items.length;
      items[next].focus();
    }
    if ((e.key === 'Home' || e.key === 'End') && items.length && d.open) { e.preventDefault(); items[e.key === 'Home' ? 0 : items.length - 1].focus(); }
  });
  document.addEventListener('focusout', (e) => {
    const d = e.target.closest && e.target.closest('details.menu[open]'); if (!d) return;
    const to = e.relatedTarget; if (to && d.contains(to)) return;
    setTimeout(() => { if (!d.contains(document.activeElement)) d.removeAttribute('open'); }, 0);
  });

  // ---- Article toolbar lives in the pane head; a full-page render leaves it inside the article ----
  function relocateToolbar() {
    const head = $('.article-head #article-toolbar'); if (!head) return;
    const inner = $('#article #article-toolbar');
    if (inner) { head.replaceChildren(...inner.childNodes); inner.remove(); if (window.htmx) window.htmx.process(head); }
    else if (!currentArticle()) head.replaceChildren();
  }

  // ---- Selection (roving tabindex; only the selected row is tabbable) ----
  function rows() { return $$('#list-body .item'); }
  function selected() { return $('#list-body .item.selected'); }
  function syncTabindex() {
    const all = rows(); if (!all.length) return;
    const cur = selected() || all[0];
    all.forEach((r) => { const on = r === cur; r.tabIndex = on ? 0 : -1; r.setAttribute('aria-selected', r.classList.contains('selected') ? 'true' : 'false'); });
  }
  function updatePos() {
    const el = $('#article-pos'); if (!el) return;
    const all = rows(); const cur = selected(); const art = currentArticle();
    if (!art || !cur || !all.length || art.dataset.id !== cur.dataset.id) { el.textContent = ''; return; }
    el.textContent = (all.indexOf(cur) + 1) + ' of ' + all.length + ($('#list-body .sentinel') ? '+' : '');
  }
  function select(row, opts) {
    opts = opts || {};
    rows().forEach((r) => { r.classList.remove('selected'); r.setAttribute('aria-selected', 'false'); r.tabIndex = -1; });
    if (!row) { syncTabindex(); updatePos(); return; }
    row.classList.add('selected'); row.setAttribute('aria-selected', 'true'); row.tabIndex = 0;
    updatePos();
    row.scrollIntoView({ block: 'nearest' });
    if (opts.focus && document.activeElement && document.activeElement.classList && document.activeElement.classList.contains('item')) row.focus({ preventScroll: true });
    if (opts.open) open(row);
  }
  function open(row) {
    if (!row) return;
    if (isMobile() && app() && app().dataset.pane === 'list') expectArticle('right');
    if (window.htmx) window.htmx.trigger(row, 'open');
    if (isMobile()) setPane('article');
  }
  function move(delta, openIt) {
    const all = rows();
    if (!all.length) return;
    const cur = selected();
    let idx = cur ? all.indexOf(cur) + delta : (delta > 0 ? 0 : all.length - 1);
    idx = Math.max(0, Math.min(all.length - 1, idx));
    select(all[idx], { open: openIt, focus: true });
    if (idx >= all.length - 3) loadMore($('#list-body .sentinel'));
  }

  // ---- Endless lists: the "Loading more…" sentinel (hx-trigger "more, click") asks for the next page once it comes
  // within a screen of view. The observer's root is whatever scrolls the list: the list pane on wider screens, the
  // page on phones (null root). A failed page leaves the sentinel in place with "Try again" instead of a spinner
  // that never ends. ----
  const moreObservers = new Map();
  function loadMore(s) {
    if (!s || !s.isConnected || s.dataset.loading || !window.htmx) return;
    s.dataset.loading = '1'; s.classList.remove('failed');
    window.htmx.trigger(s, 'more');
  }
  function scrollRoot(el) {
    for (let n = el.parentElement; n && n !== document.body; n = n.parentElement) {
      const oy = getComputedStyle(n).overflowY;
      if (oy === 'auto' || oy === 'scroll') return n;
    }
    return null;
  }
  function watchSentinels(root) {
    if (!window.IntersectionObserver) return;
    $$('.sentinel', root || document).forEach((s) => {
      if (s.dataset.watched) return;
      s.dataset.watched = '1';
      const box = scrollRoot(s);
      let io = moreObservers.get(box);
      if (!io) {
        // One shot per sentinel: success replaces it, and a failure waits for "Try again" rather than looping.
        io = new IntersectionObserver((entries) => entries.forEach((en) => { if (en.isIntersecting) { io.unobserve(en.target); loadMore(en.target); } }), { root: box, rootMargin: '0px 0px 100% 0px' });
        moreObservers.set(box, io);
      }
      io.observe(s);
    });
  }
  // A pane that scrolls on one layout and not on another (rotation, window resize): re-pick the roots.
  mqMobile.addEventListener('change', () => {
    moreObservers.forEach((io) => io.disconnect()); moreObservers.clear();
    $$('.sentinel[data-watched]').forEach((s) => { delete s.dataset.watched; });
    watchSentinels();
  });
  document.body.addEventListener('htmx:load', (e) => watchSentinels(e.detail && e.detail.elt && e.detail.elt.parentElement));
  document.body.addEventListener('htmx:afterRequest', (e) => {
    const s = e.detail && e.detail.elt;
    if (!s || !s.classList || !s.classList.contains('sentinel') || e.detail.successful) return;
    // Still on the page (the swap never happened): offer a retry; a click on it is the sentinel's own trigger.
    delete s.dataset.loading; s.classList.add('failed');
    const t = s.querySelector('.sentinel-text'); if (t) t.textContent = ' Couldn\'t load more.';
  });
  function currentArticle() { return $('#article article.article'); }
  function articleButton(action) { return $('#article-toolbar [data-action="' + action + '"]') || (currentArticle() ? currentArticle().querySelector('[data-action="' + action + '"]') : null); }
  function selectionMatchesArticle() { const a = currentArticle(); const s = selected(); return a && s && a.dataset.id === s.dataset.id; }
  function csrfToken() { const m = $('meta[name="csrf-token"]'); return m ? m.content : ''; }
  function postState(path) {
    return fetch(path, { method: 'POST', credentials: 'same-origin', headers: { 'X-CSRF-Token': csrfToken(), 'HX-Request': 'true' } });
  }
  // Read / star toggle for a row (the selection by default). The row flips at once and flips back if the save fails;
  // an open article showing the same item gets the refreshed toolbar the endpoint returns. opts.undo adds Undo.
  function toggleRowState(action, row, opts) {
    row = row || selected();
    if (!row) return false;
    const id = row.dataset.id, cls = action === 'star' ? 'starred' : 'read';
    const on = row.classList.contains(cls);
    const verb = action === 'star' ? (on ? 'unstar' : 'star') : (on ? 'unread' : 'read');
    const done = action === 'star' ? (on ? 'Unstarred' : 'Starred') : (on ? 'Marked unread' : 'Marked read');
    row.classList.toggle(cls, !on);
    postState('/items/' + id + '/' + verb).then((r) => {
      if (!r.ok) throw new Error(String(r.status));
      if (window.htmx) window.htmx.trigger(document.body, 'counts-changed');
      const art = currentArticle();
      if (!art || art.dataset.id !== id) return;
      art.dataset[action === 'star' ? 'starred' : 'read'] = on ? '0' : '1';
      return r.text().then((html) => {
        const bar = document.getElementById('toolbar-' + id); // empty when the offline queue took the request
        if (bar && html.trim()) { bar.innerHTML = html; if (window.htmx) window.htmx.process(bar); }
      });
    }).catch(() => { row.classList.toggle(cls, on); toast("That didn't save", { kind: 'error' }); });
    toast(done, opts && opts.undo ? { action: { label: 'Undo', run: () => toggleRowState(action, row) } } : undefined);
    announce(done);
    return true;
  }
  // s / m act on the open article when it matches the selection (or nothing is selected); otherwise on the row.
  function stateKey(action) {
    const useArticle = currentArticle() && (selectionMatchesArticle() || !selected());
    if (useArticle) { const b = articleButton(action); if (b) { b.click(); return; } }
    toggleRowState(action);
  }
  function markAllRead() {
    const form = $('#list form[action$="/mark-read"]');
    if (!form) return;
    const title = ($('#list .list-title') || {}).textContent || 'this view';
    confirmDialog({ title: 'Mark all as read?', body: 'Everything in ' + title.trim() + ' will be marked read. You can undo it right after.', label: 'Mark all read' }).then((ok) => {
      if (!ok) return;
      const everything = form.querySelector('button[name="older_than"][value=""]');
      if (everything) form.requestSubmit(everything); else form.requestSubmit();
    });
  }
  // Reading size keys (+ / -): step data-font on <html> through setPref, which also persists it.
  const FONT_SIZES = ['s', 'm', 'l', 'xl'];
  function stepFont(delta) {
    const idx = Math.max(0, FONT_SIZES.indexOf(prefValue('font') || 'm'));
    const next = FONT_SIZES[Math.max(0, Math.min(FONT_SIZES.length - 1, idx + delta))];
    if (next === prefValue('font')) return;
    setPref('font', next);
    toast('Reading text: ' + ({ s: 'small', m: 'medium', l: 'large', xl: 'extra large' })[next]);
  }
  function share(btn) {
    const url = btn.dataset.shareUrl, title = btn.dataset.shareTitle || document.title;
    if (!url) return;
    if (navigator.share) { navigator.share({ title, url }).catch(() => {}); return; }
    const done = () => toast('Link copied');
    if (navigator.clipboard && navigator.clipboard.writeText) navigator.clipboard.writeText(url).then(done, () => window.prompt('Copy this link', url));
    else window.prompt('Copy this link', url);
  }

  // Keep row markers in sync with server-side state changes (HX-Trigger: item-state).
  document.body.addEventListener('item-state', (e) => {
    const d = e.detail || {};
    const row = d.id ? document.getElementById('item-' + d.id) : null;
    if (row) {
      if (typeof d.read === 'boolean') row.classList.toggle('read', d.read);
      if (typeof d.starred === 'boolean') row.classList.toggle('starred', d.starred);
    }
    const art = currentArticle();
    if (art && art.dataset.id === d.id) {
      if (typeof d.read === 'boolean') art.dataset.read = d.read ? '1' : '0';
      if (typeof d.starred === 'boolean') art.dataset.starred = d.starred ? '1' : '0';
    }
    if (typeof d.starred === 'boolean') { toast(d.starred ? 'Starred' : 'Unstarred'); announce(d.starred ? 'Starred' : 'Unstarred'); }
    else if (typeof d.read === 'boolean' && !(d.read && Date.now() - autoOpenAt < 2000)) { toast(d.read ? 'Marked read' : 'Marked unread'); announce(d.read ? 'Marked read' : 'Marked unread'); }
  });
  document.body.addEventListener('list-changed', () => toast('Split from its story'));
  // Feedback for actions whose responses carry no HX-Trigger: summarize, tags, notes, reader mode.
  document.body.addEventListener('htmx:afterRequest', (e) => {
    const elt = e.detail && e.detail.elt; const xhr = e.detail && e.detail.xhr; if (!elt || !xhr) return;
    const path = (e.detail.pathInfo && e.detail.pathInfo.requestPath) || '';
    if (xhr.status >= 400) { toast(xhr.status === 403 ? 'Your session expired. Reload to sign in again.' : 'That didn\'t work (' + xhr.status + ')', { kind: 'error' }); return; }
    if (/\/summarize$/.test(path)) toast('Summary requested', { kind: 'ai' });
    else if (/\/rewrite$/.test(path)) toast('Rewrite requested; your note is kept as a correction', { kind: 'ai' });
    else if (/\/tag$/.test(path)) { const op = elt.querySelector && elt.querySelector('[name="op"]'); toast(op && op.value === 'remove' ? 'Tag removed' : 'Tag added'); }
    else if (/\/note$/.test(path)) { const del = elt.querySelector && elt.querySelector('[name="delete"]'); toast(del ? 'Note deleted' : 'Note saved'); }
    else if (/\/reader-mode$/.test(path)) {
      // Read what was sent, not elt: the swap replaces the body the request came from, so htmx reports the target,
      // whose new content always holds a "Show feed version" form. The article's own on-load request (auto) gets
      // no toast: it is not something the reader asked for.
      const sent = e.detail.requestConfig && e.detail.requestConfig.parameters;
      const param = (k) => (sent && typeof sent.get === 'function' ? sent.get(k) : sent && sent[k]);
      if (param('auto')) return;
      toast(param('off') ? 'Showing the feed version' : 'Reader view');
    }
    else if (/\/skip-read$/.test(path)) toast('Marked those as read');
    else if (/\/move$/.test(path) && /\/manage\/feeds\//.test(path)) toast('Feed moved');
  });
  document.body.addEventListener('htmx:afterSwap', (e) => {
    const t = e.detail && e.detail.target;
    if (!t) return;
    consumeSeeds(t);
    if (t.id === 'article') {
      relocateToolbar();
      const art = currentArticle();
      if (art) { const row = document.getElementById('item-' + art.dataset.id); if (row && !row.classList.contains('selected')) select(row); }
      updatePos();
      if (isMobile()) setPane('article');
    }
    if (t.id === 'list' || t.id === 'list-body' || t.closest('#list-body')) { syncTabindex(); updatePos(); }
  });
  document.body.addEventListener('htmx:afterSettle', () => { relocateToolbar(); syncPrefControls(); });
  document.body.addEventListener('htmx:responseError', (e) => {
    const xhr = e.detail && e.detail.xhr;
    if (xhr && (xhr.status === 401 || xhr.status === 403)) window.location.reload();
  });
  document.body.addEventListener('htmx:sendError', () => toast("You're offline. You can keep reading; changes will sync when you're back.", { kind: 'error' }));

  // ---- Mobile article footer: previous / mark read + next / note ----
  document.addEventListener('click', (e) => {
    const b = e.target.closest('[data-article-nav]'); if (!b) return;
    e.preventDefault();
    const what = b.dataset.articleNav;
    if (what === 'prev') move(-1, true);
    else if (what === 'note' || what === 'star' || what === 'share') { const n = articleButton(what); if (n) n.click(); }
    else if (what === 'read-next') {
      const art = currentArticle();
      if (art && art.dataset.read !== '1') { const r = articleButton('read'); if (r) r.click(); }
      const all = rows(); const cur = selected();
      if (cur && all.indexOf(cur) === all.length - 1) { toast('That was the last item'); setPane('list'); return; }
      move(1, true);
    }
  });

  // The footer's star follows the open article (starred from the footer, the s key, a row swipe or another device),
  // and share shows only for items with a link. A star made here pops; one already there on opening does not.
  let footSeen = { id: null, on: false }, footPane = null;
  const footObserver = window.MutationObserver ? new MutationObserver(() => syncFoot()) : null;
  function syncFoot() {
    const star = $('.article-foot [data-article-nav="star"]'); if (!star) return;
    const pane = $('#article');
    if (footObserver && pane && pane !== footPane) { // a boosted page brings a new pane
      footObserver.disconnect(); footPane = pane;
      footObserver.observe(pane, { subtree: true, attributes: true, attributeFilter: ['data-starred'] });
    }
    const art = currentArticle();
    const id = art ? art.dataset.id : null, on = !!art && art.dataset.starred === '1';
    star.hidden = !articleButton('star');
    star.setAttribute('aria-pressed', on ? 'true' : 'false');
    star.setAttribute('aria-label', on ? 'Starred, tap to unstar' : 'Star');
    if (on && id === footSeen.id && !footSeen.on) { star.classList.remove('pop'); void star.offsetWidth; star.classList.add('pop'); }
    footSeen = { id, on };
    const sh = $('.article-foot [data-article-nav="share"]'); if (sh) sh.hidden = !articleButton('share');
  }
  document.addEventListener('animationend', (e) => { if (e.target.closest && e.target.closest('.foot-star')) e.target.closest('.foot-star').classList.remove('pop'); });
  document.body.addEventListener('htmx:afterSettle', syncFoot);
  document.body.addEventListener('item-state', () => setTimeout(syncFoot));
  syncFoot();

  // ---- Phones: every screen's top and bottom bars slide away while reading down, and return on the way back up ----
  // On phones the document scrolls (web.css), so the browser's own toolbar shrinks along with ours. The bars also
  // come back at the top and the bottom of the page, on a new article or pane, and whenever they take focus.
  const bars = { y: 0, travel: 0, timer: null, observer: null, watched: [] };
  const TOP_BARS = '.topbar, .pane-head', BOTTOM_BARS = '.tabbar, .article-foot';
  function showBars(show) {
    const a = app(); if (!a) return;
    if (show) {
      if (!a.dataset.bars) return;
      clearTimeout(bars.timer);
      if (a.dataset.bars === 'gone') { a.dataset.bars = 'away'; void a.offsetHeight; } // back in the page, off-screen
      delete a.dataset.bars; // ...then slide in
    } else {
      if (a.dataset.bars) return;
      a.dataset.bars = 'away'; // slide out, then leave the page so Safari stops tinting its edges with them
      bars.timer = setTimeout(() => { if (a.dataset.bars === 'away') a.dataset.bars = 'gone'; }, 340);
    }
  }
  // The padding under the floating bars matches the bars on screen (list head, article head, tab bar, footer).
  function measureBars() {
    const a = app(); if (!a || !isMobile() || a.dataset.bars) return; // keep the last size while they are away
    const tallest = (sel) => Math.max(0, ...$$(sel, a).map((e) => (e.getClientRects().length ? e.offsetHeight : 0)));
    a.style.setProperty('--bar-top', tallest(TOP_BARS) + 'px');
    a.style.setProperty('--bar-bottom', tallest(BOTTOM_BARS) + 'px');
  }
  function watchBars() {
    const a = app(); if (!a || !window.ResizeObserver) return;
    const els = $$(TOP_BARS + ', ' + BOTTOM_BARS, a);
    if (els.length === bars.watched.length && els.every((e, i) => e === bars.watched[i])) return;
    if (!bars.observer) bars.observer = new ResizeObserver(() => measureBars());
    bars.observer.disconnect();
    els.forEach((e) => bars.observer.observe(e));
    bars.watched = els;
    measureBars();
  }
  document.addEventListener('htmx:afterSettle', watchBars);
  window.addEventListener('resize', measureBars);
  watchBars();
  // Scroll without the jump counting as "reading down".
  function scrollPageTo(y) { bars.y = y; bars.travel = 0; window.scrollTo(0, y); }
  window.addEventListener('scroll', () => {
    if (!isMobile()) return;
    const y = window.scrollY, dy = y - bars.y;
    bars.y = y;
    const end = document.documentElement.scrollHeight - window.innerHeight;
    // Top (and iOS's rubber band above it) or the last screenful: always show; the article's "next" lives here.
    if (y <= 8 || y >= end - 48) { bars.travel = 0; showBars(true); return; }
    if ((dy > 0) !== (bars.travel > 0)) bars.travel = 0; // direction changed: start counting again
    bars.travel += dy;
    if (bars.travel > 24) {
      // Never pull the bars out from under an open menu or a focused control.
      const f = document.activeElement;
      if ($('details.menu[open]') || $('dialog[open]') || (f && f.closest && f.closest('.topbar, .pane-head, .tabbar, .article-foot'))) return;
      showBars(false);
    } else if (bars.travel < -24) showBars(true);
  }, { passive: true });
  document.addEventListener('focusin', (e) => { if (e.target.closest && e.target.closest('.topbar, .pane-head, .tabbar, .article-foot')) showBars(true); });
  document.body.addEventListener('htmx:afterSwap', (e) => {
    if (!(e.detail && e.detail.target && e.detail.target.id === 'article')) return;
    showBars(true);
    if (isMobile() && app() && app().dataset.pane === 'article') scrollPageTo(0);
  });

  // ---- Keyboard hints (the keybar, the search box's "/"): only while Ctrl or ⌘ is held on its own ----
  // A short delay keeps them from flashing during ⌘C or Ctrl+F; any other key, releasing, or leaving the tab hides them.
  let keysTimer = null;
  function showKeys(on) {
    clearTimeout(keysTimer); keysTimer = null;
    document.documentElement.classList.toggle('keys-shown', on);
  }
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Meta' || e.key === 'Control') {
      if (!e.repeat && !keysTimer) keysTimer = setTimeout(() => showKeys(true), 250);
      return;
    }
    showKeys(false);
  });
  document.addEventListener('keyup', (e) => { if (e.key === 'Meta' || e.key === 'Control') showKeys(false); });
  window.addEventListener('blur', () => showKeys(false));
  document.addEventListener('visibilitychange', () => showKeys(false));

  // ---- Keyboard ----
  let pendingG = false, pendingTimer = null;
  function typing(e) {
    const t = e.target;
    return t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA' || t.tagName === 'SELECT' || t.isContentEditable);
  }
  function shortcuts() { return $('#shortcuts'); }
  function toggleShortcuts(force) {
    const d = shortcuts(); if (!d) return;
    const openIt = force === undefined ? !d.open : force;
    if (openIt && !d.open) d.showModal(); else if (!openIt && d.open) d.close();
  }
  document.addEventListener('click', (e) => { if (e.target.closest('[data-close-shortcuts]')) toggleShortcuts(false); });
  // ---- Generic dialogs (the paper's Customize panel), section reorder, expand-all and unfold ----
  document.addEventListener('click', (e) => {
    const open = e.target.closest('[data-open-dialog]');
    if (open) { const d = $(open.dataset.openDialog); if (d && typeof d.showModal === 'function' && !d.open) d.showModal(); return; }
    const dismiss = e.target.closest('[data-dialog-dismiss]');
    if (dismiss) { const d = dismiss.closest('dialog'); if (d) d.close(); return; }
    const move = e.target.closest('[data-move]');
    if (move) {
      const row = move.closest('.section-row'); if (!row) return;
      if (move.dataset.move === 'up' && row.previousElementSibling) row.parentElement.insertBefore(row, row.previousElementSibling);
      else if (move.dataset.move === 'down' && row.nextElementSibling) row.parentElement.insertBefore(row.nextElementSibling, row);
      move.focus();
      return;
    }
    const expand = e.target.closest('[data-expand]');
    if (expand) {
      const sec = $(expand.dataset.expand); if (!sec) return;
      const rows = $$('details.pstory', sec);
      const anyClosed = rows.some((d) => !d.open);
      rows.forEach((d) => { d.open = anyClosed; });
      const label = expand.querySelector('.btn-label'); if (label) label.textContent = anyClosed ? 'Collapse all' : 'Expand all';
      return;
    }
    const unfold = e.target.closest('[data-unfold]');
    if (unfold) { const sec = $(unfold.dataset.unfold); if (sec) sec.classList.add('unfolded'); }
  });
  // ---- Save a link ----
  function openSaveDialog() {
    const d = $('#save-dialog'); if (!d || typeof d.showModal !== 'function') return;
    if (!d.open) d.showModal();
    const f = $('#save-url', d); if (f) { f.focus(); f.select(); }
  }
  // Offline: when the Saved list is on screen, have the service worker cache those articles.
  function warmSaved() {
    if (!location.pathname.startsWith('/reader/saved')) return;
    const sw = navigator.serviceWorker && navigator.serviceWorker.controller; if (!sw) return;
    // The exact URL a row opens (GET /items/<id> is pure; marking read is a separate POST), so offline hits.
    const urls = $$('#list .item[data-id]').slice(0, 30).map((el) => '/items/' + el.dataset.id);
    if (urls.length) sw.postMessage({ type: 'warm', urls });
  }
  document.body.addEventListener('htmx:afterSettle', (e) => { if (e.target && e.target.id === 'list') warmSaved(); });
  window.addEventListener('load', () => setTimeout(warmSaved, 1500));
  document.body.addEventListener('toast', (e) => { const d = (e.detail && e.detail.value) || e.detail || {}; if (d.text) toast(d.text); });
  document.body.addEventListener('link-saved', (e) => {
    const d = $('#save-dialog');
    const detail = (e.detail && e.detail.value) || e.detail || {};
    toast(detail.created === false ? 'Already saved: moved back to the top of My list' : 'Saved. Capturing the page…');
    if (d) { const form = $('form', d); if (form) form.reset(); const r = $('#save-result', d); if (r) r.innerHTML = ''; if (d.open) d.close(); }
    if (location.pathname.startsWith('/reader/saved') && window.htmx) window.htmx.trigger(document.body, 'refresh-list');
  });
  // The paper comes back whole after a story is read or removed (fresh counts): keep open (and unfolded) what was.
  let paperOpen = null;
  document.body.addEventListener('htmx:beforeSwap', (e) => {
    const t = e.detail && e.detail.target;
    if (t && t.id === 'insight') paperOpen = [
      ...$$('details.pstory[open], details.pbrief[open]', t).map((d) => d.id || 'brief:' + (d.closest('.psection') || {}).id),
      ...$$('.psection.unfolded', t).map((s) => 'unfold:' + s.id),
    ];
  });
  document.body.addEventListener('htmx:afterSwap', (e) => {
    const t = e.detail && e.detail.target;
    if (!t || t.id !== 'insight' || !paperOpen) return;
    paperOpen.forEach((k) => {
      if (k.startsWith('unfold:')) { const sec = document.getElementById(k.slice(7)); if (sec) sec.classList.add('unfolded'); return; }
      const d = k.startsWith('brief:') ? $('#' + CSS.escape(k.slice(6)) + ' details.pbrief') : document.getElementById(k);
      if (d) d.open = true;
    });
    paperOpen = null;
  });
  // Swapping the whole paper in replaces every node, which defeats the browser's scroll anchoring: whatever sat below
  // a story read or removed jumped up by that story's height (a whole expanded story). Keep the reader's place by
  // hand: the first story or section still shown from the one acted on onwards lands where that one stood on screen.
  // Registered after the reopen above, so the reopened stories count in the layout.
  let paperPlace = null;
  document.body.addEventListener('htmx:beforeSwap', (e) => {
    const t = e.detail && e.detail.target, cfg = e.detail && e.detail.requestConfig;
    const elt = cfg && cfg.elt;
    paperPlace = null;
    if (!t || t.id !== 'insight' || !elt || !elt.closest) return;
    const start = elt.closest('.pstory[id]') || elt.closest('.psection[id]');
    if (!start) return;
    const after = $$('.pstory[id], .psection[id]', t).filter((n) => n === start || (start.compareDocumentPosition(n) & Node.DOCUMENT_POSITION_FOLLOWING));
    paperPlace = { ids: after.map((n) => n.id), top: start.getBoundingClientRect().top };
  });
  function keepPaperPlace() {
    if (!paperPlace) return;
    const n = paperPlace.ids.map((id) => document.getElementById(id)).find((el) => el && el.getClientRects().length);
    if (!n) return;
    const dy = n.getBoundingClientRect().top - paperPlace.top;
    if (Math.abs(dy) < 1) return;
    const box = scrollRoot(n);
    if (box) box.scrollTop += dy; else window.scrollBy(0, dy);
  }
  document.body.addEventListener('htmx:afterSwap', (e) => { if (e.detail && e.detail.target && e.detail.target.id === 'insight') keepPaperPlace(); });
  document.body.addEventListener('htmx:afterSettle', (e) => {
    if (!e.detail || !e.detail.target || e.detail.target.id !== 'insight') return;
    keepPaperPlace(); paperPlace = null;
  });
  // A chip jump must land the section heading below the pinned chips, however many rows they wrap to.
  let navObserver = null;
  function watchPaperNav() {
    const nav = $('.paper-nav'), box = $('#insight');
    if (!nav || !box || !window.ResizeObserver) return;
    if (!navObserver) navObserver = new ResizeObserver(() => box.style.setProperty('--paper-nav-h', nav.offsetHeight + 'px'));
    navObserver.disconnect();
    navObserver.observe(nav);
  }
  document.addEventListener('htmx:afterSettle', watchPaperNav);
  watchPaperNav();
  document.body.addEventListener('story-removed', () => toast('Removed from today\'s paper; still unread in Reader'));
  document.body.addEventListener('paper-read', () => toast('Marked read'));
  document.body.addEventListener('paper-section-read', () => toast('Section marked read'));
  // Back onto a reader page restores htmx's snapshot of it, taken before whatever was read since (in the paper, in
  // another tab): bring the rows' read and star marks and the counts up to date in place. Refetching the list
  // instead would drop the pages loaded below and the reader's place in them.
  // htmx snapshots the page's markup and the window's scroll, not a pane's own: keep the list pane's in the markup.
  document.body.addEventListener('htmx:beforeHistorySave', () => {
    const lb = $('#list-body'); if (lb) lb.dataset.scrollTop = String(Math.round(lb.scrollTop));
  });
  document.body.addEventListener('htmx:historyRestore', () => {
    const lb = $('#list-body');
    if (lb && lb.dataset.scrollTop) { lb.scrollTop = Number(lb.dataset.scrollTop); delete lb.dataset.scrollTop; }
    const shown = $$('#list-body .item[data-id]');
    if (!shown.length) return;
    if (window.htmx) window.htmx.trigger(document.body, 'counts-changed');
    fetch('/reader/states?ids=' + shown.slice(0, 500).map((r) => r.dataset.id).join(','), { credentials: 'same-origin' })
      .then((r) => (r.ok ? r.json() : null))
      .then((st) => {
        if (!st) return;
        const read = new Set(st.read), starred = new Set(st.starred);
        shown.forEach((r) => { r.classList.toggle('read', read.has(r.dataset.id)); r.classList.toggle('starred', starred.has(r.dataset.id)); });
      })
      .catch(() => {});
  });
  document.body.addEventListener('paper-tuned-more', () => toast('More like this: its tag and sources gained weight'));
  document.body.addEventListener('paper-tuned-less', () => toast('Less of this: its tag and sources lost weight'));
  document.body.addEventListener('paper-tuned-reset', () => toast('Tuning reset for this story'));

  document.addEventListener('keydown', (e) => {
    if (e.metaKey || e.ctrlKey || e.altKey || e.defaultPrevented) return;
    if (typing(e)) { if (e.key === 'Escape') e.target.blur(); return; }
    const c = $('#confirm'); if (c && c.open) return;
    const d = shortcuts();
    if (d && d.open) { if (e.key === 'Escape' || e.key === '?') { e.preventDefault(); toggleShortcuts(false); } return; }
    if (e.target.closest && e.target.closest('details.menu[open]') && (e.key === 'ArrowDown' || e.key === 'ArrowUp' || e.key === 'Escape')) return;
    const key = e.key;
    if (pendingG) {
      pendingG = false; clearTimeout(pendingTimer);
      if (key === 'g') { location.href = '/reader/unread'; e.preventDefault(); return; }
      if (key === 'a') { location.href = '/reader/all'; e.preventDefault(); return; }
      if (key === 's') { location.href = '/reader/starred'; e.preventDefault(); return; }
      if (key === 'b') { location.href = '/reader/saved'; e.preventDefault(); return; }
    }
    switch (key) {
      case 'j': case 'ArrowDown': if (key === 'ArrowDown' && !(e.target.classList && e.target.classList.contains('item'))) return; e.preventDefault(); move(1, key === 'j'); break;
      case 'k': case 'ArrowUp': if (key === 'ArrowUp' && !(e.target.classList && e.target.classList.contains('item'))) return; e.preventDefault(); move(-1, key === 'k'); break;
      case 'n': e.preventDefault(); move(1, false); break;
      case 'p': e.preventDefault(); move(-1, false); break;
      case 'o': case 'Enter': case ' ': {
        if ((key === 'Enter' || key === ' ') && !(e.target.classList && e.target.classList.contains('item')) && key === ' ') return;
        if (key === 'Enter' && e.target.closest && e.target.closest('a, button, summary, details')) return;
        e.preventDefault();
        const s = selected();
        if (!s) { move(1, true); break; }
        if (selectionMatchesArticle() && key === 'o') {
          const a = $('#article');
          if (a) a.innerHTML = '<div class="empty-state"><div class="empty-icon"><svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M4 5h16v14H4z"/><path d="M8 9h8M8 12h8M8 15h5"/></svg></div><div class="empty-title">Collapsed</div><p>Press <kbd>o</kbd> to open it again, or <kbd>j</kbd> for the next item.</p></div>';
          relocateToolbar();
          if (isMobile()) setPane('list');
        }
        else open(s);
        break;
      }
      case 's': e.preventDefault(); stateKey('star'); break;
      case 'm': e.preventDefault(); stateKey('read'); break;
      case 'A': e.preventDefault(); markAllRead(); break;
      case '+': case '=': e.preventDefault(); stepFont(1); break;
      case '-': case '_': e.preventDefault(); stepFont(-1); break;
      case 'v': {
        e.preventDefault();
        const a = currentArticle(); const s = selected();
        const url = (selectionMatchesArticle() || !s) && a ? a.dataset.url : (s ? s.dataset.url : '');
        if (url) window.open(url, '_blank', 'noopener');
        break;
      }
      case 'r': { e.preventDefault(); if (window.htmx) window.htmx.trigger(document.body, 'refresh-list'); break; }
      case 'x': {
        e.preventDefault();
        const s = selected();
        const pill = s ? s.querySelector('.sources') : null;
        if (pill && window.htmx) window.htmx.trigger(pill, 'expand');
        break;
      }
      case 'u': { e.preventDefault(); const b = articleButton('unmerge'); if (b) b.click(); break; }
      case 'g': pendingG = true; pendingTimer = setTimeout(() => { pendingG = false; }, 800); break;
      case '/': { e.preventDefault(); const f = $('#global-search') || $('input[name="q"]'); if (f) { f.focus(); f.select(); } break; }
      case '?': e.preventDefault(); toggleShortcuts(true); break;
      case 'b': { e.preventDefault(); openSaveDialog(); break; }
      case 'Escape': {
        const a = app();
        if (a && a.dataset.pane === 'nav' && !mqWide.matches) { setPane('list'); break; }
        if (isMobile() && a && a.dataset.pane === 'article') { setPane('list'); break; }
        $$('details.menu[open]').forEach((d2) => d2.removeAttribute('open'));
        break;
      }
      default: return;
    }
  });

  // Click selection (htmx opens the article on click).
  document.addEventListener('click', (e) => {
    const row = e.target.closest('#list-body .item');
    if (row && !e.target.closest('.sources, .cluster-list')) {
      select(row);
      if (isMobile()) { if (app() && app().dataset.pane === 'list') expectArticle('right'); setPane('article'); }
    }
  });

  // ---- Touch gestures (phones) ----
  // Rows: drag right to mark read or unread, left to star or unstar (Mail's layout); the action arms past a threshold,
  // fires on release and offers Undo. Article: drag sideways for the next or previous item, or from the left edge
  // back to the list. List: from the left edge, the folders; folders: drag left to close. web.css sets touch-action:
  // pan-y on these surfaces, so vertical scrolling and pinch zoom stay the browser's: a pan it claims arrives here as
  // pointercancel. Nothing runs while the page is pinch-zoomed (horizontal panning then belongs to the zoom).
  const EDGE = 28, SLOP = 10;
  const NO_DRAG = 'input, textarea, select, [contenteditable="true"], details.menu, dialog, .ctxmenu, .ctx-backdrop, .sources, .cluster-expansion, .tags-row, .chip-row';
  const ICONS = {
    read: '<path d="M5 12.5l4.5 4.5L19 7"/>',
    unread: '<circle cx="12" cy="12" r="5" fill="currentColor" stroke="none"/>',
    star: '<path d="M12 3.5l2.6 5.4 5.9.8-4.3 4.1 1.1 5.9L12 16.9l-5.3 2.8 1.1-5.9L3.5 9.7l5.9-.8z" fill="currentColor"/>',
    unstar: '<path d="M12 3.5l2.6 5.4 5.9.8-4.3 4.1 1.1 5.9L12 16.9l-5.3 2.8 1.1-5.9L3.5 9.7l5.9-.8z"/>',
    back: '<path d="M15 5l-7 7 7 7"/>',
    forward: '<path d="M9 5l7 7-7 7"/>',
    list: '<path d="M4 6h16M4 12h16M4 18h16"/>',
  };
  const svg = (name, size) => '<svg width="' + size + '" height="' + size + '" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' + ICONS[name] + '</svg>';
  const zoomed = () => !!(window.visualViewport && window.visualViewport.scale > 1.01);
  let drag = null, swallowClickUntil = 0, pendingEnter = null, enterFallback = null;

  function scrollsSideways(el, stop) {
    for (let n = el; n && n !== stop; n = n.parentElement) {
      if (n.scrollWidth > n.clientWidth + 1) { const ox = getComputedStyle(n).overflowX; if (ox === 'auto' || ox === 'scroll') return true; }
    }
    return false;
  }
  function haptic() { if (navigator.vibrate) { try { navigator.vibrate(8); } catch (_) {} } }
  function rowTitle(row) { const t = row && row.querySelector('.item-title'); return t ? t.textContent.trim() : ''; }
  function neighbour(delta) {
    const all = rows(), cur = selected();
    if (!cur || !selectionMatchesArticle()) return null;
    return all[all.indexOf(cur) + delta] || null;
  }

  // The coloured strip under a row that is being dragged: read or unread on the left, star or unstar on the right.
  function rowUnder(row) {
    let u = $('#swipe-under');
    if (!u) { u = document.createElement('div'); u.id = 'swipe-under'; u.className = 'swipe-under'; u.setAttribute('aria-hidden', 'true'); }
    const read = row.classList.contains('read'), starred = row.classList.contains('starred');
    u.innerHTML = '<span class="swipe-act swipe-act-read">' + svg(read ? 'unread' : 'read', 22) + '<span>' + (read ? 'Unread' : 'Read') + '</span></span>'
      + '<span class="swipe-act swipe-act-star"><span>' + (starred ? 'Unstar' : 'Star') + '</span>' + svg(starred ? 'unstar' : 'star', 22) + '</span>';
    const body = row.parentElement;
    if (u.parentElement !== body) body.appendChild(u);
    const top = row.getBoundingClientRect().top - body.getBoundingClientRect().top;
    u.style.top = top + 'px'; u.style.height = row.offsetHeight + 'px';
    delete u.dataset.side; delete u.dataset.armed;
    return u;
  }
  // The pill that says where a page drag goes ("Next" and its title, "List", "Folders").
  function peek(side, label, title, icon) {
    let p = $('#swipe-peek');
    if (!p) { p = document.createElement('div'); p.id = 'swipe-peek'; p.className = 'swipe-peek'; p.setAttribute('aria-hidden', 'true'); document.body.appendChild(p); }
    p.dataset.side = side; delete p.dataset.armed;
    p.innerHTML = (side === 'left' ? svg(icon, 18) : '') + '<span class="swipe-peek-text"><small>' + label + '</small>' + (title ? '<b></b>' : '') + '</span>' + (side === 'right' ? svg(icon, 18) : '');
    if (title) p.querySelector('b').textContent = title;
    p.style.setProperty('--p', '0');
    return p;
  }
  function dropPeek() { const p = $('#swipe-peek'); if (p) p.remove(); }

  // What a drag that just picked its direction will do; null when that way leads nowhere.
  function plan(d, dx) {
    const w = window.innerWidth;
    if (d.kind === 'row') {
      d.under = rowUnder(d.el); d.threshold = Math.min(112, w * 0.3);
      d.el.classList.add('swiping');
      return true;
    }
    if (d.kind === 'back') { if (dx < 0) return false; d.max = w; d.threshold = w * 0.3; d.peek = peek('left', 'Back to', 'the list', 'back'); return true; }
    if (d.kind === 'folders') { if (dx < 0) return false; d.max = w * 0.5; d.threshold = w * 0.22; d.peek = peek('left', 'Open', 'Folders', 'list'); return true; }
    if (d.kind === 'close-nav') { if (dx > 0) return false; d.max = w; d.threshold = w * 0.3; d.peek = peek('right', 'Back to', 'the list', 'forward'); return true; }
    if (d.kind === 'page') {
      d.threshold = Math.min(140, w * 0.3);
      d.next = neighbour(1); d.prev = neighbour(-1);
      const more = $('#list-body .sentinel');
      if (!d.next) loadMore(more); // ready for the next drag
      d.atEnd = !d.next && !more && selectionMatchesArticle();
      return true;
    }
    return false;
  }
  function rubber(x, limit) { const a = Math.abs(x); return Math.sign(x) * (a <= limit ? a : limit + (a - limit) * 0.25); }
  function setArmed(d, on) {
    if (d.armed === on) return;
    d.armed = on; if (on) haptic();
    const target = d.kind === 'row' ? d.under : d.peek;
    if (target) { if (on) target.dataset.armed = ''; else delete target.dataset.armed; }
  }
  function track(d, dx) {
    d.dx = dx;
    d.el.style.transition = 'none'; // a settle still running from the last drag must not smooth this one
    const w = window.innerWidth;
    if (d.kind === 'row') {
      const x = rubber(dx, w * 0.6);
      d.el.style.transform = 'translateX(' + x + 'px)';
      d.under.dataset.side = dx > 0 ? 'read' : 'star';
      d.under.style.setProperty('--p', String(Math.min(1, Math.abs(dx) / d.threshold)));
      setArmed(d, Math.abs(dx) >= d.threshold);
      return;
    }
    if (d.kind === 'page') {
      // Which way now: swap the pill when the finger crosses back over the start.
      const dir = dx < 0 ? 1 : -1;
      if (dir !== d.dir) {
        d.dir = dir; d.armed = false;
        const to = dir > 0 ? d.next : d.prev;
        if (to) d.peek = peek(dir > 0 ? 'right' : 'left', dir > 0 ? 'Next' : 'Previous', rowTitle(to), dir > 0 ? 'forward' : 'back');
        else if (dir > 0 && d.atEnd) d.peek = peek('right', 'That was the last item', 'Back to the list', 'list');
        else { d.peek = null; dropPeek(); }
      }
      const open = dir > 0 ? (d.next || d.atEnd) : d.prev;
      const x = open ? dx : rubber(dx, 0) * 0.6;
      d.el.style.transform = 'translateX(' + x + 'px)';
      d.el.style.opacity = String(1 - Math.min(0.5, Math.abs(x) / w * 0.6));
      if (d.peek) d.peek.style.setProperty('--p', String(Math.min(1, Math.abs(dx) / d.threshold)));
      setArmed(d, !!open && Math.abs(dx) >= d.threshold);
      return;
    }
    // back / folders / close-nav: the pane follows the finger one way only.
    const x = d.kind === 'close-nav' ? Math.min(0, dx) : Math.max(0, Math.min(d.max, dx));
    d.el.style.transform = 'translateX(' + x + 'px)';
    d.el.style.opacity = String(1 - Math.min(0.4, Math.abs(x) / w * 0.5));
    d.peek.style.setProperty('--p', String(Math.min(1, Math.abs(x) / d.threshold)));
    setArmed(d, Math.abs(x) >= d.threshold);
  }
  function settle(el, then) {
    el.style.transition = 'transform 0.28s cubic-bezier(0.2, 0.8, 0.2, 1), opacity 0.28s';
    el.style.transform = ''; el.style.opacity = '';
    let done = false;
    const end = () => { if (done) return; done = true; el.style.transition = ''; if (then) then(); };
    el.addEventListener('transitionend', end, { once: true });
    setTimeout(end, 340);
  }
  function slideOut(el, toRight, then) {
    el.style.transition = 'transform 0.18s cubic-bezier(0.4, 0, 1, 1), opacity 0.18s';
    el.style.transform = 'translateX(' + (toRight ? '' : '-') + '100%)'; el.style.opacity = '0';
    setTimeout(then, 170);
  }
  function resetEl(el) { if (el) { el.style.transition = ''; el.style.transform = ''; el.style.opacity = ''; } }
  // Phones: an article opened from the list stays hidden until it arrives, then slides in (never the last one first).
  function expectArticle(from) {
    const el = $('#article'); if (!el) return;
    pendingEnter = from; el.style.transition = 'none'; el.style.transform = ''; el.style.opacity = '0';
    clearTimeout(enterFallback);
    enterFallback = setTimeout(() => { if (pendingEnter) { pendingEnter = null; settle(el); } }, 2500);
  }
  // Cancel a drag in progress (the system Back arrived mid-gesture, a menu opened) and put everything back.
  function leaveDrag() {
    const d = drag; drag = null;
    if (!d || !d.live) return;
    dropPeek();
    if (d.kind === 'row') { d.el.classList.remove('swiping'); resetEl(d.el); if (d.under) d.under.remove(); } else resetEl(d.el);
  }
  function finish(d, commit) {
    dropPeek();
    swallowClickUntil = Date.now() + 400;
    if (d.kind === 'row') {
      const action = commit ? (d.dx > 0 ? 'read' : 'star') : null;
      if (action) toggleRowState(action, d.el, { undo: true });
      settle(d.el, () => {
        const next = drag && drag.kind === 'row' && drag.live ? drag : null; // a new drag started meanwhile
        if (!next || next.el !== d.el) d.el.classList.remove('swiping');
        if (!next && d.under) d.under.remove(); // otherwise the strip already sits under the new row
      });
      return;
    }
    if (!commit) { settle(d.el); return; }
    if (d.kind === 'page') {
      const dir = d.dx < 0 ? 1 : -1;
      if (dir > 0 && !d.next) { slideOut(d.el, false, () => { toast('That was the last item'); setPane('list'); resetEl(d.el); }); return; }
      pendingEnter = dir > 0 ? 'right' : 'left';
      slideOut(d.el, dir < 0, () => {
        move(dir, true);
        // Nothing came back (offline, an error): bring the article back rather than leave a blank page.
        clearTimeout(enterFallback);
        enterFallback = setTimeout(() => { if (pendingEnter) { pendingEnter = null; settle(d.el); } }, 5000);
      });
      return;
    }
    if (d.kind === 'back') { slideOut(d.el, true, () => { setPane('list'); resetEl(d.el); }); return; }
    if (d.kind === 'folders') { setPane('nav'); resetEl(d.el); return; }
    if (d.kind === 'close-nav') slideOut(d.el, false, () => { setPane('list'); resetEl(d.el); });
  }

  document.addEventListener('pointerdown', (e) => {
    if (e.pointerType === 'mouse' || !e.isPrimary || !isMobile() || zoomed()) return;
    if (drag) leaveDrag();
    const a = app(), t = e.target;
    if (!a || !t || !t.closest || t.closest(NO_DRAG) || $('.ctx-backdrop') || $('dialog[open]') || $('details.menu[open]')) return;
    const pane = a.dataset.pane, x = e.clientX;
    let kind = null, el = null;
    if (pane === 'article' && t.closest('#article-pane') && !t.closest('.article-foot')) {
      el = $('#article');
      if (x < EDGE) kind = 'back';
      else if (t.closest('#article') && currentArticle() && !scrollsSideways(t, el)) kind = 'page';
    } else if (pane === 'list' && t.closest('#list')) {
      const row = t.closest('#list-body .item');
      if (x < EDGE && $('#nav')) { kind = 'folders'; el = $('#list-body'); }
      else if (row) { kind = 'row'; el = row; }
    } else if (pane === 'nav' && t.closest('#nav')) { kind = 'close-nav'; el = $('#nav'); }
    if (!kind || !el) return;
    drag = { kind, el, id: e.pointerId, x0: x, y0: e.clientY, dx: 0, live: false, armed: false, v: 0, lastX: x, lastT: e.timeStamp };
  }, { passive: true });

  document.addEventListener('pointermove', (e) => {
    const d = drag;
    if (!d || e.pointerId !== d.id) return;
    const dx = e.clientX - d.x0, dy = e.clientY - d.y0;
    if (!d.live) {
      if (Math.abs(dx) < SLOP && Math.abs(dy) < SLOP) return;
      // Mostly vertical: a scroll, which the browser already has. Mostly sideways: ours from here on.
      if (Math.abs(dy) > Math.abs(dx) * 0.8 || !plan(d, dx)) { drag = null; return; }
      d.live = true; d.x0 += Math.sign(dx) * SLOP;
    }
    if ($('.ctx-backdrop')) { leaveDrag(); return; } // the long-press menu opened after all
    const dt = e.timeStamp - d.lastT;
    if (dt > 0) { d.v = 0.7 * ((e.clientX - d.lastX) / dt) + 0.3 * d.v; d.lastX = e.clientX; d.lastT = e.timeStamp; }
    track(d, e.clientX - d.x0);
  }, { passive: true });

  document.addEventListener('pointerup', (e) => {
    const d = drag;
    if (!d || e.pointerId !== d.id) return;
    drag = null;
    if (!d.live) return;
    // A quick flick counts even short of the threshold, as long as it goes the way the drag went.
    const fling = Math.abs(d.v) > 0.55 && Math.abs(d.dx) > 40 && Math.sign(d.v) === Math.sign(d.dx);
    let commit = d.armed;
    if (!commit && fling) {
      if (d.kind === 'row') commit = true;
      else if (d.kind === 'page') commit = d.dx < 0 ? !!(d.next || d.atEnd) : !!d.prev;
      else commit = true;
    }
    finish(d, commit);
  }, { passive: true });
  document.addEventListener('pointercancel', (e) => {
    const d = drag;
    if (!d || e.pointerId !== d.id) return;
    drag = null;
    if (d.live) finish(d, false);
  }, { passive: true });
  // A drag never ends in a tap: swallow the click some browsers still send, so the row does not open.
  document.addEventListener('click', (e) => {
    if (Date.now() < swallowClickUntil) { e.preventDefault(); e.stopPropagation(); swallowClickUntil = 0; }
  }, true);
  // The next or previous article arrives: slide it in from the side the old one left by.
  document.body.addEventListener('htmx:afterSwap', (e) => {
    const t = e.detail && e.detail.target;
    if (!t || t.id !== 'article') return;
    clearTimeout(enterFallback);
    if (!pendingEnter) return;
    const from = pendingEnter; pendingEnter = null;
    resetEl(t);
    t.classList.remove('enter-left', 'enter-right'); void t.offsetWidth;
    t.classList.add('enter-' + from);
    t.addEventListener('animationend', () => t.classList.remove('enter-' + from), { once: true });
  });
  document.body.addEventListener('htmx:responseError', () => { if (pendingEnter) { pendingEnter = null; const el = $('#article'); if (el) settle(el); } });
  // Pinch zoom hands horizontal panning back to the browser (web.css drops touch-action while html.zoomed).
  if (window.visualViewport) window.visualViewport.addEventListener('resize', () => document.documentElement.classList.toggle('zoomed', zoomed()));

  // One-time hints, so the gestures are discoverable without a manual.
  function tipOnce(key, text) {
    try { if (localStorage.getItem('pensieve.tip.' + key)) return; localStorage.setItem('pensieve.tip.' + key, '1'); } catch (_) { return; }
    setTimeout(() => toast(text, { ttl: 7000 }), 700);
  }
  function gestureTips() {
    if (!isMobile() || !app()) return;
    const pane = app().dataset.pane;
    if (pane === 'list' && rows().length) tipOnce('rows', 'Tip: swipe a story right to mark it read, left to star it. Hold it for more.');
    else if (pane === 'article' && currentArticle() && rows().length > 1) tipOnce('article', 'Tip: swipe sideways for the next or previous story, or from the left edge back to the list.');
  }
  document.body.addEventListener('htmx:afterSettle', (e) => { if (e.target && (e.target.id === 'article' || e.target.id === 'list')) gestureTips(); });

  // ---- Drag-to-reorder lists (folders) and drag feeds between folders in the nav tree ----
  let dragging = null, draggingFeed = null;
  document.addEventListener('dragstart', (e) => {
    const feed = e.target.closest('.nav-feed[data-feed-id]');
    if (feed) { draggingFeed = feed; feed.classList.add('dragging'); e.dataTransfer.effectAllowed = 'move'; try { e.dataTransfer.setData('text/plain', feed.dataset.feedId); } catch (_) {} return; }
    const li = e.target.closest('.sortable-row'); if (!li) return; dragging = li; li.classList.add('dragging'); e.dataTransfer.effectAllowed = 'move';
  });
  document.addEventListener('dragover', (e) => {
    if (draggingFeed) { const target = e.target.closest('[data-drop-folder]'); if (target) { e.preventDefault(); e.dataTransfer.dropEffect = 'move'; target.classList.add('drop-over'); } return; }
    const li = e.target.closest('.sortable-row'); if (!li || !dragging || li === dragging) return; e.preventDefault(); li.classList.add('over');
  });
  document.addEventListener('dragleave', (e) => {
    const target = e.target.closest('[data-drop-folder]'); if (target) target.classList.remove('drop-over');
    const li = e.target.closest('.sortable-row'); if (li) li.classList.remove('over');
  });
  document.addEventListener('drop', (e) => {
    if (draggingFeed) {
      const target = e.target.closest('[data-drop-folder]'); if (!target) return;
      e.preventDefault(); target.classList.remove('drop-over');
      const body = new URLSearchParams({ folder_id: target.dataset.dropFolder || '' });
      fetch('/manage/feeds/' + draggingFeed.dataset.feedId + '/move', { method: 'POST', credentials: 'same-origin', body, headers: { 'X-CSRF-Token': csrfToken(), 'HX-Request': 'true' } })
        .then(() => { toast('Feed moved'); if (window.htmx) window.htmx.trigger(document.body, 'counts-changed'); });
      return;
    }
    const li = e.target.closest('.sortable-row'); if (!li || !dragging) return;
    e.preventDefault(); li.classList.remove('over');
    const list = li.parentElement;
    const items = Array.from(list.children);
    if (items.indexOf(dragging) < items.indexOf(li)) li.after(dragging); else li.before(dragging);
    const input = $(list.dataset.sortable); const form = $(list.dataset.submit);
    if (input) input.value = Array.from(list.querySelectorAll('.sortable-row')).map((r) => r.dataset.id).join(',');
    if (form) form.requestSubmit();
  });
  document.addEventListener('dragend', () => {
    if (dragging) dragging.classList.remove('dragging'); dragging = null;
    if (draggingFeed) draggingFeed.classList.remove('dragging'); draggingFeed = null;
    $$('[data-drop-folder].drop-over').forEach((t) => t.classList.remove('drop-over'));
  });

  // ---- Offline queue replay ----
  window.addEventListener('online', () => { toast('Back online'); if (navigator.serviceWorker && navigator.serviceWorker.controller) navigator.serviceWorker.controller.postMessage({ type: 'replay' }); });
  window.addEventListener('offline', () => toast("You're offline. You can keep reading; changes will sync when you're back.", { kind: 'error', ttl: 5000 }));

  // ---- Initial state ----
  function init() {
    relocateToolbar();
    syncTabindex();
    consumeSeeds(document);
    $$('details.menu > summary').forEach((s) => { if (!s.hasAttribute('aria-haspopup')) s.setAttribute('aria-haspopup', 'menu'); s.setAttribute('aria-expanded', s.parentElement.open ? 'true' : 'false'); });
    const art = currentArticle();
    if (art) { const row = document.getElementById('item-' + art.dataset.id); if (row) select(row); }
    updatePos();
    watchSentinels();
    gestureTips();
    // Manage section chips scroll horizontally on phones: bring the active one into view.
    const mnav = $('.manage-nav'); const active = mnav && mnav.querySelector('.nav-item.active');
    if (mnav && active && mnav.scrollWidth > mnav.clientWidth) mnav.scrollLeft = Math.max(0, active.offsetLeft - (mnav.clientWidth - active.offsetWidth) / 2);
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init); else init();
})();

// ---- Context menu: right-click on a row, a feed, a folder or the open article ----
// Shift+right-click keeps the browser's own menu. Items reuse the same endpoints the toolbar and
// Manage pages use; mark-all-read goes through htmx so its Undo toast seed is consumed as usual.
(function () {
  const $ = (s, r) => (r || document).querySelector(s);
  const $$ = (s, r) => Array.from((r || document).querySelectorAll(s));
  const toast = (t, o) => (window.pensieveToast ? window.pensieveToast(t, o) : null);
  const ask = (o) => (window.pensieveConfirm ? window.pensieveConfirm(o) : Promise.resolve(window.confirm(o.body || o.title)));
  const csrf = () => { const m = $('meta[name="csrf-token"]'); return m ? m.content : ''; };
  const post = (path, body) => fetch(path, {
    method: 'POST', credentials: 'same-origin', redirect: 'follow',
    headers: { 'X-CSRF-Token': csrf(), 'HX-Request': 'true' },
    body: body ? new URLSearchParams(Object.assign({ csrf_token: csrf() }, body)) : new URLSearchParams({ csrf_token: csrf() }),
  });
  const countsChanged = () => { if (window.htmx) window.htmx.trigger(document.body, 'counts-changed'); };
  const refreshList = () => { if (window.htmx) window.htmx.trigger(document.body, 'refresh-list'); };
  const copy = (text) => {
    const done = () => toast('Link copied');
    if (navigator.clipboard && navigator.clipboard.writeText) navigator.clipboard.writeText(text).then(done, () => toast("Couldn't copy the link"));
    else { const ta = document.createElement('textarea'); ta.value = text; document.body.appendChild(ta); ta.select(); try { document.execCommand('copy'); done(); } catch (_) { toast("Couldn't copy the link"); } ta.remove(); }
  };
  const nameOf = (el) => { const n = el.querySelector('.ellipsis, .btn-label'); return (n ? n.textContent : el.textContent).trim(); };

  const phone = () => window.matchMedia('(max-width: 899.98px)').matches;
  let menu = null, opener = null, backdrop = null, openedByTouch = 0;
  function close() {
    if (menu) { menu.remove(); menu = null; }
    if (backdrop) { backdrop.remove(); backdrop = null; }
    if (opener && opener.focus && !phone()) { try { opener.focus({ preventScroll: true }); } catch (_) {} }
    opener = null;
  }
  function build(items, x, y) {
    if (menu) { menu.remove(); menu = null; }
    if (backdrop) { backdrop.remove(); backdrop = null; }
    menu = document.createElement('div');
    menu.className = 'menu-body ctxmenu'; menu.setAttribute('role', 'menu'); menu.tabIndex = -1;
    items.forEach((it) => {
      if (it === '-') { const s = document.createElement('div'); s.className = 'menu-sep'; menu.appendChild(s); return; }
      if (it.head) { const h = document.createElement('div'); h.className = 'menu-head'; h.textContent = it.head; menu.appendChild(h); return; }
      const b = document.createElement('button');
      b.type = 'button'; b.className = 'menu-item' + (it.danger ? ' danger' : ''); b.setAttribute('role', 'menuitem');
      const label = document.createElement('span'); label.textContent = it.label; b.appendChild(label);
      if (it.hint) { const k = document.createElement('kbd'); k.textContent = it.hint; b.appendChild(k); }
      b.addEventListener('click', (e) => { e.preventDefault(); const run = it.run; close(); run(); });
      menu.appendChild(b);
    });
    if (phone()) {
      // Phones: a full-width bottom sheet with a backdrop; the touch point is irrelevant.
      menu.classList.add('sheet');
      backdrop = document.createElement('div'); backdrop.className = 'ctx-backdrop'; backdrop.setAttribute('aria-hidden', 'true');
      backdrop.addEventListener('click', (e) => { e.preventDefault(); e.stopPropagation(); close(); });
      document.body.appendChild(backdrop);
      document.body.appendChild(menu);
      menu.focus({ preventScroll: true });
      return;
    }
    document.body.appendChild(menu);
    const r = menu.getBoundingClientRect();
    menu.style.left = Math.max(4, Math.min(x, window.innerWidth - r.width - 4)) + 'px';
    menu.style.top = Math.max(4, Math.min(y, window.innerHeight - r.height - 4)) + 'px';
    const first = $('.menu-item', menu); if (first) first.focus();
  }
  document.addEventListener('keydown', (e) => {
    if (!menu) return;
    const items = $$('.menu-item', menu); const idx = items.indexOf(document.activeElement);
    if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); close(); }
    else if (e.key === 'ArrowDown') { e.preventDefault(); items[(idx + 1) % items.length].focus(); }
    else if (e.key === 'ArrowUp') { e.preventDefault(); items[(idx - 1 + items.length) % items.length].focus(); }
    else if (e.key === 'Home') { e.preventDefault(); items[0].focus(); }
    else if (e.key === 'End') { e.preventDefault(); items[items.length - 1].focus(); }
    else if (e.key === 'Tab') close();
  }, true);
  document.addEventListener('mousedown', (e) => { if (menu && !menu.contains(e.target) && !(backdrop && backdrop.contains(e.target))) close(); }, true);
  window.addEventListener('scroll', () => { if (!backdrop) close(); }, true);
  window.addEventListener('resize', () => close());
  window.addEventListener('blur', () => close());

  function markView(path, label) {
    // Same request the list's "Mark all as read" makes: the response carries the Undo toast seed.
    if (!window.htmx) { post(path).then(() => { toast('Marked read'); countsChanged(); }); return; }
    window.htmx.ajax('POST', path, { target: '#ctx-sink', swap: 'innerHTML', values: { csrf_token: csrf() }, headers: { 'X-CSRF-Token': csrf() } })
      .then(() => { countsChanged(); refreshList(); const sink = $('#ctx-sink'); if (sink && !sink.querySelector('.toast-seed') && !sink.querySelector('.toast')) toast('Marked ' + label + ' as read'); if (sink) setTimeout(() => { sink.innerHTML = ''; }, 100); });
  }
  function selectRow(row) {
    $$('#list-body .item.selected').forEach((r) => { r.classList.remove('selected'); r.setAttribute('aria-selected', 'false'); r.tabIndex = -1; });
    row.classList.add('selected'); row.setAttribute('aria-selected', 'true'); row.tabIndex = 0;
  }
  function toggle(row, action) {
    const id = row.dataset.id;
    const on = row.classList.contains(action === 'star' ? 'starred' : 'read');
    const path = '/items/' + id + '/' + (action === 'star' ? (on ? 'unstar' : 'star') : (on ? 'unread' : 'read'));
    post(path).then((r) => {
      if (!r.ok) { toast("That didn't save"); return; }
      row.classList.toggle(action === 'star' ? 'starred' : 'read', !on);
      toast(action === 'star' ? (on ? 'Unstarred' : 'Starred') : (on ? 'Marked unread' : 'Marked read'));
      countsChanged();
    });
  }
  function markAbove(row) {
    const rows = $$('#list-body .item'); const upto = rows.indexOf(row);
    const targets = rows.slice(0, upto).filter((r) => !r.classList.contains('read')).slice(0, 200);
    if (!targets.length) { toast('Nothing unread above this item'); return; }
    Promise.all(targets.map((r) => post('/items/' + r.dataset.id + '/read').then((res) => { if (res.ok) r.classList.add('read'); return res.ok; })))
      .then((oks) => { const n = oks.filter(Boolean).length; toast('Marked ' + n + (n === 1 ? ' item' : ' items') + ' above as read'); countsChanged(); });
  }
  function rowItems(row) {
    const id = row.dataset.id, url = row.dataset.url;
    const read = row.classList.contains('read'), starred = row.classList.contains('starred');
    const items = [
      { label: 'Open', hint: 'o', run: () => { if (window.htmx) window.htmx.trigger(row, 'open'); else row.click(); } },
      '-',
      { label: read ? 'Mark unread' : 'Mark read', hint: 'm', run: () => toggle(row, 'read') },
      { label: starred ? 'Unstar' : 'Star', hint: 's', run: () => toggle(row, 'star') },
      { label: 'Mark items above as read', run: () => markAbove(row) },
      '-',
      { label: 'Summarize with AI', run: () => { if (window.htmx) window.htmx.trigger(row, 'open'); setTimeout(() => { const b = $('#article-toolbar [data-action="summarize"]'); if (b && ($('#article article') || {}).dataset && $('#article article').dataset.id === id) b.click(); }, 700); } },
    ];
    if (url) {
      items.push('-', { label: 'Open original in a new tab', hint: 'v', run: () => window.open(url, '_blank', 'noopener') }, { label: 'Copy link', run: () => copy(url) });
      if (navigator.share) items.push({ label: 'Share…', run: () => navigator.share({ title: nameOf(row) || document.title, url }).catch(() => {}) });
    }
    return items;
  }
  function folderChoices() {
    const out = [];
    $$('#nav [data-drop-folder]').forEach((el) => {
      const id = el.dataset.dropFolder; const label = id ? nameOf(el) : 'Inbox (unfiled)';
      if (!out.some((o) => o.id === id)) out.push({ id, label });
    });
    return out;
  }
  function feedItems(a) {
    const id = a.dataset.feedId, name = nameOf(a) || 'this feed';
    const here = location.pathname.indexOf('/reader/feed/' + id) === 0;
    return [
      { head: name },
      { label: 'Open', run: () => { a.click(); } },
      { label: 'Mark all as read', run: () => markView('/reader/feed/' + id + '/mark-read', name) },
      { label: 'Refresh now', run: () => post('/manage/feeds/' + id + '/refresh').then((r) => toast(r.ok ? 'Fetching ' + name : "Couldn't queue a refresh")) },
      '-',
      { label: 'Rename…', run: () => ask({ title: 'Rename feed', body: 'Shown in the sidebar and lists.', input: name, label: 'Rename' }).then((v) => { if (typeof v === 'string' && v && v !== name) post('/manage/feeds/' + id + '/rename', { title: v }).then((r) => { if (r.ok) { toast('Renamed to ' + v); countsChanged(); } }); }) },
      { label: 'Move to folder…', run: () => {
        const choices = folderChoices().map((c) => ({ label: c.label, run: () => post('/manage/feeds/' + id + '/move', { folder_id: c.id }).then((r) => { if (r.ok) { toast('Moved to ' + c.label); countsChanged(); } else toast("Couldn't move the feed"); }) }));
        if (!choices.length) { toast('No folders yet. Create one under Manage → Folders.'); return; }
        const rect = a.getBoundingClientRect(); build([{ head: 'Move ' + name + ' to' }].concat(choices), rect.right, rect.top);
      } },
      { label: 'Pause fetching', run: () => post('/manage/feeds/' + id + '/pause').then((r) => toast(r.ok ? 'Paused ' + name : "Couldn't pause")) },
      { label: 'Resume fetching', run: () => post('/manage/feeds/' + id + '/resume').then((r) => toast(r.ok ? 'Resumed ' + name : "Couldn't resume")) },
      '-',
      { label: 'Unsubscribe…', danger: true, run: () => ask({ title: 'Unsubscribe from ' + name + '?', body: 'Its items are removed from your library too, including starred ones.', label: 'Unsubscribe', danger: true }).then((ok) => { if (ok === true) post('/manage/feeds/' + id + '/unsubscribe').then((r) => { if (r.ok) { toast('Unsubscribed from ' + name); countsChanged(); if (here) location.href = '/reader/unread'; } else toast("Couldn't unsubscribe"); }); }) },
    ];
  }
  function folderItems(a) {
    const id = a.dataset.dropFolder, name = nameOf(a) || 'this folder';
    const here = location.pathname.indexOf('/reader/folder/' + id) === 0;
    return [
      { head: name },
      { label: 'Open', run: () => { a.click(); } },
      { label: 'Mark all as read', run: () => markView('/reader/folder/' + id + '/mark-read', name) },
      '-',
      { label: 'Rename…', run: () => ask({ title: 'Rename folder', input: name, label: 'Rename' }).then((v) => { if (typeof v === 'string' && v && v !== name) post('/manage/folders/' + id + '/rename', { name: v }).then((r) => { if (r.ok) { toast('Renamed to ' + v); countsChanged(); } }); }) },
      { label: 'Delete folder…', danger: true, run: () => ask({ title: 'Delete ' + name + '?', body: 'Its feeds stay subscribed and move to Inbox.', label: 'Delete folder', danger: true }).then((ok) => { if (ok === true) post('/manage/folders/' + id + '/delete').then((r) => { if (r.ok) { toast('Deleted ' + name); countsChanged(); if (here) location.href = '/reader/unread'; } else toast("Couldn't delete the folder"); }); }) },
    ];
  }
  function articleItems() {
    // Mirror the header toolbar so the two never disagree.
    const buttons = $$('#article-toolbar .toolbar > [data-action]');
    if (!buttons.length) return null;
    const items = [];
    buttons.forEach((b) => {
      const label = (b.querySelector('.btn-label') || {}).textContent || b.getAttribute('aria-label') || b.title;
      if (!label) return;
      if (b.dataset.action === 'reader' || b.dataset.action === 'open' || b.dataset.action === 'summarize') { if (items.length && items[items.length - 1] !== '-') items.push('-'); }
      items.push({ label: label.trim(), run: () => b.click() });
    });
    const url = ($('#article article.article') || {}).dataset ? $('#article article.article').dataset.url : '';
    if (url) items.push({ label: 'Copy link', run: () => copy(url) });
    return items;
  }

  // What a right-click or a long-press on `t` should open: null when the target has no menu of its own.
  function resolve(t, longPress) {
    if (!t || !t.closest) return null;
    if (t.closest('input, textarea, select, [contenteditable="true"], a[href^="http"]:not(.nav-item):not(.plain), .prose')) return null;
    const row = t.closest('#list-body .item');
    const feed = t.closest('#nav .nav-feed[data-feed-id]');
    const folder = t.closest('#nav .nav-item[data-drop-folder]');
    // Long-press only fires on the article header (title, meta, toolbar), never on the body text.
    const article = t.closest(longPress ? '#article article.article > .article-header, #article-pane .article-head' : '#article article.article, #article-pane .article-head');
    if (row) { selectRow(row); return rowItems(row); }
    if (feed) return feedItems(feed);
    if (folder && folder.dataset.dropFolder) return folderItems(folder);
    if (article) return articleItems();
    return null;
  }
  document.addEventListener('contextmenu', (e) => {
    if (e.shiftKey || e.ctrlKey) return;
    const t = e.target; if (!t || !t.closest) return;
    // Android fires contextmenu after a long-press too; the touch handler already opened the sheet.
    if (menu && Date.now() - openedByTouch < 1500) { e.preventDefault(); return; }
    if (t.closest('input, textarea, select, [contenteditable="true"], a[href^="http"]:not(.nav-item):not(.plain), .prose')) return;
    const summary = t.closest('details.menu > summary');
    if (summary) { e.preventDefault(); summary.parentElement.open = true; return; }
    const items = resolve(t, false);
    if (!items || !items.length) return;
    e.preventDefault();
    opener = t.closest('a, button, [tabindex]') || t;
    build(items, e.clientX, e.clientY);
  });

  // ---- Long-press (500 ms, under 10 px of travel) opens the same menu on touch screens ----
  let press = null, suppressClick = false;
  function cancelPress() { if (press) { clearTimeout(press.timer); press = null; } }
  function blockScroll(e) { e.preventDefault(); }
  document.addEventListener('touchstart', (e) => {
    cancelPress();
    if (e.touches.length !== 1 || menu) return;
    const touch = e.touches[0]; const t = e.target;
    if (!t || !t.closest || !t.closest('#list-body .item, #nav .nav-feed[data-feed-id], #nav .nav-item[data-drop-folder], #article article.article > .article-header, #article-pane .article-head')) return;
    if (t.closest('input, textarea, select, button, summary, details.menu, .chip, .sources, .tags-row')) return;
    press = { x: touch.clientX, y: touch.clientY, target: t, timer: setTimeout(() => {
      const p = press; press = null; if (!p) return;
      const items = resolve(p.target, true);
      if (!items || !items.length) return;
      openedByTouch = Date.now(); suppressClick = true;
      opener = p.target.closest('a, button, [tabindex]') || p.target;
      if (window.navigator.vibrate) { try { window.navigator.vibrate(10); } catch (_) {} }
      // Keep the finger's release from scrolling the list or following the row's link.
      document.addEventListener('touchmove', blockScroll, { passive: false });
      build(items, p.x, p.y);
    }, 500) };
  }, { passive: true });
  document.addEventListener('touchmove', (e) => {
    if (!press) return;
    const touch = e.touches[0];
    if (Math.abs(touch.clientX - press.x) > 10 || Math.abs(touch.clientY - press.y) > 10) cancelPress();
  }, { passive: true });
  document.addEventListener('touchend', () => { cancelPress(); document.removeEventListener('touchmove', blockScroll); setTimeout(() => { suppressClick = false; }, 400); }, { passive: true });
  document.addEventListener('touchcancel', () => { cancelPress(); document.removeEventListener('touchmove', blockScroll); }, { passive: true });
  // The click that follows a long-press would open the row or follow the link: swallow it.
  document.addEventListener('click', (e) => {
    if (!suppressClick) return;
    if (menu && menu.contains(e.target)) return;
    e.preventDefault(); e.stopPropagation(); suppressClick = false;
  }, true);
  // Same for the synthetic mousedown, which would otherwise close the sheet as an outside click.
  document.addEventListener('mousedown', (e) => { if (suppressClick && !(menu && menu.contains(e.target))) { e.stopPropagation(); } }, true);
})();

// ---- Web-page fallback: embed the original page on demand (sandboxed, no referrer) ----
(function () {
  document.addEventListener('click', (e) => {
    const b = e.target.closest && e.target.closest('[data-embed-toggle]'); if (!b) return;
    e.preventDefault();
    const box = document.querySelector(b.dataset.embedToggle); if (!box) return;
    const open = box.classList.toggle('hidden') === false;
    b.setAttribute('aria-expanded', open ? 'true' : 'false');
    if (open && !box.querySelector('iframe')) {
      const f = document.createElement('iframe');
      f.src = box.dataset.src; f.loading = 'lazy'; f.referrerPolicy = 'no-referrer';
      f.setAttribute('sandbox', 'allow-scripts allow-same-origin allow-popups allow-forms');
      f.setAttribute('title', 'Original web page');
      box.appendChild(f);
    }
    b.querySelector('.btn-label') && (b.querySelector('.btn-label').textContent = open ? 'Close it' : 'Show it here');
  });
})();
