import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { runInNewContext } from 'node:vm';

const html = readFileSync(new URL('../plugin/assets/research-card.html', import.meta.url), 'utf8');
const script = html.match(/<script>([\s\S]+)<\/script>/)[1];
const id = (character) => '1:0:' + character.repeat(64);
const record = id('a');
const linked = id('b');
const head = id('c');
const captured = '2026-11-22T15:24:55Z';
const written = {
  structuredContent: { record_id: record, title: 'Synthetic result', category: 'result',
    project: 'calibration', lab: 'A', summary: 'Short summary' },
  _meta: { 'naome/research-block': { record_id: record, summary: 'Full summary',
    sources: ['<img src=x>'], linked_records: [linked] } },
};
const recent = {
  structuredContent: { project: 'calibration', lab: 'A', source: 'codex_archive_receipts',
    records: [{ record_id: record, title: 'Latest result', category: 'result', captured_at: captured,
      preview: 'Saved summary preview' },
      { record_id: linked, title: 'Earlier method', category: 'method' }] },
  _meta: { 'naome/research-summaries': { [record]: 'The full saved summary' } },
};
const searched = {
  structuredContent: { project: 'calibration', lab: 'A', head, query_limited: true,
    hits: [{ record_id: record, subject: 'Matched result', kind: 'derivation',
      snippet: 'A matched excerpt from the approved snapshot.',
      links: [{ role: 'references', target: linked }], location: { pointer: '/fields/summary' } },
    { record_id: linked, kind: 'plan' }] },
};

function node(tagName, hidden = false) {
  return {
    tagName, hidden, children: [], attributes: {}, listeners: {}, text: '',
    get textContent() { return this.text + this.children.map(child => child.textContent).join(''); },
    set textContent(value) { this.text = value; this.children = []; },
    append(...children) { this.children.push(...children); },
    replaceChildren(...children) { this.text = ''; this.children = children; },
    setAttribute(key, value) { this.attributes[key] = value; },
    addEventListener(name, listener) { this.listeners[name] = listener; },
  };
}

function cloneCard() {
  const article = node('article');
  const parts = {
    h2: node('h2'), '.naome-card-context': node('p'), button: node('button'),
    '.naome-card-preview': node('span', true), dl: node('dl', true),
  };
  article.querySelector = (selector) => parts[selector];
  article.append(...Object.values(parts));
  return article;
}

function widget(locale) {
  const records = node('div');
  const results = node('section', true);
  const notice = node('p', true);
  const nodes = { results, records, notice, 'record-template': {
    content: { firstElementChild: { cloneNode: cloneCard } },
  } };
  const events = new Map();
  const messages = [];
  const parent = { postMessage: (message) => messages.push(message) };
  results.getBoundingClientRect = () => ({height: results.hidden ? 0 : records.children
    .reduce((sum, card) => sum + (card.querySelector('dl').hidden ? 75 : 180), notice.hidden ? 0 : 24)});
  let resize;
  const styles = new Map();
  const style = { setProperty: (key, value) => styles.set(key, value), removeProperty: (key) => styles.delete(key) };
  const document = { getElementById: (id) => nodes[id], createElement: node, documentElement: {style} };
  runInNewContext(script, {
    window: { parent, addEventListener: (name, listener) => events.set(name, listener) },
    document,
    CSS: { supports: (property, value) => property === 'color' && /^#[a-f0-9]{6}$/.test(value) },
    ResizeObserver: class {
      constructor(callback) { resize = callback; }
      observe() {}
    },
  });
  const deliver = (data, source = parent) => events.get('message')({ source, data: { jsonrpc: '2.0', ...data } });
  const receive = (params) => deliver({ method: 'ui/notifications/tool-result', params });
  assert.equal(messages[0].method, 'ui/initialize');
  assert.deepEqual([...messages[0].params.appCapabilities.availableDisplayModes], ['inline']);
  deliver({ id: 1, result: {} }, {});
  receive(written);
  assert.equal(results.hidden, true);
  deliver({ id: 1, result: { hostContext: { locale, theme: 'dark' } } });
  assert.equal(messages[1].method, 'ui/notifications/initialized');
  assert.equal(document.documentElement.style.colorScheme, 'dark');
  return { results, records, notice, deliver, receive, resize, messages, styles, style,
    card: (index = 0) => records.children[index].querySelector,
    click: (index = 0) => records.children[index].querySelector('button').listeners.click() };
}

function details(card) {
  return Object.fromEntries(card('dl').children.map(({ children: [label, value] }) =>
    [label.textContent, value.textContent]));
}

const view = widget('en-GB');
view.receive({});
assert.equal(view.results.hidden, true);
view.receive(written);
assert.equal(view.results.hidden, false);
assert.equal(view.card()('h2').textContent, 'Synthetic result');
assert.equal(view.card()('.naome-card-context').textContent, 'Result · calibration · Lab A');
assert.equal(view.card()('.naome-card-preview').textContent, 'Short summary');
assert.equal(view.card()('button').textContent, 'more');
assert.deepEqual(details(view.card()), {});
assert.equal(view.messages.at(-1).params.height, 75);
const beforeResize = view.messages.length;
view.resize();
assert.equal(view.messages.length, beforeResize);
view.click();
assert.equal(view.card()('button').textContent, 'less');
assert.equal(view.card()('button').attributes['aria-expanded'], 'true');
assert.deepEqual(details(view.card()), { Summary: 'Full summary', Sources: '<img src=x>',
  'Linked records': linked, 'Record ID': record });
assert.equal(view.messages.at(-1).params.height, 180);

view.deliver({ method: 'ui/notifications/host-context-changed', params: { locale: 'de-DE' } });
assert.equal(view.card()('button').textContent, 'weniger');
assert.equal(view.card()('.naome-card-context').textContent, 'Ergebnis · calibration · Lab A');
assert.equal(details(view.card()).Quellen, '<img src=x>');
view.receive({ ...written, structuredContent: { ...written.structuredContent, record_id: linked } });
assert.equal(view.results.hidden, true);
assert.equal(view.records.children.length, 0);

view.receive(recent);
const date = new Intl.DateTimeFormat('de-DE', { dateStyle: 'medium', timeStyle: 'short' }).format(new Date(captured));
assert.equal(view.card()('h2').textContent, 'Latest result');
assert.equal(view.card()('.naome-card-context').textContent, 'Ergebnis · calibration · Lab A · ' + date);
assert.equal(view.card()('.naome-card-preview').textContent, 'Saved summary preview');
view.click();
assert.equal(details(view.card()).Zusammenfassung, 'The full saved summary');
assert.equal(view.card()('.naome-card-preview').hidden, true);
view.click();
assert.equal(view.card()('.naome-card-preview').hidden, false);
assert.equal(view.card(1)('h2').textContent, 'Earlier method');
assert.notEqual(view.card()('button').attributes['aria-controls'], view.card(1)('button').attributes['aria-controls']);
view.click(1);
assert.deepEqual(details(view.card(1)), { 'Eintrags-ID': linked });
assert.deepEqual(details(view.card()), {});
view.click(1);
assert.deepEqual(details(view.card(1)), {});
assert.equal(view.notice.hidden, true);

view.receive({ structuredContent: { ...recent.structuredContent, lab: 'default' } });
assert.equal(view.card()('.naome-card-context').textContent, 'Ergebnis · calibration · ' + date);
assert.equal(view.card()('button').textContent, 'mehr');
view.click();
assert.deepEqual(details(view.card()), { Zusammenfassung: 'Saved summary preview',
  Labor: 'default', 'Eintrags-ID': record });
assert.equal(view.card()('.naome-card-preview').hidden, true);

view.deliver({ method: 'ui/notifications/host-context-changed', params: {
  theme: 'light', styles: { variables: { '--color-text-primary': '#123456',
    '--color-border-secondary': 'url(bad)', '--unknown': '#000000' } },
} });
assert.equal(view.style.colorScheme, 'light');
assert.deepEqual([...view.styles], [['--color-text-primary', '#123456']]);
view.deliver({ method: 'ui/notifications/host-context-changed', params: { styles: { variables: {} } } });
assert.equal(view.styles.size, 0);

const replay = widget();
replay.receive(searched);
assert.equal(replay.card()('h2').textContent, 'Matched result');
assert.equal(replay.card()('.naome-card-context').textContent, 'Derivation · calibration · Lab A');
assert.equal(replay.card()('.naome-card-preview').textContent, 'A matched excerpt from the approved snapshot.');
assert.equal(replay.card(1)('h2').textContent, 'Research record');
assert.equal(replay.notice.hidden, false);
replay.click();
assert.deepEqual(details(replay.card()), { 'Search excerpt': 'A matched excerpt from the approved snapshot.',
  'Linked records': linked, 'Search snapshot': head, 'Match location': '/fields/summary',
  'Record ID': record });
replay.receive({ ...searched, isError: true });
assert.equal(replay.results.hidden, true);
assert.equal(replay.messages.at(-1).params.height, 0);
replay.receive({ structuredContent: { ...searched.structuredContent, hits: [] } });
assert.equal(replay.results.hidden, true);
replay.receive(searched);
assert.equal(replay.card()('dl').hidden, true);
replay.deliver({ id: 2, method: 'ui/resource-teardown' });
assert.equal(replay.results.hidden, true);
assert.equal(replay.records.children.length, 0);
assert.equal(replay.messages.at(-1).id, 2);
assert.equal(view.messages.concat(replay.messages).some(message => message.method === 'tools/call'), false);
console.log('Verified shared write, recent and search cards, disclosure, localization and empty states.');
