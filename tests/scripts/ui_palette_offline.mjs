
import assert from 'node:assert/strict';
import { fuzzy } from './ui-fuzzy.js';
import { palette, configurePalette, openPalette, closePalette, queryPalette, register, runPalette, Palette, paletteRouteChanged } from './ui-palette.js';
import { startKeys, screenKeys, mode, pane, pending, sheet } from './ui-keys.js';
const handlers = {}, windows = {};
let restored = 0, focused = 0, scrolled = 0, prepared = 0, ran = 0;
const opener = {isConnected:true, focus(){restored++;}};
const input = {tagName:'INPUT', type:'search', focus(){focused++;}};
globalThis.document = {activeElement:opener, documentElement:{dataset:{}},
  querySelector:()=>input, querySelectorAll:()=>[], addEventListener(name, fn){handlers[name]=fn;}};
globalThis.window = {addEventListener(name, fn){windows[name]=fn;}};
globalThis.location = {hash:'#floor'};
configurePalette(screenKeys.map(([key,id,title,href])=>({key,id,title,href})), ()=>{prepared++; pending.value=''; sheet.value=false;});
const { paletteFixture } = await import('./ui-hooks.js');
startKeys();
function key(value, flags={}, target={}) {
  let prevented = false;
  handlers.keydown({key:value, target, ...flags, preventDefault(){prevented=true;}});
  return prevented;
}
const ids = () => palette.peek().matches.map(row=>row.id);
const rows = [
  {id:'sub', title:'File library', group:'Go'}, {id:'long',title:'Floor plan', group:'Go'},
  {id:'exact',title:'Floor',group:'Go'}, {id:'other',title:'Floor',group:'Items'}];
const frozen = JSON.stringify(rows);
assert.deepEqual(fuzzy(rows,'FLOOR').map(([g,rs])=>[g,rs.map(r=>r.id)]), [['Go',['exact','long']],['Items',['other']]]);
assert.deepEqual(fuzzy(rows,'fl')[0][1].map(r=>r.id), ['long','exact','sub']);
assert.deepEqual(fuzzy(rows,'')[0][1].map(r=>r.id), ['sub','long','exact']);
assert.deepEqual(fuzzy(rows,'f l o o r'), []);
assert.deepEqual(fuzzy(rows,'zzzz'), []);
assert.equal(JSON.stringify(rows), frozen);
pending.value = 'g'; sheet.value = true;
assert(key(':'));
assert.equal(prepared, 1);
assert.equal(pending.value, ''); assert.equal(sheet.value,false);
assert.equal(palette.peek().groups[0][0], 'Go to');
assert.equal(ids().length,11);
assert.equal(palette.peek().index,0);
key('k'); key('p',{ctrlKey:true}); assert.equal(palette.peek().index,0);
key('j'); key('n',{ctrlKey:true}); assert.equal(palette.peek().index,2);
for(let i=0;i<30;i++) key('j');
assert.equal(palette.peek().index,10);
assert.equal(location.hash,'#floor'); assert.equal(pane.value,'queue');
queryPalette('fl'); assert.equal(ids()[0],'floor');
key('Enter'); assert.equal(palette.peek(),null); assert.equal(location.hash,'#floor');
assert.equal(restored,1);
mode.value='INSERT';
assert.equal(key(':',{},input),false);
assert.equal(palette.peek(),null);
assert(key('k',{metaKey:true},input));
queryPalette('sett'); assert.deepEqual(ids(),['settings']);
queryPalette('impossible');
assert.deepEqual(ids(),[]);
for(const [k,flags] of [['j',{}],['k',{}],['n',{ctrlKey:true}],['p',{ctrlKey:true}],['Enter',{}]]) key(k,flags,input);
assert(palette.peek()); assert.equal(location.hash,'#floor');
assert(key('Tab',{},input)); assert.equal(focused,1);
assert(key('Escape',{},input)); assert.equal(palette.peek(),null);
key(':',{isComposing:true}); assert.equal(palette.peek(),null);
mode.value='NORMAL';
key('f'); assert.equal(location.hash,'#floor');
key('?'); assert.equal(sheet.value,true); key('Escape'); assert.equal(sheet.value,false);
const old = register('floor',[{id:'old',title:'Old'}]);
const cleanup = register('floor',[{id:'item',title:'Item'}],[{id:'cmd',title:'Display',run(){ran++;}}]);
old();
register('triage',[{id:'triage-item',title:'Triage item'}],[{id:'triage-cmd',title:'Triage command'}]);
openPalette();
assert(ids().includes('item')); assert(!ids().includes('old')); assert(ids().includes('triage-item'));
queryPalette('disp'); assert.equal(palette.peek().groups[0][0],'Commands');
key('Enter'); key('Enter'); assert.equal(ran,1);
openPalette(); location.hash='#triage'; windows.hashchange();
assert(ids().includes('triage-cmd')); assert(!ids().includes('cmd')); assert(ids().includes('item'));
cleanup(); assert(!ids().includes('item'));
closePalette();
for(const picker of ['steps','triggers','prompts','roles','drop','wires','create']) {
  paletteFixture(picker);
  assert.equal(palette.peek().mode,picker);
  assert(palette.peek().groups.length);
  if(picker==='prompts') {
    assert.deepEqual(palette.peek().groups.map(([g])=>g),['None','This ADW','Other ADWs','Loops','Arete']);
    assert.equal(palette.peek().matches[palette.peek().index].id,'build');
    queryPalette('buil'); assert.deepEqual(ids(),['build']);
    queryPalette(''); key('k'); key('k'); key('Enter');
    assert.equal(document.documentElement.dataset.uiPaletteResult,'none');
  } else if(picker==='roles') {
    key('Enter'); assert.equal(document.documentElement.dataset.uiPaletteResult,'default');
  } else {
    const title = palette.peek().matches[0].title;
    queryPalette(title[0]+title.at(-1)); assert(ids().length);
    const selected = ids()[0];
    key('Enter'); assert.equal(document.documentElement.dataset.uiPaletteResult,selected);
  }
  assert.equal(palette.peek(),null);
  const result = document.documentElement.dataset.uiPaletteResult;
  paletteFixture(picker); key('Escape');
  assert.equal(document.documentElement.dataset.uiPaletteResult,result);
  paletteFixture(picker); location.hash='#adws/another'; paletteRouteChanged();
  assert.equal(palette.peek(),null);
}
paletteFixture('unknown'); assert.equal(palette.peek(),null);
location.hash='#floor'; paletteFixture('registry');
queryPalette('sett'); assert.deepEqual(palette.peek().groups.map(([g])=>g),['Go to','Items','Commands']);
queryPalette('sample item');
function nodes(node) {
  if(!node || typeof node!=='object') return [];
  return [node,...[node.props?.children].flat(Infinity).flatMap(nodes)];
}
const component = new Palette();
component.setState = change => Object.assign(component.state,change);
component.base = {querySelector(selector){return selector==='input' ? input : {scrollIntoView(){scrolled++;}};}};
component.componentDidMount(); component.componentDidUpdate();
const tree = component.render();
assert(nodes(tree).some(n=>n.props?.children==='Sample item <literal>'));
assert(nodes(tree).some(n=>n.props?.children==='<b>plain text</b>'));
const option = nodes(tree).find(n=>n.props?.role==='option');
option.props.onClick(); assert.equal(document.documentElement.dataset.uiPaletteResult,'sample-item');
component.componentDidUpdate();
openPalette({mode:'steps',rows:[{id:'x',title:'Example',group:'Agent'}],onPick(){ran++;}});
component.componentDidUpdate(); assert(focused>=3); assert(scrolled>=2);
const stable = palette.peek(); component.render(); component.componentDidUpdate();
assert.equal(palette.peek(),stable);
const scrim = component.render(); const outside = {};
scrim.props.onClick({target:outside,currentTarget:outside}); assert.equal(palette.peek(),null);
assert.equal(ran,1);
component.componentWillUnmount();
