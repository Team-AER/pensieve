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
  function setPane(name) {
    const a = app();
    if (!a) return;
    a.dataset.pane = name;
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
    $$('details.menu[open]').forEach((d) => { if (!d.contains(e.target)) d.removeAttribute('open'); });
  });
  document.addEventListener('change', (e) => {
    const t = e.target;
    if (t.matches('[data-toggle]')) { const el = $(t.dataset.toggle); if (el) el.classList.toggle('hidden', !t.checked); if (el && t.checked) { const f = el.querySelector('textarea'); if (f) f.focus(); } }
    if (t.matches('[data-theme-select]')) {
      const html = document.documentElement;
      if (t.value === 'auto') html.removeAttribute('data-theme'); else html.dataset.theme = t.value;
      html.setAttribute('data-theme-src', t.value);
      try { const uid = html.dataset.uid || ''; localStorage.setItem('pensieve.theme:' + uid, t.value); localStorage.setItem('pensieve.theme:', t.value); } catch (_) {}
    }
    if (t.matches('[data-font-select]')) document.documentElement.dataset.font = t.value;
    if (t.matches('[data-measure-select]')) document.documentElement.dataset.measure = t.value;
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
      d.showModal();
      ok.focus();
    });
  }
  function settleConfirm(value) {
    const d = $('#confirm'); if (d && d.open) d.close();
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
  function menuItems(d) { return $$('.menu-item, .menu-form .input, .menu-form .btn', d).filter((el) => el.offsetParent !== null); }
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
  function select(row, opts) {
    opts = opts || {};
    rows().forEach((r) => { r.classList.remove('selected'); r.setAttribute('aria-selected', 'false'); r.tabIndex = -1; });
    if (!row) { syncTabindex(); return; }
    row.classList.add('selected'); row.setAttribute('aria-selected', 'true'); row.tabIndex = 0;
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
  // Reading size: cycle data-font on <html>, persist through /manage/account/font.
  const FONT_SIZES = ['s', 'm', 'l', 'xl'];
  function stepFont(delta) {
    const html = document.documentElement;
    const idx = Math.max(0, FONT_SIZES.indexOf(html.dataset.font || 'm'));
    const next = FONT_SIZES[Math.max(0, Math.min(FONT_SIZES.length - 1, idx + delta))];
    if (next === html.dataset.font) return;
    html.dataset.font = next;
    const body = new URLSearchParams({ font_size: next });
    fetch('/manage/account/font', { method: 'POST', credentials: 'same-origin', body, headers: { 'X-CSRF-Token': csrfToken() } }).catch(() => {});
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
      if (isMobile()) setPane('article');
    }
    if (t.id === 'list' || t.id === 'list-body' || t.closest('#list-body')) syncTabindex();
  });
  document.body.addEventListener('htmx:afterSettle', () => relocateToolbar());
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
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init); else init();
})();
