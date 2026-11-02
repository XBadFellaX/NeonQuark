const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const test = require('node:test');
const source = fs.readFileSync(`${__dirname}/ion-drift.user.js`, 'utf8');
const mediator = 'https://greenmotors.cc/homelander/';
const target = 'https://hubcloud.ist/drive/example';
const b64 = value => Buffer.from(value).toString('base64');
function encode(url = target, page = mediator) {
    const value = b64(JSON.stringify({ o: b64(url), l: page, w: 10 }))
        .replace(/[a-zA-Z]/g, c => String.fromCharCode(c.charCodeAt(0) + (c.toLowerCase() <= 'm' ? 13 : -13)));
    return b64(b64(value));
}
function run({ html = '', href = mediator, stored = null, blockedStorage = false } = {}) {
    const redirects = [];
    const document = { scripts: [{ textContent: html }] };
    let inspect;
    let disconnected = false;
    let finish;
    vm.runInNewContext(source, {
        URL, atob, Date,
        document,
        location: { href, origin: new URL(href).origin, replace: url => redirects.push(url) },
        localStorage: { getItem() { if (blockedStorage) throw Error('Blocked'); return JSON.stringify(stored); } },
        MutationObserver: class {
            constructor(callback) { inspect = callback; }
            observe() {}
            disconnect() { disconnected = true; }
        },
        setTimeout: callback => { finish = callback; return 1; },
        clearTimeout() {},
        console: { warn() {} },
    });
    return { redirects, document, inspect, finish, disconnected: () => disconnected };
}
test('inline payload navigates once and disconnects the observer', () => {
    const result = run({ html: `s('o','${encode()}',180000);`, href: 'https://greenmotors.cc/?id=new' });
    assert.deepEqual(result.redirects, [target]);
    assert.equal(result.disconnected(), true);
    result.inspect();
    assert.equal(result.redirects.length, 1);
});
test('document-start detects a script added later', () => {
    const result = run({ href: 'https://greenmotors.cc/?id=new' });
    result.document.scripts.push({ textContent: `s("o", "${encode('https://hblinks.pro/example')}")` });
    result.inspect();
    assert.deepEqual(result.redirects, ['https://hblinks.pro/example']);
});
test('mediator resumes an unexpired stored link', () => {
    assert.deepEqual(run({ stored: { value: encode(), expiry: Date.now() + 60000 } }).redirects, [target]);
});
test('stale, expired, unrelated and id-page stored links are ignored', () => {
    for (const options of [
        { href: 'https://greenmotors.cc/?id=new', stored: { value: encode(), expiry: Date.now() + 60000 } },
        { stored: { value: encode(), expiry: 1 } },
        { stored: { value: encode(target, 'https://greenmotors.cc/other/'), expiry: Date.now() + 60000 } },
        { blockedStorage: true },
    ]) assert.deepEqual(run(options).redirects, []);
});
test('malformed payloads and unexpected destinations leave the page available', () => {
    for (const value of ['bad', encode('javascript:alert(1)'), encode('http://hubcloud.ist/a'),
        encode('https://hubcloud.ist.evil.example/a'), encode('https://hubcloud.ist@evil.example/a'),
        encode('https://user:pass@hubcloud.ist/a'), encode('https://hubcloud.ist:8443/a'),
        encode(target, 'https://evil.example/homelander/')]) {
        assert.deepEqual(run({ html: `s('o','${value}')` }).redirects, []);
    }
});
test('observer stops after fifteen seconds on unrelated pages', () => {
    const result = run();
    result.finish();
    assert.equal(result.disconnected(), true);
});
