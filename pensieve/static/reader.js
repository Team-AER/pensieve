// Reader behaviours: Google Reader keyboard map, selection, HTMX state sync, mobile panes, small UI helpers.
// Registered once per page load; survives hx-boost body swaps because handlers live on document.
(function () {
  if (window.__pensieveReader) return;
  window.__pensieveReader = true;

  const $ = (sel, root) => (root || document).querySelector(sel);
  const $$ = (sel, root) => Array.from((root || document).querySelectorAll(sel));
  const isMobile = () => window.matchMedia('(max-width: 900px)').matches;
  const app = () => $('#app');

  // ---- Panes (mobile) ----
  function setPane(name) {
    const a = app();
    if (a) a.dataset.pane = name;
  }
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
      setPane(target);
      return;
    }
    const navLink = e.target.closest('.pane-nav a.nav-item');
    if (navLink && isMobile()) setPane('list');
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
    if (t.matches('[data-theme-select]')) { try { localStorage.setItem('pensieve.theme', t.value); } catch (_) {} document.documentElement.dataset.theme = t.value; }
    if (t.matches('[data-font-select]')) document.documentElement.dataset.font = t.value;
    if (t.matches('[data-measure-select]')) document.documentElement.dataset.measure = t.value;
  });
  document.addEventListener('click', (e) => { const b = e.target.closest('[data-action="share"]'); if (b) { e.preventDefault(); share(b); } });
  // Signing out: tell the service worker to drop its cached reader pages and offline queue.
  document.addEventListener('submit', (e) => {
    const f = e.target;
    if (f && f.getAttribute && f.getAttribute('action') === '/logout' && navigator.serviceWorker && navigator.serviceWorker.controller) {
      navigator.serviceWorker.controller.postMessage({ type: 'clear' });
    }
  });

  // ---- Selection ----
  function rows() { return $$('#list-body .item'); }
  function selected() { return $('#list-body .item.selected'); }
  function select(row, opts) {
    opts = opts || {};
    rows().forEach((r) => r.classList.remove('selected'));
    if (!row) return;
    row.classList.add('selected');
    row.scrollIntoView({ block: 'nearest' });
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
    select(all[idx], { open: openIt });
    if (idx >= all.length - 3) { const s = $('#list-body .sentinel'); if (s && window.htmx) window.htmx.trigger(s, 'revealed'); }
  }
  function currentArticle() { return $('#article article.article'); }
  function articleButton(action) { const a = currentArticle(); return a ? a.querySelector('[data-action="' + action + '"]') : null; }
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
      postState('/items/' + id + '/' + (on ? 'unstar' : 'star')).then((r) => { if (r.ok) { row.classList.toggle('starred', !on); if (window.htmx) window.htmx.trigger(document.body, 'counts-changed'); } });
    } else {
      const on = row.classList.contains('read');
      postState('/items/' + id + '/' + (on ? 'unread' : 'read')).then((r) => { if (r.ok) { row.classList.toggle('read', !on); if (window.htmx) window.htmx.trigger(document.body, 'counts-changed'); } });
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
    if (!window.confirm('Mark everything in ' + title.trim() + ' as read?')) return;
    const everything = form.querySelector('button[name="older_than"][value=""]');
    if (everything) form.requestSubmit(everything); else form.requestSubmit();
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
  }
  function share(btn) {
    const url = btn.dataset.shareUrl, title = btn.dataset.shareTitle || document.title;
    if (!url) return;
    if (navigator.share) { navigator.share({ title, url }).catch(() => {}); return; }
    const done = () => { const old = btn.textContent; btn.textContent = 'Link copied'; setTimeout(() => { btn.textContent = old; }, 1500); };
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
  });
  document.body.addEventListener('htmx:afterSwap', (e) => {
    const t = e.detail && e.detail.target;
    if (!t) return;
    if (t.id === 'article') {
      const art = currentArticle();
      if (art) { const row = document.getElementById('item-' + art.dataset.id); if (row && !row.classList.contains('selected')) select(row); }
      if (isMobile()) setPane('article');
    }
    if (t.id === 'list' && selected() == null) { /* nothing selected after refresh */ }
  });
  document.body.addEventListener('htmx:responseError', (e) => {
    const xhr = e.detail && e.detail.xhr;
    if (xhr && (xhr.status === 401 || xhr.status === 403)) window.location.reload();
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
    if (e.metaKey || e.ctrlKey || e.altKey) return;
    if (typing(e)) { if (e.key === 'Escape') e.target.blur(); return; }
    const d = shortcuts();
    if (d && d.open) { if (e.key === 'Escape' || e.key === '?') { e.preventDefault(); toggleShortcuts(false); } return; }
    const key = e.key;
    if (pendingG) {
      pendingG = false; clearTimeout(pendingTimer);
      if (key === 'g') { location.href = '/reader/unread'; e.preventDefault(); return; }
      if (key === 'a') { location.href = '/reader/all'; e.preventDefault(); return; }
      if (key === 's') { location.href = '/reader/starred'; e.preventDefault(); return; }
    }
    switch (key) {
      case 'j': e.preventDefault(); move(1, true); break;
      case 'k': e.preventDefault(); move(-1, true); break;
      case 'n': e.preventDefault(); move(1, false); break;
      case 'p': e.preventDefault(); move(-1, false); break;
      case 'o': case 'Enter': {
        e.preventDefault();
        const s = selected();
        if (!s) { move(1, true); break; }
        if (selectionMatchesArticle()) { const a = $('#article'); if (a) a.innerHTML = '<div class="empty-article"><div class="eyebrow">Collapsed</div><p class="muted">Press <kbd>o</kbd> to open again.</p></div>'; if (isMobile()) setPane('list'); }
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
        if (isMobile() && app() && app().dataset.pane === 'article') { setPane('list'); break; }
        $$('details.menu[open]').forEach((d2) => d2.removeAttribute('open'));
        break;
      }
      default: return;
    }
  });

  // Row focus via keyboard (tab + enter/space) and click selection.
  document.addEventListener('keydown', (e) => {
    if ((e.key === ' ') && e.target.matches && e.target.matches('.item[role="button"]')) { e.preventDefault(); select(e.target, { open: true }); }
  });
  document.addEventListener('click', (e) => {
    const row = e.target.closest('#list-body .item');
    if (row) { rows().forEach((r) => r.classList.remove('selected')); row.classList.add('selected'); if (isMobile()) setPane('article'); }
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
        .then(() => { if (window.htmx) window.htmx.trigger(document.body, 'counts-changed'); });
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
  window.addEventListener('online', () => { if (navigator.serviceWorker && navigator.serviceWorker.controller) navigator.serviceWorker.controller.postMessage({ type: 'replay' }); });
})();
