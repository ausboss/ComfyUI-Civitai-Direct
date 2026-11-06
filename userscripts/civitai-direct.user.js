// ==UserScript==
// @name         Civitai Direct for ComfyUI
// @namespace    https://github.com/ausboss/ComfyUI-Civitai-Direct
// @version      0.3.0
// @description  One-click Civitai downloads into your ComfyUI model folders: a button on model pages and model cards, and a saver for gallery images. Needs the ComfyUI-Civitai-Direct custom node.
// @match        *://civitai.com/*
// @match        *://www.civitai.com/*
// @match        *://civitai.red/*
// @match        *://www.civitai.red/*
// @connect      127.0.0.1
// @connect      localhost
// @grant        GM_xmlhttpRequest
// @grant        GM_setClipboard
// @grant        GM_getValue
// @grant        GM_setValue
// @grant        GM_registerMenuCommand
// @downloadURL  https://raw.githubusercontent.com/ausboss/ComfyUI-Civitai-Direct/main/userscripts/civitai-direct.user.js
// @updateURL    https://raw.githubusercontent.com/ausboss/ComfyUI-Civitai-Direct/main/userscripts/civitai-direct.user.js
// ==/UserScript==

// Three features share one connection to your ComfyUI:
//   1. a download button on model pages
//   2. a small button on model cards in listings
//   3. copy and save buttons on gallery images
// The connection code is first, then each feature in its own block.

// ----------------------------------------------------------------------
// Connection to ComfyUI
// ----------------------------------------------------------------------
const Core = (function () {
    'use strict';

    // 8188 is ComfyUI's default, 8000 is the ComfyUI desktop app, 8189 is the
    // usual second instance. A ComfyUI anywhere else is set from the
    // Tampermonkey menu ("Set ComfyUI address").
    const DEFAULT_PORTS = [8188, 8000, 8189];
    const ADDRESS_KEY = 'comfyui_address';
    const NODE_NAME = 'ComfyUI-Civitai-Direct';
    let serverBase = null;

    function configuredAddress() {
        try {
            return String(GM_getValue(ADDRESS_KEY, '') || '').trim().replace(/\/+$/, '');
        } catch (_) {
            return '';
        }
    }

    function candidates() {
        const custom = configuredAddress();
        return custom ? [custom] : DEFAULT_PORTS.map(port => `http://127.0.0.1:${port}`);
    }

    function describeTargets() {
        return configuredAddress() || `ports ${DEFAULT_PORTS.join(', ')}`;
    }

    // Only a ComfyUI that has this node answers with its name, so another
    // program on the same port doesn't count.
    function check(base, timeout, ok, fail) {
        GM_xmlhttpRequest({
            method: 'GET',
            url: `${base}/civitai/health`,
            timeout,
            onload(res) {
                let body = null;
                try { body = JSON.parse(res.responseText); } catch (_) { /* not our server */ }
                if (res.status === 200 && body && body.node === NODE_NAME) ok(); else fail();
            },
            onerror: fail,
            ontimeout: fail,
        });
    }

    function timeoutMs() {
        // A saved address may be on another machine, and Tampermonkey may be
        // waiting for permission to connect to it.
        return configuredAddress() ? 8000 : 1500;
    }

    function discover(callback) {
        if (serverBase) { callback(serverBase); return; }
        const targets = candidates();
        let pending = targets.length;
        let settled = false;
        for (const base of targets) {
            check(base, timeoutMs(),
                () => { if (!settled) { settled = true; serverBase = base; callback(base); } },
                () => { pending--; if (pending === 0 && !settled) { settled = true; callback(null); } });
        }
    }

    // Re-check the server we last used (or find one), so restarting ComfyUI,
    // even on another port, heals without a second click.
    function probe(callback) {
        if (!serverBase) { discover(callback); return; }
        const base = serverBase;
        check(base, timeoutMs(), () => callback(base), () => { serverBase = null; discover(callback); });
    }

    // Poll a queued job until it ends, reporting progress on the way.
    function pollJob(base, jobId, onProgress, onDone, options) {
        const { timeoutMs: limit = 60 * 60 * 1000, firstDelayMs = 1000 } = options || {};
        const startedAt = Date.now();
        const tick = () => {
            if (Date.now() - startedAt > limit) { onDone(false, 'timed out'); return; }
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
                    if (onProgress) onProgress(job);
                    setTimeout(tick, 1000);
                },
                onerror() { setTimeout(tick, 2000); },
                ontimeout() { setTimeout(tick, 2000); },
            });
        };
        setTimeout(tick, firstDelayMs);
    }

    if (window.top === window.self && typeof GM_registerMenuCommand === 'function') {
        GM_registerMenuCommand('Set ComfyUI address', () => {
            const answer = window.prompt(
                'Address of your ComfyUI, for example http://192.168.1.20:8188\n\n' +
                `Leave it empty to find ComfyUI automatically (${describeTargets()}).`,
                configuredAddress());
            if (answer === null) return;
            const value = answer.trim().replace(/\/+$/, '');
            if (value && !/^https?:\/\/[^\s/]+$/i.test(value)) {
                window.alert('That is not an address. Use something like http://127.0.0.1:8188');
                return;
            }
            GM_setValue(ADDRESS_KEY, value);
            serverBase = null;
            window.alert(value ? `Using ${value}. Reload the Civitai page to apply it.`
                               : 'Finding ComfyUI automatically again. Reload the Civitai page to apply it.');
        });
    }

    return { probe, pollJob, describeTargets, forget() { serverBase = null; }, current() { return serverBase; } };
})();

// ----------------------------------------------------------------------
// 1. Download button on model pages
// ----------------------------------------------------------------------
(function modelPageButton() {
    'use strict';

    const BUTTON_ID = 'tm-direct-server-btn';

    const OLD_CTA_CONTAINER_SELECTOR = '.ModelVersionDetails_ctaContainer__m2yUr';
    const CREATE_SELECTOR = 'button[data-tour="model:create"], button[data-activity="create:model"]';
    const DOWNLOAD_SELECTOR = 'a[data-tour="model:download"], a[href*="/api/download/models/"]';

    let observer = null;
    let lastUrl = location.href;
    let retryTimer = null;
    let retryDeadline = 0;
    let observerDebounce = null;

    const RETRY_INTERVAL_MS = 250;
    const RETRY_WINDOW_MS = 30000;
    const OBSERVER_DEBOUNCE_MS = 150;


    function parseIntSafe(value) {
        const n = Number.parseInt(value, 10);
        return Number.isFinite(n) ? n : null;
    }

    function getModelIdFromUrl() {
        const match = location.pathname.match(/\/models\/(\d+)/);
        return match ? parseIntSafe(match[1]) : null;
    }

    function getModelVersionId() {
        const url = new URL(window.location.href);
        const fromQuery = url.searchParams.get('modelVersionId');
        if (fromQuery) return parseIntSafe(fromQuery);

        const nativeDownload = document.querySelector(`${DOWNLOAD_SELECTOR}[href*="/api/download/models/"]`);
        if (!nativeDownload) return null;

        const href = nativeDownload.getAttribute('href') || '';
        const match = href.match(/\/api\/download\/models\/(\d+)/);
        return match ? parseIntSafe(match[1]) : null;
    }

    function getDownloadUrl(modelVersionId) {
        const nativeDownload = document.querySelector(`${DOWNLOAD_SELECTOR}[href*="/api/download/models/"]`);

        if (nativeDownload) {
            const href = nativeDownload.getAttribute('href');

            if (href?.startsWith('http')) return href;
            if (href?.startsWith('/')) return `${window.location.origin}${href}`;
        }

        if (!modelVersionId) return '';
        return `https://civitai.com/api/download/models/${modelVersionId}?type=Model&format=SafeTensor`;
    }

    // The readable part of /models/<id>/<slug>, if the address has one.
    function getModelSlug() {
        const parts = window.location.pathname.split('/').filter(Boolean);
        if (parts[0] !== 'models' || !parts[2]) return null;
        try { return decodeURIComponent(parts[2]); } catch (_) { return parts[2]; }
    }

    // Civitai's version-specific early-access notice ("The creator ... has
    // set this version to Early Access ...", EarlyAccessAlert.tsx) only
    // renders for the currently selected version while its window is open —
    // unlike the title chip, which flags the whole model.
    function isEarlyAccessSelectedVersion() {
        return /set this version to Early Access/i.test(document.body.innerText || '');
    }

    // What the server needs from the page: which model, which version, and
    // where the version's own download link points.
    function pageInfo() {
        const versionId = getModelVersionId();
        return {
            modelId: getModelIdFromUrl(),
            versionId,
            slug: getModelSlug(),
            downloadUrl: getDownloadUrl(versionId),
        };
    }

    function buildPayload() {
        const info = pageInfo();
        return {
            url: info.downloadUrl,
            model_id: info.modelId,
            model_version_id: info.versionId,
            model_slug: info.slug,
            metadata: { page_url: location.href },
        };
    }

    // The version's type and base model come from Civitai's own API rather
    // than from the page's markup, which changes without notice. They only
    // label the button and narrow the "already in your library" check.
    const VERSION_INFO_RETRY_MS = 60000;
    const versionInfo = new Map();
    const versionInflight = new Map();

    function loadVersionInfo(versionId) {
        const known = versionInfo.get(versionId);
        if (known && (known.ok || Date.now() - known.at < VERSION_INFO_RETRY_MS)) return Promise.resolve(known);
        if (versionInflight.has(versionId)) return versionInflight.get(versionId);

        const request = fetch(`/api/v1/model-versions/${versionId}`, { credentials: 'same-origin' })
            .then(res => (res.ok ? res.json() : null))
            .catch(() => null)
            .then(data => {
                const entry = {
                    ok: !!data,
                    at: Date.now(),
                    type: (data && data.model && data.model.type) || '',
                    baseModel: (data && data.baseModel) || '',
                    air: (data && data.air) || '',
                };
                const typeName = entry.type === 'LORA' ? 'LoRA' : entry.type;
                entry.label = [typeName, entry.baseModel].filter(Boolean).join(' / ');
                versionInfo.set(versionId, entry);
                versionInflight.delete(versionId);
                return entry;
            });
        versionInflight.set(versionId, request);
        return request;
    }

    function labelButton(button) {
        const versionId = Number(button.dataset.cdVersion);
        if (!versionId) return;
        loadVersionInfo(versionId).then(info => {
            if (!button.isConnected || button.dataset.cdVersion !== String(versionId)) return;
            button.dataset.cdLabel = info.label;
            paintIdleIfCalm(button);
        });
    }


    function findInsertionPoint() {
        const actionGroup =
            document.querySelector('[data-tour="model:like"]')?.closest('.mantine-Group-root[data-grow="true"]') ||
            document.querySelector('button[aria-haspopup="dialog"]')?.closest('.mantine-Group-root[data-grow="true"]');

        if (actionGroup) {
            const actionCard = actionGroup.closest('.mantine-Card-root');

            if (actionCard?.parentElement) {
                return {
                    parent: actionCard.parentElement,
                    before: actionCard,
                    mode: 'before'
                };
            }
        }

        const downloadLink = document.querySelector('a[href*="/api/download/models/"]');

        if (downloadLink) {
            const downloadCard = downloadLink.closest('.mantine-Card-root');

            if (downloadCard?.parentElement) {
                return {
                    parent: downloadCard.parentElement,
                    before: downloadCard,
                    mode: 'before'
                };
            }
        }

        const createBtn = document.querySelector(CREATE_SELECTOR);

        if (createBtn) {
            const card = createBtn.closest('.mantine-Card-root');

            if (card?.parentElement) {
                return {
                    parent: card.parentElement,
                    before: card,
                    mode: 'before'
                };
            }
        }

        const oldCtaContainer = document.querySelector(OLD_CTA_CONTAINER_SELECTOR);

        if (oldCtaContainer) {
            return {
                parent: oldCtaContainer,
                before: null,
                mode: 'append'
            };
        }

        return null;
    }

    function applyButtonStyles(button) {
        button.style.display = 'inline-flex';
        button.style.alignItems = 'center';
        button.style.justifyContent = 'center';
        button.style.gap = '6px';
        button.style.width = '100%';
        button.style.height = '42px';
        button.style.minHeight = '42px';
        button.style.padding = '0 14px';
        button.style.margin = '0 0 8px 0';
        button.style.border = '1px solid transparent';
        button.style.borderRadius = '8px';
        button.style.background = 'linear-gradient(135deg, #0b7285, #0c8599)';
        button.style.color = '#ffffff';
        button.style.fontSize = '14px';
        button.style.fontWeight = '700';
        button.style.lineHeight = '1';
        button.style.whiteSpace = 'nowrap';
        button.style.cursor = 'pointer';
        button.style.flex = 'none';
        button.style.boxSizing = 'border-box';
        button.style.boxShadow = '0 1px 3px rgba(0,0,0,0.25)';
        button.style.transition = 'background-color 0.15s ease, opacity 0.15s ease, transform 0.08s ease';
    }

    const HAVE_BG = 'linear-gradient(135deg, #5f3dc4, #7048e8)';
    const RED_BG = 'linear-gradient(135deg, #c92a2a, #e03131)';
    const GREEN_BG = 'linear-gradient(135deg, #2b8a3e, #37b24d)';
    const offlineTitle = () =>
        `No ComfyUI with the Civitai-Direct node found at ${Core.describeTargets()}. Start ComfyUI, then click to retry.`;

    function setButtonState(button, state) {
        const labelBits = button.dataset.cdLabel || '';
        const labelBase = labelBits ? `Direct to Server (${labelBits})` : 'Direct to Server';

        if (state === 'idle' && button.dataset.cdOffline === '1') {
            button.textContent = '🔌 ComfyUI Offline';
            button.style.background = RED_BG;
            button.style.color = '#ffffff';
            button.disabled = false;
            button.style.opacity = '0.9';
            button.title = offlineTitle();
            return;
        }

        if (state === 'idle' && button.dataset.cdHave === '1') {
            button.textContent = labelBits ? `📁 In Your Library (${labelBits})` : '📁 In Your Library';
            button.style.background = HAVE_BG;
            button.style.color = '#ffffff';
            button.disabled = false;
            button.style.opacity = '1';
            button.title = button.dataset.cdHavePath
                ? `Already downloaded: ${button.dataset.cdHavePath}\nClick to re-check on the server.`
                : 'Already downloaded. Click to re-check on the server.';
            return;
        }

        if (state === 'idle' && button.dataset.cdEa === '1') {
            button.textContent = labelBits ? `⚡ Early Access (${labelBits})` : '⚡ Early Access';
            button.style.background = 'linear-gradient(135deg, #f59f00, #fab005)';
            button.style.color = '#1a1b1e';
            button.disabled = false;
            button.style.opacity = '0.8';
            button.title = 'Early Access: needs the purchase or subscription on your Civitai account. Click to try anyway.';
            return;
        }
        button.style.color = '#ffffff';
        button.title = '';

        if (state === 'idle') {
            button.textContent = `⚡ ${labelBase}`;
            button.style.background = 'linear-gradient(135deg, #0b7285, #0c8599)';
            button.disabled = false;
            button.style.opacity = '1';
            return;
        }

        if (state === 'sending') {
            button.textContent = '⏳ Sending...';
            button.style.background = 'linear-gradient(135deg, #e67700, #f08c00)';
            button.disabled = true;
            button.style.opacity = '0.85';
            return;
        }

        if (state === 'success') {
            button.textContent = '✅ Saved to Library';
            button.style.background = GREEN_BG;
            button.disabled = false;
            button.style.opacity = '1';
            return;
        }

        // Older servers reply without a job id, so queued is all we know.
        if (state === 'sent') {
            button.textContent = '✅ Server Queued Download';
            button.style.background = GREEN_BG;
            button.disabled = false;
            button.style.opacity = '1';
            return;
        }

        if (state === 'exists') {
            button.textContent = '📁 Already in Your Library';
            button.style.background = HAVE_BG;
            button.disabled = false;
            button.style.opacity = '1';
            if (button.dataset.cdHavePath) button.title = `Already downloaded: ${button.dataset.cdHavePath}`;
            return;
        }

        if (state === 'error') {
            // The server's own words say what went wrong; show them on hover.
            const detail = button.dataset.cdErr || '';
            button.textContent = /API key|log ?in/i.test(detail) ? '🔑 API Key Needed' : '❌ Request Failed';
            button.style.background = RED_BG;
            button.disabled = false;
            button.style.opacity = '1';
            button.title = detail || 'The request failed. The browser console has details.';
            return;
        }

        if (state === 'offline') {
            button.textContent = '🔌 ComfyUI Offline';
            button.style.background = RED_BG;
            button.disabled = false;
            button.style.opacity = '1';
            button.title = offlineTitle();
        }
    }

    function removeExistingButton() {
        const existing = document.getElementById(BUTTON_ID);
        if (existing) existing.remove();
    }

    function errorText(responseText) {
        try { return String(JSON.parse(responseText).error || ''); } catch (_) { return ''; }
    }

    function injectButton() {
        const insertion = findInsertionPoint();
        if (!insertion) return false;

        const info = pageInfo();
        if (!info.downloadUrl) return false;

        removeExistingButton();

        const button = document.createElement('button');
        button.id = BUTTON_ID;
        button.type = 'button';
        button.setAttribute('data-tm-role', 'direct-server-download');
        button.dataset.cdVersion = String(info.versionId || '');

        applyButtonStyles(button);
        button.dataset.cdEa = isEarlyAccessSelectedVersion() ? '1' : '';
        setButtonState(button, 'idle');
        labelButton(button);

        button.addEventListener('mouseenter', () => {
            if (!button.disabled && (button.textContent || '').includes('Direct to Server')) {
                button.style.transform = 'translateY(-1px)';
                button.style.opacity = '0.95';
            }
        });

        button.addEventListener('mouseleave', () => {
            button.style.transform = 'translateY(0)';
            button.style.opacity = '1';
        });

        button.addEventListener('click', (e) => {
            e.preventDefault();
            e.stopPropagation();

            const payload = buildPayload();
            const fail = (detail) => {
                button.dataset.cdErr = detail || '';
                setButtonState(button, 'error');
                resetSoon();
            };

            const resetSoon = () => setTimeout(() => {
                button.dataset.cdBusy = '';
                setButtonState(button, 'idle');
            }, 4000);

            if (!payload.url) {
                console.error('[Civitai Direct] Could not find the download link on this page.');
                fail('Could not find the download link on this page.');
                return;
            }

            console.log('[Civitai Direct] Sending payload:', payload);
            button.dataset.cdBusy = '1';
            button.dataset.cdErr = '';
            setButtonState(button, 'sending');

            Core.probe((base) => {
                if (!base) {
                    console.error('[Civitai Direct] No ComfyUI with the Civitai-Direct node found at', Core.describeTargets());
                    button.dataset.cdOffline = '1';
                    setButtonState(button, 'offline');
                    resetSoon();
                    return;
                }
                button.dataset.cdOffline = '';

                GM_xmlhttpRequest({
                    method: 'POST',
                    url: `${base}/civitai/download`,
                    headers: { 'Content-Type': 'application/json' },
                    data: JSON.stringify(payload),

                    onload(response) {
                        if (response.status < 200 || response.status >= 300) {
                            console.error('[Civitai Direct] Server error:', response.responseText);
                            fail(errorText(response.responseText));
                            return;
                        }

                        let jobId = null;
                        try { jobId = JSON.parse(response.responseText).job_id; } catch (_) { /* older server */ }
                        if (!jobId) {
                            setButtonState(button, 'sent');
                            resetSoon();
                            return;
                        }

                        Core.pollJob(base, jobId,
                            (job) => {
                                if (job.state === 'downloading' && job.bytes_total) {
                                    const pct = Math.floor(job.bytes_done / job.bytes_total * 100);
                                    try { button.textContent = `⬇ ${pct}%`; } catch (_) { /* cosmetic only */ }
                                }
                            },
                            (ok, detail, job) => {
                                if (ok) {
                                    // Either way the file is in the library now, and
                                    // the idle button keeps saying so.
                                    button.dataset.cdHave = '1';
                                    button.dataset.cdHavePath = (job && job.path) || '';
                                    console.log('[Civitai Direct] Download finished:', detail, (job && job.path) || '');
                                    setButtonState(button, detail === 'already_exists' ? 'exists' : 'success');
                                    resetSoon();
                                } else {
                                    console.error('[Civitai Direct] Download failed:', detail);
                                    fail(detail);
                                }
                            });
                    },

                    onerror(err) {
                        console.error('[Civitai Direct] Could not reach', base, err);
                        Core.forget(); // find the server again on the next click
                        button.dataset.cdOffline = '1';
                        setButtonState(button, 'offline');
                        resetSoon();
                    }
                });
            });
        });

        if (insertion.mode === 'before' && insertion.before && insertion.before.parentNode === insertion.parent) {
            insertion.parent.insertBefore(button, insertion.before);
        } else {
            insertion.parent.appendChild(button);
        }

        return true;
    }

    // ------------------------------------------------------------------
    // Server health and library awareness: probe /civitai/health so the
    // button shows "ComfyUI Offline" before anyone clicks, and ask
    // /civitai/exists whether this version is already on disk.
    // ------------------------------------------------------------------

    const HEALTH_INTERVAL_MS = 45000;
    let healthTimer = null;

    function paintIdleIfCalm(button) {
        // Never clobber an in-flight click (sending, progress, result flash).
        if (button.dataset.cdBusy === '1') return;
        setButtonState(button, 'idle');
    }

    function checkLibrary(button) {
        const base = Core.current();
        const versionId = Number(button.dataset.cdVersion);
        if (!base || !versionId) return;
        if (button.dataset.cdHaveChecked === String(versionId)) return;
        button.dataset.cdHaveChecked = String(versionId);

        loadVersionInfo(versionId).then(info => {
            const modelId = getModelIdFromUrl();
            const params = [
                `model_version_id=${encodeURIComponent(versionId)}`,
                modelId ? `model_id=${encodeURIComponent(modelId)}` : '',
                info.type ? `type=${encodeURIComponent(info.type)}` : '',
                info.baseModel ? `base_model=${encodeURIComponent(info.baseModel)}` : '',
                // The AIR urn lets the server pick the same folder slug it
                // used at download time, even for base models it has no
                // mapping for.
                info.air ? `air=${encodeURIComponent(info.air)}` : '',
            ].filter(Boolean).join('&');

            GM_xmlhttpRequest({
                method: 'GET',
                url: `${base}/civitai/exists?${params}`,
                timeout: 4000,
                onload(res) {
                    if (res.status !== 200) return;
                    let answer = null;
                    try { answer = JSON.parse(res.responseText); } catch (_) { return; }
                    if (!answer || typeof answer.exists !== 'boolean') return;
                    button.dataset.cdHave = answer.exists ? '1' : '';
                    button.dataset.cdHavePath = (answer.exists && answer.path) || '';
                    paintIdleIfCalm(button);
                },
                // On error the button keeps its normal state; offline
                // handling belongs to the health probe.
            });
        });
    }

    function applyServerStatus(online) {
        const button = document.getElementById(BUTTON_ID);
        if (!button) return;
        const flag = online ? '' : '1';
        const changed = (button.dataset.cdOffline || '') !== flag;
        button.dataset.cdOffline = flag;
        if (online) checkLibrary(button);
        if (changed) paintIdleIfCalm(button);
    }

    function probeHealth() {
        if (!isModelPage() || !document.getElementById(BUTTON_ID)) return;
        Core.probe((base) => applyServerStatus(!!base));
    }

    function startHealthMonitor() {
        if (healthTimer) return;
        healthTimer = setInterval(probeHealth, HEALTH_INTERVAL_MS);
        document.addEventListener('visibilitychange', () => {
            if (document.visibilityState === 'visible') probeHealth();
        });
    }

    function renderButtonIfReady() {
        if (!isModelPage()) {
            removeExistingButton();
            return;
        }

        const existing = document.getElementById(BUTTON_ID);
        const info = pageInfo();
        if (!info.downloadUrl) return;

        const renderKey = `${location.pathname}|${info.versionId || ''}|${info.downloadUrl}|ea:${isEarlyAccessSelectedVersion() ? 1 : 0}`;
        if (existing && existing.dataset.renderKey === renderKey) return;

        if (injectButton()) {
            const button = document.getElementById(BUTTON_ID);
            if (button) {
                button.dataset.renderKey = renderKey;
                probeHealth();
            }
        }
    }

    // Civitai hydrates model pages asynchronously; fixed timers miss slow
    // renders, so keep retrying until the button is placed (or we time out).
    function stopRetryLoop() {
        if (retryTimer) {
            clearInterval(retryTimer);
            retryTimer = null;
        }
    }

    function isModelPage() {
        return /\/models\//.test(location.pathname);
    }

    function retryTick() {
        if (location.href !== lastUrl) {
            lastUrl = location.href;
            removeExistingButton();
            retryDeadline = Date.now() + RETRY_WINDOW_MS;
        }

        // The script matches all of civitai.com (Tampermonkey doesn't inject
        // on SPA navigation, so matching only /models/* missed navigations
        // from other pages); only model pages get the button.
        if (!isModelPage()) {
            removeExistingButton();
            stopRetryLoop();
            return;
        }

        renderButtonIfReady();

        if (document.getElementById(BUTTON_ID) || Date.now() > retryDeadline) {
            stopRetryLoop();
        }
    }

    function startRetryLoop() {
        stopRetryLoop();
        retryDeadline = Date.now() + RETRY_WINDOW_MS;
        retryTick();
        if (!retryTimer && !document.getElementById(BUTTON_ID)) {
            retryTimer = setInterval(retryTick, RETRY_INTERVAL_MS);
        }
    }

    function observePage() {
        if (observer) observer.disconnect();

        observer = new MutationObserver(() => {
            if (observerDebounce) clearTimeout(observerDebounce);
            observerDebounce = setTimeout(() => {
                observerDebounce = null;

                if (location.href !== lastUrl) {
                    lastUrl = location.href;
                    removeExistingButton();
                    startRetryLoop();
                    return;
                }

                if (!document.getElementById(BUTTON_ID)) {
                    if (isModelPage() && !retryTimer) startRetryLoop();
                    return;
                }

                renderButtonIfReady();
            }, OBSERVER_DEBOUNCE_MS);
        });

        observer.observe(document.body, {
            childList: true,
            subtree: true
        });
    }

    function hookNavigation() {
        const originalPushState = history.pushState;
        const originalReplaceState = history.replaceState;

        function handleNavChange() {
            lastUrl = location.href;
            removeExistingButton();
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

    function init() {
        hookNavigation();
        observePage();
        startRetryLoop();
        startHealthMonitor();
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init, { once: true });
    } else {
        init();
    }
})();

// ----------------------------------------------------------------------
// 2. Quick-download button on model cards
// ----------------------------------------------------------------------
(function modelCardButtons() {
    'use strict';

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
        btn.title = 'Early Access: needs the purchase or subscription on your Civitai account. Click twice to try anyway.';
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
        let slug = m[2] || null;
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
                key: ['🔑', ERR_BG],
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

            Core.probe((base) => {
                if (!base) {
                    console.error('[Civitai Direct] No ComfyUI with the Civitai-Direct node found at', Core.describeTargets());
                    settle('offline', `ComfyUI is not running (checked ${Core.describeTargets()}). Start ComfyUI with the Civitai-Direct node, then retry.`);
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
                        Core.pollJob(base, jobId,
                            (job) => {
                                if (job.state === 'downloading' && job.bytes_total) {
                                    btn.textContent = `${Math.floor(job.bytes_done / job.bytes_total * 100)}`;
                                }
                            },
                            (ok, detail, job) => {
                                if (!ok) {
                                    console.error('[Civitai Direct] Download failed:', detail);
                                    settle(/API key|log ?in/i.test(detail || '') ? 'key' : 'err', detail ? `Download failed: ${detail}` : '');
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
                        Core.forget(); // find the server again on the next click
                        settle('offline', `Lost the connection to ComfyUI. Start it (${Core.describeTargets()}), then retry.`);
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

// ----------------------------------------------------------------------
// 3. Gallery images: copy prompt and nodes, save the image
// ----------------------------------------------------------------------
(function galleryImages() {
    'use strict';

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
            btn.title = `ComfyUI is not running (checked ${Core.describeTargets()}). Start ComfyUI with the Civitai-Direct node, then retry.`;
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
            Core.probe(base => {
                if (!base) {
                    console.error('[civitai-direct] No ComfyUI with the Civitai-Direct node found at', Core.describeTargets());
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

                        Core.pollJob(base, jobId, null, (ok, detail) => {
                            if (!ok) console.error('[civitai-direct] Image download failed:', detail);
                            const state = !ok ? 'error' : (detail === 'already_exists' ? 'exists' : 'success');
                            setButtonState(btn, state);
                            if (!ok) btn.title = String(detail || 'Download failed');
                            setTimeout(() => setButtonState(btn, 'idle'), 3000);
                        });
                    },
                    onerror() {
                        Core.forget(); // find the server again on the next click
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
