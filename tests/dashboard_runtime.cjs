const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const html = fs.readFileSync(process.argv[2], 'utf8');
const elements = new Map();
function element() {
  return {value: 0, textContent: '', className: '', style: {}, appendChild() {},
    set innerHTML(value) { this.html = value; },
    get innerHTML() { return this.html ?? this.textContent.replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;'); }};
}
for (const match of html.matchAll(/id="([^"]+)"/g)) elements.set(match[1], element());
for (const [id,value] of [['sliderBear',25],['sliderBase',50],['sliderBull',25]]) elements.get(id).value=value;
const document = { getElementById(id) { assert(elements.has(id), `Missing element: ${id}`); return elements.get(id); },
  createElement() { return element(); }};
const context = vm.createContext({document,console});
vm.runInContext(html.match(/<script>([\s\S]*?)<\/script>/)[1],context);
const text=id=>elements.get(id).textContent;
assert.equal(text('dynExpectedValue'),'$20.00');
assert.equal(text('target15'),'$17.00');
assert.equal(text('target25'),'$15.00');
assert.equal(text('target35'),'$13.00');
assert.match(text('modelWarnings'),/Fixture warning/);
elements.get('sliderBear').value=0;elements.get('sliderBase').value=0;elements.get('sliderBull').value=100;
vm.runInContext('updateScenarioWeights()',context);
assert.equal(text('dynExpectedValue'),'$40.00');
assert.equal(text('target15'),'$34.00');
assert.equal(text('target25'),'$30.00');
assert.equal(text('target35'),'$26.00');
assert.equal(text('dispDiscount'),'75.0%');
for (const id of ['sliderBear','sliderBase','sliderBull']) elements.get(id).value=0;
vm.runInContext('updateScenarioWeights()',context);
assert.equal(text('dynExpectedValue'),'$20.00');
console.log('Dashboard calculations, warnings, targets and zero-weight reset verified');
