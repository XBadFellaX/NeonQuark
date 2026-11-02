// ==UserScript==
// @name         Ion Drift
// @namespace    https://github.com/XBadFellaX/NeonQuark
// @version      1.0.2
// @homepageURL  https://github.com/XBadFellaX/NeonQuark
// @downloadURL  https://raw.githubusercontent.com/XBadFellaX/NeonQuark/main/ion-drift.user.js
// @updateURL    https://raw.githubusercontent.com/XBadFellaX/NeonQuark/main/ion-drift.user.js
// @description  A small personal browser navigation utility.
// @include      https://greenmotors.*/*
// @include      https://www.greenmotors.*/*
// @run-at       document-start
// @grant        none
// @noframes
// ==/UserScript==

(() => {
    'use strict';

    const page = new URL(location.href);
    // shortcut: single-part suffixes only; update if the site changes its name or uses a compound suffix.
    if (page.protocol !== 'https:' || page.username || page.password || page.port ||
        !/^(?:www\.)?greenmotors\.[a-z]{2,63}$/.test(page.hostname)) return;

    let redirected = false;
    let observer;
    let timeout;

    function decode(value) {
        // shortcut: this site's encoding and hosts only; update when its redirect format changes.
        const rotated = atob(atob(value)).replace(/[a-zA-Z]/g, c =>
            String.fromCharCode(c.charCodeAt(0) + (c.toLowerCase() <= 'm' ? 13 : -13)));
        const data = JSON.parse(atob(rotated));
        const target = new URL(atob(data.o));
        const mediator = new URL(data.l);
        if (target.protocol !== 'https:' || target.username || target.password || target.port ||
            (!/^(?:www\.)?(?:hubcloud|hblinks)\.[a-z]+$/.test(target.hostname) &&
                target.hostname !== 'new.multicloudlinks.com') ||
            mediator.origin !== location.origin) {
            throw new Error('Unexpected redirect destination');
        }
        return { target: target.href, mediator: mediator.href };
    }

    function redirect(value, fromStorage = false) {
        if (redirected) return;
        try {
            const result = decode(value);
            // Stored links belong to their mediator page, never to a newly opened id link.
            if (fromStorage && result.mediator !== location.href) return;
            redirected = true;
            observer?.disconnect();
            clearTimeout(timeout);
            location.replace(result.target);
        } catch (error) {
            console.warn('[Ion Drift] Link could not be decoded; leaving page available.', error);
        }
    }

    function inspect() {
        for (const script of document.scripts) {
            const match = script.textContent.match(/\bs\(\s*['"]o['"]\s*,\s*['"]([A-Za-z0-9+/=]+)['"]/);
            if (match) redirect(match[1]);
        }
        if (redirected || new URL(location.href).searchParams.has('id')) return;
        try {
            const stored = JSON.parse(localStorage.getItem('o'));
            if (stored && stored.expiry > Date.now()) redirect(stored.value, true);
        } catch (error) {
            console.warn('[Ion Drift] Stored link unavailable; leaving page available.', error);
        }
    }

    observer = new MutationObserver(inspect);
    observer.observe(document, { childList: true, subtree: true });
    timeout = setTimeout(() => observer.disconnect(), 15000);
    inspect();
})();
