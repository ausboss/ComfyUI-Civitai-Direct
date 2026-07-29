// ==UserScript==
// @name         Civitai Direct for ComfyUI (Grid)
// @namespace    https://github.com/ausboss/ComfyUI-Civitai-Direct
// @version      0.1.0
// @description  Adds a quick-download button to each model card on Civitai listing/grid pages (ComfyUI-Civitai-Direct).
// @match        *://civitai.com/*
// @match        *://www.civitai.com/*
// @match        *://civitai.red/*
// @match        *://www.civitai.red/*
// @exclude      *://civitai.com/models/*
// @exclude      *://www.civitai.com/models/*
// @exclude      *://civitai.red/models/*
// @exclude      *://www.civitai.red/models/*
// @connect      127.0.0.1
// @grant        GM_xmlhttpRequest
// @downloadURL  https://raw.githubusercontent.com/ausboss/ComfyUI-Civitai-Direct/main/userscripts/civitai-direct-grid.user.js
// @updateURL    https://raw.githubusercontent.com/ausboss/ComfyUI-Civitai-Direct/main/userscripts/civitai-direct-grid.user.js
// ==/UserScript==

(function () {
    'use strict';

    // Ports probed for a running ComfyUI with the Civitai-Direct node.
    const SERVER_PORTS = [8188, 8189];
    let serverBase = null;

    function discoverServer(callback) {
        if (serverBase) { callback(serverBase); return; }
        let pending = SERVER_PORTS.length;
        let settled = false;
        const finish = (base) => {
            if (settled) return;
            if (base) { settled = true; serverBase = base; callback(base); }
            else if (pending === 0) { settled = true; callback(null); }
        };
        for (const port of SERVER_PORTS) {
            GM_xmlhttpRequest({
                method: 'GET',
                url: `http://127.0.0.1:${port}/civitai/health`,
                timeout: 1500,
                onload(res) { pending--; finish(res.status === 200 ? `http://127.0.0.1:${port}` : null); },
                onerror() { pending--; finish(null); },
                ontimeout() { pending--; finish(null); },
            });
        }
    }

    function pollJob(base, jobId, onProgress, onDone) {
        const startedAt = Date.now();
        const tick = () => {
            if (Date.now() - startedAt > 60 * 60 * 1000) { onDone(false, 'timed out'); return; }
            GM_xmlhttpRequest({
                method: 'GET',
                url: `${base}/civitai/status?id=${jobId}`,
                timeout: 5000,
                onload(res) {
                    if (res.status !== 200) { onDone(false, `status ${res.status}`); return; }
                    let job;
                    try { job = JSON.parse(res.responseText); } catch (_) { job = null; }
                    if (!job) { onDone(false, 'bad status payload'); return; }
                    if (job.state === 'done' || job.state === 'already_exists') { onDone(true, job.state); return; }
                    if (job.state === 'error') { onDone(false, job.error || 'download failed'); return; }
                    onProgress(job);
                    setTimeout(tick, 1000);
                },
                onerror() { setTimeout(tick, 2000); },
                ontimeout() { setTimeout(tick, 2000); },
            });
        };
        setTimeout(tick, 1000);
    }

    const MARK = 'data-tm-grid-btn';
    const BUTTON_MARK = 'data-tm-grid-server-btn';

    function isModelDetailPage() {
        return /^\/models\/\d+(?:\/|$)/.test(window.location.pathname);
    }

    function removeGridButtons() {
        document.querySelectorAll('button[' + BUTTON_MARK + ']').forEach(btn => btn.remove());
    }

    function modelInfoFromHref(href) {
        const m = href.match(/\/models\/(\d+)(?:\/([^/?#]+))?/);
        if (!m) return null;
        let slug = m[2] || 'model';
        try {
            slug = decodeURIComponent(slug);
        } catch (_) {
            // Keep the raw slug if Civitai ever ships a malformed URL segment.
        }
        return { id: Number.parseInt(m[1], 10), slug };
    }

    function makeButton(info) {
        const btn = document.createElement('button');
        btn.type = 'button';
        btn.setAttribute(BUTTON_MARK, '1');
        btn.title = 'Queue download on local server';
        btn.setAttribute('aria-label', 'Queue download on local server');
        btn.textContent = '⚡';
        Object.assign(btn.style, {
            position: 'absolute', right: '8px', bottom: '8px', zIndex: 30,
            width: '26px', height: '24px', padding: '0',
            fontSize: '13px', lineHeight: '24px', fontWeight: '700',
            color: '#fff', border: 'none', borderRadius: '6px',
            background: 'linear-gradient(135deg,#0b7285,#0c8599)',
            cursor: 'pointer', boxShadow: '0 1px 3px rgba(0,0,0,.35)'
        });

        btn.addEventListener('click', (e) => {
            e.preventDefault();
            e.stopPropagation();
            btn.textContent = '⏳';

            const IDLE_BG = 'linear-gradient(135deg,#0b7285,#0c8599)';
            const OK_BG = 'linear-gradient(135deg,#2b8a3e,#37b24d)';
            const ERR_BG = 'linear-gradient(135deg,#c92a2a,#e03131)';
            const settle = (ok) => {
                btn.textContent = ok ? '✅' : '❌';
                btn.style.background = ok ? OK_BG : ERR_BG;
                setTimeout(() => { btn.textContent = '⚡'; btn.style.background = IDLE_BG; }, 3000);
            };

            discoverServer((base) => {
                if (!base) {
                    console.error('[Civitai Direct] No ComfyUI with the Civitai-Direct node found on ports', SERVER_PORTS);
                    settle(false);
                    return;
                }
                GM_xmlhttpRequest({
                    method: 'POST',
                    url: `${base}/civitai/download`,
                    headers: { 'Content-Type': 'application/json' },
                    data: JSON.stringify({ model_id: info.id, model_version_id: null, model_slug: info.slug }),
                    onload(res) {
                        if (res.status < 200 || res.status >= 300) { settle(false); return; }
                        let jobId = null;
                        try { jobId = JSON.parse(res.responseText).job_id; } catch (_) { /* keep null */ }
                        if (!jobId) { settle(true); return; }
                        pollJob(base, jobId,
                            (job) => {
                                if (job.state === 'downloading' && job.bytes_total) {
                                    btn.textContent = `${Math.floor(job.bytes_done / job.bytes_total * 100)}`;
                                }
                            },
                            (ok, detail) => {
                                if (!ok) console.error('[Civitai Direct] Download failed:', detail);
                                settle(ok);
                            });
                    },
                    onerror() {
                        serverBase = null; // force re-discovery next click
                        settle(false);
                    }
                });
            });
        });
        return btn;
    }

    function decorate() {
        if (isModelDetailPage()) {
            removeGridButtons();
            return;
        }

        const links = document.querySelectorAll('a[href*="/models/"]:not([' + MARK + '])');
        links.forEach(link => {
            const info = modelInfoFromHref(link.getAttribute('href') || '');
            if (!info) return;
            // attach to the card-like container so the button sits over the thumbnail
            const card = link.closest('[class*="Card"]') || link.parentElement || link;
            if (!card) return;
            link.setAttribute(MARK, '1');
            if (getComputedStyle(card).position === 'static') card.style.position = 'relative';
            card.appendChild(makeButton(info));
        });
    }

    let decorateDebounce = null;
    const obs = new MutationObserver(() => {
        if (decorateDebounce) clearTimeout(decorateDebounce);
        decorateDebounce = setTimeout(() => {
            decorateDebounce = null;
            decorate();
        }, 150);
    });
    obs.observe(document.body, { childList: true, subtree: true });
    decorate();
})();
