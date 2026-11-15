
import assert from 'node:assert/strict';
import { effect } from './vendor-signals.js';
import { createList, List } from './ui-list.js';
import { startKeys, selectedItem } from './ui-keys.js';
import { live, snapshot } from './ui-live.js';
const revision = {value:1};
globalThis.fetch = async url => ({ok:true, status:200, json:async () => url === '/api/sidebar'
  ? {needs_you:0, running:3, health:'ok', serve:{alive:true, age_seconds:1}}
  : {live:{rows:{runs:['a', 'b', 'c'].map((id, i) => ({id, ticket:`T-${id}`, step:`step ${revision.value}`, seconds:i, repo:'arete'})), slots:3, max_parallel:4}}, needs_you:{}}});
const handlers = {};
let scrolled = 0;
const marked = new Set();
const makeRow = id => ({dataset:{item:id}, hidden:false, disabled:false, getClientRects:() => [{}], getAttribute:() => null,
  setAttribute() { marked.add(id); }, removeAttribute() { marked.delete(id); }, scrollIntoView() { scrolled++; }, matches:() => false,
  querySelector:() => null, click() {}});
const rows = ['a', 'b', 'c'].map(makeRow);
const scroll = {scrollTop:140, scrollLeft:9};
const list = {dataset:{list:'floor-runs'}, querySelectorAll:selector => selector === '[data-row]' ? rows : rows.filter(row => marked.has(row.dataset.item)),
  querySelector:selector => selector === '.ui-list-scroll' ? scroll : null};
const paneEl = {dataset:{pane:'queue'}, toggleAttribute() {}, matches:() => false, querySelectorAll:selector => selector === '[data-list]' ? [list] : []};
globalThis.document = {hidden:false, querySelectorAll:selector => selector === '[data-pane]' ? [paneEl] : selector === '[data-list]' ? [list] : [],
  querySelector:() => null, addEventListener(name, fn) { handlers[name] = fn; }, removeEventListener() {}};
globalThis.window = {addEventListener() {}};
globalThis.location = {hash:'#floor'};
const controller = createList({rows:[], empty:'No running work'});
const component = new List();
component.props = {controller, id:'floor-runs', queue:true};
component.base = list;
component.setState = change => Object.assign(component.state, change);
component.componentDidMount();
effect(() => controller.setRows((snapshot.value.runs ?? []).map(run => ({id:run.id, title:`${run.ticket} · ${run.step}`, state:'Running', repo:run.repo, priority:'', number:`${run.seconds}s`}))));
startKeys();
const feed = live('floor');
await new Promise(resolve => setTimeout(resolve, 50));
assert.deepEqual(controller.rows().map(row => row.id), ['a', 'b', 'c']);
handlers.keydown({key:'j', target:{}, preventDefault() {}});
assert.equal(selectedItem('floor-runs'), 'b');
controller.setQuery('step');
const calls = scrolled;
revision.value = 2;
await feed.retry();
scroll.scrollTop = 0;
scroll.scrollLeft = 0;
component.componentDidUpdate();
assert.equal(controller.rows().find(row => row.id === 'b').title, 'T-b · step 2');
assert.equal(selectedItem('floor-runs'), 'b');
assert.deepEqual([...marked], ['b']);
assert.equal(scroll.scrollTop, 140);
assert.equal(scroll.scrollLeft, 9);
assert.equal(controller.state.query, 'step');
assert.equal(scrolled, calls);
feed.stop();
