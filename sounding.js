(() => {
    const PRESSURE_LEVELS_HPA = [
        1000, 975, 950, 925, 900, 850, 800, 700, 600, 500, 400, 300, 250, 200, 150, 100,
    ];

    const profileCache = new Map();
    const MAX_CACHE_ENTRIES = 24;

    const PANEL_SIZE_STORAGE_KEY = 'avmapsExtra.soundingPanelSizePx';
    const MIN_PANEL_PX = 240;
    const MIN_MAPS_PX = 280;
    const DEFAULT_PANEL_PX = 420;

    let panelEl;
    let metaEl;
    let statusEl;
    let canvasEl;
    let hoverEl;
    let closeBtn;
    let toolBtn;
    let pinTargets = [];
    let resizeHandleEl;
    let workspaceEl;
    let pinLat = null;
    let pinLon = null;
    let toolEnabled = false;
    let panelOpen = false;
    let lastProfile = null;
    let lastPlot = null;
    let storedPanelSizePx = null;
    let resizeRaf = 0;
    let activePointerId = null;
    let hoverRaf = 0;

    function ensureUi() {
        if (panelEl) {
            return;
        }

        panelEl = document.getElementById('sounding-panel');
        metaEl = document.getElementById('sounding-meta');
        statusEl = document.getElementById('sounding-status');
        canvasEl = document.getElementById('sounding-canvas');
        hoverEl = document.getElementById('sounding-hover');
        closeBtn = document.getElementById('sounding-close-btn');
        toolBtn = document.getElementById('sounding-tool-btn');
        resizeHandleEl = document.getElementById('sounding-resize-handle');
        workspaceEl = document.querySelector('.map-workspace');
        pinTargets = [
            {
                pinEl: document.getElementById('sounding-pin-left'),
                imgEl: document.getElementById('left-image'),
            },
            {
                pinEl: document.getElementById('sounding-pin-right'),
                imgEl: document.getElementById('right-image'),
            },
        ];

        if (closeBtn) {
            closeBtn.addEventListener('click', close);
        }
        if (toolBtn) {
            toolBtn.addEventListener('click', () => {
                setToolEnabled(!toolEnabled);
            });
        }
        bindResizeHandle();
        bindHoverInteractions();
        window.addEventListener('resize', () => {
            if (!panelOpen || activePointerId != null) return;
            // Re-clamp only; avoid re-dispatching resize (script.js already handles maps).
            applyPanelSize(storedPanelSizePx, { persist: false, notify: false });
            if (lastProfile) {
                renderSkewT(lastProfile);
            }
        });
    }

    function isVerticalLayout() {
        if (!workspaceEl) return false;
        return getComputedStyle(workspaceEl).flexDirection === 'column';
    }

    function readStoredPanelSize() {
        try {
            const raw = localStorage.getItem(PANEL_SIZE_STORAGE_KEY);
            const value = Number(raw);
            return Number.isFinite(value) && value > 0 ? value : null;
        } catch (_) {
            return null;
        }
    }

    function writeStoredPanelSize(sizePx) {
        try {
            localStorage.setItem(PANEL_SIZE_STORAGE_KEY, String(Math.round(sizePx)));
        } catch (_) {
            // Ignore quota / private-mode failures.
        }
    }

    function clampPanelSize(sizePx) {
        if (!workspaceEl) {
            return Math.max(MIN_PANEL_PX, sizePx);
        }
        const rect = workspaceEl.getBoundingClientRect();
        const vertical = isVerticalLayout();
        const available = vertical ? rect.height : rect.width;
        const handleSize = resizeHandleEl ? resizeHandleEl.getBoundingClientRect()[vertical ? 'height' : 'width'] : 8;
        const maxSize = Math.max(MIN_PANEL_PX, available - MIN_MAPS_PX - handleSize);
        return Math.min(Math.max(MIN_PANEL_PX, sizePx), maxSize);
    }

    function scheduleLayoutNotify() {
        if (resizeRaf) {
            cancelAnimationFrame(resizeRaf);
        }
        resizeRaf = requestAnimationFrame(() => {
            resizeRaf = 0;
            window.dispatchEvent(new Event('resize'));
            syncPin();
            if (lastProfile) {
                renderSkewT(lastProfile);
            }
        });
    }

    function applyPanelSize(sizePx, { persist = true, notify = true } = {}) {
        ensureUi();
        if (!panelEl || !workspaceEl) return;

        let nextSize = sizePx;
        if (!Number.isFinite(nextSize)) {
            nextSize = storedPanelSizePx;
        }
        if (!Number.isFinite(nextSize)) {
            const workspaceSize = isVerticalLayout()
                ? workspaceEl.getBoundingClientRect().height
                : workspaceEl.getBoundingClientRect().width;
            nextSize = Math.min(DEFAULT_PANEL_PX, workspaceSize * 0.38);
        }

        nextSize = clampPanelSize(nextSize);
        storedPanelSizePx = nextSize;
        document.documentElement.style.setProperty('--sounding-panel-size', `${Math.round(nextSize)}px`);

        if (persist) {
            writeStoredPanelSize(nextSize);
        }
        if (notify) {
            scheduleLayoutNotify();
        }
    }

    function clearPanelSizeStyle() {
        document.documentElement.style.removeProperty('--sounding-panel-size');
    }

    function setResizeHandleVisible(visible) {
        if (!resizeHandleEl) return;
        resizeHandleEl.classList.toggle('hidden', !visible);
        resizeHandleEl.setAttribute('aria-hidden', visible ? 'false' : 'true');
        resizeHandleEl.setAttribute('aria-orientation', isVerticalLayout() ? 'horizontal' : 'vertical');
    }

    function endResizeDrag() {
        if (activePointerId == null) return;
        activePointerId = null;
        document.body.classList.remove('sounding-resizing', 'sounding-resize-vertical');
        if (Number.isFinite(storedPanelSizePx)) {
            writeStoredPanelSize(storedPanelSizePx);
        }
        scheduleLayoutNotify();
    }

    function updateSizeFromPointer(clientX, clientY) {
        if (!workspaceEl) return;
        const rect = workspaceEl.getBoundingClientRect();
        const vertical = isVerticalLayout();
        const nextSize = vertical
            ? rect.bottom - clientY
            : rect.right - clientX;
        applyPanelSize(nextSize, { persist: false, notify: true });
    }

    function bindResizeHandle() {
        if (!resizeHandleEl || resizeHandleEl.dataset.bound === '1') return;
        resizeHandleEl.dataset.bound = '1';

        resizeHandleEl.addEventListener('pointerdown', (event) => {
            if (!panelOpen || event.button !== 0) return;
            event.preventDefault();
            activePointerId = event.pointerId;
            resizeHandleEl.setPointerCapture?.(event.pointerId);
            document.body.classList.add('sounding-resizing');
            document.body.classList.toggle('sounding-resize-vertical', isVerticalLayout());
            updateSizeFromPointer(event.clientX, event.clientY);
        });

        resizeHandleEl.addEventListener('pointermove', (event) => {
            if (activePointerId !== event.pointerId) return;
            updateSizeFromPointer(event.clientX, event.clientY);
        });

        const stop = (event) => {
            if (activePointerId !== event.pointerId) return;
            endResizeDrag();
        };
        resizeHandleEl.addEventListener('pointerup', stop);
        resizeHandleEl.addEventListener('pointercancel', stop);
        resizeHandleEl.addEventListener('lostpointercapture', endResizeDrag);

        resizeHandleEl.addEventListener('keydown', (event) => {
            if (!panelOpen) return;
            const step = event.shiftKey ? 40 : 16;
            if (event.key === 'ArrowLeft' || event.key === 'ArrowUp') {
                event.preventDefault();
                applyPanelSize((storedPanelSizePx || DEFAULT_PANEL_PX) + step);
            } else if (event.key === 'ArrowRight' || event.key === 'ArrowDown') {
                event.preventDefault();
                applyPanelSize((storedPanelSizePx || DEFAULT_PANEL_PX) - step);
            } else if (event.key === 'Home') {
                event.preventDefault();
                applyPanelSize(DEFAULT_PANEL_PX);
            }
        });
    }

    function syncToolUi() {
        document.body.classList.toggle('sounding-tool-active', toolEnabled);
        if (toolBtn) {
            toolBtn.classList.toggle('active', toolEnabled);
            toolBtn.setAttribute('aria-pressed', toolEnabled ? 'true' : 'false');
            toolBtn.title = toolEnabled ? 'Sounding tool on — click a map point' : 'Sounding tool';
        }
    }

    function setToolEnabled(enabled) {
        ensureUi();
        toolEnabled = Boolean(enabled);
        syncToolUi();
        if (!toolEnabled) {
            close();
        }
    }

    function isToolEnabled() {
        return toolEnabled;
    }

    function cacheKey(lat, lon, validTimeIso) {
        // v2: filter below-ground isobaric levels to avoid near-surface artefacts.
        return `v2:${lat.toFixed(3)},${lon.toFixed(3)},${validTimeIso}`;
    }

    function rememberProfile(key, profile) {
        profileCache.set(key, profile);
        if (profileCache.size > MAX_CACHE_ENTRIES) {
            const oldest = profileCache.keys().next().value;
            profileCache.delete(oldest);
        }
    }

    function buildHourlyVariableList() {
        const vars = ['temperature_2m', 'dew_point_2m', 'surface_pressure'];
        PRESSURE_LEVELS_HPA.forEach((level) => {
            vars.push(`temperature_${level}hPa`);
            vars.push(`dew_point_${level}hPa`);
            vars.push(`wind_speed_${level}hPa`);
            vars.push(`wind_direction_${level}hPa`);
            vars.push(`geopotential_height_${level}hPa`);
        });
        return vars.join(',');
    }

    function nearestHourIndex(times, targetDate) {
        const targetMs = targetDate.getTime();
        const targetKey = targetDate.toISOString().slice(0, 13); // YYYY-MM-DDTHH
        const exactIndex = times.findIndex((timeText) => String(timeText).slice(0, 13) === targetKey);
        if (exactIndex >= 0) {
            return exactIndex;
        }

        let bestIndex = 0;
        let bestDelta = Infinity;
        times.forEach((timeText, index) => {
            const delta = Math.abs(parseUtcHour(timeText).getTime() - targetMs);
            if (delta < bestDelta) {
                bestDelta = delta;
                bestIndex = index;
            }
        });
        return bestIndex;
    }

    function parseUtcHour(timeText) {
        const text = String(timeText || '');
        // Open-Meteo returns "YYYY-MM-DDTHH:MM" without Z; treat as UTC.
        if (/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}/.test(text) && !/[zZ]|[+-]\d{2}:?\d{2}$/.test(text)) {
            return new Date(`${text.length === 16 ? `${text}:00` : text}Z`);
        }
        return new Date(text);
    }

    async function fetchOpenMeteoProfile(lat, lon, validTime) {
        const startDate = validTime.toISOString().slice(0, 10);
        const params = new URLSearchParams({
            latitude: lat.toFixed(4),
            longitude: lon.toFixed(4),
            hourly: buildHourlyVariableList(),
            models: 'gfs_seamless',
            wind_speed_unit: 'kn',
            timezone: 'UTC',
            start_date: startDate,
            end_date: startDate,
        });

        const url = `https://api.open-meteo.com/v1/forecast?${params.toString()}`;
        const response = await fetch(url);
        if (!response.ok) {
            throw new Error(`Open-Meteo request failed (${response.status})`);
        }

        const payload = await response.json();
        const hourly = payload.hourly;
        if (!hourly?.time?.length) {
            throw new Error('No hourly profile data returned');
        }

        const hourIndex = nearestHourIndex(hourly.time, validTime);
        const levels = [];

        PRESSURE_LEVELS_HPA.forEach((pressure) => {
            const temperature = hourly[`temperature_${pressure}hPa`]?.[hourIndex];
            const dewpoint = hourly[`dew_point_${pressure}hPa`]?.[hourIndex];
            if (!Number.isFinite(temperature) || !Number.isFinite(dewpoint)) {
                return;
            }
            levels.push({
                pressure,
                temperature,
                dewpoint,
                height: hourly[`geopotential_height_${pressure}hPa`]?.[hourIndex] ?? null,
                windSpeedKt: hourly[`wind_speed_${pressure}hPa`]?.[hourIndex] ?? null,
                windDirDeg: hourly[`wind_direction_${pressure}hPa`]?.[hourIndex] ?? null,
            });
        });

        const surfacePressure = hourly.surface_pressure?.[hourIndex];
        const surfaceTemp = hourly.temperature_2m?.[hourIndex];
        const surfaceDew = hourly.dew_point_2m?.[hourIndex];
        if (Number.isFinite(surfacePressure) && Number.isFinite(surfaceTemp) && Number.isFinite(surfaceDew)) {
            // Drop isobaric points at/below ground — they cause near-surface zig-zags.
            const aboveGround = levels.filter((level) => level.pressure < surfacePressure - 0.5);
            aboveGround.push({
                pressure: surfacePressure,
                temperature: surfaceTemp,
                dewpoint: surfaceDew,
                height: 0,
                windSpeedKt: null,
                windDirDeg: null,
                isSurface: true,
            });
            levels.length = 0;
            levels.push(...aboveGround);
        }

        levels.sort((a, b) => b.pressure - a.pressure);

        return {
            source: 'Open-Meteo GFS',
            model: payload.model || 'gfs_seamless',
            validTime: parseUtcHour(hourly.time[hourIndex]),
            latitude: lat,
            longitude: lon,
            levels,
            surfacePressure: Number.isFinite(surfacePressure) ? surfacePressure : null,
        };
    }

    // Skew-T geometry: isotherms at ~45° (Windy-like), temperature axis auto-fit to profile.
    function skewX(temperatureC, pressureHpa, plot) {
        const logP = Math.log(Math.max(pressureHpa, plot.pMin));
        const skew = (plot.logPMax - logP) * plot.skewFactor;
        return plot.padL + ((temperatureC - plot.tMin) / (plot.tMax - plot.tMin)) * plot.innerW + skew;
    }

    function yFromPressure(pressureHpa, plot) {
        const logP = Math.log(Math.min(Math.max(pressureHpa, plot.pMin), plot.pMax));
        return plot.padT + ((logP - plot.logPMin) / (plot.logPMax - plot.logPMin)) * plot.innerH;
    }

    function pressureFromY(y, plot) {
        const fy = (y - plot.padT) / Math.max(plot.innerH, 1e-6);
        const logP = plot.logPMin + fy * (plot.logPMax - plot.logPMin);
        return Math.exp(logP);
    }

    function nearestProfileLevel(levels, pressure) {
        if (!Number.isFinite(pressure) || !levels?.length) return null;
        const logP = Math.log(pressure);
        let best = null;
        let bestDelta = Infinity;
        levels.forEach((level) => {
            if (!Number.isFinite(level.pressure) || level.pressure <= 0) return;
            const delta = Math.abs(Math.log(level.pressure) - logP);
            if (delta < bestDelta) {
                bestDelta = delta;
                best = level;
            }
        });
        return best;
    }

    function formatHoverReadout(sample) {
        const pressureText = Number.isFinite(sample.pressure) && sample.pressure > 0 && sample.pressure < 1100
            ? `${Math.round(sample.pressure)} hPa`
            : '—';

        let altitudeText = '—';
        if (Number.isFinite(sample.heightM)) {
            const altitudeFt = Math.round(sample.heightM * 3.28084);
            if (altitudeFt >= -500 && altitudeFt <= 80000) {
                altitudeText = `${altitudeFt.toLocaleString('en-US')} ft`;
            }
        }

        const windDirText = Number.isFinite(sample.windDirDeg) && sample.windDirDeg >= 0 && sample.windDirDeg <= 360
            ? `${Math.round(sample.windDirDeg)}°`
            : '—';
        const windSpeedText = Number.isFinite(sample.windSpeedKt) && sample.windSpeedKt >= 0 && sample.windSpeedKt <= 400
            ? `${Math.round(sample.windSpeedKt)} kt`
            : '—';
        const temperatureText = Number.isFinite(sample.temperature) && sample.temperature > -120 && sample.temperature < 60
            ? `${Math.round(sample.temperature)} °C`
            : '—';

        return (
            `<span class="sounding-hover-row">Pressure: ${pressureText}</span>`
            + `<span class="sounding-hover-row">Altitude: ${altitudeText}</span>`
            + `<span class="sounding-hover-row">Wind: ${windDirText}, ${windSpeedText}</span>`
            + `<span class="sounding-hover-row">Temperature: ${temperatureText}</span>`
        );
    }

    function hideHoverReadout() {
        if (!hoverEl) return;
        hoverEl.classList.add('hidden');
        hoverEl.innerHTML = '';
    }

    function updateHoverReadout(clientX, clientY) {
        if (!canvasEl || !hoverEl || !lastPlot || !lastProfile?.levels?.length) {
            hideHoverReadout();
            return;
        }

        const rect = canvasEl.getBoundingClientRect();
        const x = clientX - rect.left;
        const y = clientY - rect.top;
        if (
            x < lastPlot.padL
            || x > lastPlot.padL + lastPlot.innerW
            || y < lastPlot.padT
            || y > lastPlot.padT + lastPlot.innerH
        ) {
            hideHoverReadout();
            return;
        }

        const pressure = pressureFromY(y, lastPlot);
        const level = nearestProfileLevel(lastProfile.levels, pressure);
        if (!level) {
            hideHoverReadout();
            return;
        }

        hoverEl.innerHTML = formatHoverReadout({
            pressure: level.pressure,
            temperature: level.temperature,
            heightM: level.height,
            windDirDeg: level.windDirDeg,
            windSpeedKt: level.windSpeedKt,
        });
        hoverEl.classList.remove('hidden');

        const tipW = hoverEl.offsetWidth || 160;
        const tipH = hoverEl.offsetHeight || 72;
        let left = x + 12;
        let top = y + 12;
        if (left + tipW > rect.width - 4) left = x - tipW - 12;
        if (top + tipH > rect.height - 4) top = y - tipH - 12;
        left = Math.max(4, Math.min(left, rect.width - tipW - 4));
        top = Math.max(4, Math.min(top, rect.height - tipH - 4));
        hoverEl.style.left = `${left}px`;
        hoverEl.style.top = `${top}px`;
    }

    function bindHoverInteractions() {
        if (!canvasEl || canvasEl.dataset.hoverBound === '1') return;
        canvasEl.dataset.hoverBound = '1';

        canvasEl.addEventListener('pointermove', (event) => {
            if (!panelOpen || !lastProfile) return;
            const { clientX, clientY } = event;
            if (hoverRaf) cancelAnimationFrame(hoverRaf);
            hoverRaf = requestAnimationFrame(() => {
                hoverRaf = 0;
                updateHoverReadout(clientX, clientY);
            });
        });

        canvasEl.addEventListener('pointerleave', () => {
            if (hoverRaf) {
                cancelAnimationFrame(hoverRaf);
                hoverRaf = 0;
            }
            hideHoverReadout();
        });
    }

    function choosePressureRange(levels) {
        let pMax = 1050;
        const surface = levels.find((level) => level.isSurface);
        if (Number.isFinite(surface?.pressure)) {
            pMax = Math.min(1050, Math.max(1000, Math.ceil(surface.pressure / 10) * 10 + 10));
        }
        // Windy-style crop: keep mid/upper troposphere without a large empty top.
        return { pMin: 150, pMax };
    }

    function collectTemperatureSamples(levels, pMin, pMax) {
        // Fit the axis to temperature only (Windy-like). Dewpoint may clip on the left.
        const samples = [];
        levels.forEach((level) => {
            if (!Number.isFinite(level.pressure)) return;
            if (level.pressure < pMin || level.pressure > pMax) return;
            if (Number.isFinite(level.temperature)) {
                samples.push({ temperature: level.temperature, pressure: level.pressure });
            }
        });
        return samples;
    }

    function chooseTemperatureRange(samples, plot) {
        if (!samples.length) {
            return { tMin: -40, tMax: 30 };
        }

        const marginLeftPx = 8;
        const marginRightPx = 12;
        const rawMin = Math.min(...samples.map((s) => s.temperature));
        const rawMax = Math.max(...samples.map((s) => s.temperature));
        // Tight span: skew already shifts cold upper air right, so avoid a huge empty cold flank.
        let span = Math.max(40, Math.ceil((rawMax - rawMin + 6) / 5) * 5);

        const tryFit = (tempSpan) => {
            let tMinLow = -Infinity;
            let tMinHigh = Infinity;
            samples.forEach((sample) => {
                const skew = (plot.logPMax - Math.log(Math.max(sample.pressure, plot.pMin))) * plot.skewFactor;
                // x = padL + (T - tMin)/span * innerW + skew
                tMinHigh = Math.min(
                    tMinHigh,
                    sample.temperature - ((marginLeftPx - skew) / plot.innerW) * tempSpan,
                );
                tMinLow = Math.max(
                    tMinLow,
                    sample.temperature - ((plot.innerW - marginRightPx - skew) / plot.innerW) * tempSpan,
                );
            });
            if (!(tMinLow <= tMinHigh)) {
                return null;
            }

            // Prefer a higher tMin (less empty negative axis) while keeping surface in the right half.
            const surface = samples.reduce((best, sample) => (
                !best || sample.pressure > best.pressure ? sample : best
            ), null);
            const room = Math.max(0, tMinHigh - tMinLow);
            let tMin = tMinHigh - Math.min(4, room * 0.15);
            if (surface) {
                const rightish = surface.temperature - 0.72 * tempSpan;
                tMin = Math.min(tMinHigh, Math.max(tMinLow, rightish));
            }
            const snapped = Math.floor(tMin / 5) * 5;
            return { tMin: snapped, tMax: snapped + tempSpan };
        };

        for (let i = 0; i < 14; i += 1) {
            const fitted = tryFit(span);
            if (fitted) return fitted;
            span += 5;
        }

        const fallbackMin = Math.floor((rawMin - 2) / 5) * 5;
        return { tMin: fallbackMin, tMax: fallbackMin + span };
    }

    function buildPlotLayout(width, height, levels) {
        const padL = 32;
        const padR = 40;
        const padT = 8;
        const padB = 22;
        const pressure = choosePressureRange(levels);
        const plot = {
            width,
            height,
            padL,
            padR,
            padT,
            padB,
            pMin: pressure.pMin,
            pMax: pressure.pMax,
            innerW: Math.max(40, width - padL - padR),
            innerH: Math.max(40, height - padT - padB),
        };
        plot.logPMin = Math.log(plot.pMin);
        plot.logPMax = Math.log(plot.pMax);
        const logSpan = Math.max(plot.logPMax - plot.logPMin, 1e-6);
        // Strong diagonal isotherms: cross most of the plot width over the full height.
        const targetSkewPx = Math.min(plot.innerH * 0.92, plot.innerW * 0.92);
        plot.skewFactor = targetSkewPx / logSpan;

        const temps = chooseTemperatureRange(
            collectTemperatureSamples(levels, plot.pMin, plot.pMax),
            plot,
        );
        plot.tMin = temps.tMin;
        plot.tMax = temps.tMax;
        return plot;
    }

    function drawBackground(ctx, plot) {
        ctx.fillStyle = '#f4f7fb';
        ctx.fillRect(0, 0, plot.width, plot.height);

        ctx.save();
        ctx.beginPath();
        ctx.rect(plot.padL, plot.padT, plot.innerW, plot.innerH);
        ctx.clip();

        // Isotherms every 5°; keep 0°C and -20°C prominent, mute the rest.
        for (let t = plot.tMin - 60; t <= plot.tMax + 60; t += 5) {
            const emphasis = t === 0 || t === -20;
            ctx.strokeStyle = emphasis ? 'rgba(210, 130, 145, 0.75)' : 'rgba(200, 150, 160, 0.14)';
            ctx.lineWidth = emphasis ? 1.25 : 0.7;
            ctx.beginPath();
            let started = false;
            for (let p = plot.pMax; p >= plot.pMin; p -= 5) {
                const x = skewX(t, p, plot);
                const y = yFromPressure(p, plot);
                if (!started) {
                    ctx.moveTo(x, y);
                    started = true;
                } else {
                    ctx.lineTo(x, y);
                }
            }
            ctx.stroke();
        }

        const pressureTicks = [1000, 925, 850, 700, 500, 400, 300, 250, 200, 150];
        ctx.strokeStyle = 'rgba(120, 140, 165, 0.55)';
        ctx.lineWidth = 1;
        pressureTicks.forEach((p) => {
            if (p < plot.pMin || p > plot.pMax) return;
            const y = yFromPressure(p, plot);
            ctx.beginPath();
            ctx.moveTo(plot.padL, y);
            ctx.lineTo(plot.padL + plot.innerW, y);
            ctx.stroke();
        });

        ctx.restore();

        ctx.strokeStyle = '#8a97a8';
        ctx.lineWidth = 1.2;
        ctx.strokeRect(plot.padL, plot.padT, plot.innerW, plot.innerH);

        ctx.fillStyle = '#4a5a70';
        ctx.font = '11px sans-serif';
        ctx.textAlign = 'right';
        pressureTicks.forEach((p) => {
            if (p < plot.pMin || p > plot.pMax) return;
            const y = yFromPressure(p, plot);
            ctx.fillText(`${p}`, plot.padL - 6, y + 3);
        });

        ctx.textAlign = 'center';
        for (let t = Math.ceil(plot.tMin / 10) * 10; t <= plot.tMax; t += 10) {
            const x = skewX(t, plot.pMax, plot);
            if (x < plot.padL - 2 || x > plot.padL + plot.innerW + 2) continue;
            ctx.fillText(`${t}`, Math.min(Math.max(x, plot.padL), plot.padL + plot.innerW), plot.padT + plot.innerH + 14);
        }
    }

    function pointInPlot(x, y, plot, pad = 2) {
        return (
            x >= plot.padL - pad
            && x <= plot.padL + plot.innerW + pad
            && y >= plot.padT - pad
            && y <= plot.padT + plot.innerH + pad
        );
    }

    function drawTrace(ctx, levels, key, color, plot) {
        ctx.save();
        ctx.beginPath();
        ctx.rect(plot.padL, plot.padT, plot.innerW, plot.innerH);
        ctx.clip();

        ctx.strokeStyle = color;
        ctx.lineWidth = 2.6;
        ctx.lineJoin = 'round';
        ctx.lineCap = 'round';
        ctx.beginPath();
        let started = false;
        let prevInside = false;
        levels.forEach((level) => {
            const value = level[key];
            if (!Number.isFinite(value) || !Number.isFinite(level.pressure)) return;
            if (level.pressure < plot.pMin - 1 || level.pressure > plot.pMax + 2) return;
            const x = skewX(value, level.pressure, plot);
            const y = yFromPressure(level.pressure, plot);
            const inside = pointInPlot(x, y, plot, 24);
            if (!inside) {
                // Break the path so clipped exterior segments don't draw edge artefacts.
                started = false;
                prevInside = false;
                return;
            }
            if (!started || !prevInside) {
                ctx.moveTo(x, y);
                started = true;
            } else {
                ctx.lineTo(x, y);
            }
            prevInside = true;
        });
        if (started) ctx.stroke();
        ctx.restore();
    }

    function levelsForDisplay(levels, plot) {
        const surface = levels.find((level) => level.isSurface);
        const surfaceP = Number.isFinite(surface?.pressure) ? surface.pressure : null;
        return levels.filter((level) => {
            if (!Number.isFinite(level.pressure)) return false;
            if (level.pressure < plot.pMin - 1 || level.pressure > plot.pMax + 2) return false;
            if (surfaceP != null && !level.isSurface && level.pressure >= surfaceP - 0.5) return false;
            return true;
        });
    }

    function drawWindBarb(ctx, x, y, speedKt, dirDeg, southernHemisphere = true) {
        if (!Number.isFinite(speedKt) || !Number.isFinite(dirDeg) || speedKt < 2) return;
        // Meteorological dir = where wind is FROM. Staff points that way so feathers
        // sit on the upwind end (away from the direction of travel).
        const angle = (dirDeg * Math.PI) / 180;
        // NH: feathers on +90° side of staff; SH: opposite side.
        const side = southernHemisphere ? -Math.PI / 2 : Math.PI / 2;
        const staff = 16;
        const sideX = (len) => Math.sin(angle + side) * len;
        const sideY = (len) => -Math.cos(angle + side) * len;
        const alongX = (len) => Math.sin(angle) * len;
        const alongY = (len) => -Math.cos(angle) * len;

        ctx.strokeStyle = '#222';
        ctx.lineWidth = 1.15;
        ctx.beginPath();
        ctx.moveTo(x, y);
        ctx.lineTo(x + alongX(staff), y + alongY(staff));
        ctx.stroke();

        let remaining = Math.round(speedKt / 5) * 5;
        let along = staff;
        while (remaining >= 50) {
            const bx = x + alongX(along);
            const by = y + alongY(along);
            ctx.beginPath();
            ctx.moveTo(bx, by);
            ctx.lineTo(
                bx + sideX(7) - alongX(3.5),
                by + sideY(7) - alongY(3.5)
            );
            ctx.stroke();
            remaining -= 50;
            along -= 5;
        }
        while (remaining >= 10) {
            const bx = x + alongX(along);
            const by = y + alongY(along);
            ctx.beginPath();
            ctx.moveTo(bx, by);
            ctx.lineTo(bx + sideX(7), by + sideY(7));
            ctx.stroke();
            remaining -= 10;
            along -= 3.5;
        }
        if (remaining >= 5) {
            const bx = x + alongX(along);
            const by = y + alongY(along);
            ctx.beginPath();
            ctx.moveTo(bx, by);
            ctx.lineTo(bx + sideX(3.5), by + sideY(3.5));
            ctx.stroke();
        }
    }

    function renderSkewT(profile) {
        ensureUi();
        if (!canvasEl || !profile?.levels?.length) return;
        lastProfile = profile;
        hideHoverReadout();

        const width = canvasEl.clientWidth || 640;
        const height = canvasEl.clientHeight || 520;
        const dpr = window.devicePixelRatio || 1;
        canvasEl.width = Math.floor(width * dpr);
        canvasEl.height = Math.floor(height * dpr);

        const ctx = canvasEl.getContext('2d');
        ctx.setTransform(dpr, 0, 0, dpr, 0, 0);

        const plot = buildPlotLayout(width, height, profile.levels);
        lastPlot = plot;
        // Default SH for this AU-focused viewer; follow profile latitude when available.
        const southernHemisphere = !(Number.isFinite(profile.latitude) && profile.latitude > 0);

        const displayLevels = levelsForDisplay(profile.levels, plot);

        drawBackground(ctx, plot);
        drawTrace(ctx, displayLevels, 'temperature', '#d62828', plot);
        drawTrace(ctx, displayLevels, 'dewpoint', '#1d6fd8', plot);

        let lastBarbY = -Infinity;
        displayLevels.forEach((level) => {
            if (level.isSurface) return;
            if (!Number.isFinite(level.windSpeedKt) || !Number.isFinite(level.windDirDeg)) return;
            const y = yFromPressure(level.pressure, plot);
            // Keep near-surface barbs from stacking on top of each other.
            if (Math.abs(y - lastBarbY) < 14) return;
            drawWindBarb(
                ctx,
                plot.padL + plot.innerW + 14,
                y,
                level.windSpeedKt,
                level.windDirDeg,
                southernHemisphere,
            );
            lastBarbY = y;
        });
    }

    function placePinOnTarget(target, offsetX, offsetY) {
        if (!target?.pinEl || !target?.imgEl) return;
        const panel = target.imgEl.closest('.image-panel') || target.imgEl.parentElement;
        if (!panel) return;

        panel.style.position = panel.style.position || 'relative';
        if (target.pinEl.parentElement !== panel) {
            panel.appendChild(target.pinEl);
        }

        const rect = target.imgEl.getBoundingClientRect();
        const panelRect = panel.getBoundingClientRect();
        target.pinEl.style.left = `${rect.left - panelRect.left + offsetX}px`;
        target.pinEl.style.top = `${rect.top - panelRect.top + offsetY}px`;
        target.pinEl.classList.remove('hidden');
        target.pinEl.setAttribute('aria-hidden', 'false');
    }

    function hideAllPins() {
        pinTargets.forEach((target) => {
            if (!target?.pinEl) return;
            target.pinEl.classList.add('hidden');
            target.pinEl.setAttribute('aria-hidden', 'true');
        });
    }

    function syncPin() {
        ensureUi();
        if (!panelOpen || !Number.isFinite(pinLat) || !Number.isFinite(pinLon)) {
            hideAllPins();
            return;
        }

        pinTargets.forEach((target) => {
            if (!target?.pinEl || !target?.imgEl) return;
            const offsets = window.MapGeoref?.lonLatToImageOffsets?.(target.imgEl, pinLon, pinLat);
            if (!offsets) {
                target.pinEl.classList.add('hidden');
                target.pinEl.setAttribute('aria-hidden', 'true');
                return;
            }
            placePinOnTarget(target, offsets.offsetX, offsets.offsetY);
        });
    }

    function formatLatLon(lat, lon) {
        const latHem = lat >= 0 ? 'N' : 'S';
        const lonHem = lon >= 0 ? 'E' : 'W';
        return `${Math.abs(lat).toFixed(2)}°${latHem}, ${Math.abs(lon).toFixed(2)}°${lonHem}`;
    }

    async function openAt({ lat, lon, validTime, mapImg, offsetX, offsetY }) {
        ensureUi();
        if (!panelEl || !toolEnabled) return;

        pinLat = lat;
        pinLon = lon;

        panelEl.classList.remove('hidden');
        panelEl.setAttribute('aria-hidden', 'false');
        document.body.classList.add('sounding-panel-open');
        panelOpen = true;
        setResizeHandleVisible(true);
        if (storedPanelSizePx == null) {
            storedPanelSizePx = readStoredPanelSize();
        }
        applyPanelSize(storedPanelSizePx, { persist: false, notify: false });
        syncPin();

        metaEl.textContent = `${formatLatLon(lat, lon)} · fetching GFS profile…`;
        statusEl.textContent = 'Loading sounding…';
        statusEl.classList.remove('error');

        // Allow layout to settle before measuring canvas size / final pin position.
        requestAnimationFrame(() => {
            scheduleLayoutNotify();
        });

        const key = cacheKey(lat, lon, validTime.toISOString());
        try {
            let profile = profileCache.get(key);
            if (!profile) {
                profile = await fetchOpenMeteoProfile(lat, lon, validTime);
                rememberProfile(key, profile);
            }

            metaEl.textContent = (
                `${formatLatLon(lat, lon)} · Valid ${profile.validTime.toISOString().replace('.000Z', 'Z')} · `
                + `${profile.source}`
            );
            statusEl.textContent = '';
            renderSkewT(profile);
        } catch (error) {
            console.warn('Sounding fetch failed:', error);
            lastProfile = null;
            statusEl.textContent = error?.message || 'Unable to load sounding';
            statusEl.classList.add('error');
            const ctx = canvasEl?.getContext('2d');
            if (ctx && canvasEl) {
                ctx.clearRect(0, 0, canvasEl.width, canvasEl.height);
            }
        }
    }

    function close() {
        ensureUi();
        if (!panelEl) return;
        endResizeDrag();
        panelEl.classList.add('hidden');
        panelEl.setAttribute('aria-hidden', 'true');
        document.body.classList.remove('sounding-panel-open');
        panelOpen = false;
        setResizeHandleVisible(false);
        clearPanelSizeStyle();
        hideAllPins();
        pinLat = null;
        pinLon = null;
        lastProfile = null;
        lastPlot = null;
        hideHoverReadout();
        if (metaEl) {
            metaEl.textContent = toolEnabled
                ? 'Click a map point to load a profile.'
                : 'Enable the balloon tool, then click a map point.';
        }
        if (statusEl) {
            statusEl.textContent = '';
            statusEl.classList.remove('error');
        }
        requestAnimationFrame(() => {
            window.dispatchEvent(new Event('resize'));
        });
    }

    function isOpen() {
        return panelOpen;
    }

    window.SoundingViewer = {
        openAt,
        close,
        isOpen,
        isToolEnabled,
        setToolEnabled,
        renderSkewT,
        syncPin,
    };

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', () => {
            ensureUi();
            syncToolUi();
            storedPanelSizePx = readStoredPanelSize();
        });
    } else {
        ensureUi();
        syncToolUi();
        storedPanelSizePx = readStoredPanelSize();
    }
})();
