/* global chrome */
const { settings, pageHtml, saveToPensieve } = self.PensieveExt;
const $ = (id) => document.getElementById(id);

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
    $('save').disabled = true;
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
      setTimeout(() => window.close(), 2200);
    } catch (err) {
      $('status').textContent = err.message;
      $('status').className = 'error';
      $('save').disabled = false;
    }
  });
})();
