
import assert from 'node:assert/strict';
import { startKeys, readRetry, mode, notice } from './ui-keys.js';
const handlers = {};
let withItem = false;
const row = {dataset:{item:'a'}, hidden:false, disabled:false, getClientRects:() => [{}], getAttribute:() => null,
  setAttribute() {}, removeAttribute() {}, scrollIntoView() {}, matches:() => false, querySelector:() => null, click() {}};
const list = {dataset:{list:'x'}, querySelectorAll:selector => selector === '[data-row]' ? [row] : [], querySelector:() => null,
  uiContext:() => withItem ? {item:{id:'a'}} : null};
const paneEl = {dataset:{pane:'queue'}, toggleAttribute() {}, matches:() => false, querySelectorAll:selector => selector === '[data-list]' ? [list] : []};
globalThis.document = {querySelectorAll:selector => selector === '[data-pane]' ? [paneEl] : selector === '[data-list]' ? [list] : [],
  querySelector:() => null, addEventListener(name, fn) { handlers[name] = fn; }};
globalThis.window = {addEventListener() {}};
globalThis.location = {hash:'#floor'};
startKeys();
const key = value => handlers.keydown({key:value, target:{}, preventDefault() {}});
let retried = 0;
key('R');
assert.equal(retried, 0);
readRetry.value = () => { retried++; };
key('R');
assert.equal(retried, 1);
key('r');
assert.equal(retried, 1);
mode.value = 'INSERT';
key('R');
assert.equal(retried, 1);
mode.value = 'NORMAL';
withItem = true;
notice.value = '';
key('R');
assert.equal(retried, 1);
assert.notEqual(notice.value, '');
