// ==UserScript==
// @name         Civitai Direct Images for ComfyUI
// @namespace    https://github.com/ausboss/ComfyUI-Civitai-Direct
// @version      0.2.1
// @description  Outlines Civitai images by available generation data (red = none, blue = prompt, green = ComfyUI nodes), adds hover buttons to copy the prompt/info/nodes, and saves image + metadata via the ComfyUI-Civitai-Direct custom node.
// @match        *://civitai.com/*
// @match        *://www.civitai.com/*
// @match        *://civitai.red/*
// @match        *://www.civitai.red/*
// @connect      127.0.0.1
// @connect      localhost
// @grant        GM_xmlhttpRequest
// @grant        GM_setClipboard
// @downloadURL  https://raw.githubusercontent.com/ausboss/ComfyUI-Civitai-Direct/main/userscripts/civitai-direct-images.user.js
// @updateURL    https://raw.githubusercontent.com/ausboss/ComfyUI-Civitai-Direct/main/userscripts/civitai-direct-images.user.js
// ==/UserScript==

(function () {
    'use strict';

    // Ports probed for a running ComfyUI with the Civitai-Direct node.
    // 8188 is ComfyUI's default; add yours if you run on another port.
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

    // Poll a queued job until it reaches a terminal state.
    function pollJob(base, jobId, onDone) {
        const startedAt = Date.now();
        const tick = () => {
            if (Date.now() - startedAt > 5 * 60 * 1000) { onDone(false, 'timed out'); return; }
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
                    setTimeout(tick, 1000);
                },
                onerror() { setTimeout(tick, 2000); },
                ontimeout() { setTimeout(tick, 2000); },
            });
        };
        setTimeout(tick, 500);
    }

    const BUTTON_MARK = 'data-tm-image-server-btn';
    const DECORATED_MARK = 'data-tm-image-server-decorated';
    const HOST_MARK = 'data-cdi-host';
    const ID_MARK = 'data-cdi-id';
    const DETAIL_MARK = 'data-cdi-detail';

    // How each metadata state is shown. The outline answers "is this image
    // worth interacting with?" at a glance:
    //   full    -> has a ComfyUI node graph (copy the whole workflow)
    //   partial -> has a prompt / generation parameters (copy prompt or info)
    //   none    -> nothing to copy, don't bother
    //   unknown -> lookup failed (server hiccup); no outline drawn
    const STATES = {
        full: { color: '#51cf66', label: 'ComfyUI workflow available — hover to copy prompt or nodes' },
        partial: { color: '#4dabf7', label: 'Generation info available — hover to copy' },
        none: { color: '#ff6b6b', label: 'No generation data' },
        unknown: { color: null, label: 'Generation data unknown' },
    };

    const MAX_CONCURRENT_META_FETCHES = 3;
    const META_FETCH_TIMEOUT_MS = 8000;
    const RETRY_INTERVAL_MS = 250;
    const RETRY_WINDOW_MS = 30000;
    const OBSERVER_DEBOUNCE_MS = 150;

    let observer = null;
    let lastUrl = location.href;
    let retryTimer = null;
    let retryDeadline = 0;
    let observerDebounce = null;

    // ------------------------------------------------------------------
    // Small helpers
    // ------------------------------------------------------------------

    function cleanText(value) {
        return (value || '').replace(/\s+/g, ' ').trim();
    }

    function wait(ms) {
        return new Promise(resolve => setTimeout(resolve, ms));
    }

    function withTimeout(promise, ms) {
        return Promise.race([
            promise,
            wait(ms).then(() => { throw new Error('timeout'); }),
        ]);
    }

    function imageIdFromUrl(url = location.href) {
        const match = String(url).match(/\/images\/(\d+)/);
        return match ? Number.parseInt(match[1], 10) : null;
    }

    function bestImageElementOnPage() {
        const images = Array.from(document.images)
            .map(img => {
                const rect = img.getBoundingClientRect();
                return {
                    img,
                    src: img.currentSrc || img.src,
                    area: Math.max(0, rect.width) * Math.max(0, rect.height),
                };
            })
            .filter(item => item.src && item.area > 10000 && /image\.civitai\.com/i.test(item.src))
            .sort((a, b) => b.area - a.area);

        return images[0]?.img || null;
    }

    function bestImageOnPage() {
        const img = bestImageElementOnPage();
        return img ? (img.currentSrc || img.src) : null;
    }

    function promptFromVisibleText() {
        const text = cleanText(document.body.innerText);
        const promptIndex = text.search(/\bPrompt\b/i);
        if (promptIndex < 0) return null;

        let prompt = text.slice(promptIndex).replace(/^Prompt\b/i, '').trim();
        while (/^(EXTERNAL GENERATOR|TXT2IMG|IMG2IMG|INPAINT|COMFYUI)\b/i.test(prompt)) {
            prompt = prompt.replace(/^(EXTERNAL GENERATOR|TXT2IMG|IMG2IMG|INPAINT|COMFYUI)\b\s*/i, '').trim();
        }

        const stop = prompt.search(/\b(Negative prompt|Other metadata|Resources used|Discussion|Comments?)\b/i);
        if (stop >= 0) prompt = prompt.slice(0, stop).trim();

        prompt = prompt.replace(/\s*\.\.\.\s*Show more\s*$/i, '').trim();
        return prompt || null;
    }

    async function expandPromptText() {
        const buttons = Array.from(document.querySelectorAll('button'))
            .filter(btn => /^Show more$/i.test(cleanText(btn.innerText)));

        if (!buttons.length) return;
        buttons.forEach(btn => btn.click());
        await wait(200);
    }

    function filenameForImage(imageId, imageUrl) {
        let ext = '.jpg';
        try {
            const suffix = new URL(imageUrl).pathname.match(/\.(jpe?g|png|webp|gif|avif)$/i)?.[0];
            if (suffix) ext = suffix.toLowerCase().replace('.jpeg', '.jpg');
        } catch (_) {
            // Keep .jpg fallback.
        }

        return `civitai-image-${imageId || Date.now()}${ext}`;
    }

    // ------------------------------------------------------------------
    // Generation data (the same endpoint the Civitai frontend uses for the
    // "Generation Data" panel; same-origin, so the user's session applies)
    // ------------------------------------------------------------------

    const generationCache = new Map(); // imageId -> Promise<{state, data}>

    const fetchQueue = [];
    let activeFetches = 0;

    function enqueueFetch(job) {
        return new Promise((resolve, reject) => {
            fetchQueue.push({ job, resolve, reject });
            pumpFetchQueue();
        });
    }

    function pumpFetchQueue() {
        while (activeFetches < MAX_CONCURRENT_META_FETCHES && fetchQueue.length) {
            const { job, resolve, reject } = fetchQueue.shift();
            activeFetches++;
            Promise.resolve()
                .then(job)
                .then(resolve, reject)
                .finally(() => {
                    activeFetches--;
                    pumpFetchQueue();
                });
        }
    }

    async function fetchGenerationData(imageId) {
        const input = encodeURIComponent(JSON.stringify({ json: { id: imageId } }));
        const url = `${location.origin}/api/trpc/image.getGenerationData?input=${input}`;

        const res = await withTimeout(fetch(url, {
            credentials: 'same-origin',
            headers: { 'x-client': 'web', 'Accept': 'application/json' },
        }), META_FETCH_TIMEOUT_MS);

        if (res.status === 404) return { state: 'none', data: null };
        if (!res.ok) throw new Error(`generation data HTTP ${res.status}`);

        const body = await res.json();
        const data = body?.result?.data?.json ?? null;
        return { state: classifyGenerationData(data), data };
    }

    function ensureGenerationData(imageId) {
        if (!generationCache.has(imageId)) {
            generationCache.set(
                imageId,
                enqueueFetch(() => fetchGenerationData(imageId)).catch(err => {
                    console.warn(`[civitai-direct] generation data lookup failed for ${imageId}:`, err);
                    return { state: 'unknown', data: null };
                }),
            );
        }
        return generationCache.get(imageId);
    }

    function parseComfy(meta) {
        let comfy = meta?.comfy;
        if (!comfy) return null;
        if (typeof comfy === 'string') {
            try {
                comfy = JSON.parse(comfy);
            } catch (_) {
                return null;
            }
        }
        return comfy && typeof comfy === 'object' ? comfy : null;
    }

    function comfyWorkflowJson(meta) {
        const comfy = parseComfy(meta);
        if (!comfy) return null;
        const workflow = comfy.workflow;
        const target = workflow && typeof workflow === 'object' && Object.keys(workflow).length
            ? workflow
            : comfy;
        return JSON.stringify(target);
    }

    function classifyGenerationData(data) {
        const meta = data?.meta;
        if (parseComfy(meta)) return 'full';
        if (meta && (cleanText(String(meta.prompt || '')) || Object.keys(meta).length)) return 'partial';
        return 'none';
    }

    function promptFromRecord(record) {
        const prompt = record?.data?.meta?.prompt;
        return prompt ? cleanText(String(prompt)) : null;
    }

    // A1111-style parameter block: prompt, negative prompt, then a line of
    // key/value pairs. Most tools (and Civitai itself) parse this format.
    function buildInfoText(data) {
        const meta = data?.meta || {};
        const lines = [];

        const prompt = cleanText(String(meta.prompt || ''));
        if (prompt) lines.push(prompt);

        const negative = cleanText(String(meta.negativePrompt || ''));
        if (negative) lines.push(`Negative prompt: ${negative}`);

        const parts = [];
        const push = (label, value) => {
            if (value !== undefined && value !== null && value !== '') parts.push(`${label}: ${value}`);
        };
        push('Steps', meta.steps);
        push('Sampler', meta.sampler);
        push('Schedule type', meta.scheduler);
        push('CFG scale', meta.cfgScale);
        push('Seed', meta.seed);
        push('Size', meta.Size || (meta.width && meta.height ? `${meta.width}x${meta.height}` : undefined));
        push('Clip skip', meta.clipSkip);
        push('Model', meta.Model || meta.model);
        push('Base model', meta.baseModel);
        if (parts.length) lines.push(parts.join(', '));

        const resources = Array.isArray(data?.resources) ? data.resources : [];
        if (resources.length) {
            const items = resources.map(r => {
                let item = `${r.modelType || 'Model'}: ${r.modelName || r.modelId || '?'}`;
                if (r.versionName) item += ` (${r.versionName})`;
                if (typeof r.strength === 'number') item += ` ×${r.strength}`;
                return item;
            });
            lines.push(`Resources: ${items.join('; ')}`);
        }

        return lines.join('\n');
    }

    function copyText(text) {
        try {
            if (typeof GM_setClipboard === 'function') {
                GM_setClipboard(String(text));
                return Promise.resolve(true);
            }
        } catch (_) {
            // Fall through to the clipboard API.
        }
        if (navigator.clipboard?.writeText) {
            return navigator.clipboard.writeText(String(text)).then(() => true, () => false);
        }
        return Promise.resolve(false);
    }

    // ------------------------------------------------------------------
    // UI pieces
    // ------------------------------------------------------------------

    function injectStyles() {
        if (document.getElementById('cdi-styles')) return;
        const style = document.createElement('style');
        style.id = 'cdi-styles';
        style.textContent = `
            .cdi-bar {
                position: absolute;
                top: 0; left: 0; right: 0;
                display: flex;
                gap: 6px;
                justify-content: center;
                align-items: flex-start;
                padding: 6px 6px 16px;
                background: linear-gradient(to bottom, rgba(0,0,0,.65), rgba(0,0,0,0));
                opacity: 0;
                /* The bar itself never eats clicks — only its buttons do,
                   and only while visible — so Civitai's own card controls
                   (e.g. the "..." menu) keep working. */
                pointer-events: none;
                transition: opacity .15s ease;
                z-index: 45;
            }
            [${HOST_MARK}]:hover > .cdi-bar { opacity: 1; }
            .cdi-copy {
                border: none;
                border-radius: 5px;
                padding: 3px 9px;
                font-size: 11px;
                font-weight: 700;
                line-height: 16px;
                color: #fff;
                background: rgba(20,21,23,.85);
                cursor: pointer;
                box-shadow: 0 1px 3px rgba(0,0,0,.4);
                pointer-events: none;
            }
            [${HOST_MARK}]:hover > .cdi-bar .cdi-copy { pointer-events: auto; }
            .cdi-copy:hover { background: rgba(50,53,58,.95); }
            .cdi-badge {
                position: absolute;
                top: 6px;
                left: 6px;
                z-index: 44;
                font-size: 14px;
                pointer-events: none;
                filter: drop-shadow(0 1px 2px rgba(0,0,0,.7));
            }
        `;
        document.head.appendChild(style);
    }

    function makeCopyButton(label, getText) {
        const btn = document.createElement('button');
        btn.type = 'button';
        btn.className = 'cdi-copy';
        btn.textContent = label;
        btn.title = `Copy ${label.toLowerCase()} to clipboard`;

        btn.addEventListener('click', e => {
            e.preventDefault();
            e.stopPropagation();
            Promise.resolve(getText()).then(text => {
                if (!text) throw new Error('nothing to copy');
                return copyText(text);
            }).then(ok => {
                btn.textContent = ok ? '✓' : '!';
                setTimeout(() => { btn.textContent = label; }, 1200);
            }).catch(() => {
                btn.textContent = '!';
                setTimeout(() => { btn.textContent = label; }, 1200);
            });
        });

        return btn;
    }

    function buildCopyBar(host, record) {
        if (host.querySelector('.cdi-bar')) return;
        const data = record.data;
        const meta = data?.meta || {};

        const bar = document.createElement('div');
        bar.className = 'cdi-bar';

        const prompt = cleanText(String(meta.prompt || ''));
        if (prompt) bar.appendChild(makeCopyButton('Prompt', () => prompt));

        bar.appendChild(makeCopyButton('Info', () => buildInfoText(data)));

        if (parseComfy(meta)) {
            bar.appendChild(makeCopyButton('Nodes', () => comfyWorkflowJson(meta)));
        }

        host.appendChild(bar);
    }

    function addWorkflowBadge(host) {
        if (host.querySelector('.cdi-badge')) return;
        const badge = document.createElement('span');
        badge.className = 'cdi-badge';
        badge.textContent = '🧩';
        host.appendChild(badge);
    }

    async function applyState(host) {
        const imageId = Number(host.getAttribute(ID_MARK));
        if (!imageId) return;

        const record = await ensureGenerationData(imageId);
        // The host may have been re-used for another image (carousel) or
        // removed while we were waiting.
        if (!host.isConnected || Number(host.getAttribute(ID_MARK)) !== imageId) return;

        const state = STATES[record.state] || STATES.unknown;
        host.setAttribute('data-cdi-state', record.state);
        host.title = state.label;
        if (state.color) {
            host.style.outline = `2px solid ${state.color}`;
            host.style.outlineOffset = '-2px';
        } else {
            host.style.outline = '';
        }

        if (record.state === 'full') addWorkflowBadge(host);
        if (record.state === 'full' || record.state === 'partial') buildCopyBar(host, record);
    }

    function resetHost(host) {
        host.querySelectorAll(`.cdi-bar, .cdi-badge, button[${BUTTON_MARK}]`).forEach(node => node.remove());
        [HOST_MARK, ID_MARK, DETAIL_MARK, 'data-cdi-state', DECORATED_MARK].forEach(attr => host.removeAttribute(attr));
        host.style.outline = '';
        host.style.outlineOffset = '';
        host.title = '';
    }

    function setButtonState(btn, state) {
        if (state === 'sending') {
            btn.textContent = '...';
            btn.style.background = 'linear-gradient(135deg,#e67700,#f08c00)';
            return;
        }
        if (state === 'success') {
            btn.textContent = '✓';
            btn.style.background = 'linear-gradient(135deg,#2b8a3e,#37b24d)';
            return;
        }
        if (state === 'error') {
            btn.textContent = '!';
            btn.style.background = 'linear-gradient(135deg,#c92a2a,#e03131)';
            return;
        }
        if (state === 'offline') {
            btn.textContent = '🔌';
            btn.style.background = 'linear-gradient(135deg,#c92a2a,#e03131)';
            btn.title = `ComfyUI is not running (checked ports ${SERVER_PORTS.join(', ')}). Start ComfyUI with the Civitai-Direct node, then retry.`;
            return;
        }
        if (state === 'exists') {
            btn.textContent = '📁';
            btn.style.background = 'linear-gradient(135deg,#5f3dc4,#7048e8)';
            btn.title = 'Already saved.';
            return;
        }

        btn.textContent = '⬇';
        btn.style.background = 'linear-gradient(135deg,#0b7285,#0c8599)';
        btn.title = 'Save image and metadata via ComfyUI';
    }

    function makeButton(getPayload) {
        const btn = document.createElement('button');
        btn.type = 'button';
        btn.setAttribute(BUTTON_MARK, '1');
        btn.title = 'Save image and metadata via ComfyUI';
        btn.setAttribute('aria-label', 'Save image and metadata via ComfyUI');

        Object.assign(btn.style, {
            position: 'absolute',
            right: '8px',
            bottom: '8px',
            zIndex: 40,
            width: '28px',
            height: '26px',
            padding: '0',
            border: 'none',
            borderRadius: '6px',
            color: '#fff',
            cursor: 'pointer',
            fontSize: '14px',
            fontWeight: '700',
            lineHeight: '26px',
            boxShadow: '0 1px 3px rgba(0,0,0,.4)',
        });
        setButtonState(btn, 'idle');

        const fail = () => {
            setButtonState(btn, 'error');
            setTimeout(() => setButtonState(btn, 'idle'), 3000);
        };

        btn.addEventListener('click', async e => {
            e.preventDefault();
            e.stopPropagation();

            let payload = null;
            try {
                payload = await Promise.resolve(getPayload());
            } catch (_) {
                fail();
                return;
            }

            if (!payload || (!payload.image_url && !payload.image_id)) {
                fail();
                return;
            }

            setButtonState(btn, 'sending');
            probeServer(base => {
                if (!base) {
                    console.error('[civitai-direct] No ComfyUI with the Civitai-Direct node found on ports', SERVER_PORTS);
                    setButtonState(btn, 'offline');
                    setTimeout(() => setButtonState(btn, 'idle'), 3000);
                    return;
                }

                GM_xmlhttpRequest({
                    method: 'POST',
                    url: `${base}/civitai/download-image`,
                    headers: { 'Content-Type': 'application/json' },
                    data: JSON.stringify(payload),
                    onload(res) {
                        if (res.status < 200 || res.status >= 300) {
                            console.error('[civitai-direct] Server error:', res.responseText);
                            fail();
                            return;
                        }

                        let jobId = null;
                        try { jobId = JSON.parse(res.responseText).job_id; } catch (_) { /* older server */ }
                        if (!jobId) {
                            setButtonState(btn, 'success');
                            setTimeout(() => setButtonState(btn, 'idle'), 3000);
                            return;
                        }

                        pollJob(base, jobId, (ok, detail) => {
                            if (!ok) console.error('[civitai-direct] Image download failed:', detail);
                            const state = !ok ? 'error' : (detail === 'already_exists' ? 'exists' : 'success');
                            setButtonState(btn, state);
                            setTimeout(() => setButtonState(btn, 'idle'), 3000);
                        });
                    },
                    onerror() {
                        serverBase = null; // force re-discovery next click
                        setButtonState(btn, 'offline');
                        setTimeout(() => setButtonState(btn, 'idle'), 3000);
                    },
                });
            });
        });

        return btn;
    }

    // Include whatever generation data we already have (or can get quickly)
    // so the server can write richer sidecars even for JPEG/WebP originals
    // that carry no embedded metadata.
    async function generationDataForPayload(imageId) {
        if (!imageId) return null;
        try {
            const record = await withTimeout(ensureGenerationData(imageId), 4000);
            return record?.data ?? null;
        } catch (_) {
            return null;
        }
    }

    // ------------------------------------------------------------------
    // Page decoration
    // ------------------------------------------------------------------

    const intersectionObserver = new IntersectionObserver(entries => {
        for (const entry of entries) {
            if (!entry.isIntersecting) continue;
            intersectionObserver.unobserve(entry.target);
            applyState(entry.target);
        }
    }, { rootMargin: '250px' });

    function decorateDetailPage() {
        const pageId = imageIdFromUrl();
        if (!pageId) return;

        const img = bestImageElementOnPage();
        const host = img?.parentElement;
        if (!host) return;

        // The carousel swaps the image in place when navigating between
        // images of a post; refresh our UI when the id changes.
        const previousId = host.getAttribute(ID_MARK);
        if (previousId && Number(previousId) !== pageId) resetHost(host);
        if (host.hasAttribute(HOST_MARK)) return;

        host.setAttribute(HOST_MARK, '1');
        host.setAttribute(ID_MARK, String(pageId));
        host.setAttribute(DETAIL_MARK, '1');
        if (getComputedStyle(host).position === 'static') host.style.position = 'relative';

        const btn = makeButton(async () => {
            await expandPromptText();
            const generationData = await generationDataForPayload(pageId);
            const imageUrl = bestImageOnPage();
            const record = generationData ? { data: generationData } : null;
            return {
                image_id: pageId,
                image_url: imageUrl,
                page_url: location.href,
                prompt: promptFromRecord(record) || promptFromVisibleText(),
                filename: filenameForImage(pageId, imageUrl || ''),
                generation_data: generationData,
            };
        });
        btn.setAttribute('data-detail', '1');
        host.appendChild(btn);

        applyState(host);
    }

    function decorateGalleryCards() {
        document.querySelectorAll('a[href*="/images/"]').forEach(link => {
            if (link.hasAttribute(DECORATED_MARK) || link.hasAttribute(HOST_MARK)) return;

            const imageId = imageIdFromUrl(link.getAttribute('href') || link.href || '');
            if (!imageId) return;

            // Only decorate actual media cards, not text links to image pages.
            const media = link.querySelector('img, video');
            if (!media) return;

            link.setAttribute(DECORATED_MARK, '1');
            link.setAttribute(HOST_MARK, '1');
            link.setAttribute(ID_MARK, String(imageId));
            if (getComputedStyle(link).position === 'static') link.style.position = 'relative';

            const img = link.querySelector('img');
            const imageUrl = img ? (img.currentSrc || img.src) : null;
            const isVideo = !!link.querySelector('video');

            // The image download pipeline doesn't handle video originals;
            // videos still get the outline + copy buttons.
            if (!isVideo && imageUrl) {
                const btn = makeButton(async () => {
                    const generationData = await generationDataForPayload(imageId);
                    return {
                        image_id: imageId,
                        image_url: imageUrl,
                        page_url: new URL(link.getAttribute('href') || link.href, location.origin).href,
                        prompt: promptFromRecord({ data: generationData }),
                        filename: filenameForImage(imageId, imageUrl),
                        generation_data: generationData,
                    };
                });
                link.appendChild(btn);
            }

            intersectionObserver.observe(link);
        });
    }

    function render() {
        injectStyles();
        decorateDetailPage();
        decorateGalleryCards();
    }

    // Civitai hydrates pages asynchronously; fixed timers miss slow renders,
    // so keep retrying until the page is decorated (or we time out). Gallery
    // pages stream cards continuously; the MutationObserver picks those up
    // after the initial retry window ends.
    function retryDone() {
        const pageId = imageIdFromUrl();
        if (pageId) {
            return !!document.querySelector(`[${DETAIL_MARK}][${ID_MARK}="${pageId}"]`);
        }
        return !!document.querySelector(`[${DECORATED_MARK}]`);
    }

    function stopRetryLoop() {
        if (retryTimer) {
            clearInterval(retryTimer);
            retryTimer = null;
        }
    }

    function retryTick() {
        if (location.href !== lastUrl) {
            lastUrl = location.href;
            retryDeadline = Date.now() + RETRY_WINDOW_MS;
        }

        render();

        if (retryDone() || Date.now() > retryDeadline) {
            stopRetryLoop();
        }
    }

    function startRetryLoop() {
        stopRetryLoop();
        retryDeadline = Date.now() + RETRY_WINDOW_MS;
        retryTick();
        if (!retryTimer && !retryDone()) {
            retryTimer = setInterval(retryTick, RETRY_INTERVAL_MS);
        }
    }

    function observe() {
        if (observer) observer.disconnect();
        observer = new MutationObserver(() => {
            if (observerDebounce) clearTimeout(observerDebounce);
            observerDebounce = setTimeout(() => {
                observerDebounce = null;

                if (location.href !== lastUrl) {
                    lastUrl = location.href;
                    startRetryLoop();
                    return;
                }

                render();
            }, OBSERVER_DEBOUNCE_MS);
        });
        observer.observe(document.body, { childList: true, subtree: true });
    }

    function hookNavigation() {
        const originalPushState = history.pushState;
        const originalReplaceState = history.replaceState;

        function handleNavChange() {
            lastUrl = location.href;
            startRetryLoop();
        }

        history.pushState = function (...args) {
            const result = originalPushState.apply(this, args);
            handleNavChange();
            return result;
        };

        history.replaceState = function (...args) {
            const result = originalReplaceState.apply(this, args);
            handleNavChange();
            return result;
        };

        window.addEventListener('popstate', handleNavChange);
    }

    hookNavigation();
    observe();
    startRetryLoop();
})();
