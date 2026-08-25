// ==UserScript==
// @name         Civitai Direct for ComfyUI
// @namespace    https://github.com/ausboss/ComfyUI-Civitai-Direct
// @version      0.2.2
// @description  One-click Civitai model downloads straight into your ComfyUI model folders, via the ComfyUI-Civitai-Direct custom node.
// @match        *://civitai.com/*
// @match        *://www.civitai.com/*
// @match        *://civitai.red/*
// @match        *://www.civitai.red/*
// @connect      127.0.0.1
// @connect      localhost
// @grant        GM_xmlhttpRequest
// @downloadURL  https://raw.githubusercontent.com/ausboss/ComfyUI-Civitai-Direct/main/userscripts/civitai-direct.user.js
// @updateURL    https://raw.githubusercontent.com/ausboss/ComfyUI-Civitai-Direct/main/userscripts/civitai-direct.user.js
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

    // Re-validate the cached server (or discover one). Used by the health
    // badge and by clicks, so a ComfyUI restart -- even on the other port --
    // heals without a second click.
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

    // Poll a queued job until it reaches a terminal state, reporting progress.
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

    function cleanText(value) {
        return (value || '').replace(/\s+/g, ' ').trim();
    }

    function normalizeList(items) {
        return [...new Set((items || []).map(x => cleanText(x)).filter(Boolean))];
    }

    function sanitizeForFilename(value, fallback = 'model') {
        const cleaned = cleanText(value || fallback)
            .replace(/[^\w.-]+/g, '_')
            .replace(/_+/g, '_')
            .replace(/^_+|_+$/g, '')
            .slice(0, 120)
            .replace(/^_+|_+$/g, '');

        return cleaned || fallback;
    }

    function isWanModel(metadata) {
        const baseModel = cleanText(metadata?.base_model || '').toLowerCase();
        const compactBase = baseModel.replace(/[\s._-]+/g, '');
        if (baseModel.startsWith('wan') || compactBase.startsWith('wan')) return true;

        const haystack = [
            metadata?.model_slug,
            metadata?.model_name,
            metadata?.version_name,
        ].filter(Boolean).join(' ').toLowerCase();
        const compact = haystack.replace(/[\s._-]+/g, '');

        return /(^|[^a-z0-9])wan([^a-z0-9]|$)/i.test(haystack) ||
            /^wan/.test(compact) ||
            /wan(?:video|i2v|t2v|2\d|1\d)/.test(compact);
    }

    function parseIntSafe(value) {
        const n = Number.parseInt(value, 10);
        return Number.isFinite(n) ? n : null;
    }

    function normalizeBaseModel(value) {
        value = cleanText(value || '')
            .replace(/Copy$/i, '')
            .replace(/\s+/g, ' ')
            .trim();

        const lower = value.toLowerCase().replace(/[\s._-]+/g, '');

        if (lower.includes('zimageturbo') || lower.includes('zimage turbo')) return 'ZImageTurbo';
        if (lower.includes('zimage')) return 'ZImage';
        if (lower.includes('flux2klein9b')) return 'Flux.2 Klein 9B';
        if (lower.includes('flux1dev')) return 'Flux.1 Dev';
        if (lower.includes('sdxl')) return 'SDXL';
        if (lower.includes('pony')) return 'Pony';
        if (lower.includes('sd15') || lower.includes('sd1.5')) return 'SD 1.5';

        return value || null;
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

    function getModelSlug() {
        const parts = window.location.pathname.split('/').filter(Boolean);
        return parts[parts.length - 1] || 'model';
    }

    function looksLikeVersionText(text) {
        if (!text || text.length > 120) return false;
        if (/^\d+$/.test(text)) return false;
        if (/^(download|create|share|like|save|follow|report|copy|open|close|details|stats|reviews|published|base model|hash|air)$/i.test(text)) {
            return false;
        }

        return /(^v?\d+(?:\.\d+)?\b|\bv\d+\b|\b(alpha|beta|final|preview|high|low|noise|wan|i2v|t2v|fp\d+|pruned|full|dev|schnell|pony|flux|sdxl|krea)\b)/i.test(text);
    }

    function getVersionName() {
        const visibleButtons = Array.from(document.querySelectorAll('button'))
            .filter(button => {
                const rect = button.getBoundingClientRect();
                return rect.width > 0 && rect.height > 0 && cleanText(button.innerText || button.textContent);
            });
        const versionButtons = visibleButtons.filter(button => looksLikeVersionText(cleanText(button.innerText || button.textContent)));
        const activeBlueButtons = versionButtons.filter(button => /rgb\(\s*25,\s*113,\s*194\s*\)/.test(getComputedStyle(button).backgroundColor));

        if (activeBlueButtons.length === 1) {
            return cleanText(activeBlueButtons[0].innerText || activeBlueButtons[0].textContent);
        }

        if (versionButtons.length === 1) {
            return cleanText(versionButtons[0].innerText || versionButtons[0].textContent);
        }

        const colorCounts = new Map();
        const coloredButtons = versionButtons.map(button => {
            const color = getComputedStyle(button).backgroundColor;
            colorCounts.set(color, (colorCounts.get(color) || 0) + 1);
            return { button, color };
        });
        const inactiveColor = [...colorCounts.entries()]
            .sort((a, b) => b[1] - a[1])[0]?.[0];
        const colorOutliers = coloredButtons.filter(item => item.color !== inactiveColor);

        if (colorOutliers.length === 1) {
            return cleanText(colorOutliers[0].button.innerText || colorOutliers[0].button.textContent);
        }

        const groups = [];
        const byParent = new Map();

        for (const button of versionButtons) {
            if (!button.parentElement) continue;

            const siblings = byParent.get(button.parentElement) || [];
            siblings.push(button);
            byParent.set(button.parentElement, siblings);
        }

        for (const buttons of byParent.values()) {
            const siblingVersionButtons = buttons.filter(button => {
                const text = cleanText(button.innerText || button.textContent);
                return looksLikeVersionText(text);
            });

            if (siblingVersionButtons.length) groups.push(siblingVersionButtons);
        }

        for (const buttons of groups) {
            if (buttons.length === 1) continue;

            const counts = new Map();
            const backgrounds = buttons.map(button => {
                const color = getComputedStyle(button).backgroundColor;
                counts.set(color, (counts.get(color) || 0) + 1);
                return { button, color };
            });

            const inactiveColor = [...counts.entries()]
                .sort((a, b) => b[1] - a[1])[0]?.[0];
            const outliers = backgrounds.filter(item => item.color !== inactiveColor);

            if (outliers.length === 1 && looksLikeVersionText(cleanText(outliers[0].button.innerText || outliers[0].button.textContent))) {
                return cleanText(outliers[0].button.innerText || outliers[0].button.textContent);
            }

            const blueActive = backgrounds.filter(item =>
                /rgb\(\s*25,\s*113,\s*194\s*\)/.test(item.color) &&
                looksLikeVersionText(cleanText(item.button.innerText || item.button.textContent))
            );
            if (blueActive.length === 1) {
                return cleanText(blueActive[0].button.innerText || blueActive[0].button.textContent);
            }
        }

        return null;
    }

    // Civitai's version-specific early-access notice ("The creator ... has
    // set this version to Early Access ...", EarlyAccessAlert.tsx) only
    // renders for the currently selected version while its window is open —
    // unlike the title chip, which flags the whole model.
    function isEarlyAccessSelectedVersion() {
        return /set this version to Early Access/i.test(document.body.innerText || '');
    }

    function getTitleText() {
        const h1 = document.querySelector('h1');
        if (h1) return cleanText(h1.innerText);

        const title = cleanText(document.title || '');
        return title.replace(/\s*\|\s*Civitai.*$/i, '');
    }

    function getCreator() {
        const links = Array.from(document.querySelectorAll('a[href^="/user/"], a[href^="/users/"]'));
        const first = links.find(a => cleanText(a.innerText));
        return first ? cleanText(first.innerText) : null;
    }

    function getDetailsRows() {
        const rows = [];

        for (const group of Array.from(document.querySelectorAll('.mantine-Group-root'))) {
            const directParagraphs = Array.from(group.children)
                .map(el => cleanText(el.innerText))
                .filter(Boolean);

            if (directParagraphs.length >= 2) {
                rows.push({
                    key: directParagraphs[0],
                    value: directParagraphs.slice(1).join(' '),
                    element: group
                });
            }
        }

        for (const row of Array.from(document.querySelectorAll('tr'))) {
            const cells = row.querySelectorAll('td, th');
            if (cells.length >= 2) {
                rows.push({
                    key: cleanText(cells[0].innerText),
                    value: cleanText(cells[1].innerText),
                    element: row
                });
            }
        }

        return rows;
    }

    function findDetailValue(label) {
        const labelRegex = new RegExp(`^${label}$`, 'i');

        for (const row of getDetailsRows()) {
            if (labelRegex.test(row.key)) return cleanText(row.value);
        }

        return null;
    }

    function getType() {
        const value = findDetailValue('Type');
        if (value) {
            if (/lora/i.test(value)) return 'LoRA';
            if (/checkpoint/i.test(value)) return 'Checkpoint';
            if (/embedding/i.test(value)) return 'Embedding';
            return value;
        }

        if (/\bLoRA\b/i.test(document.body.innerText)) return 'LoRA';
        return null;
    }

    function getBaseModel() {
        return normalizeBaseModel(findDetailValue('Base Model'));
    }

    function getPublished() {
        return findDetailValue('Published');
    }

    function getHashInfo() {
        for (const row of getDetailsRows()) {
            if (/^Hash$/i.test(row.key)) {
                const text = row.value;
                const typeMatch = text.match(/\b(AutoV1|AutoV2|SHA256|CRC32|Blake3)\b/i);
                const hashMatch = text.match(/\b[A-Fa-f0-9]{8,128}\b/);

                return {
                    hash_type: typeMatch ? typeMatch[1] : null,
                    hash_value: hashMatch ? hashMatch[0] : null
                };
            }
        }

        return { hash_type: null, hash_value: null };
    }

    function getTriggerWords() {
        const words = [];

        for (const row of getDetailsRows()) {
            if (/^Trigger Words$/i.test(row.key)) {
                const value = row.value.replace(/Copy/g, '').trim();
                if (value) words.push(value);
            }
        }

        const possibleBadges = Array.from(document.querySelectorAll('.mantine-Badge-label'));

        for (const badge of possibleBadges) {
            const text = cleanText(badge.innerText).replace(/Copy$/i, '').trim();

            if (
                text &&
                text.length > 2 &&
                !/^(LoRA|Checkpoint|Embedding|AutoV1|AutoV2|SHA256|CRC32|Blake3|SafeTensor)$/i.test(text)
            ) {
                if (/trigger|seamlessly|object|style|person|character|concept|girl|woman|man|ellie/i.test(text)) {
                    words.push(text);
                }
            }
        }

        return normalizeList(words);
    }

    function getAir() {
        const codeBlocks = Array.from(document.querySelectorAll('code'))
            .map(code => cleanText(code.innerText))
            .filter(Boolean);

        const joined = codeBlocks.join('');
        const direct = joined.match(/civitai:\d+@\d+/i);

        if (direct) return direct[0];

        const modelId = getModelIdFromUrl();
        const versionId = getModelVersionId();

        if (modelId && versionId) return `civitai:${modelId}@${versionId}`;
        return null;
    }

    function getDescriptionElement() {
        const wrappers = Array.from(document.querySelectorAll(
            '.RenderHtml_htmlRenderer__z8vxT, .mantine-TypographyStylesProvider-root'
        ));

        for (const wrapper of wrappers) {
            const text = cleanText(wrapper.innerText);
            if (/Suggested tags|Suggested strength|Suggested usage|How to use|trigger/i.test(text)) {
                return wrapper;
            }
        }

        return wrappers[0] || null;
    }

    function getDescription() {
        const el = getDescriptionElement();

        if (!el) {
            return {
                description_html: null,
                description_text: null
            };
        }

        return {
            description_html: el.innerHTML || null,
            description_text: cleanText(el.innerText) || null
        };
    }

    function extractSuggestedFromDescription(descriptionText) {
        const text = descriptionText || '';

        const suggested = {
            tags: [],
            strength: null,
            usage: null,
            notes: null
        };

        const tagsMatch = text.match(/Suggested tags:\s*(.*?)(?=Suggested strength:|Suggested usage:|$)/i);
        if (tagsMatch) {
            suggested.tags = normalizeList(
                tagsMatch[1]
                    .split(/\n|,|\|/)
                    .map(x => x.replace(/^`|`$/g, '').trim())
            );
        }

        const strengthMatch = text.match(/Suggested strength:\s*(.*?)(?=Suggested usage:|$)/i);
        if (strengthMatch) suggested.strength = cleanText(strengthMatch[1]);

        const usageMatch = text.match(/Suggested usage:\s*(.*?)(?=\n[A-Z][a-z]|$)/i);
        if (usageMatch) suggested.usage = cleanText(usageMatch[1]);

        const leftover = text
            .replace(/Suggested tags:\s*.*?(?=Suggested strength:|Suggested usage:|$)/i, '')
            .replace(/Suggested strength:\s*.*?(?=Suggested usage:|$)/i, '')
            .replace(/Suggested usage:\s*.*?(?=\n[A-Z][a-z]|$)/i, '')
            .trim();

        suggested.notes = leftover || null;
        return suggested;
    }

    function buildMetadata() {
        const description = getDescription();
        const suggested = extractSuggestedFromDescription(description.description_text);
        const hashInfo = getHashInfo();

        const triggerWords = normalizeList([
            ...getTriggerWords(),
            ...suggested.tags
        ]);

        return {
            page_url: location.href,
            source_domain: location.hostname,

            model_id: getModelIdFromUrl(),
            model_version_id: getModelVersionId(),

            model_name: getTitleText(),
            model_slug: getModelSlug(),
            version_name: getVersionName(),
            creator: getCreator(),

            type: getType(),
            base_model: getBaseModel(),
            published: getPublished(),

            hash_type: hashInfo.hash_type,
            hash_value: hashInfo.hash_value,
            air: getAir(),

            trigger_words: triggerWords,
            trained_words: triggerWords,
            tags: [],

            description_html: description.description_html,
            description_text: description.description_text,
            suggested,

            stats: {}
        };
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
    const OFFLINE_TITLE = `No running ComfyUI with the Civitai-Direct node found on ports ${SERVER_PORTS.join(', ')}. Start ComfyUI, then click to retry.`;

    function setButtonState(button, state, baseModel = '', modelType = '') {
        const labelBits = [modelType, baseModel].filter(Boolean).join(' / ');
        const labelBase = labelBits ? `Direct to Server (${labelBits})` : 'Direct to Server';

        if (state === 'idle' && button.dataset.cdOffline === '1') {
            button.textContent = '🔌 ComfyUI Offline';
            button.style.background = 'linear-gradient(135deg, #c92a2a, #e03131)';
            button.style.color = '#ffffff';
            button.disabled = false;
            button.style.opacity = '0.9';
            button.title = OFFLINE_TITLE;
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
            button.title = 'Early Access — needs the purchase/subscription on your Civitai account. Click to try anyway.';
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
            button.textContent = '⏳ Sending metadata...';
            button.style.background = 'linear-gradient(135deg, #e67700, #f08c00)';
            button.disabled = true;
            button.style.opacity = '0.85';
            return;
        }

        if (state === 'success') {
            button.textContent = '✅ Saved to Library';
            button.style.background = 'linear-gradient(135deg, #2b8a3e, #37b24d)';
            button.disabled = false;
            button.style.opacity = '1';
            return;
        }

        // Older servers reply without a job id -- queued is all we know.
        if (state === 'sent') {
            button.textContent = '✅ Server Queued Download';
            button.style.background = 'linear-gradient(135deg, #2b8a3e, #37b24d)';
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
            button.textContent = '❌ Request Failed';
            button.style.background = 'linear-gradient(135deg, #c92a2a, #e03131)';
            button.disabled = false;
            button.style.opacity = '1';
            return;
        }

        if (state === 'offline') {
            button.textContent = '🔌 ComfyUI Offline';
            button.style.background = 'linear-gradient(135deg, #c92a2a, #e03131)';
            button.disabled = false;
            button.style.opacity = '1';
            button.title = OFFLINE_TITLE;
        }
    }

    function removeExistingButton() {
        const existing = document.getElementById(BUTTON_ID);
        if (existing) existing.remove();
    }

    function buildFilename(metadata) {
        if (!isWanModel(metadata)) return null;

        const versionId = metadata.model_version_id || getModelVersionId();
        const slug = sanitizeForFilename(metadata.model_slug || 'model');
        const versionName = metadata.version_name ? sanitizeForFilename(metadata.version_name) : null;
        const base = versionName ? `${slug}__${versionName}` : slug;
        const suffix = versionId ? `-v${versionId}` : '';
        return `${base}${suffix}.safetensors`;
    }

    function buildPayload() {
        const metadata = buildMetadata();

        return {
            url: getDownloadUrl(metadata.model_version_id),
            base_model: metadata.base_model,
            filename: buildFilename(metadata),
            metadata
        };
    }

    function injectButton() {
        const insertion = findInsertionPoint();
        if (!insertion) return false;

        const metadata = buildMetadata();
        const downloadUrl = getDownloadUrl(metadata.model_version_id);

        if (!downloadUrl) return false;

        removeExistingButton();

        const button = document.createElement('button');
        button.id = BUTTON_ID;
        button.type = 'button';
        button.setAttribute('data-tm-role', 'direct-server-download');

        applyButtonStyles(button);
        button.dataset.cdEa = isEarlyAccessSelectedVersion() ? '1' : '';
        setButtonState(button, 'idle', metadata.base_model, metadata.type);

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

            if (!payload.url) {
                console.error('[Tampermonkey] Could not find download URL.');
                setButtonState(button, 'error', payload.metadata.base_model, payload.metadata.type);
                return;
            }

            console.log('[Civitai Direct] Sending payload:', payload);
            button.dataset.cdBusy = '1';
            setButtonState(button, 'sending', payload.metadata.base_model, payload.metadata.type);

            const resetSoon = () => setTimeout(() => {
                button.dataset.cdBusy = '';
                const freshMeta = buildMetadata();
                setButtonState(button, 'idle', freshMeta.base_model, freshMeta.type);
            }, 4000);

            probeServer((base) => {
                if (!base) {
                    console.error('[Civitai Direct] No ComfyUI with the Civitai-Direct node found on ports', SERVER_PORTS);
                    button.dataset.cdOffline = '1';
                    setButtonState(button, 'offline', payload.metadata.base_model, payload.metadata.type);
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
                            setButtonState(button, 'error', payload.metadata.base_model, payload.metadata.type);
                            resetSoon();
                            return;
                        }

                        let jobId = null;
                        try { jobId = JSON.parse(response.responseText).job_id; } catch (_) { /* older server */ }
                        if (!jobId) {
                            setButtonState(button, 'sent', payload.metadata.base_model, payload.metadata.type);
                            resetSoon();
                            return;
                        }

                        pollJob(base, jobId,
                            (job) => {
                                if (job.state === 'downloading' && job.bytes_total) {
                                    const pct = Math.floor(job.bytes_done / job.bytes_total * 100);
                                    try { button.textContent = `⬇ ${pct}%`; } catch (_) { /* cosmetic only */ }
                                }
                            },
                            (ok, detail, job) => {
                                if (ok) {
                                    // Either way the file is in the library now --
                                    // the idle button keeps saying so.
                                    button.dataset.cdHave = '1';
                                    button.dataset.cdHavePath = (job && job.path) || '';
                                    const state = detail === 'already_exists' ? 'exists' : 'success';
                                    console.log('[Civitai Direct] Download finished:', detail, (job && job.path) || '');
                                    setButtonState(button, state, payload.metadata.base_model, payload.metadata.type);
                                } else {
                                    console.error('[Civitai Direct] Download failed:', detail);
                                    setButtonState(button, 'error', payload.metadata.base_model, payload.metadata.type);
                                }
                                resetSoon();
                            });
                    },

                    onerror(err) {
                        console.error('[Civitai Direct] Could not reach', base, err);
                        serverBase = null; // force re-discovery next click
                        button.dataset.cdOffline = '1';
                        setButtonState(button, 'offline', payload.metadata.base_model, payload.metadata.type);
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
    // Server health + library awareness: probe /civitai/health so the button
    // shows "ComfyUI Offline" before anyone clicks, and ask /civitai/exists
    // whether this version is already on disk.
    // ------------------------------------------------------------------

    const HEALTH_INTERVAL_MS = 45000;
    let healthTimer = null;

    function paintIdleIfCalm(button) {
        // Never clobber an in-flight click (sending / progress / result flash).
        if (button.dataset.cdBusy === '1') return;
        const metadata = buildMetadata();
        setButtonState(button, 'idle', metadata.base_model, metadata.type);
    }

    function checkLibrary(button) {
        if (!serverBase) return;
        const metadata = buildMetadata();
        const versionId = metadata.model_version_id;
        if (!versionId) return;
        if (button.dataset.cdHaveChecked === String(versionId)) return;
        button.dataset.cdHaveChecked = String(versionId);

        const params = [
            `model_version_id=${encodeURIComponent(versionId)}`,
            metadata.model_id ? `model_id=${encodeURIComponent(metadata.model_id)}` : '',
            metadata.type ? `type=${encodeURIComponent(metadata.type)}` : '',
            metadata.base_model ? `base_model=${encodeURIComponent(metadata.base_model)}` : '',
            // The AIR urn lets the server pick the same folder slug it used
            // at download time, even for base models it has no mapping for.
            metadata.air ? `air=${encodeURIComponent(metadata.air)}` : '',
        ].filter(Boolean).join('&');

        GM_xmlhttpRequest({
            method: 'GET',
            url: `${serverBase}/civitai/exists?${params}`,
            timeout: 4000,
            onload(res) {
                if (res.status !== 200) return;
                let info = null;
                try { info = JSON.parse(res.responseText); } catch (_) { return; }
                if (!info || typeof info.exists !== 'boolean') return;
                button.dataset.cdHave = info.exists ? '1' : '';
                button.dataset.cdHavePath = (info.exists && info.path) || '';
                paintIdleIfCalm(button);
            },
            // On error the button just keeps its normal state; offline
            // handling belongs to the health probe.
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
        probeServer((base) => applyServerStatus(!!base));
    }

    function startHealthMonitor() {
        if (healthTimer) return;
        healthTimer = setInterval(probeHealth, HEALTH_INTERVAL_MS);
        document.addEventListener('visibilitychange', () => {
            if (document.visibilityState === 'visible') probeHealth();
        });
    }

    function renderButtonIfReady() {
        if (!/\/models\//.test(location.pathname)) {
            removeExistingButton();
            return;
        }

        const existing = document.getElementById(BUTTON_ID);
        const metadata = buildMetadata();
        const downloadUrl = getDownloadUrl(metadata.model_version_id);

        if (!downloadUrl) return;

        const renderKey = `${location.pathname}|${metadata.model_version_id || ''}|${metadata.base_model || ''}|${downloadUrl}|ea:${isEarlyAccessSelectedVersion() ? 1 : 0}`;

        if (existing && existing.dataset.renderKey === renderKey) return;

        const injected = injectButton();

        if (injected) {
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
