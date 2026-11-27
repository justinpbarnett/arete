// #978 was observed after release. Replay the user's chat switch while B's fetch is outstanding.
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import vm from "node:vm";

const elements = new Map();
function element(id) {
  if (!elements.has(id)) elements.set(id, {value: "", hidden: false, disabled: false});
  return elements.get(id);
}
const page = vm.createContext({
  document: {getElementById: element}, matchMedia: () => ({matches: false}),
  putBackConsoleScroll: () => {}, money: () => "$0", quiet: () => "", fill: () => {}, clock: () => "", withFigures: x => x,
});
vm.runInContext(readFileSync(resolve(process.argv[2], "arete/web/console.js"), "utf8"), page);
vm.runInContext(`
  switchChat(1);
  drawChat({id:1, draft:{chat:1,title:"A",body:"A"}, messages:[], sessions:{sessions:[]}});
`, page);
const initiallyEnabled = !element("draft-file").disabled && !element("draft-drop").disabled;
vm.runInContext("switchChat(2)", page);
const held = element("draft-file").disabled && element("draft-drop").disabled;
vm.runInContext('drawChat({id:1,draft:{chat:1,title:"A",body:"A"},messages:[],sessions:{sessions:[]}})', page);
const staleIgnored = element("draft-file").disabled && element("draft-drop").disabled;
vm.runInContext('drawChat({id:2,draft:{chat:2,title:"B",body:"B"},messages:[],sessions:{sessions:[]}})', page);
const resumed = !element("draft-file").disabled && !element("draft-drop").disabled && element("draft-title").textContent === "B";
console.log(JSON.stringify({pass:initiallyEnabled && held && staleIgnored && resumed, initiallyEnabled, held, staleIgnored, resumed}));
