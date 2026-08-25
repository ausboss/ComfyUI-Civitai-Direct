// ==UserScript==
// @name         Civitai Direct for ComfyUI (Grid)
// @namespace    https://github.com/ausboss/ComfyUI-Civitai-Direct
// @version      0.2.0
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

    // Re-validate the cached server (or discover one), so a ComfyUI restart
    // -- even on the other port -- heals without a second click.
    function probeServer(callback) {
        if (!serverBase) { discoverServer(callback); return; }
        const rediscover = () => { serverBase = null; discoverServer(callback); };
        GM_xmlhttpRequest({
            method: 'GET',
            url: `${serverBase}/civitai/health`,
            timeout: 1500,
            onload(res) { if (res.status === 200) callback(serverBase); else rediscover(); },
            onerror: rediscover,
            ontimeout: rediscover,
        });
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
                    if (job.state === 'done' || job.state === 'already_exists') { onDone(true, job.state, job); return; }
                    if (job.state === 'error') { onDone(false, job.error || 'download failed', job); return; }
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

    const IDLE_BG = 'linear-gradient(135deg,#0b7285,#0c8599)';
    const EA_BG = 'linear-gradient(135deg,#f59f00,#fab005)';

    // Civitai renders a literal "Early Access" status badge on cards whose
    // newest version is still inside its early-access window (ModelCard.tsx:
    // isEarlyAccess = earlyAccessDeadline > now). The newest version is also
    // what a grid download would fetch, so the badge is exactly our signal.
    function isEarlyAccessCard(card) {
        for (const el of card.querySelectorAll('span,div,p,strong,b')) {
            if (el.childElementCount === 0 && /^early access$/i.test((el.textContent || '').trim())) {
                return true;
            }
        }
        return false;
    }

    function applyEarlyAccessLook(btn) {
        btn.dataset.cdEa = '1';
        btn.textContent = '⚡';
        btn.style.background = EA_BG;
        btn.style.color = '#1a1b1e';
        btn.style.opacity = '0.65';
        btn.style.cursor = 'not-allowed';
        btn.title = 'Early Access — needs the purchase/subscription on your Civitai account. Click twice to try anyway.';
        btn.setAttribute('aria-label', btn.title);
    }

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

            // Early Access: block the accidental click. A second click within
            // 3s still sends it, for accounts that purchased the version.
            if (btn.dataset.cdEa === '1' && btn.dataset.cdEaArmed !== '1') {
                btn.dataset.cdEaArmed = '1';
                btn.textContent = '🔒';
                console.info('[Civitai Direct] Early Access model — needs the purchase on your Civitai account. Click again to try anyway.');
                setTimeout(() => { btn.dataset.cdEaArmed = ''; applyEarlyAccessLook(btn); }, 3000);
                return;
            }
            btn.dataset.cdEaArmed = '';
            btn.textContent = '⏳';

            const wasEa = btn.dataset.cdEa === '1';
            const OK_BG = 'linear-gradient(135deg,#2b8a3e,#37b24d)';
            const ERR_BG = 'linear-gradient(135deg,#c92a2a,#e03131)';
            const HAVE_BG = 'linear-gradient(135deg,#5f3dc4,#7048e8)';
            const FACES = {
                ok: ['✅', OK_BG],
                have: ['📁', HAVE_BG],
                err: ['❌', ERR_BG],
                offline: ['🔌', ERR_BG],
            };
            const settle = (kind, title) => {
                const [face, bg] = FACES[kind] || FACES.err;
                btn.textContent = face;
                btn.style.background = bg;
                if (title) { btn.title = title; btn.setAttribute('aria-label', title); }
                setTimeout(() => {
                    btn.title = 'Queue download on local server';
                    btn.setAttribute('aria-label', btn.title);
                    if (wasEa) { applyEarlyAccessLook(btn); return; }
                    btn.textContent = '⚡';
                    btn.style.background = IDLE_BG;
                }, 3000);
            };

            probeServer((base) => {
                if (!base) {
                    console.error('[Civitai Direct] No ComfyUI with the Civitai-Direct node found on ports', SERVER_PORTS);
                    settle('offline', `ComfyUI is not running (checked ports ${SERVER_PORTS.join(', ')}). Start ComfyUI with the Civitai-Direct node, then retry.`);
                    return;
                }
                GM_xmlhttpRequest({
                    method: 'POST',
                    url: `${base}/civitai/download`,
                    headers: { 'Content-Type': 'application/json' },
                    data: JSON.stringify({ model_id: info.id, model_version_id: null, model_slug: info.slug }),
                    onload(res) {
                        if (res.status < 200 || res.status >= 300) { settle('err'); return; }
                        let jobId = null;
                        try { jobId = JSON.parse(res.responseText).job_id; } catch (_) { /* keep null */ }
                        if (!jobId) { settle('ok'); return; }
                        pollJob(base, jobId,
                            (job) => {
                                if (job.state === 'downloading' && job.bytes_total) {
                                    btn.textContent = `${Math.floor(job.bytes_done / job.bytes_total * 100)}`;
                                }
                            },
                            (ok, detail, job) => {
                                if (!ok) {
                                    console.error('[Civitai Direct] Download failed:', detail);
                                    settle('err', detail ? `Download failed: ${detail}` : '');
                                    return;
                                }
                                const path = (job && job.path) || '';
                                if (detail === 'already_exists') {
                                    settle('have', path ? `Already in your library: ${path}` : 'Already in your library');
                                } else {
                                    settle('ok', path ? `Saved: ${path}` : '');
                                }
                            });
                    },
                    onerror() {
                        serverBase = null; // force re-discovery next click
                        settle('offline', `Lost the connection to ComfyUI. Start it (ports ${SERVER_PORTS.join(', ')}), then retry.`);
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
            const btn = makeButton(info);
            if (isEarlyAccessCard(card)) applyEarlyAccessLook(btn);
            card.appendChild(btn);
        });

        // Cards hydrate progressively, so the Early Access badge can appear
        // after our button did — re-check idle buttons on every pass.
        document.querySelectorAll('button[' + BUTTON_MARK + ']:not([data-cd-ea="1"])').forEach(btn => {
            if (btn.textContent === '⚡' && btn.parentElement && isEarlyAccessCard(btn.parentElement)) {
                applyEarlyAccessLook(btn);
            }
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
