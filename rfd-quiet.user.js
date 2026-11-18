// ==UserScript==
// @name         RFD Quiet
// @namespace    https://github.com/XBadFellaX/NeonQuark
// @version      1.0.3
// @homepageURL  https://github.com/XBadFellaX/NeonQuark
// @downloadURL  https://raw.githubusercontent.com/XBadFellaX/NeonQuark/refs/heads/main/rfd-quiet.user.js
// @updateURL    https://raw.githubusercontent.com/XBadFellaX/NeonQuark/refs/heads/main/rfd-quiet.user.js
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
    const isHomepage = /^(?:www\.)?redflagdeals\.com$/i.test(page.hostname);

    const SPONSORED_CSS = `
        li.topic:has(.sponsored-offer),
        .ad_sponsored_deal {
            display: none !important;
        }
    `;

    const HOMEPAGE_DARK_CSS = `
        html[data-theme="dark"] {
            color-scheme: dark;
        }

        html[data-theme="dark"] .rfd-pref-src,
        html[data-theme="dark"] .image-wrapper,
        html[data-theme="dark"] .layouts__standard__card_image_link,
        html[data-theme="dark"] .layouts__featured__frame_image_link,
        html[data-theme="dark"] .component_layout__standard .layouts__standard__frame_image_link,
        html[data-theme="dark"] .component_layout__standard .layouts__standard__square_image_link,
        html[data-theme="dark"] .component_layout__featured .layouts__featured__square_image_link {
            background: #2a2a2a !important;
        }
    `;

    function persistDarkMode() {
        const cookie = 'rfd_dark_mode=1';
        if (document.cookie.includes(cookie)) return;
        document.cookie = `${cookie}; path=/; domain=.redflagdeals.com; max-age=${365 * 24 * 60 * 60}; SameSite=Lax; Secure`;
    }

    function applyDarkTheme() {
        if (root.getAttribute('data-theme') !== 'dark') {
            root.setAttribute('data-theme', 'dark');
        }
    }

    function ensureStyle() {
        if (document.getElementById(STYLE_ID)) return;
        const style = document.createElement('style');
        style.id = STYLE_ID;
        style.textContent = SPONSORED_CSS + (isHomepage ? HOMEPAGE_DARK_CSS : '');
        (document.head || root).appendChild(style);
    }

    persistDarkMode();
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