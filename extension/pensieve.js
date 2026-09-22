// Shared by the popup, the options page and the background worker.
/* global chrome */
const MAX_HTML = 8 * 1024 * 1024;

async function settings() {
  const s = await chrome.storage.sync.get({ server: '', token: '', sendPage: true });
  s.server = (s.server || '').replace(/\/+$/, '');
  return s;
}

// The DOM the user is looking at (after scripts, signed in, past a paywall), capped at 8 MB.
async function pageHtml(tabId) {
  try {
    const [res] = await chrome.scripting.executeScript({
      target: { tabId },
      func: () => '<!doctype html>\n' + document.documentElement.outerHTML,
    });
    const html = res && res.result;
    return typeof html === 'string' && html.length <= 8 * 1024 * 1024 ? html : null;
  } catch (_) {
    return null; // chrome:// pages, the web store, PDFs in the viewer: save the URL only
  }
}

async function saveToPensieve({ url, title, tags, note, html }) {
  const s = await settings();
  if (!s.server || !s.token) throw new Error('Set your Pensieve address and token in the extension options.');
  const body = { url, title, tags, note };
  if (html && html.length <= MAX_HTML) body.html = html;
  const resp = await fetch(s.server + '/api/v1/save', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', Authorization: 'Bearer ' + s.token },
    body: JSON.stringify(body),
  });
  let data = {};
  try { data = await resp.json(); } catch (_) { /* not JSON */ }
  if (resp.status === 401) throw new Error('Pensieve rejected the token. Create a new one under Manage > Saving.');
  if (!resp.ok) throw new Error(data.error || data.detail || ('Pensieve answered HTTP ' + resp.status));
  return data;
}

if (typeof self !== 'undefined') { self.PensieveExt = { settings, pageHtml, saveToPensieve }; }
