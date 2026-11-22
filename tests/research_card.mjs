import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { runInNewContext } from 'node:vm';

const html = readFileSync(new URL('../plugin/assets/research-card.html', import.meta.url), 'utf8');
const script = html.match(/<script>([\s\S]+)<\/script>/)[1];
const record = `1:0:${'a'.repeat(64)}`;
const linked = `1:0:${'b'.repeat(64)}`;
const result = {
  structuredContent: { record_id: record, title: 'Synthetic result', category: 'result',
    lab: 'A', summary: 'Short summary' },
  _meta: { 'naome/research-block': { record_id: record, summary: 'Full summary',
    sources: ['<img src=x>'], linked_records: [linked] } },
};

function widget(locale) {
  const nodes = new Map();
  const events = new Map();
  const messages = [];
  const node = () => ({
    hidden: true, textContent: '', children: [], attributes: {}, listeners: {},
    replaceChildren(...children) { this.children = children; },
    setAttribute(key, value) { this.attributes[key] = value; },
    addEventListener(name, listener) { this.listeners[name] = listener; },
  });
  const get = (id) => {
    if (!nodes.has(id)) nodes.set(id, node());
    return nodes.get(id);
  };
  const parent = { postMessage: (message) => messages.push(message) };
  let resize;
  const document = { getElementById: get, createElement: node, documentElement: {style: {}},
    body: { getBoundingClientRect: () => ({height: get('card').hidden ? 0 : get('details').hidden ? 75 : 180}) } };
  runInNewContext(script, {
    window: { parent, addEventListener: (name, listener) => events.set(name, listener) },
    document,
    ResizeObserver: class {
      constructor(callback) { resize = callback; }
      observe() {}
    },
  });
  const deliver = (data, source = parent) => events.get('message')({ source, data: { jsonrpc: '2.0', ...data } });
  const receive = (params) => deliver({ method: 'ui/notifications/tool-result', params });
  assert.equal(messages[0].method, 'ui/initialize');
  deliver({ id: 1, result: {} }, {});
  receive(result);
  assert.equal(get('card').hidden, true);
  deliver({ id: 1, result: { hostContext: { locale, theme: 'dark' } } });
  assert.equal(messages[1].method, 'ui/notifications/initialized');
  assert.equal(document.documentElement.style.colorScheme, 'dark');
  return { get, deliver, receive, resize, messages, click: () => get('toggle').listeners.click() };
}

const view = widget('en-GB');
view.receive({});
assert.equal(view.get('card').hidden, true);
view.receive(result);
assert.equal(view.get('card').hidden, false);
assert.equal(view.get('heading').textContent, 'Synthetic result');
assert.equal(view.get('labels').textContent, 'Result · Lab A');
assert.equal(view.get('preview').textContent, 'Short summary');
assert.equal(view.get('toggle').textContent, 'Show details');
assert.equal(view.get('summary').textContent, '');
assert.equal(view.get('details').hidden, true);
assert.equal(view.messages.at(-1).params.height, 75);
const beforeResize = view.messages.length;
view.resize();
assert.equal(view.messages.length, beforeResize);
view.click();
assert.equal(view.get('toggle').textContent, 'Hide details');
assert.equal(view.get('toggle').attributes['aria-expanded'], 'true');
assert.equal(view.get('details').hidden, false);
assert.equal(view.get('summary').textContent, 'Full summary');
assert.equal(view.get('record').textContent, record);
assert.deepEqual(view.get('sources').children.map(item => item.textContent), ['<img src=x>']);
assert.deepEqual(view.get('linked').children.map(item => item.textContent), [linked]);
assert.equal(view.messages.at(-1).params.height, 180);
view.deliver({ method: 'ui/notifications/host-context-changed', params: { locale: 'de-DE' } });
assert.equal(view.get('toggle').textContent, 'Details ausblenden');
assert.equal(view.get('labels').textContent, 'Ergebnis · Lab A');
assert.equal(view.get('sources-label').textContent, 'Quellen');
assert.equal(view.get('summary').textContent, 'Full summary');

const mismatched = { ...result, structuredContent: { ...result.structuredContent, record_id: linked } };
view.receive(mismatched);
assert.equal(view.get('card').hidden, true);
assert.equal(view.get('record').textContent, '');
assert.equal(view.get('sources').children.length, 0);
assert.equal(view.get('summary').textContent, '');
assert.equal(view.get('toggle').textContent, 'Details anzeigen');
view.receive(result);
view.click();
view.click();
assert.equal(view.get('details').hidden, true);
assert.equal(view.get('record').textContent, '');
assert.equal(view.messages.some(message => message.method === 'tools/call'), false);

const replay = widget();
replay.receive(result);
assert.equal(replay.get('toggle').textContent, 'Show details');
replay.click();
assert.equal(replay.get('record').textContent, record);
replay.deliver({ id: 2, method: 'ui/resource-teardown' });
assert.equal(replay.get('card').hidden, true);
assert.equal(replay.messages.at(-1).id, 2);
console.log('Verified the research card lifecycle, archived data, host locale and disclosure.');
