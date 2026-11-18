const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const test = require('node:test');

const source = fs.readFileSync(`${__dirname}/rfd-quiet.user.js`, 'utf8');

function run({ href = 'https://forums.redflagdeals.com/hot-deals-f9/', theme = 'light', cookie = '' } = {}) {
    const root = { getAttribute(name) { return name === 'data-theme' ? theme : null; }, setAttribute(name, value) { if (name === 'data-theme') theme = value; } };
    const head = { appendChild(node) { nodes.push(node); } };
    const nodes = [];
    const document = {
        documentElement: root,
        head,
        cookie,
        getElementById(id) { return nodes.find(node => node.id === id) ?? null; },
        createElement(tag) { return { id: '', textContent: '', tagName: tag.toUpperCase() }; },
    };
    let observerCallback;
    vm.runInNewContext(source, {
        URL,
        location: { href, origin: new URL(href).origin },
        document,
        MutationObserver: class {
            constructor(callback) { observerCallback = callback; }
            observe() {}
            disconnect() {}
        },
    });
    return { theme: () => theme, nodes, observerCallback, cookie: () => document.cookie };
}

test('metadata targets redflagdeals hosts', () => {
    const includes = [...source.matchAll(/^\/\/ @include\s+(\S+)/gm)].map(([, pattern]) =>
        new RegExp('^' + pattern.split('*').map(part => part.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')).join('.*') + '$'));
    for (const href of [
        'https://redflagdeals.com/',
        'https://www.redflagdeals.com/',
        'https://forums.redflagdeals.com/hot-deals-f9/',
    ]) {
        assert.ok(includes.some(pattern => pattern.test(href)), href);
    }
});

test('forces dark theme and injects sponsored hiding rules', () => {
    const result = run();
    assert.equal(result.theme(), 'dark');
    assert.match(result.nodes[0].textContent, /\.sponsored-offer/);
    assert.match(result.nodes[0].textContent, /\.ad_sponsored_deal/);
});

test('re-applies dark theme when the site changes data-theme', () => {
    const result = run({ theme: 'light' });
    assert.equal(result.theme(), 'dark');
    result.observerCallback([{ type: 'attributes', attributeName: 'data-theme' }]);
    assert.equal(result.theme(), 'dark');
});

test('hostname guard ignores unrelated sites', () => {
    const result = run({ href: 'https://evil-redflagdeals.com/' });
    assert.notEqual(result.theme(), 'dark');
    assert.equal(result.nodes.length, 0);
});

test('persists the site dark-mode cookie for future requests', () => {
    const result = run({ cookie: 'loggedin=1' });
    assert.match(result.cookie(), /rfd_dark_mode=1/);
});

test('adds homepage dark overrides only on the main site', () => {
    const home = run({ href: 'https://www.redflagdeals.com/' });
    const forums = run({ href: 'https://forums.redflagdeals.com/hot-deals-f9/' });
    assert.match(home.nodes[0].textContent, /\.image-wrapper/);
    assert.doesNotMatch(forums.nodes[0].textContent, /\.image-wrapper/);
});