/* global chrome, importScripts */
// Right-click menu: save this page (as seen) or a link on it.
if (typeof importScripts === 'function') importScripts('pensieve.js');
const { settings, pageHtml, saveToPensieve } = self.PensieveExt;

function menus() {
  chrome.contextMenus.removeAll(() => {
    chrome.contextMenus.create({ id: 'save-page', title: 'Save page to Pensieve', contexts: ['page'] });
    chrome.contextMenus.create({ id: 'save-link', title: 'Save link to Pensieve', contexts: ['link'] });
  });
}
chrome.runtime.onInstalled.addListener(() => {
  menus();
  chrome.storage.sync.get({ server: '' }).then((s) => { if (!s.server) chrome.runtime.openOptionsPage(); });
});
chrome.runtime.onStartup.addListener(menus);

function badge(tabId, text, color) {
  chrome.action.setBadgeBackgroundColor({ color, tabId });
  chrome.action.setBadgeText({ text, tabId });
  setTimeout(() => chrome.action.setBadgeText({ text: '', tabId }), 2500);
}

chrome.contextMenus.onClicked.addListener(async (info, tab) => {
  try {
    if (info.menuItemId === 'save-link') {
      await saveToPensieve({ url: info.linkUrl, title: info.selectionText || '' });
    } else {
      const s = await settings();
      const html = s.sendPage && tab ? await pageHtml(tab.id) : null;
      await saveToPensieve({ url: tab.url, title: tab.title, html });
    }
    if (tab) badge(tab.id, 'OK', '#2E8B6E');
  } catch (err) {
    if (tab) badge(tab.id, '!', '#8A4A2E');
    console.warn('Pensieve:', err);
  }
});
