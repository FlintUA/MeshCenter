// tests/frontend/test_nodes_csv.mjs
//
// H1-C3 (F-finding): the old node CSV export/import used
// `"${escapeCsv(n.name)}","${n.node_id}",...` (only n.name was escaped -
// the other 7 columns were interpolated raw) and
// `line.replace(/^"|"$/g,'').split('","')` on import (a naive string
// split, not an RFC-4180 parser - breaks on an embedded comma, an
// embedded newline inside a quoted field since the file was already torn
// into "lines" by '\n' before parsing even started, and an escaped ""
// since it just stripped every " unconditionally afterward).
//
// Dependency-free, same pattern as the other tests here: runs the REAL
// csvField()/parseCsv()/stripCsvFormulaGuard()/CSV_NODE_COLUMNS from
// static/chat.js in node:vm, round-tripping export -> import through the
// exact same string format a browser would download/re-upload.

import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import path from 'node:path';
import vm from 'node:vm';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const source = readFileSync(path.join(__dirname, '..', '..', 'static', 'chat.js'), 'utf8');

function extractFunction(name) {
    const match = new RegExp(`function\\s+${name}\\s*\\(`).exec(source);
    assert.ok(match, `function ${name} not found in static/chat.js`);
    const open = source.indexOf('{', source.indexOf(')', match.index));
    let depth = 0;
    for (let i = open; i < source.length; i++) {
        if (source[i] === '{') depth++;
        else if (source[i] === '}' && --depth === 0) return source.slice(match.index, i + 1);
    }
    throw new Error(`unbalanced braces in ${name}`);
}

function extractConst(name) {
    const match = new RegExp(`const\\s+${name}\\s*=\\s*\\[`).exec(source);
    assert.ok(match, `const ${name} not found in static/chat.js`);
    const open = source.indexOf('[', match.index);
    let depth = 0;
    for (let i = open; i < source.length; i++) {
        if (source[i] === '[') depth++;
        else if (source[i] === ']' && --depth === 0) {
            return source.slice(match.index, i + 1);
        }
    }
    throw new Error(`unbalanced brackets in ${name}`);
}

const ctx = vm.createContext({});
vm.runInContext(
    `${extractConst('CSV_NODE_COLUMNS')}\n` +
    `${extractFunction('csvField')}\n` +
    `${extractFunction('stripCsvFormulaGuard')}\n` +
    `${extractFunction('parseCsv')}\n` +
    `globalThis.CSV_NODE_COLUMNS = CSV_NODE_COLUMNS;\n` +
    `globalThis.csvField = csvField;\n` +
    `globalThis.stripCsvFormulaGuard = stripCsvFormulaGuard;\n` +
    `globalThis.parseCsv = parseCsv;\n`,
    ctx
);
const { CSV_NODE_COLUMNS, csvField, stripCsvFormulaGuard, parseCsv } = ctx;

// Values returned by parseCsv() are arrays constructed inside the vm
// context's own realm - deepEqual/deepStrictEqual on a different-realm
// Array can fail structural comparison even with identical contents
// (different Array constructor). All CSV cells are plain strings, so a
// JSON round-trip is a safe, simple way to get a same-realm plain array.
function toPlain(value) {
    return JSON.parse(JSON.stringify(value));
}

function exportRow(node) {
    return CSV_NODE_COLUMNS.map(col => csvField(col.get(node))).join(',');
}

function exportCsv(nodes) {
    const header = CSV_NODE_COLUMNS.map(col => csvField(col.header)).join(',');
    return [header].concat(nodes.map(exportRow)).join('\r\n');
}

function importCsv(text) {
    const rows = parseCsv(text).filter(row => row.some(cell => cell.trim() !== ''));
    const headers = rows[0].map(h => h.trim());
    const columnsByHeader = new Map(CSV_NODE_COLUMNS.map(col => [col.header, col]));
    const nodes = [];
    for (let i = 1; i < rows.length; i++) {
        const row = rows[i];
        const node = {};
        headers.forEach((h, idx) => {
            const column = columnsByHeader.get(h);
            if (!column) return;
            const raw = (row[idx] || '').trim();
            column.set(node, stripCsvFormulaGuard(raw));
        });
        if (node.node_id) nodes.push(node);
    }
    return nodes;
}

// ---------------------------------------------------------------------------
// 1) Round-trip: export -> import reproduces the original node fields,
//    across every hostile value the task calls out: , " newlines leading =
//    Cyrillic umlauts.
// ---------------------------------------------------------------------------
const HOSTILE_NODES = [
    {
        name: 'Plain Node', node_id: '!aabbccdd', last_time: '2026-10-02 10:00:00',
        rssi: '-80', snr: '5.5', role: 'CLIENT', short_name: 'PLN1', hw_model: 'RAK4631',
    },
    {
        // comma
        name: 'Node, With Comma', node_id: '!11111111', last_time: '', rssi: '', snr: '',
        role: 'CLIENT', short_name: 'CMA1', hw_model: '',
    },
    {
        // embedded double-quote
        name: 'Node "Quoted" Name', node_id: '!22222222', last_time: '', rssi: '', snr: '',
        role: 'CLIENT', short_name: '', hw_model: '',
    },
    {
        // embedded newline inside a field
        name: 'Line1\nLine2\r\nLine3', node_id: '!33333333', last_time: '', rssi: '', snr: '',
        role: 'CLIENT', short_name: '', hw_model: '',
    },
    {
        // leading = (formula injection attempt)
        name: '=1+1', node_id: '!44444444', last_time: '', rssi: '', snr: '',
        role: 'CLIENT', short_name: '', hw_model: '',
    },
    {
        // leading + and -
        name: '+SUM(A1:A9)', node_id: '!55555555', last_time: '', rssi: '', snr: '',
        role: '-DANGER', short_name: '', hw_model: '',
    },
    {
        // leading @ (another spreadsheet formula trigger)
        name: '@cmd|"/c calc"!A1', node_id: '!66666666', last_time: '', rssi: '', snr: '',
        role: 'CLIENT', short_name: '', hw_model: '',
    },
    {
        // Cyrillic + umlauts - must round-trip byte-for-byte, no mangling
        name: 'Узел Мюнхен Größe äöüß', node_id: '!77777777', last_time: '', rssi: '', snr: '',
        role: 'CLIENT', short_name: 'МЮН1', hw_model: 'Müllerhütte',
    },
    {
        // genuine leading apostrophe that is NOT a formula-injection guard -
        // must survive the round trip unchanged (not stripped).
        name: "'Tis a node name", node_id: '!88888888', last_time: '', rssi: '', snr: '',
        role: 'CLIENT', short_name: '', hw_model: '',
    },
];

const csv = exportCsv(HOSTILE_NODES);
const imported = importCsv(csv);

assert.equal(imported.length, HOSTILE_NODES.length, 'every node must survive the round trip');

for (let i = 0; i < HOSTILE_NODES.length; i++) {
    const original = HOSTILE_NODES[i];
    const got = imported[i];
    assert.equal(got.node_id, original.node_id, `node_id mismatch for row ${i}`);
    assert.equal(got.name, original.name, `name mismatch for row ${i}: ${JSON.stringify(got.name)} !== ${JSON.stringify(original.name)}`);
    assert.equal(got.role, original.role, `role mismatch for row ${i}`);
    assert.equal(got.short_name, original.short_name || '', `short_name mismatch for row ${i}`);
    assert.equal(got.hw_model, original.hw_model || '', `hw_model mismatch for row ${i}`);
}

// ---------------------------------------------------------------------------
// 2) A field containing a comma must not be split into two columns - the
//    exported CSV's row count (by comma, outside quotes) must match the
//    column count exactly.
// ---------------------------------------------------------------------------
{
    const node = { name: 'A, B, C', node_id: '!99999999' };
    const row = exportRow(node);
    const parsed = parseCsv(row)[0];
    assert.equal(parsed.length, CSV_NODE_COLUMNS.length, 'a comma inside a quoted field must not create extra columns');
    assert.equal(parsed[0], 'A, B, C');
}

// ---------------------------------------------------------------------------
// 3) Every exported field is wrapped in quotes, including empty ones -
//    the old template only quoted/escaped n.name.
// ---------------------------------------------------------------------------
{
    const row = exportRow({ name: 'X', node_id: '!aaaaaaaa' });
    const cells = row.split(',');
    assert.equal(cells.length, CSV_NODE_COLUMNS.length);
    for (const cell of cells) {
        assert.ok(cell.startsWith('"') && cell.endsWith('"'), `cell not quoted: ${cell}`);
    }
}

// ---------------------------------------------------------------------------
// 4) Formula-injection guard: csvField() prefixes =, +, -, @ with a plain
//    leading apostrophe; stripCsvFormulaGuard() reverses exactly that case
//    on import, never a genuine leading apostrophe (case 9 above already
//    covers the "don't over-strip" side).
// ---------------------------------------------------------------------------
for (const dangerous of ['=1+1', '+CMD', '-1', '@SUM(1)']) {
    const field = csvField(dangerous);
    assert.ok(field.includes("'" + dangerous.replace(/"/g, '""')), `csvField did not guard ${dangerous}: ${field}`);
    const parsedBack = parseCsv(field)[0][0];
    assert.equal(stripCsvFormulaGuard(parsedBack), dangerous);
}
assert.equal(stripCsvFormulaGuard("'Tis fine"), "'Tis fine", 'a genuine leading apostrophe must survive unstripped');

// ---------------------------------------------------------------------------
// 5) parseCsv() itself: embedded newline inside a quoted field spans what
//    would otherwise look like two "lines".
// ---------------------------------------------------------------------------
{
    const text = '"a","b\nc","d"\r\n"1","2","3"';
    const rows = parseCsv(text);
    assert.equal(rows.length, 2, 'a newline inside a quoted field must not create an extra row');
    assert.deepEqual(toPlain(rows[0]), ['a', 'b\nc', 'd']);
    assert.deepEqual(toPlain(rows[1]), ['1', '2', '3']);
}

// ---------------------------------------------------------------------------
// 6) parseCsv() unescapes a doubled "" into a single literal " inside a
//    quoted field.
// ---------------------------------------------------------------------------
{
    const rows = parseCsv('"he said ""hi""","ok"');
    assert.deepEqual(toPlain(rows[0]), ['he said "hi"', 'ok']);
}

console.log('test_nodes_csv.mjs: ok');
