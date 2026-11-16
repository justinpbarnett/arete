import assert from 'node:assert/strict';
import { webcrypto } from 'node:crypto';
import { readFile, writeFile } from 'node:fs/promises';
globalThis.crypto ??= webcrypto;
globalThis.location = {hash: '#run/r1'};
globalThis.window = {addEventListener() {}};
globalThis.document = {querySelector: () => null, querySelectorAll: () => [], addEventListener() {},
  documentElement: {dataset: {}}, body: {style: {removeProperty() {}, setProperty() {}}}};
globalThis.matchMedia = () => ({matches: false});
globalThis.cancelAnimationFrame = () => {};
globalThis.CSS = {escape: value => value};
const {dispatch, actions, runContext, runPageContext, repairThisRun, replayLast} = await import('./ui-actions.js');
const {notice} = await import('./ui-notice.js');
let conflict = false, refreshes = 0;
const posts = [];
globalThis.fetch = async (url, options) => {
  posts.push({url, ...options});
  return {ok: !conflict, status: conflict ? 409 : 200, headers: {get: () => 'fixture-code'},
    json: async () => conflict ? {error: 'Refresh before trying again.', code: 'version_conflict', current: {id: 'r1', version: 8}}
      : {run: 'r2', stopped: true}};
};
const body = () => JSON.parse(posts.at(-1).body);
const run = {id: 'r1', status: 'failed', version: 7};
const context = runPageContext(run, {refresh: () => refreshes++, go() {}, openLink() {}});
await dispatch(context.run.retry, context);
assert.equal(body().base_version, 7);
assert.equal(posts.at(-1).url, '/api/runs/r1/resume');
await dispatch(repairThisRun, context);
assert.equal(body().base_version, 7);
assert.equal(posts.at(-1).url, '/api/runs/r1/repair');
for (const verb of ['stop', 'resume', 'repair', 'steps']) {
  await dispatch(actions[verb], runContext({...run, version: 6}, verb, {}, verb === 'steps' ? 'build' : ''));
  assert.equal(body().base_version, 6);
}
conflict = true;
const count = posts.length;
assert.equal(await dispatch(actions.stop, context), false);
assert.equal(posts.length, count + 1);
assert.equal(body().base_version, 7);
assert.equal(refreshes, 3);
assert.match(notice.value, /Refresh before trying again/);
conflict = false;
await replayLast();
assert.equal(body().base_version, 6);
assert.equal(posts.at(-1).body, posts[count - 1].body);
assert.equal(posts.at(-1).headers['Idempotency-Key'], posts[count - 1].headers['Idempotency-Key']);
const names = ['common.js', 'said-line.js', 'live.js', 'run-page.js'];
const legacy = (await Promise.all(names.map(name => readFile(new URL(name, import.meta.url), 'utf8')))).join('\n');
await writeFile(new URL('legacy-check.mjs', import.meta.url), legacy + `
let refreshed = 0, served = 0;
function refresh() { refreshed++; }
function currentSection() { return {querySelector() { return null; }}; }
function noteServedCode(response) { if (response.headers.get('X-Arete-Code')) served++; }
export {stopLiveRun, stopRun, versionedRunWrite, lastSaid};
export const observations = () => ({refreshed, served});
`);
const old = await import('./legacy-check.mjs');
const btn = () => ({disabled: false, closest: () => null});
await old.stopLiveRun({id: 'old-live', version: 11, ticket: 'task'}, btn());
assert.equal(body().base_version, 11);
await old.stopRun({id: 'old-detail', version: 12}, 'detail:run:old-detail', btn());
assert.equal(body().base_version, 12);
conflict = true;
const before = posts.length;
await old.stopLiveRun({id: 'old-live', version: 11, ticket: 'task'}, btn());
assert.equal(posts.length, before + 1);
assert.equal(body().base_version, 11);
assert.match(old.lastSaid.get('running')[1], /Refresh before trying again/);
assert.deepEqual(old.observations(), {refreshed: 3, served: 3});
await new Promise(resolve => setTimeout(resolve, 5));
