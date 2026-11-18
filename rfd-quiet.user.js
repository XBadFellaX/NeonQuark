// ==UserScript==
// @name         RFD Quiet
// @namespace    https://github.com/XBadFellaX/NeonQuark
// @version      1.0.0
// @homepageURL  https://github.com/XBadFellaX/NeonQuark
// @downloadURL  https://raw.githubusercontent.com/XBadFellaX/NeonQuark/main/rfd-quiet.user.js
// @updateURL    https://raw.githubusercontent.com/XBadFellaX/NeonQuark/main/rfd-quiet.user.js
// @description  Hide sponsored RedFlagDeals listings and keep RFD's built-in dark theme.
// @include      https://redflagdeals.com/*
// @include      https://*.redflagdeals.com/*
// @run-at       document-start
// @grant        none
// ==/UserScript==

(() => {
    'use strict';

    const page = new URL(location.href);
    if (page.protocol !== 'https:' || page.username || page.password || page.port ||
        !/^(?:[a-z0-9-]+\.)*redflagdeals\.com$/i.test(page.hostname)) return;

    const root = document.documentElement;
    const STYLE_ID = 'neonquark-rfd-quiet';

    const SPONSORED_CSS = `
        li.topic:has(.sponsored-offer),
        .ad_sponsored_deal {
            display: none !important;
        }
    `;

    function applyDarkTheme() {
        if (root.getAttribute('data-theme') !== 'dark') {
            root.setAttribute('data-theme', 'dark');
        }
    }

    function ensureStyle() {
        if (document.getElementById(STYLE_ID)) return;
        const style = document.createElement('style');
        style.id = STYLE_ID;
        style.textContent = SPONSORED_CSS;
        (document.head || root).appendChild(style);
    }

    applyDarkTheme();
    ensureStyle();

    new MutationObserver(mutations => {
        for (const mutation of mutations) {
            if (mutation.type === 'attributes' && mutation.attributeName === 'data-theme') {
                applyDarkTheme();
            }
        }
        ensureStyle();
    }).observe(root, { attributes: true, attributeFilter: ['data-theme'], childList: true, subtree: true });
})();