import assert from 'node:assert/strict';
import { webcrypto } from 'node:crypto';
import fs from 'node:fs';
import vm from 'node:vm';
globalThis.crypto ??= webcrypto;
globalThis.location = {hash:'#floor', host:'127.0.0.1:1'};
globalThis.window = {addEventListener(){}};
globalThis.document = {documentElement:{dataset:{}}, querySelector:()=>null,
  querySelectorAll:()=>[], addEventListener(){}};
const {Sidebar} = await import('./ui-shell.js');

const text = (node) => node == null || node === false ? '' : typeof node !== 'object' ? String(node)
  : Array.isArray(node) ? node.map(text).join('') : [node.text, ...(node.kids ?? []).map(text), ...[node.props?.children].flat(Infinity).map(text)].join('');
const context = vm.createContext({
  el: (tag, props = {}, ...kids) => ({tag, text: props.text, kids}),
  stateChip: (state) => `[${state}]`, clock: (ts) => `at ${ts}`, ago: (ts) => `${ts} ago`, liveAgo: (ts) => `${ts} ago`,
  count: (n, one, many) => `${n} ${n === 1 ? one : many}`, button: (label) => ({text: label}), saidLine: () => '',
  SLOW_MS: 1000, capitalized:(s) => s.charAt(0).toUpperCase() + s.slice(1), cell: (c) => c, itemLink: (...a) => a[2], link: (h, t) => t,
});
for (const file of ['loops.js', 'refresh.js', 'health.js']) vm.runInContext(fs.readFileSync(file, 'utf8'), context, {filename: file});
const run = (code) => vm.runInContext(code, context);
context.beat = (extra) => ({at: 'T', alive: true, started: 'S', loops: 3, off: 1, running: 0, pending: 0, ...extra});
const failing = {'loop:aaa': {error: 'ValueError: boom', since: 'S', ticks: 3}};
const rendered = (serve) => ({
  loops: text(run('serveState')(serve)), short: text(run('serveShort')(serve)),
  health: text(run('healthSummary')({serve, state: 'fault', checks: {rows: {}}, alerts: {rows: {}}, devices: {rows: []}}, run('failures')({serve, checks: {rows: {}}, alerts: {rows: {}}, devices: {rows: []}}).map(r => ({state: r.state})))),
  rows: text(run('failures')({serve, checks: {rows: {}}, alerts: {rows: {}}, devices: {rows: []}}).map(r => r.row)),
  side: text(Sidebar({route: {active: 'floor'}, snapshot: {serve: {...serve, age_seconds: 2}, needsYou: 0, running: 0, received: 0}, now: 0})),
});

const fresh = rendered(context.beat({failing, stuck: true, tick_ok: false}));
for (const part of ['loops', 'short', 'health', 'rows', 'side']) {
  assert.match(fresh[part], /loop:aaa/, part);
  assert.match(fresh[part], /ValueError: boom/, part);
  assert.doesNotMatch(fresh[part], part === 'side' ? /serve down/ : /down|stuck/i, part);
}
assert.match(fresh.side, /serve up/);

const withConfig = rendered(context.beat({failing: {'tick:load': {error: 'ValueError: bad toml', since: 'S', ticks: 2}}, config_problem: 'config.toml is unreadable'}));
for (const part of ['loops', 'short']) {
  assert.match(withConfig[part], /config\.toml is unreadable/, part);
  assert.match(withConfig[part], /tick:load/, part);
  assert.match(withConfig[part], /bad toml/, part);
}
assert.match(withConfig.rows, /bad toml/);

const recovered = rendered(context.beat({failing: {}, tick_ok: true, stuck: false}));
for (const part of ['loops', 'short', 'health', 'side']) assert.doesNotMatch(recovered[part], /tick fails/, part);
assert.match(recovered.side, /serve up/);

const older = rendered(context.beat({running: undefined}));
assert.doesNotMatch(older.loops + older.short + older.side, /tick fails|undefined/);

for (const serve of [context.beat({alive: false, process_gone: true, failing}), context.beat({alive: false, failing})]) {
  const gone = rendered(serve);
  assert.match(gone.loops, /process is gone|looks stuck/);
  assert.match(gone.short, /Serve down/);
  assert.doesNotMatch(gone.loops + gone.short, /tick fails/);
  assert.match(gone.side, /serve down/);
  assert.doesNotMatch(gone.side, /tick fails/);
}
