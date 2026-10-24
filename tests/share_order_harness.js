// Node harness for tests/test_share_order.py (and for reproducing the old bug).
//
// Loads the share-ordering code straight out of a copy of templates/index.html (no copy of the logic lives
// here), runs it against the scenarios on stdin and prints JSON on stdout.
//   node share_order_harness.js <index.html>  < scenarios.json
// A scenario is {name, ids:[...], fail:[slots that never load], types:{id: mime}, seed:int}.
// "New" code = the block between the SHARE-ORDER-BEGIN/END markers. If the markers are absent the file is the
// OLD template and its chunkArray()/buildShareFiles() are used the way copySelectedImages() used them.
const fs = require("fs");
const vm = require("vm");

class FakeFile {
    constructor(parts, name, opts) { this.parts = parts; this.name = name; this.type = (opts && opts.type) || ""; }
}

function extractFunction(source, name) {
    const match = new RegExp("(?:async )?function " + name + "\\(").exec(source);
    if (!match) throw new Error("function not found: " + name);
    let i = source.indexOf("{", match.index);
    let depth = 0;
    for (let j = i; j < source.length; j += 1) {
        if (source[j] === "{") depth += 1;
        if (source[j] === "}") { depth -= 1; if (depth === 0) return source.slice(match.index, j + 1); }
    }
    throw new Error("unbalanced: " + name);
}

function loadCode(html) {
    const begin = html.indexOf("SHARE-ORDER-BEGIN");
    const end = html.indexOf("SHARE-ORDER-END");
    let code;
    let isNew = false;
    if (begin >= 0 && end > begin) {
        code = html.slice(html.indexOf("\n", begin), end);
        isNew = true;
    } else {
        code = "const SHARE_BATCH_SIZE = 10;\n" + extractFunction(html, "chunkArray") + "\n" + extractFunction(html, "buildShareFiles");
    }
    const sandbox = { File: FakeFile, String, Math, Array, Promise, setTimeout, Error, Map, Set, Number };
    vm.createContext(sandbox);
    vm.runInContext(code + "\nthis.__api = {" + (isNew
        ? "shareFileName, shareIndexWidth, fetchInIntendedOrder, planShareBatches, buildShareFiles, describeMissingSlots, SHARE_BATCH_SIZE, SHARE_FETCH_CONCURRENCY"
        : "chunkArray, buildShareFiles, SHARE_BATCH_SIZE") + "};", sandbox);
    return { api: sandbox.__api, isNew };
}

function rng(seed) {
    let s = seed >>> 0 || 1;
    return () => { s = (Math.imul(s, 1664525) + 1013904223) >>> 0; return s / 4294967296; };
}

function byName(files) {   // what a chooser that sorts by file name shows (stable: ties keep arrival order)
    return files.map((f, arrival) => ({ f, arrival }))
        .sort((a, b) => (a.f.name < b.f.name ? -1 : a.f.name > b.f.name ? 1 : a.arrival - b.arrival))
        .map(x => x.f);
}

async function run(scenario, api, isNew) {
    const ids = scenario.ids;
    const fail = new Set(scenario.fail || []);
    const types = scenario.types || {};
    const rand = rng(scenario.seed || 1);
    const blobFor = id => ({ type: types[id] || "image/jpeg", id });
    let results;
    let batches;
    let completionOrder = [];
    if (isNew) {
        const slotOf = new Map(ids.map((id, i) => [id, i]));
        const fetchOne = id => new Promise((resolve, reject) => {
            // random delay => completion order is NOT intended order
            setTimeout(() => {
                completionOrder.push(slotOf.get(id));
                if (fail.has(slotOf.get(id))) reject(new Error("fail")); else resolve(blobFor(id));
            }, Math.floor(rand() * 8));
        });
        results = await api.fetchInIntendedOrder(ids, fetchOne, api.SHARE_FETCH_CONCURRENCY);
        batches = api.planShareBatches(results, api.SHARE_BATCH_SIZE);
    } else {
        results = ids.map((id, i) => ({ id, blob: fail.has(i) ? null : blobFor(id) }));
        batches = api.chunkArray(results.filter(r => r.blob), api.SHARE_BATCH_SIZE);
    }
    const total = ids.length;
    const sent = batches.map(batch => {
        const files = isNew ? api.buildShareFiles(batch, total) : api.buildShareFiles(batch);
        return files.map((file, i) => ({ name: file.name, id: batch[i].id }));
    });
    const expected = ids.filter((_, i) => !fail.has(i));
    const flat = sent.flat();
    const sheetView = sent.map(batch => byName(batch)).flat().map(f => f.id);        // each share sheet on its own, in batch order
    const folderView = byName(flat).map(f => f.id);                                  // all batches land in one folder / gallery
    const names = flat.map(f => f.name);
    return {
        name: scenario.name,
        count: total,
        batches: sent.length,
        batchSizes: sent.map(b => b.length),
        firstName: names[0], lastName: names[names.length - 1],
        uniqueNames: new Set(names).size === names.length,
        namesSortedEqualsSent: JSON.stringify(names) === JSON.stringify([...names].sort()),
        expected, sheetView, folderView,
        sentIds: flat.map(f => f.id),
        sentNames: names,
        // true when the (random) completion order really differed from the intended order, i.e. the test had teeth
        completionWasShuffled: isNew ? completionOrder.some((v, i) => i > 0 && v < completionOrder[i - 1]) : null,
        missing: isNew ? api.describeMissingSlots(results, total) : null,
        slots: isNew ? results.map(r => (r.blob ? r.slot : -1)) : null,
        width: isNew ? api.shareIndexWidth(total) : null,
    };
}

(async () => {
    const html = fs.readFileSync(process.argv[2], "utf8");
    const { api, isNew } = loadCode(html);
    const input = JSON.parse(fs.readFileSync(0, "utf8"));
    const out = [];
    for (const scenario of input.scenarios) out.push(await run(scenario, api, isNew));
    process.stdout.write(JSON.stringify({ isNew, results: out }));
})().catch(e => { console.error(e.stack); process.exit(1); });
