// Run the actual browser script; DOM events clear currentTarget after dispatch.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');

const handlers = new Map();
function element(id = '') {
  return {value: '', checked: false, textContent: '', children: [], disabled: false,
    addEventListener(type, handler) { handlers.set(`${id}:${type}`, handler); },
    replaceChildren() { this.children = []; }, append(child) { this.children.push(child); }};
}
const nodes = new Map(['notice', 'digest-preview', 'digest-send', 'digest-date', 'digest-type', 'digest-new-revision', 'draft'].map(id => [id, element(id)]));
nodes.get('digest-type').value = 'daily';
const draft = {digest_id: 'confirmed', revision: 1, payload_hash: 'frozen', recipient: 'reader@example.test', provider: 'fake',
  payload: {selected_items: [{title: '<script>untrusted</script>', summary: 'summary'}], selected_count: 1}};
let sends = 0;
vm.runInNewContext(fs.readFileSync(path.join(__dirname, '../web_static/dashboard.js'), 'utf8'), {
  document: {body: {dataset: {writes: 'true'}}, querySelector(selector) { return nodes.get(selector.slice(1)) || null; },
    querySelectorAll() { return []; }, createElement() { return element(); }},
  crypto: {randomUUID: () => 'test-request'}, setTimeout,
  async fetch(url) {
    if (url === '/api/digest/send') sends++;
    const data = url.endsWith('/preview') ? draft : url.endsWith('/send') ? {task_id: 'task'} :
      {status: 'succeeded', result: {status: 'accepted'}, cancellation_note: 'not a refund'};
    return {ok: true, async json() { return {data}; }};
  },
});

(async () => {
  await handlers.get('digest-preview:submit')({preventDefault() {}});
  assert.equal(nodes.get('digest-send').hidden, false);
  const event = {currentTarget: nodes.get('digest-send')};
  const finished = handlers.get('digest-send:click')(event);
  assert.equal(event.currentTarget.disabled, true);
  event.currentTarget = null;
  await assert.doesNotReject(finished);
  assert.equal(nodes.get('digest-send').disabled, false);
  assert.equal(sends, 1);
  assert.match(nodes.get('draft').children[0].textContent, /供應商已接受/);
  console.log('1 PASS - frozen digest completes after event dispatch, sends once and restores its button');
})().catch(error => { console.error(error); process.exitCode = 1; });
