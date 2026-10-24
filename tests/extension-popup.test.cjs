const { test } = require('node:test');
const assert = require('node:assert/strict');
const { readFileSync } = require('node:fs');
const { join } = require('node:path');
const { createContext, runInContext } = require('node:vm');

async function popup(save, loadShared = false) {
  const elements = new Map();
  function element() {
    return { disabled: false, value: '', textContent: '', className: '', children: [], attributes: {}, listeners: {},
      set innerHTML(value) { this.children = []; this.html = value; },
      addEventListener(name, callback) { this.listeners[name] = callback; },
      setAttribute(name, value) { this.attributes[name] = value; },
      removeAttribute(name) { delete this.attributes[name]; },
      append(...nodes) { this.children.push(...nodes); },
    };
  }
  const get = id => { if (!elements.has(id)) elements.set(id, element()); return elements.get(id); };
  const timers = [];
  let closed = false;
  const context = {
    document: { getElementById: get, createElement: element },
    chrome: { tabs: { query: async () => [{ id: 1, title: 'Fixture article', url: 'https://example.test/article' }] }, storage: { sync: { get: async () => ({ server: 'https://pensieve.test', token: 'fixture', sendPage: false }) } }, runtime: { openOptionsPage() {} } },
    self: { PensieveExt: { settings: async () => ({ server: 'https://pensieve.test', token: 'fixture', sendPage: false }), pageHtml: async () => null, saveToPensieve: save } },
    window: { close: () => { closed = true; } },
    setTimeout: callback => timers.push(callback),
    fetch: async (_url, options) => ({ ok: true, status: 200, json: async () => save(JSON.parse(options.body)) }),
  };
  createContext(context);
  if (loadShared) {
    runInContext(readFileSync(join(__dirname, '../extension/pensieve.js'), 'utf8'), context);
    assert.equal(Object.hasOwn(context, 'settings'), false, 'only self.PensieveExt is exported; popup and background can use their own bindings');
  }
  await runInContext(readFileSync(join(__dirname, '../extension/popup.js'), 'utf8'), context);
  return { get, timers, isClosed: () => closed, submit: () => get('form').listeners.submit({ preventDefault() {} }) };
}

test('the shared client and popup initialize together as classic extension scripts', async () => {
  const app = await popup(async () => ({ created: true, open: 'https://pensieve.test/items/1' }), true);
  await app.submit();
  assert.equal(app.get('save').textContent, '✓ Saved');
});

test('confirmed save keeps its destination available without auto-closing', async () => {
  const app = await popup(async () => ({ created: true, open: 'https://pensieve.test/items/1' }));
  app.get('note').value = 'Keep this context';
  await app.submit();
  assert.equal(app.get('save').textContent, '✓ Saved');
  assert.equal(app.get('status').children[1].href, 'https://pensieve.test/items/1');
  assert.equal(app.get('status').children[1].textContent, 'Open in Pensieve');
  assert.equal(app.timers.length, 0);
  assert.equal(app.isClosed(), false);
  assert.equal(app.get('note').value, 'Keep this context');
  assert.equal(app.get('note').readOnly, true);
  assert.equal(app.get('form').attributes['aria-busy'], undefined);
});

test('failed saves retain the note and allow retry without duplicate pending requests', async () => {
  let calls = 0;
  let reject;
  const app = await popup(() => { calls++; return new Promise((_, no) => { reject = no; }); });
  app.get('note').value = 'My note';
  const pending = app.submit();
  await Promise.resolve();
  await app.submit();
  assert.equal(calls, 1);
  reject(new Error('Offline; try again'));
  await pending;
  assert.equal(app.get('save').disabled, false);
  assert.equal(app.get('save').textContent, 'Save');
  assert.equal(app.get('note').value, 'My note');
  assert.equal(app.get('status').textContent, 'Offline; try again');
});
