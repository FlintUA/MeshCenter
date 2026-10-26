// tests/frontend/test_node_filters_popover_fit.mjs
//
// MOB2-F: on a phone the Sort and filters popover must fit between its top edge and the dock (a 320x568 screen is
// shorter than the whole list of options): opening it sets a max-height from the real space left, it scrolls
// inside; on a wide viewport no height is set; closing hides it. Runs the REAL functions from static/chat.js.
// `node tests/frontend/test_node_filters_popover_fit.mjs`

import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import path from 'node:path';
import vm from 'node:vm';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const source = readFileSync(path.join(__dirname, '..', '..', 'static', 'chat.js'), 'utf8').split('\r\n').join('\n');
function extractFunction(name) {
    const match = new RegExp(`^(async )?function ${name}\\(`, 'm').exec(source);
    assert.ok(match, `${name} not found`);
    return source.slice(match.index, source.indexOf('\n}\n', match.index) + 3);
}

function makeEnv({ mobile, innerHeight, top }) {
    const popover = {
        hidden: true,
        style: { maxHeight: '' },
        getBoundingClientRect: () => ({ top }),
    };
    const context = {
        document: { getElementById: (id) => (id === 'nodeFiltersPopover' ? popover : null) },
        window: { innerHeight, MCMobileLayout: { isMobile: () => mobile } },
        renderNodeFilterControls() {},
    };
    context.window.window = context.window;
    vm.createContext(context);
    vm.runInContext(['fitNodeFiltersPopover', 'setNodeFiltersPopoverOpen'].map(extractFunction).join('\n'), context);
    return { context, popover };
}

// 320x568 phone: the popover starts at y = 170, the dock and the safe area take about 64: 568 - 170 - 64 = 334
{
    const { context, popover } = makeEnv({ mobile: true, innerHeight: 568, top: 170 });
    context.setNodeFiltersPopoverOpen(true);
    assert.equal(popover.hidden, false);
    assert.equal(popover.style.maxHeight, '334px');
}

// never below a usable minimum
{
    const { context, popover } = makeEnv({ mobile: true, innerHeight: 400, top: 380 });
    context.setNodeFiltersPopoverOpen(true);
    assert.equal(popover.style.maxHeight, '160px');
}

// wide viewport: no inline height, the desktop popover is untouched
{
    const { context, popover } = makeEnv({ mobile: false, innerHeight: 900, top: 120 });
    popover.style.maxHeight = '123px';
    context.setNodeFiltersPopoverOpen(true);
    assert.equal(popover.style.maxHeight, '');
}

// closing hides it
{
    const { context, popover } = makeEnv({ mobile: true, innerHeight: 800, top: 120 });
    context.setNodeFiltersPopoverOpen(true);
    context.setNodeFiltersPopoverOpen(false);
    assert.equal(popover.hidden, true);
}

console.log('test_node_filters_popover_fit.mjs: ok');
