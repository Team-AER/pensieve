/* global chrome */
const $ = (id) => document.getElementById(id);

async function load() {
  const s = await chrome.storage.sync.get({ server: '', token: '', sendPage: true });
  $('server').value = s.server; $('token').value = s.token; $('send-page').checked = s.sendPage;
}

async function persist() {
  const server = $('server').value.trim().replace(/\/+$/, '');
  const origin = new URL(server).origin + '/*';
  // Only the Pensieve server gets host access (so the extension can POST to it); nothing else.
  const granted = await chrome.permissions.request({ origins: [origin] });
  if (!granted) throw new Error('Permission to reach ' + origin + ' was not granted.');
  await chrome.storage.sync.set({ server, token: $('token').value.trim(), sendPage: $('send-page').checked });
  return server;
}

$('form').addEventListener('submit', async (e) => {
  e.preventDefault();
  try { await persist(); $('status').textContent = 'Saved.'; $('status').className = 'ok'; }
  catch (err) { $('status').textContent = err.message; $('status').className = 'error'; }
});

$('test').addEventListener('click', async () => {
  try {
    const server = await persist();
    const resp = await fetch(server + '/api/v1/save', { method: 'POST', headers: { Authorization: 'Bearer ' + $('token').value.trim(), 'Content-Type': 'application/json' }, body: '{}' });
    // An empty body is refused (422) only after the token was accepted.
    $('status').textContent = resp.status === 401 ? 'The token was rejected.' : resp.status === 422 ? 'Connected: the token works.' : 'Pensieve answered HTTP ' + resp.status;
    $('status').className = resp.status === 422 ? 'ok' : 'error';
  } catch (err) { $('status').textContent = err.message; $('status').className = 'error'; }
});

load();
