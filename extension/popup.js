/* global chrome */
const { settings, pageHtml, saveToPensieve } = self.PensieveExt;
const $ = (id) => document.getElementById(id);
// Success owns the state; finite, cancellable effects only carry it into place.
function confirmedMotion(element, frames, duration) {
  const media = window.matchMedia ? window.matchMedia('(prefers-reduced-motion: reduce)') : null;
  if (media?.matches || typeof element.animate !== 'function') return;
  let animation;
  let settled = false;
  const stop = () => { if (settled) return; settled = true; animation?.cancel(); media?.removeEventListener('change', changed); window.removeEventListener('pagehide', stop); };
  const changed = (event) => { if (event.matches) stop(); };
  try {
    animation = element.animate(frames, { duration, easing: 'cubic-bezier(.2,.8,.2,1)' });
    animation.finished.then(stop, stop);
    media?.addEventListener('change', changed);
    window.addEventListener('pagehide', stop);
  } catch (_) { stop(); }
}


(async () => {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  const s = await settings();
  $('page-title').textContent = tab ? tab.title : '';
  $('page-url').textContent = tab ? tab.url : '';
  $('send-page').checked = s.sendPage;
  if (!s.server || !s.token) {
    $('status').innerHTML = 'Set your Pensieve address and token first. <a href="#" id="setup">Open settings</a>';
    $('save').disabled = true;
    $('setup').addEventListener('click', (e) => { e.preventDefault(); chrome.runtime.openOptionsPage(); });
  }
  $('options').addEventListener('click', (e) => { e.preventDefault(); chrome.runtime.openOptionsPage(); });
  $('form').addEventListener('submit', async (e) => {
    e.preventDefault();
    if ($('save').disabled) return;
    $('save').disabled = true;
    $('save').textContent = 'Saving…';
    $('form').setAttribute('aria-busy', 'true');
    $('status').className = '';
    $('status').textContent = 'Saving…';
    try {
      const html = $('send-page').checked ? await pageHtml(tab.id) : null;
      const tags = $('tags').value.split(',').map((t) => t.trim()).filter(Boolean);
      const data = await saveToPensieve({ url: tab.url, title: tab.title, tags, note: $('note').value, html });
      $('status').innerHTML = '';
      const done = document.createElement('p');
      done.className = 'ok';
      done.textContent = data.created ? 'Saved. Pensieve is keeping a copy.' : 'Already saved: moved back to the top.';
      const open = document.createElement('a');
      open.href = data.open; open.target = '_blank'; open.textContent = 'Open in Pensieve';
      $('status').append(done, open);
      $('save').textContent = 'Saved';
      const check = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
      check.setAttribute('viewBox', '0 0 20 20');
      check.setAttribute('aria-hidden', 'true');
      check.classList.add('saved-check');
      const stroke = document.createElementNS('http://www.w3.org/2000/svg', 'path');
      stroke.setAttribute('d', 'M3 10l5 5L17 5');
      check.append(stroke);
      $('save').prepend(check);
      $('status').className = 'saved-result';
      $('capture-slip').classList.add('is-filed');
      confirmedMotion($('capture-slip'), [{ transform: 'translateY(-9px) rotate(-1deg)' }, { transform: 'translateY(3px) rotate(.3deg)', offset: .65 }, { transform: 'none' }], 520);
      confirmedMotion($('status'), [{ opacity: 0, transform: 'translateY(14px)' }, { opacity: 1, transform: 'none' }], 460);
      confirmedMotion(check, [{ strokeDashoffset: 24 }, { strokeDashoffset: 0 }], 160);
      $('tags').readOnly = true;
      $('note').readOnly = true;
      $('send-page').disabled = true;
    } catch (err) {
      $('status').textContent = err.message;
      $('status').className = 'error';
      $('save').disabled = false;
      $('save').textContent = 'Save';
    } finally {
      $('form').removeAttribute('aria-busy');
    }
  });
})();
