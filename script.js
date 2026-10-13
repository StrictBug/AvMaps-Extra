const PRODUCT_CATEGORY = 'Airmass';

const imageConfig = {
    Airmass: {
        left: { folder: 'FZL', title: 'FZL' },
        right: { folder: '4C', title: '4C isotherm' }
    }
};

const domainHotkeys = {
    '1': 'AU',
    '2': 'WA_SA',
    '3': 'VIC_TAS',
    '4': 'NSW'
};

const LEGEND_PREFS_KEY = 'avmaps-extra.legendPrefs.v1';
const TIMEZONE_PREFS_KEY = 'avmaps-extra.timezonePrefs.v1';
const TIMEZONE_OPTIONS = {
    UTC: { abbr: 'UTC', offsetMinutes: 0 },
    AWST: { abbr: 'AWST', offsetMinutes: 8 * 60 },
    ACST: { abbr: 'ACST', offsetMinutes: (9 * 60) + 30 },
    AEST: { abbr: 'AEST', offsetMinutes: 10 * 60 },
    ACDT: { abbr: 'ACDT', offsetMinutes: (10 * 60) + 30 },
    AEDT: { abbr: 'AEDT', offsetMinutes: 11 * 60 },
};
const DEFAULT_TIMEZONE_KEY = 'UTC';
const EXCLUDED_LEGEND_FIELD_IDS = new Set([
    'airmass-fzl-fzl',
]);

const FALLBACK_LEGENDS = {
    Airmass: {
        left: {
            title: 'FZL',
            fields: [
                {
                    id: 'airmass-fzl-freezing-layer',
                    label: 'Freezing layer',
                    units: 'hatch',
                    items: [
                        { pattern: 'crosshatch', label: 'Multiple 0C crossings' },
                    ],
                },
                {
                    id: 'airmass-fzl-icing',
                    label: 'Icing severity',
                    units: '%',
                    items: [
                        { color: '#00ccff', label: '95-97.5' },
                        { color: '#00ffff', label: '97.5-100' },
                    ],
                },
                {
                    id: 'airmass-fzl-thermals',
                    label: 'Thermals',
                    units: 'ft',
                    items: [
                        { color: '#ffcc00', label: '6000-10000' },
                        { color: '#ff6600', label: '10000-20000+' },
                    ],
                },
                {
                    id: 'airmass-fzl-hail',
                    label: '1 hr small hail',
                    units: 'mm/h',
                    items: [
                        { color: '#b2ebf2', label: '0.1-0.5' },
                        { color: '#00bcd4', label: '2.5-5' },
                        { color: '#0097a7', label: '5-10' },
                        { color: '#006064', label: '10-20' },
                        { color: '#00363a', label: '20+' },
                    ],
                },
                {
                    id: 'airmass-fzl-freezing-fog',
                    label: 'Freezing fog',
                    units: 'category',
                    items: [
                        { color: '#ff0000', label: 'F1' },
                        { color: '#ffaa7f', label: 'F2' },
                        { color: '#ffff00', label: 'F3' },
                    ],
                },
            ],
        },
        right: {
            title: '4C isotherm',
            fields: [
                {
                    id: 'airmass-4c-height',
                    label: '4C isotherm height',
                    units: 'ft AMSL',
                    items: [
                        { color: '#99509f', label: '0-2000' },
                        { color: '#36c6f4', label: '2000-4000' },
                        { color: '#6fccdd', label: '4000-6000' },
                        { color: '#69bd45', label: '6000-8000' },
                        { color: '#cbdb2a', label: '8000-10000' },
                        { color: '#f6eb14', label: '10000-12000' },
                        { color: '#fbce08', label: '12000-14000' },
                        { color: '#f8981d', label: '14000-16000' },
                        { color: '#ed2024', label: '16000-18000' },
                        { color: '#ed2891', label: '18000-20000+' },
                    ],
                },
                {
                    id: 'airmass-4c-marginal-icing',
                    label: 'Marginal icing',
                    units: 'hatch',
                    items: [
                        { pattern: 'crosshatch', label: 'Mean RH 95%+ between 0C and 4C' },
                    ],
                },
            ],
        },
    },
};

let currentCategory = PRODUCT_CATEGORY;
let currentDomain = 'AU';
let currentFrame = 1;
const DEFAULT_MAX_FRAMES = 40;
const DECODE_WINDOW_RADIUS = 15;
let maxFrames = DEFAULT_MAX_FRAMES;
let isPlaying = false;
let animationInterval = null;
let animationSpeed = 500;
let selectedRangeStart = 1;
let selectedRangeEnd = DEFAULT_MAX_FRAMES;
let selectedTimezoneKey = DEFAULT_TIMEZONE_KEY;
let frameManifest = null;
const framePairsCache = {};
const decodedImageObjects = new Map();
const pendingDecodePromises = new Map();

let frameSlider;
let frameRangeStartSlider;
let frameRangeEndSlider;
let frameSelectionHighlight;
let rangeStartTimeLabel;
let rangeEndTimeLabel;
let currentFrameTimeLabel;
let timezoneSelect;
let leftImage;
let rightImage;
let playBtn;
let speedSlider;
let speedDisplay;
let categoryButtons;
let mobileCategorySelect;
let mobileDomainSelect;
let loadingOverlay;
let loadingBarFill;
let loadingStatus;
let infoBtn;
let infoOverlay;
let infoCloseBtn;
let legendStrip;
let legendContent;
let legendToggleBtn;
let controlsPanel;

let legendEnabled = false;
let hiddenLegendFields = {};

function ensureDomainCache(domainId) {
    if (!framePairsCache[domainId]) {
        framePairsCache[domainId] = {};
    }
    return framePairsCache[domainId];
}

function normalizeFrameManifest(manifest) {
    const normalized = manifest || {};
    const domains = normalized.domains || {};
    const domainOrder = Array.isArray(normalized.domainOrder) && normalized.domainOrder.length > 0
        ? normalized.domainOrder
        : Object.keys(domains);

    const normalizedDomains = {};

    domainOrder.forEach(domainId => {
        normalizedDomains[domainId] = {
            ...domains[domainId],
            categories: domains[domainId]?.categories || {}
        };
    });

    return {
        ...normalized,
        domainOrder,
        domains: normalizedDomains,
        legends: normalized.legends || {}
    };
}

function getLegendStoragePayload() {
    return {
        hiddenFields: hiddenLegendFields,
    };
}

function loadLegendPreferences() {
    try {
        const raw = localStorage.getItem(LEGEND_PREFS_KEY);
        if (!raw) {
            return;
        }

        const parsed = JSON.parse(raw);
        hiddenLegendFields = parsed?.hiddenFields && typeof parsed.hiddenFields === 'object'
            ? parsed.hiddenFields
            : {};
    } catch (error) {
        console.warn('Unable to restore legend preferences:', error);
    }
}

function loadTimezonePreference() {
    try {
        const raw = localStorage.getItem(TIMEZONE_PREFS_KEY);
        if (!raw) {
            return;
        }

        const parsed = JSON.parse(raw);
        if (parsed && typeof parsed.timezone === 'string' && TIMEZONE_OPTIONS[parsed.timezone]) {
            selectedTimezoneKey = parsed.timezone;
        }
    } catch (error) {
        console.warn('Unable to restore timezone preference:', error);
    }
}

function saveTimezonePreference() {
    try {
        localStorage.setItem(TIMEZONE_PREFS_KEY, JSON.stringify({ timezone: selectedTimezoneKey }));
    } catch (error) {
        console.warn('Unable to save timezone preference:', error);
    }
}

function saveLegendPreferences() {
    try {
        localStorage.setItem(LEGEND_PREFS_KEY, JSON.stringify(getLegendStoragePayload()));
    } catch (error) {
        console.warn('Unable to save legend preferences:', error);
    }
}

function getLegendConfigForCategory(category) {
    const manifestLegend = frameManifest?.legends?.[category];
    if (manifestLegend?.left || manifestLegend?.right) {
        return manifestLegend;
    }
    return FALLBACK_LEGENDS[category] || null;
}

function getFieldHiddenState(category, panelKey, fieldId) {
    return Boolean(hiddenLegendFields?.[category]?.[panelKey]?.includes(fieldId));
}

function setFieldHiddenState(category, panelKey, fieldId, isHidden) {
    if (!hiddenLegendFields[category]) {
        hiddenLegendFields[category] = {};
    }
    if (!hiddenLegendFields[category][panelKey]) {
        hiddenLegendFields[category][panelKey] = [];
    }

    const bucket = hiddenLegendFields[category][panelKey];
    const index = bucket.indexOf(fieldId);
    if (isHidden && index === -1) {
        bucket.push(fieldId);
    }
    if (!isHidden && index !== -1) {
        bucket.splice(index, 1);
    }
}

function getFieldAccentColor(field) {
    const items = Array.isArray(field?.items) ? field.items : [];
    const picked = items.find(item => item?.color && !String(item.color).includes('rgba(255,255,255,0)'));
    if (picked) return picked?.color || '#7d9ecf';
    const barbItem = items.find(item => item?.pattern === 'barb');
    if (barbItem) return barbItem?.color || '#2b0000';
    return '#7d9ecf';
}

function createBarbSwatch(color) {
    const c = color || '#2b0000';
    const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    svg.setAttribute('width', '20');
    svg.setAttribute('height', '12');
    svg.setAttribute('viewBox', '0 0 20 12');
    svg.setAttribute('aria-hidden', 'true');
    svg.style.display = 'inline-block';
    svg.style.verticalAlign = 'middle';
    svg.style.flexShrink = '0';

    const makeLine = (x1, y1, x2, y2) => {
        const line = document.createElementNS('http://www.w3.org/2000/svg', 'line');
        line.setAttribute('x1', x1); line.setAttribute('y1', y1);
        line.setAttribute('x2', x2); line.setAttribute('y2', y2);
        line.setAttribute('stroke', c);
        line.setAttribute('stroke-width', '1.6');
        line.setAttribute('stroke-linecap', 'round');
        return line;
    };

    // Staff
    svg.appendChild(makeLine(2, 6, 18, 6));
    // Two barbs at the left end, slanted back (away from rightward motion)
    svg.appendChild(makeLine(2, 6, 0.5, 10));
    svg.appendChild(makeLine(5.5, 6, 4, 10));

    return svg;
}

function createCrosshatchSwatch(color) {
    const c = color || '#4a5a74';
    const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    svg.setAttribute('width', '12');
    svg.setAttribute('height', '12');
    svg.setAttribute('viewBox', '0 0 12 12');
    svg.setAttribute('aria-hidden', 'true');
    svg.style.display = 'inline-block';
    svg.style.verticalAlign = 'middle';
    svg.style.flexShrink = '0';

    const rect = document.createElementNS('http://www.w3.org/2000/svg', 'rect');
    rect.setAttribute('x', '0.5');
    rect.setAttribute('y', '0.5');
    rect.setAttribute('width', '11');
    rect.setAttribute('height', '11');
    rect.setAttribute('fill', '#ffffff');
    rect.setAttribute('stroke', 'rgba(0, 0, 0, 0.24)');
    rect.setAttribute('stroke-width', '1');
    svg.appendChild(rect);

    const makeLine = (x1, y1, x2, y2) => {
        const line = document.createElementNS('http://www.w3.org/2000/svg', 'line');
        line.setAttribute('x1', x1);
        line.setAttribute('y1', y1);
        line.setAttribute('x2', x2);
        line.setAttribute('y2', y2);
        line.setAttribute('stroke', c);
        line.setAttribute('stroke-width', '1');
        line.setAttribute('stroke-linecap', 'round');
        return line;
    };

    // Two diagonal sets to represent crosshatching.
    svg.appendChild(makeLine(0, 9, 3, 12));
    svg.appendChild(makeLine(0, 5, 7, 12));
    svg.appendChild(makeLine(0, 1, 11, 12));
    svg.appendChild(makeLine(3, 0, 12, 9));
    svg.appendChild(makeLine(7, 0, 12, 5));
    svg.appendChild(makeLine(11, 0, 12, 1));

    svg.appendChild(makeLine(0, 3, 3, 0));
    svg.appendChild(makeLine(0, 7, 7, 0));
    svg.appendChild(makeLine(0, 11, 11, 0));
    svg.appendChild(makeLine(5, 12, 12, 5));
    svg.appendChild(makeLine(9, 12, 12, 9));

    return svg;
}

function formatLegendLabel(label) {
    const raw = (label || '').toString().trim();
    if (!raw) return raw;

    const overrides = {
        '1h precipitation': '1 hr precipitation',
        'low cloud': 'Low cloud',
        '1h thunderstorm precipitation': '1 hr thunderstorm precipitation',
        'upper isotachs (250 hpa)': 'Upper isotachs',
        '0-6km bulk shear': '0-6km bulk shear',
        'icing rh': 'Icing severity',
        'thermals (pblh)': 'Thermals',
        'cold pool hail precip': '1 hr small hail',
        'freezing fog': 'Freezing fog',
        '1h snow precipitation': '1 hr snow',
        'mountain wave intensity': 'Mountain wave intensity',
        'max wind below 850 hpa': 'Max wind below 5000ft',
        'shear and lee turbulence': 'Shear and lee turbulence',
    };

    return overrides[raw.toLowerCase()] || raw;
}

function normalizeLegendFieldForCompare(field) {
    return {
        label: field?.label || '',
        units: field?.units || '',
        note: field?.note || '',
        items: (field?.items || []).map(item => ({
            color: item?.color || '',
            label: item?.label || '',
            pattern: item?.pattern || '',
        })),
    };
}

function areLegendPanelsEquivalent(panelA, panelB) {
    const fieldsA = (panelA?.fields || []).map(normalizeLegendFieldForCompare);
    const fieldsB = (panelB?.fields || []).map(normalizeLegendFieldForCompare);
    return JSON.stringify(fieldsA) === JSON.stringify(fieldsB);
}

function applyLegendLayoutState() {
    document.body.classList.toggle('legend-enabled', legendEnabled);

    if (legendStrip) {
        legendStrip.classList.toggle('is-hidden', !legendEnabled);
    }
    if (legendToggleBtn) {
        legendToggleBtn.textContent = legendEnabled ? 'Hide legend' : 'Show legend';
        legendToggleBtn.setAttribute('aria-label', legendEnabled ? 'Hide legend' : 'Show legend');
    }

    if (legendStrip) {
        legendStrip.setAttribute('aria-label', legendEnabled ? 'Legend panel' : 'Legend hidden. Use Show legend to restore.');
    }

    requestAnimationFrame(updateViewerBottomOffset);
    setTimeout(updateViewerBottomOffset, 220);
}

function updateViewerBottomOffset() {
    if (!controlsPanel) {
        return;
    }

    const controlsHeight = Math.ceil(controlsPanel.getBoundingClientRect().height);
    if (controlsHeight > 0) {
        document.body.style.setProperty('--viewer-bottom-offset', `${controlsHeight}px`);
    }
}

function renderLegend() {
    if (!legendContent) {
        return;
    }

    legendContent.innerHTML = '';
    if (!legendEnabled) {
        applyLegendLayoutState();
        return;
    }

    const legendConfig = getLegendConfigForCategory(currentCategory);
    if (!legendConfig) {
        applyLegendLayoutState();
        return;
    }

    const panelKeys = ['left', 'right'];
    const panelKeysToRender = [];

    panelKeys.forEach(panelKey => {
        const panelConfig = legendConfig[panelKey];
        if (!panelConfig) {
            return;
        }

        const existingKey = panelKeysToRender.find(key => {
            const existingPanel = legendConfig[key];
            return areLegendPanelsEquivalent(existingPanel, panelConfig);
        });

        if (!existingKey) {
            panelKeysToRender.push(panelKey);
        }
    });

    panelKeysToRender.forEach(panelKey => {
        const panelConfig = legendConfig[panelKey];
        if (!panelConfig) {
            return;
        }

        const panelNode = document.createElement('section');
        panelNode.className = 'legend-panel';
        panelNode.setAttribute('aria-label', panelConfig.title || panelKey.toUpperCase());

        const togglesNode = document.createElement('div');
        togglesNode.className = 'legend-field-toggles';

        const fieldsToRender = (panelConfig.fields || []).filter(field => !EXCLUDED_LEGEND_FIELD_IDS.has(field?.id));

        fieldsToRender.forEach((field, index) => {
            const fieldId = field.id || `${panelKey}-${index}`;
            const hidden = getFieldHiddenState(currentCategory, panelKey, fieldId);

            const toggleBtn = document.createElement('button');
            toggleBtn.type = 'button';
            toggleBtn.className = `legend-field-toggle${hidden ? ' is-disabled' : ''}`;
            toggleBtn.setAttribute('aria-pressed', hidden ? 'false' : 'true');

            const barbItem = (field.items || []).find(item => item?.pattern === 'barb');
            const crosshatchItem = (field.items || []).find(item => item?.pattern === 'crosshatch');
            if (barbItem) {
                const barbSvg = createBarbSwatch(barbItem.color);
                toggleBtn.appendChild(barbSvg);
            } else if (crosshatchItem) {
                const hatchSwatch = createCrosshatchSwatch(crosshatchItem.color);
                toggleBtn.appendChild(hatchSwatch);
            } else {
                const dot = document.createElement('span');
                dot.className = 'legend-field-toggle-dot';
                dot.style.setProperty('--legend-dot-color', getFieldAccentColor(field));
                toggleBtn.appendChild(dot);
            }

            const toggleLabel = document.createElement('span');
            toggleLabel.className = 'legend-field-toggle-label';
            toggleLabel.textContent = formatLegendLabel(field.label || fieldId);
            toggleBtn.appendChild(toggleLabel);

            toggleBtn.addEventListener('click', () => {
                setFieldHiddenState(currentCategory, panelKey, fieldId, !hidden);
                saveLegendPreferences();
                renderLegend();
            });

            togglesNode.appendChild(toggleBtn);
        });

        panelNode.appendChild(togglesNode);
        legendContent.appendChild(panelNode);
    });

    legendContent.classList.toggle('single-panel', panelKeysToRender.length === 1);

    applyLegendLayoutState();
}

async function loadFrameManifest(forceRefresh = false) {
    if (forceRefresh || frameManifest === null) {
        const manifestUrl = forceRefresh
            ? `images/manifest.json?t=${Date.now()}`
            : 'images/manifest.json';

        const response = await fetch(manifestUrl, { cache: 'no-store' });
        if (!response.ok) {
            throw new Error(`Unable to read frame manifest: ${response.status}`);
        }

        frameManifest = normalizeFrameManifest(await response.json());
    }

    return frameManifest;
}

async function loadCategoryFrames(domainId, category, forceRefresh = false) {
    const domainCache = ensureDomainCache(domainId);
    if (!forceRefresh && Array.isArray(domainCache[category])) {
        return domainCache[category];
    }

    const manifest = await loadFrameManifest(forceRefresh);
    const framePairs = manifest?.domains?.[domainId]?.categories?.[category];
    domainCache[category] = Array.isArray(framePairs) ? framePairs : [];
    return domainCache[category];
}

function getCurrentFramePairs() {
    return framePairsCache[currentDomain]?.[currentCategory] || [];
}

function setMaxFramesForCurrentSelection() {
    const framePairs = getCurrentFramePairs();
    maxFrames = framePairs.length > 0 ? framePairs.length : DEFAULT_MAX_FRAMES;
    frameSlider.max = maxFrames;
    selectedRangeStart = Math.max(1, Math.min(selectedRangeStart, maxFrames));
    selectedRangeEnd = Math.max(selectedRangeStart, Math.min(selectedRangeEnd, maxFrames));
    frameRangeStartSlider.min = 1;
    frameRangeStartSlider.max = maxFrames;
    frameRangeStartSlider.value = selectedRangeStart;
    frameRangeEndSlider.min = 1;
    frameRangeEndSlider.max = maxFrames;
    frameRangeEndSlider.value = selectedRangeEnd;
    currentFrame = clampFrameToSelectedRange(currentFrame);
    frameSlider.value = currentFrame;
    updateSelectedRangeHighlight();
    updateRangeTimeDisplay();
}

function clampFrameToSelectedRange(frame) {
    return Math.max(selectedRangeStart, Math.min(frame, selectedRangeEnd));
}

function getSelectedRangeSize() {
    return (selectedRangeEnd - selectedRangeStart) + 1;
}

function updateSelectedRangeHighlight() {
    if (!frameSelectionHighlight || maxFrames <= 0) {
        return;
    }

    const startPercent = ((selectedRangeStart - 1) / maxFrames) * 100;
    const widthPercent = (getSelectedRangeSize() / maxFrames) * 100;
    frameSelectionHighlight.style.left = `${startPercent}%`;
    frameSelectionHighlight.style.width = `${widthPercent}%`;
}

function parseValidDateFromFramePath(path) {
    if (typeof path !== 'string' || path.length === 0) {
        return null;
    }

    const match = path.match(/_(\d{8})_(\d{2})_(\d{2})\.[^.]+$/);
    if (!match) {
        return null;
    }

    const [, ymd, initHourText] = match;
    const year = Number(ymd.slice(0, 4));
    const monthIndex = Number(ymd.slice(4, 6)) - 1;
    const day = Number(ymd.slice(6, 8));
    const initHour = Number(initHourText);

    if (![year, monthIndex, day, initHour].every(Number.isFinite)) {
        return null;
    }

    return new Date(Date.UTC(year, monthIndex, day, initHour, 0, 0));
}

function getValidDateForFramePair(framePair) {
    if (!framePair) {
        return null;
    }

    const baseDate = parseValidDateFromFramePath(framePair.leftPath) || parseValidDateFromFramePath(framePair.rightPath);
    const leadHour = Number(framePair.hour);

    if (!baseDate || !Number.isFinite(leadHour)) {
        return null;
    }

    return new Date(baseDate.getTime() + (leadHour * 60 * 60 * 1000));
}

function formatDateForRangeDisplay(dateValue) {
    if (!(dateValue instanceof Date) || Number.isNaN(dateValue.getTime())) {
        return '--';
    }

    const timezone = TIMEZONE_OPTIONS[selectedTimezoneKey] || TIMEZONE_OPTIONS[DEFAULT_TIMEZONE_KEY];
    const shiftedDate = new Date(dateValue.getTime() + (timezone.offsetMinutes * 60 * 1000));
    const day = String(shiftedDate.getUTCDate()).padStart(2, '0');
    const month = String(shiftedDate.getUTCMonth() + 1).padStart(2, '0');
    const year = shiftedDate.getUTCFullYear();
    const shortYear = String(year).slice(-2);
    const hour = String(shiftedDate.getUTCHours()).padStart(2, '0');
    const minute = String(shiftedDate.getUTCMinutes()).padStart(2, '0');

    const isMobile = typeof window !== 'undefined' && window.matchMedia && window.matchMedia('(max-width: 768px)').matches;
    if (isMobile) {
        return `${day}/${month}/${shortYear} ${hour} ${timezone.abbr}`;
    }

    const dayNames = ['Sunday', 'Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday'];
    const dayName = dayNames[shiftedDate.getUTCDay()];
    return `${dayName} ${day}/${month}/${year}, ${hour}:${minute} ${timezone.abbr}`;
}

function updateRangeTimeDisplay() {
    if (!rangeStartTimeLabel || !rangeEndTimeLabel) {
        return;
    }

    const framePairs = getCurrentFramePairs();
    if (framePairs.length === 0) {
        rangeStartTimeLabel.textContent = '--';
        rangeEndTimeLabel.textContent = '--';
        return;
    }

    const startFramePair = framePairs[selectedRangeStart - 1];
    const endFramePair = framePairs[selectedRangeEnd - 1];
    rangeStartTimeLabel.textContent = formatDateForRangeDisplay(getValidDateForFramePair(startFramePair));
    rangeEndTimeLabel.textContent = formatDateForRangeDisplay(getValidDateForFramePair(endFramePair));
}

function updateCurrentTimestepDisplay(framePair = null) {
    if (!currentFrameTimeLabel) {
        return;
    }

    currentFrameTimeLabel.textContent = formatDateForRangeDisplay(getValidDateForFramePair(framePair));
}

function updateRangeStartValue(value) {
    selectedRangeStart = Math.max(1, Math.min(value, selectedRangeEnd));
    frameRangeStartSlider.value = selectedRangeStart;
    currentFrame = clampFrameToSelectedRange(currentFrame);
    frameSlider.value = currentFrame;
    updateSelectedRangeHighlight();
    updateRangeTimeDisplay();
    updateImages();
}

function updateRangeEndValue(value) {
    selectedRangeEnd = Math.min(maxFrames, Math.max(value, selectedRangeStart));
    frameRangeEndSlider.value = selectedRangeEnd;
    currentFrame = clampFrameToSelectedRange(currentFrame);
    frameSlider.value = currentFrame;
    updateSelectedRangeHighlight();
    updateRangeTimeDisplay();
    updateImages();
}

function initializeElements() {
    frameSlider = document.getElementById('frame-slider');
    frameRangeStartSlider = document.getElementById('frame-range-start');
    frameRangeEndSlider = document.getElementById('frame-range-end');
    frameSelectionHighlight = document.getElementById('frame-selection-highlight');
    rangeStartTimeLabel = document.getElementById('range-start-time');
    rangeEndTimeLabel = document.getElementById('range-end-time');
    currentFrameTimeLabel = document.getElementById('current-frame-time');
    timezoneSelect = document.getElementById('timezone-select');
    leftImage = document.getElementById('left-image');
    rightImage = document.getElementById('right-image');
    playBtn = document.getElementById('play-btn');
    speedSlider = document.getElementById('speed-slider');
    speedDisplay = document.getElementById('speed-display');
    categoryButtons = document.querySelectorAll('.category-btn');
    mobileCategorySelect = document.getElementById('mobile-category-select'); // unused in AvMaps Extra
    mobileDomainSelect = document.getElementById('mobile-domain-select');
    loadingOverlay = document.getElementById('loading-overlay');
    loadingBarFill = document.getElementById('loading-bar-fill');
    loadingStatus = document.getElementById('loading-status');
    infoBtn = document.getElementById('info-btn');
    infoOverlay = document.getElementById('info-overlay');
    infoCloseBtn = document.getElementById('info-close-btn');
    legendStrip = document.getElementById('legend-strip');
    legendContent = document.getElementById('legend-content');
    legendToggleBtn = document.getElementById('legend-toggle-btn');
    controlsPanel = document.querySelector('.controls');
}

function isInfoModalOpen() {
    return infoOverlay && !infoOverlay.classList.contains('hidden');
}

function openInfoModal() {
    stopAnimation();
    infoOverlay.classList.remove('hidden');
    infoOverlay.setAttribute('aria-hidden', 'false');
}

function closeInfoModal() {
    infoOverlay.classList.add('hidden');
    infoOverlay.setAttribute('aria-hidden', 'true');
}

function setLoadingProgress(completedCount, totalCount) {
    if (!loadingBarFill || !loadingStatus) {
        return;
    }

    if (totalCount === 0) {
        loadingBarFill.style.width = '0%';
        loadingStatus.textContent = 'Preparing frames...';
        return;
    }

    const percent = totalCount === 0 ? 100 : Math.round((completedCount / totalCount) * 100);
    loadingBarFill.style.width = `${percent}%`;
    loadingStatus.textContent = `Loading frames... ${completedCount} of ${totalCount}`;
}

function hideLoadingOverlay() {
    document.body.classList.remove('loading');
    if (loadingOverlay) {
        loadingOverlay.classList.add('hidden');
    }
}

function preloadImageSource(src) {
    return new Promise(resolve => {
        const img = new Image();
        img.onload = resolve;
        img.onerror = resolve;
        img.src = src;
    });
}

function decodeImageSource(src) {
    if (!src) {
        return Promise.resolve();
    }

    if (decodedImageObjects.has(src)) {
        return Promise.resolve();
    }

    if (pendingDecodePromises.has(src)) {
        return pendingDecodePromises.get(src);
    }

    const decodeImage = new Image();
    decodeImage.src = src;

    const decodePromise = (typeof decodeImage.decode === 'function'
        ? decodeImage.decode().catch(() => preloadImageSource(src))
        : preloadImageSource(src)
    ).then(() => {
        decodedImageObjects.set(src, decodeImage);
        pendingDecodePromises.delete(src);
    }).catch(() => {
        pendingDecodePromises.delete(src);
    });

    pendingDecodePromises.set(src, decodePromise);
    return decodePromise;
}

function getWrappedFrameIndex(index, frameCount) {
    if (frameCount <= 0) {
        return 1;
    }

    let wrapped = index;
    while (wrapped < 1) {
        wrapped += frameCount;
    }
    while (wrapped > frameCount) {
        wrapped -= frameCount;
    }
    return wrapped;
}

function collectDecodeWindowSourcesForAllStreams(centerFrame) {
    const targetSources = new Set();
    const domainIds = frameManifest?.domainOrder || Object.keys(framePairsCache);

    domainIds.forEach(domainId => {
        Object.keys(imageConfig).forEach(category => {
            const framePairs = framePairsCache[domainId]?.[category] || [];
            const frameCount = framePairs.length;
            if (frameCount === 0) {
                return;
            }

            for (let offset = -DECODE_WINDOW_RADIUS; offset <= DECODE_WINDOW_RADIUS; offset += 1) {
                const frameIndex = getWrappedFrameIndex(centerFrame + offset, frameCount);
                const framePair = framePairs[frameIndex - 1];
                if (!framePair) {
                    continue;
                }

                targetSources.add(framePair.leftPath);
                targetSources.add(framePair.rightPath);
            }
        });
    });

    return targetSources;
}

function updateDecodeWindowsForAllStreams(centerFrame) {
    const targetSources = collectDecodeWindowSourcesForAllStreams(centerFrame);

    targetSources.forEach(src => {
        decodeImageSource(src);
    });

    Array.from(decodedImageObjects.keys()).forEach(src => {
        if (!targetSources.has(src)) {
            decodedImageObjects.delete(src);
        }
    });
}

async function preloadAllFrames() {
    const domainIds = Array.from(new Set(Object.values(domainHotkeys)));
    const sources = [];

    for (const domainId of domainIds) {
        for (const category of Object.keys(imageConfig)) {
            const framePairs = await loadCategoryFrames(domainId, category);
            framePairs.forEach(framePair => {
                sources.push(framePair.leftPath, framePair.rightPath);
            });
        }
    }

    const uniqueSources = [...new Set(sources)];
    let completedCount = 0;
    setLoadingProgress(completedCount, uniqueSources.length);

    await Promise.all(uniqueSources.map(async src => {
        await preloadImageSource(src);
        completedCount += 1;
        setLoadingProgress(completedCount, uniqueSources.length);
    }));

    updateDecodeWindowsForAllStreams(currentFrame);
}

function updateImages() {
    const config = imageConfig[currentCategory];
    if (!config) {
        return;
    }

    const framePairs = getCurrentFramePairs();
    if (framePairs.length === 0) {
        console.warn(`No ${currentCategory} frames available for domain ${currentDomain}.`);
        updateCurrentTimestepDisplay();
        return;
    }

    const framePair = framePairs[currentFrame - 1];
    if (!framePair) {
        console.warn(`No ${currentCategory} frame available for domain ${currentDomain} at index ${currentFrame}.`);
        updateCurrentTimestepDisplay();
        return;
    }

    leftImage.classList.remove('error');
    rightImage.classList.remove('error');

    leftImage.src = framePair.leftPath;
    rightImage.src = framePair.rightPath;
    leftImage.alt = `${config.left.title} - Hour ${framePair.hour}`;
    rightImage.alt = `${config.right.title} - Hour ${framePair.hour}`;
    updateCurrentTimestepDisplay(framePair);

    updateDecodeWindowsForAllStreams(currentFrame);
}

function startAnimation() {
    if (animationInterval) {
        return;
    }

    currentFrame = clampFrameToSelectedRange(currentFrame);
    frameSlider.value = currentFrame;

    isPlaying = true;
    playBtn.textContent = '⏸ Pause';

    animationInterval = setInterval(() => {
        currentFrame += 1;
        if (currentFrame > selectedRangeEnd) {
            currentFrame = selectedRangeStart;
        }

        frameSlider.value = currentFrame;
        updateImages();
    }, animationSpeed);
}

function stopAnimation() {
    if (!isPlaying) {
        return;
    }

    isPlaying = false;
    playBtn.textContent = '▶ Play';

    if (animationInterval) {
        clearInterval(animationInterval);
        animationInterval = null;
    }
}

async function loadSelection(forceRefresh = false) {
    await loadCategoryFrames(currentDomain, currentCategory, forceRefresh);
    setMaxFramesForCurrentSelection();
    updateImages();
    renderLegend();
}

async function switchCategory(category) {
    if (category === currentCategory) {
        return;
    }

    const wasPlaying = isPlaying;
    if (wasPlaying && animationInterval) {
        clearInterval(animationInterval);
        animationInterval = null;
    }

    categoryButtons.forEach(btn => {
        btn.classList.toggle('active', btn.getAttribute('data-category') === category);
    });

    currentCategory = category;
    if (mobileCategorySelect) mobileCategorySelect.value = category;
    await loadSelection(true);

    if (wasPlaying) {
        startAnimation();
    }
}

async function switchDomain(domainId) {
    if (domainId === currentDomain) {
        return;
    }

    const wasPlaying = isPlaying;
    if (wasPlaying && animationInterval) {
        clearInterval(animationInterval);
        animationInterval = null;
    }

    currentDomain = domainId;
    if (mobileDomainSelect) mobileDomainSelect.value = domainId;
    await loadSelection(true);

    if (wasPlaying) {
        startAnimation();
    }
}

async function preloadImages(domainId, category) {
    const framePairs = await loadCategoryFrames(domainId, category);
    framePairs.forEach(framePair => {
        const leftImg = new Image();
        leftImg.src = framePair.leftPath;

        const rightImg = new Image();
        rightImg.src = framePair.rightPath;
    });
}

function setupEventListeners() {
    frameSlider.addEventListener('input', function() {
        currentFrame = clampFrameToSelectedRange(parseInt(this.value, 10));
        this.value = currentFrame;
        updateImages();
    });

    frameRangeStartSlider.addEventListener('input', function() {
        updateRangeStartValue(parseInt(this.value, 10));
    });

    frameRangeEndSlider.addEventListener('input', function() {
        updateRangeEndValue(parseInt(this.value, 10));
    });

    if (timezoneSelect) {
        timezoneSelect.addEventListener('change', function() {
            if (TIMEZONE_OPTIONS[this.value]) {
                selectedTimezoneKey = this.value;
                saveTimezonePreference();
                updateRangeTimeDisplay();
                updateCurrentTimestepDisplay(getCurrentFramePairs()[currentFrame - 1]);
            }
        });
    }

    playBtn.addEventListener('click', function() {
        if (isPlaying) {
            stopAnimation();
        } else {
            startAnimation();
        }
    });

    speedSlider.addEventListener('input', function() {
        animationSpeed = parseInt(this.value, 10);
        speedDisplay.textContent = `${animationSpeed}ms`;

        if (isPlaying) {
            stopAnimation();
            startAnimation();
        }
    });

    categoryButtons.forEach(btn => {
        btn.addEventListener('click', function() {
            switchCategory(this.getAttribute('data-category'));
        });
    });

    if (mobileCategorySelect) {
        mobileCategorySelect.addEventListener('change', function() {
            switchCategory(this.value);
        });
    }

    if (mobileDomainSelect) {
        mobileDomainSelect.addEventListener('change', function() {
            switchDomain(this.value);
        });
    }

    infoBtn.addEventListener('click', function() {
        openInfoModal();
    });

    infoCloseBtn.addEventListener('click', function() {
        closeInfoModal();
    });

    infoOverlay.addEventListener('click', function(event) {
        if (event.target === infoOverlay) {
            closeInfoModal();
        }
    });

    if (legendToggleBtn) {
        legendToggleBtn.addEventListener('click', function() {
            legendEnabled = !legendEnabled;
            saveLegendPreferences();
            renderLegend();
        });
    }

    leftImage.addEventListener('load', function() {
        this.classList.add('loaded');
    });

    rightImage.addEventListener('load', function() {
        this.classList.add('loaded');
    });

    leftImage.addEventListener('error', function() {
        this.classList.add('error');
        console.warn(`Failed to load left image: ${this.src}`);
    });

    rightImage.addEventListener('error', function() {
        this.classList.add('error');
        console.warn(`Failed to load right image: ${this.src}`);
    });

    document.addEventListener('keydown', function(event) {
        if (isInfoModalOpen()) {
            if (event.key === 'Escape') {
                closeInfoModal();
            }
            if (event.key === 'Escape' || event.key === ' ' || event.key === 'ArrowLeft' || event.key === 'ArrowRight' || event.key in domainHotkeys) {
                event.preventDefault();
            }
            return;
        }

        if (event.key in domainHotkeys) {
            switchDomain(domainHotkeys[event.key]);
            event.preventDefault();
            return;
        }

        const lowerKey = event.key.toLowerCase();
        switch (event.key) {
            case 'ArrowLeft':
                if (currentFrame > selectedRangeStart) {
                    currentFrame -= 1;
                    frameSlider.value = currentFrame;
                    updateImages();
                }
                event.preventDefault();
                break;
            case 'ArrowRight':
                if (currentFrame < selectedRangeEnd) {
                    currentFrame += 1;
                    frameSlider.value = currentFrame;
                    updateImages();
                }
                event.preventDefault();
                break;
            case ' ':
                if (isPlaying) {
                    stopAnimation();
                } else {
                    startAnimation();
                }
                event.preventDefault();
                break;
        }
    });
}

document.addEventListener('DOMContentLoaded', function() {
    loadLegendPreferences();
    loadTimezonePreference();
    initializeElements();
    updateViewerBottomOffset();
    if (timezoneSelect) {
        timezoneSelect.value = selectedTimezoneKey;
    }
    applyLegendLayoutState();
    updateSelectedRangeHighlight();
    setupEventListeners();
    setLoadingProgress(0, 0);
    loadSelection()
        .then(() => {
            updateImages();
            renderLegend();
        })
        .catch(error => {
            console.warn('Failed to initialize category data:', error);
        });
});

window.addEventListener('load', function() {
    preloadAllFrames()
        .catch(error => {
            console.warn('Failed to preload one or more domain images:', error);
        })
        .finally(() => {
            hideLoadingOverlay();
            updateViewerBottomOffset();
        });
});

window.addEventListener('resize', function() {
    updateImages();
    updateRangeTimeDisplay();
    updateViewerBottomOffset();
});

window.AnimationController = {
    switchCategory,
    startAnimation,
    stopAnimation,
    setFrame(frame) {
        if (frame >= 1 && frame <= maxFrames) {
            currentFrame = clampFrameToSelectedRange(frame);
            frameSlider.value = currentFrame;
            updateImages();
        }
    },
    getCurrentFrame: () => currentFrame,
    getCurrentCategory: () => currentCategory,
    getCurrentDomain: () => currentDomain,
    isAnimationPlaying: () => isPlaying
};
