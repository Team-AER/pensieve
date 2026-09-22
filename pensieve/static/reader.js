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
  function setPane(name) {
    const a = app();
    if (!a) return;
    const was = a.dataset.pane;
    if (isMobile() && was !== name) {
      // One document scroll serves every pane on phones: keep each pane's place (a new article starts at the top).
      if (paneY.app !== a) { paneY.app = a; paneY.at = {}; }
      paneY.at[was] = window.scrollY;
    }
    a.dataset.pane = name;
    showBars(true);
    if (isMobile() && was !== name) scrollPageTo(name === 'article' ? 0 : paneY.at[name] || 0);
    if (name === 'nav') { const first = $('#nav .nav-item'); if (first && !isMobile()) first.focus({ preventScroll: true }); }
  }
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
  const THEME_COLORS = { light: '#F3F1EA', sepia: '#EFE6D2', dark: '#171614' };
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
    while (stack.children.length > 3) dismiss(stack.firstElementChild, true);
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
    confirmDialog({ title: src.dataset.confirmTitle, body: question, label: src.dataset.confirmLabel, danger: src.hasAttribute('data-confirm-danger') })
      .then((ok) => { if (ok) e.detail.issueRequest(true); });
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
    if (idx >= all.length - 3) { const s = $('#list-body .sentinel'); if (s && window.htmx) window.htmx.trigger(s, 'revealed'); }
  }
  function currentArticle() { return $('#article article.article'); }
  function articleButton(action) { return $('#article-toolbar [data-action="' + action + '"]') || (currentArticle() ? currentArticle().querySelector('[data-action="' + action + '"]') : null); }
  function selectionMatchesArticle() { const a = currentArticle(); const s = selected(); return a && s && a.dataset.id === s.dataset.id; }
  function csrfToken() { const m = $('meta[name="csrf-token"]'); return m ? m.content : ''; }
  function postState(path) {
    return fetch(path, { method: 'POST', credentials: 'same-origin', headers: { 'X-CSRF-Token': csrfToken(), 'HX-Request': 'true' } });
  }
  // s / m act on the open article when it matches the selection (or nothing is selected); otherwise on the row.
  function toggleRowState(action) {
    const row = selected();
    if (!row) return false;
    const id = row.dataset.id;
    if (action === 'star') {
      const on = row.classList.contains('starred');
      postState('/items/' + id + '/' + (on ? 'unstar' : 'star')).then((r) => { if (r.ok) { row.classList.toggle('starred', !on); toast(on ? 'Unstarred' : 'Starred'); if (window.htmx) window.htmx.trigger(document.body, 'counts-changed'); } });
    } else {
      const on = row.classList.contains('read');
      postState('/items/' + id + '/' + (on ? 'unread' : 'read')).then((r) => { if (r.ok) { row.classList.toggle('read', !on); toast(on ? 'Marked unread' : 'Marked read'); if (window.htmx) window.htmx.trigger(document.body, 'counts-changed'); } });
    }
    return true;
  }
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
  document.body.addEventListener('list-changed', () => toast('Unmerged from its story group'));
  // Feedback for actions whose responses carry no HX-Trigger: summarize, tags, notes, reader mode.
  document.body.addEventListener('htmx:afterRequest', (e) => {
    const elt = e.detail && e.detail.elt; const xhr = e.detail && e.detail.xhr; if (!elt || !xhr) return;
    const path = (e.detail.pathInfo && e.detail.pathInfo.requestPath) || '';
    if (xhr.status >= 400) { toast(xhr.status === 403 ? 'Session expired, reload the page' : 'That didn\'t work (' + xhr.status + ')', { kind: 'error' }); return; }
    if (/\/summarize$/.test(path)) toast('Summary requested', { kind: 'ai' });
    else if (/\/rewrite$/.test(path)) toast('Rewrite requested; your note is kept as a correction', { kind: 'ai' });
    else if (/\/tag$/.test(path)) { const op = elt.querySelector && elt.querySelector('[name="op"]'); toast(op && op.value === 'remove' ? 'Tag removed' : 'Tag added'); }
    else if (/\/note$/.test(path)) { const del = elt.querySelector && elt.querySelector('[name="delete"]'); toast(del ? 'Note deleted' : 'Note saved'); }
    else if (/\/reader-mode$/.test(path)) { const off = elt.querySelector && elt.querySelector('[name="off"]'); toast(off ? 'Showing the feed version' : 'Reader view'); }
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
  document.body.addEventListener('htmx:sendError', () => toast('You seem to be offline; changes are queued', { kind: 'error' }));

  // ---- Mobile article footer: previous / mark read + next / note ----
  document.addEventListener('click', (e) => {
    const b = e.target.closest('[data-article-nav]'); if (!b) return;
    e.preventDefault();
    const what = b.dataset.articleNav;
    if (what === 'prev') move(-1, true);
    else if (what === 'note') { const n = articleButton('note'); if (n) n.click(); }
    else if (what === 'read-next') {
      const art = currentArticle();
      if (art && art.dataset.read !== '1') { const r = articleButton('read'); if (r) r.click(); }
      const all = rows(); const cur = selected();
      if (cur && all.indexOf(cur) === all.length - 1) { toast('That was the last item'); setPane('list'); return; }
      move(1, true);
    }
  });

  // ---- Phones: every screen's top and bottom bars slide away while reading down, and return on the way back up ----
  // On phones the document scrolls (web.css), so the browser's own toolbar shrinks along with ours. The bars also
  // come back at the top and the bottom of the page, on a new article or pane, and whenever they take focus.
  const bars = { y: 0, travel: 0 };
  function showBars(show) {
    const a = app(); if (!a) return;
    if (show) delete a.dataset.bars; else a.dataset.bars = 'hidden';
  }
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
  // The paper comes back whole after a story is read or removed (fresh counts): keep open what was open.
  let paperOpen = null;
  document.body.addEventListener('htmx:beforeSwap', (e) => {
    const t = e.detail && e.detail.target;
    if (t && t.id === 'insight') paperOpen = $$('details.pstory[open], details.pbrief[open]', t).map((d) => d.id || 'brief:' + (d.closest('.psection') || {}).id);
  });
  document.body.addEventListener('htmx:afterSwap', (e) => {
    const t = e.detail && e.detail.target;
    if (!t || t.id !== 'insight' || !paperOpen) return;
    paperOpen.forEach((k) => {
      const d = k.startsWith('brief:') ? $('#' + CSS.escape(k.slice(6)) + ' details.pbrief') : document.getElementById(k);
      if (d) d.open = true;
    });
    paperOpen = null;
  });
  document.body.addEventListener('story-removed', () => toast('Removed from today\'s paper; still unread in Reader'));
  document.body.addEventListener('paper-read', () => toast('Marked read'));
  document.body.addEventListener('paper-section-read', () => toast('Section marked read'));
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
    if (row && !e.target.closest('.sources, .cluster-list')) { select(row); if (isMobile()) setPane('article'); }
  });

  // ---- Swipe back on mobile (article -> list) ----
  let touchX = null, touchY = null;
  document.addEventListener('touchstart', (e) => { if (!isMobile()) return; const t = e.touches[0]; touchX = t.clientX; touchY = t.clientY; }, { passive: true });
  document.addEventListener('touchend', (e) => {
    if (touchX === null || !isMobile()) return;
    const t = e.changedTouches[0];
    const dx = t.clientX - touchX, dy = Math.abs(t.clientY - touchY);
    if (touchX < 40 && dx > 80 && dy < 60 && app() && app().dataset.pane === 'article') setPane('list');
    touchX = touchY = null;
  }, { passive: true });

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
  window.addEventListener('offline', () => toast('You are offline; reading works, changes queue', { kind: 'error', ttl: 5000 }));

  // ---- Initial state ----
  function init() {
    relocateToolbar();
    syncTabindex();
    consumeSeeds(document);
    $$('details.menu > summary').forEach((s) => { if (!s.hasAttribute('aria-haspopup')) s.setAttribute('aria-haspopup', 'menu'); s.setAttribute('aria-expanded', s.parentElement.open ? 'true' : 'false'); });
    const art = currentArticle();
    if (art) { const row = document.getElementById('item-' + art.dataset.id); if (row) select(row); }
    updatePos();
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
    if (navigator.clipboard && navigator.clipboard.writeText) navigator.clipboard.writeText(text).then(done, () => toast('Could not copy the link'));
    else { const ta = document.createElement('textarea'); ta.value = text; document.body.appendChild(ta); ta.select(); try { document.execCommand('copy'); done(); } catch (_) { toast('Could not copy the link'); } ta.remove(); }
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
      if (!r.ok) { toast('That did not save'); return; }
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
      { label: 'Refresh now', run: () => post('/manage/feeds/' + id + '/refresh').then((r) => toast(r.ok ? 'Fetching ' + name : 'Could not queue a refresh')) },
      '-',
      { label: 'Rename…', run: () => ask({ title: 'Rename feed', body: 'Shown in the sidebar and lists.', input: name, label: 'Rename' }).then((v) => { if (typeof v === 'string' && v && v !== name) post('/manage/feeds/' + id + '/rename', { title: v }).then((r) => { if (r.ok) { toast('Renamed to ' + v); countsChanged(); } }); }) },
      { label: 'Move to folder…', run: () => {
        const choices = folderChoices().map((c) => ({ label: c.label, run: () => post('/manage/feeds/' + id + '/move', { folder_id: c.id }).then((r) => { if (r.ok) { toast('Moved to ' + c.label); countsChanged(); } else toast('Could not move the feed'); }) }));
        if (!choices.length) { toast('No folders yet. Create one under Manage → Folders.'); return; }
        const rect = a.getBoundingClientRect(); build([{ head: 'Move ' + name + ' to' }].concat(choices), rect.right, rect.top);
      } },
      { label: 'Pause fetching', run: () => post('/manage/feeds/' + id + '/pause').then((r) => toast(r.ok ? 'Paused ' + name : 'Could not pause')) },
      { label: 'Resume fetching', run: () => post('/manage/feeds/' + id + '/resume').then((r) => toast(r.ok ? 'Resumed ' + name : 'Could not resume')) },
      '-',
      { label: 'Unsubscribe…', danger: true, run: () => ask({ title: 'Unsubscribe from ' + name + '?', body: 'Its items are removed from your library too, including starred ones.', label: 'Unsubscribe', danger: true }).then((ok) => { if (ok === true) post('/manage/feeds/' + id + '/unsubscribe').then((r) => { if (r.ok) { toast('Unsubscribed from ' + name); countsChanged(); if (here) location.href = '/reader/unread'; } else toast('Could not unsubscribe'); }); }) },
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
      { label: 'Delete folder…', danger: true, run: () => ask({ title: 'Delete ' + name + '?', body: 'Its feeds stay subscribed and move to Inbox.', label: 'Delete folder', danger: true }).then((ok) => { if (ok === true) post('/manage/folders/' + id + '/delete').then((r) => { if (r.ok) { toast('Deleted ' + name); countsChanged(); if (here) location.href = '/reader/unread'; } else toast('Could not delete the folder'); }); }) },
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
