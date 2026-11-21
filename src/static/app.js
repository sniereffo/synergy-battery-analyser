/* === Synergy Battery Feasibility Analyser — Frontend === */

// === Theme ===

function initTheme() {
    const stored = localStorage.getItem('theme');
    if (stored === 'dark' || (!stored && window.matchMedia('(prefers-color-scheme: dark)').matches)) {
        document.documentElement.classList.add('dark');
    }
    // Listen for system preference changes (only applies when no manual override)
    window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', (e) => {
        if (!localStorage.getItem('theme')) {
            document.documentElement.classList.toggle('dark', e.matches);
            if (currentChart) renderChart();
        }
    });
}

function toggleTheme() {
    const isDark = document.documentElement.classList.toggle('dark');
    localStorage.setItem('theme', isDark ? 'dark' : 'light');
    if (currentChart) renderChart();
}

// Apply theme before DOM renders to prevent flash
initTheme();

let analysisResult = null;
let currentChart = null;
let currentTab = 'daily';
let batteryDayIndex = 0;
let rateSchedules = null;
let services = [];
let selectedServiceId = null;
let loggedIn = false;
let statusPoll = null;
let dataFiles = {};      // saved dataset name -> {name, tariff_label, start, end, days}
let planDetails = {};    // plan name -> {periods: [{name, key}], flat}
let ratesRenderedFor = null;  // what the rate fields currently show (avoid clobbering edits)
let ratesSeq = 0;             // guards against out-of-order rate fetches

// Display labels for the API's tariff-period column names.
const PERIOD_LABELS = {
    'PEAK': 'Peak',
    'OFF_PEAK': 'Off Peak',
    'OFF_PEAK(AM)': 'Off Peak (AM)',
    'OVERNIGHT': 'Overnight',
    'SUPER_OFFPEAK': 'Super Off Peak',
};
const ALL_PERIODS = ['PEAK', 'OFF_PEAK', 'OFF_PEAK(AM)', 'OVERNIGHT', 'SUPER_OFFPEAK'];

// Tariff color map — keyed by both API column names and display names so charts
// (which use the API names) and the plan banner (display names) stay consistent.
const TARIFF_COLORS = {
    'PEAK': '#ef4444', 'Peak': '#ef4444',
    'OFF_PEAK': '#3b82f6', 'Off Peak': '#3b82f6',
    'OFF_PEAK(AM)': '#60a5fa', 'Off Peak (AM)': '#60a5fa',
    'SUPER_OFFPEAK': '#22c55e', 'Super Off Peak': '#22c55e',
    'OVERNIGHT': '#8b5cf6', 'Overnight': '#8b5cf6',
    'Anytime': '#f59e0b',
};

function getTariffColor(name) {
    return TARIFF_COLORS[name] || '#64748b';
}

// === DOM helpers (never interpolate external strings into innerHTML) ===

// el('div', {class: 'x', onclick: fn}, 'text', childNode, ...)
function el(tag, attrs, ...children) {
    const node = document.createElement(tag);
    Object.entries(attrs || {}).forEach(([k, v]) => {
        if (v === null || v === undefined || v === false) return;
        if (k === 'class') node.className = v;
        else if (k.startsWith('on') && typeof v === 'function') node.addEventListener(k.slice(2), v);
        else if (k in node && typeof v !== 'string') node[k] = v;
        else node.setAttribute(k, v === true ? '' : v);
    });
    children.flat().forEach(c => {
        if (c === null || c === undefined || c === false) return;
        node.appendChild(typeof c === 'string' || typeof c === 'number' ? document.createTextNode(String(c)) : c);
    });
    return node;
}

function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, ch => (
        { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[ch]
    ));
}

// === Init ===

document.addEventListener('DOMContentLoaded', async () => {
    // Default date range: last 3 months (longer ranges take longer to fetch).
    const today = new Date();
    const start = new Date(today);
    start.setMonth(start.getMonth() - 3);
    document.getElementById('endDate').value = fmt(today);
    document.getElementById('startDate').value = fmt(start);
    setDateBounds(null, fmt(today));
    ['startDate', 'endDate'].forEach(id =>
        document.getElementById(id).addEventListener('change', clampDates));

    // Tab switching
    document.getElementById('chartTabs').addEventListener('click', (e) => {
        if (e.target.classList.contains('tab')) {
            document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
            e.target.classList.add('active');
            currentTab = e.target.dataset.tab;
            renderChart();
        }
    });

    // Resize handler
    window.addEventListener('resize', () => {
        if (currentChart) currentChart.resize();
    });

    // Fetch rate schedules, plans and cached data files, then render the rates.
    await Promise.all([fetchRateSchedules(), fetchPlans(), fetchDataFiles()]);
    refreshRates({ force: true });
    checkInitialStatus();
});

// YYYY-MM-DD in local time (toISOString would shift to UTC).
function fmt(d) {
    const pad = n => String(n).padStart(2, '0');
    return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
}

// === Rate Schedules ===

async function fetchRateSchedules() {
    try {
        const res = await fetch('/api/rate-schedules');
        if (res.ok) rateSchedules = await res.json();
    } catch (e) {
        // Rate schedules are optional prefill; silently ignore fetch errors.
    }
}

// Built-in prefill schedule for a plan name or tariff label (case-insensitive).
function localSchedule(name) {
    if (!rateSchedules || !name) return null;
    const key = name.trim().toUpperCase();
    const match = Object.keys(rateSchedules).find(k => k.toUpperCase() === key);
    return match ? rateSchedules[match] : null;
}

// Tariff label of the data that will be analysed: the saved dataset's label,
// or the selected service's tariff for a live fetch.
function currentTariffLabel() {
    const cached = document.getElementById('dataSource').value;
    if (cached) return (dataFiles[cached] && dataFiles[cached].tariff_label) || null;
    const sel = document.getElementById('serviceSelector');
    return sel && sel.selectedOptions[0] ? (sel.selectedOptions[0].dataset.tariff || null) : null;
}

// Re-render the editable rate fields for the selected plan. With "My current
// plan" ("" value) the plan is resolved from the data's tariff label. Skips
// re-rendering (which would discard edits) unless what's shown would change.
async function refreshRates({ force = false } = {}) {
    const explicitPlan = document.getElementById('planSelector').value || null;
    const tariffLabel = currentTariffLabel();
    const renderKey = explicitPlan ? 'plan:' + explicitPlan : 'data:' + (tariffLabel || '');
    if (!force && renderKey === ratesRenderedFor) return;
    const seq = ++ratesSeq;

    let planId = explicitPlan;
    if (!planId && tariffLabel) {
        try {
            const res = await fetch('/api/plans?tariff=' + encodeURIComponent(tariffLabel));
            if (res.ok) planId = (await res.json()).default || null;
        } catch (e) {
            // unknown plan -> data-driven fallback below
        }
    }

    const rateSource = planId || tariffLabel;
    let fetched = null;
    if (rateSource) {
        try {
            const res = await fetch('/api/rates?tariff=' + encodeURIComponent(rateSource));
            if (res.ok) fetched = await res.json();
        } catch (e) {
            // fall back to local prefill below
        }
    }
    if (seq !== ratesSeq) return;  // a newer refresh superseded this one
    renderRateFields({ planId, tariffLabel, explicit: !!explicitPlan, fetched });
    ratesRenderedFor = renderKey;
}

// Render editable per-period rate fields. A known plan shows exactly its own
// periods (one "Anytime" field for a flat plan); otherwise the periods the
// rate lookup returned. Blank fields mean "not overridden".
function renderRateFields({ planId, tariffLabel, explicit, fetched }) {
    const container = document.getElementById('tariffFields');

    let supply = null, rates = {}, source = 'none';
    if (fetched && fetched.rates && Object.keys(fetched.rates).length) {
        supply = fetched.supply_charge_cents;
        rates = fetched.rates;
        source = fetched.source;
    } else {
        const schedule = localSchedule(planId) || localSchedule(tariffLabel);
        if (schedule) {
            supply = schedule.supply_charge_cents;
            rates = schedule.rates || {};
            source = 'default';
        }
    }

    // [{name, key}] — label + API period column the rate applies to.
    let periods;
    if (planId && planDetails[planId]) {
        periods = planDetails[planId].periods;
    } else {
        const keys = Object.keys(rates).length ? Object.keys(rates) : ALL_PERIODS;
        periods = keys.map(k => ({ name: PERIOD_LABELS[k] || k, key: k }));
    }

    const rateField = (label, unit, inputAttrs) => el('div', { class: 'tariff-field' },
        el('label', {}, label),
        el('input', { type: 'number', class: 'input', step: '0.0001', placeholder: '—', ...inputAttrs }),
        el('span', { class: 'tariff-unit' }, unit),
    );

    const nodes = [rateField('Supply charge', 'c/day', {
        id: 'tariffSupplyCharge', value: supply != null ? String(supply) : '',
    })];
    periods.forEach(p => {
        const r = rates[p.key];
        nodes.push(rateField(p.name, 'c/kWh', {
            value: r !== undefined && r !== null ? String(r) : '', 'data-period': p.key,
        }));
    });

    let hint = null;
    if (source === 'synergy') {
        hint = '✓ Rates sourced live from Synergy — adjust if your plan differs.';
    } else if (source === 'default') {
        hint = 'Using built-in default rates — adjust to match your bill.';
    } else if (!explicit && !tariffLabel) {
        hint = 'Log in or choose a saved dataset to load your plan\'s rates, or enter them here.';
    } else {
        hint = `No preset for "${tariffLabel || planId}" — enter your plan's rates (blank fields are not priced).`;
    }
    if (!explicit && planId && tariffLabel) {
        nodes.push(el('div', { class: 'field-hint' }, `Your tariff "${tariffLabel}" is priced as ${planId}.`));
    } else if (!explicit && !planId && tariffLabel) {
        nodes.push(el('div', { class: 'field-hint' },
            `"${tariffLabel}" isn't a known plan — periods are priced as labelled in your data.`));
    }
    nodes.push(el('div', { class: 'field-hint' }, hint));
    container.replaceChildren(...nodes);
}

// === Saved datasets ===

async function fetchDataFiles() {
    try {
        const res = await fetch('/api/data/files');
        if (!res.ok) return;
        const files = (await res.json()).map(f => (typeof f === 'string' ? { name: f } : f));
        const selector = document.getElementById('dataSource');
        const previous = selector.value;
        dataFiles = {};
        // Clear existing file options (keep first "Fetch live" option)
        while (selector.options.length > 1) selector.remove(1);
        if (files.length) {
            const divider = document.createElement('option');
            divider.disabled = true;
            divider.textContent = '— Saved —';
            selector.appendChild(divider);
        }
        files.forEach(f => {
            dataFiles[f.name] = f;
            const opt = document.createElement('option');
            opt.value = f.name;
            let label = f.name.replace(/\.json$/, '').replace(/_/g, ' ');
            if (f.tariff_label) label += ` · ${f.tariff_label}`;
            if (f.days) label += ` · ${f.days} days`;
            opt.textContent = label;
            selector.appendChild(opt);
        });
        if (previous && dataFiles[previous]) selector.value = previous;
    } catch (e) {
        // Silently ignore
    }
}

function setDateBounds(min, max) {
    ['startDate', 'endDate'].forEach(id => {
        const input = document.getElementById(id);
        input.min = min || '';
        input.max = max || '';
    });
}

// Keep the date inputs within their min/max and in order.
function clampDates() {
    ['startDate', 'endDate'].forEach(id => {
        const input = document.getElementById(id);
        if (!input.value) return;
        if (input.min && input.value < input.min) input.value = input.min;
        if (input.max && input.value > input.max) input.value = input.max;
    });
}

function onDataSourceChange() {
    const cached = document.getElementById('dataSource').value;
    const file = cached ? dataFiles[cached] : null;
    // A saved dataset needs no Synergy connection — hide the connect step.
    document.getElementById('connectCard').classList.toggle('hidden', !!cached);

    const hint = document.getElementById('dataSourceHint');
    if (cached) {
        const parts = [];
        if (file && file.tariff_label) parts.push(`Tariff: ${file.tariff_label}`);
        if (file && file.days) parts.push(`${file.days} days`);
        if (file && file.start && file.end) parts.push(`${file.start} → ${file.end}`);
        hint.textContent = (parts.length ? parts.join(' · ') + '. ' : '')
            + 'Narrow the date range below to analyse part of it — no Synergy connection needed.';
    } else {
        hint.textContent = 'A live fetch needs you to log in below. Saved datasets load instantly with no connection.';
    }

    if (file && file.start && file.end) {
        document.getElementById('startDate').value = file.start;
        document.getElementById('endDate').value = file.end;
        setDateBounds(file.start, file.end);
    } else {
        setDateBounds(null, fmt(new Date()));
        clampDates();
    }

    const bt = document.querySelector('#analyseBtn .btn-text');
    if (bt) bt.textContent = cached ? 'Analyse' : 'Fetch & Analyse';
    document.getElementById('analyseBtn').disabled = !canAnalyse();
    refreshRates();
}

// === Toast ===

function toast(msg, type = '') {
    const node = document.getElementById('toast');
    node.textContent = msg;
    node.className = 'toast ' + type + ' visible';
    clearTimeout(toast._timer);
    toast._timer = setTimeout(() => node.classList.remove('visible'), type === 'error' ? 6000 : 3500);
}

// Best-effort human message from a failed fetch Response.
async function responseError(res, fallback) {
    const text = await res.text().catch(() => '');
    try {
        const body = JSON.parse(text);
        if (body && body.detail) return typeof body.detail === 'string' ? body.detail : JSON.stringify(body.detail);
    } catch (e) {
        // not JSON
    }
    return text || fallback || `HTTP ${res.status}`;
}

// === Auth Flow (browser-assisted) ===

function setStatus(state, text) {
    const status = document.getElementById('authStatus');
    const cls = state === 'online' ? 'online' : state === 'connecting' ? 'connecting' : 'offline';
    status.querySelector('.status-dot').className = 'status-dot ' + cls;
    status.querySelector('.status-text').textContent = text;
}

async function openLogin() {
    const btn = document.getElementById('loginBtn');
    btn.disabled = true;
    btn.textContent = 'Opening browser…';
    try {
        const res = await fetch('/api/auth/open-login', { method: 'POST' });
        if (!res.ok) throw new Error(await responseError(res, 'Could not open the login browser'));
        btn.textContent = 'Waiting for login…';
        document.getElementById('loginProgress').classList.remove('hidden');
        setStatus('connecting', 'Waiting for login…');
        startStatusPolling();
    } catch (e) {
        toast('Could not open login: ' + e.message, 'error');
        resetLoginUI();
    }
}

function stopStatusPolling() {
    if (statusPoll) clearInterval(statusPoll);
    statusPoll = null;
}

function startStatusPolling() {
    stopStatusPolling();
    statusPoll = setInterval(pollStatus, 2000);
    pollStatus();
}

async function pollStatus() {
    try {
        const res = await fetch('/api/auth/status');
        const data = await res.json();
        if (!statusPoll) return;  // polling was stopped while this was in flight
        if (data.logged_in) {
            stopStatusPolling();
            loggedIn = true;
            services = data.services || [];
            onLoggedIn();
        } else if (data.browser_open === false) {
            // The user closed the login window before finishing.
            stopStatusPolling();
            resetLoginUI();
            toast('Login window was closed — click "Open Synergy login" to try again.', 'error');
        }
    } catch (e) {
        // transient — keep polling
    }
}

// On page load, pick up an existing session (e.g. after a page refresh).
async function checkInitialStatus() {
    try {
        const res = await fetch('/api/auth/status');
        const data = await res.json();
        if (data.logged_in) {
            loggedIn = true;
            services = data.services || [];
            onLoggedIn({ quiet: true });
        } else if (data.browser_open) {
            document.getElementById('loginBtn').disabled = true;
            document.getElementById('loginBtn').textContent = 'Waiting for login…';
            document.getElementById('loginProgress').classList.remove('hidden');
            setStatus('connecting', 'Waiting for login…');
            startStatusPolling();
        }
    } catch (e) {
        // server not reachable yet; the user can still click "Open Synergy login"
    }
}

function onLoggedIn({ quiet = false } = {}) {
    setStatus('online', 'Connected');
    document.getElementById('loginProgress').classList.add('hidden');
    document.getElementById('stepLogin').classList.add('completed');
    const btn = document.getElementById('loginBtn');
    btn.textContent = 'Logged in';
    btn.disabled = true;
    document.getElementById('authActions').classList.remove('hidden');
    populateServices();
    if (!quiet) toast('Logged in successfully', 'success');
}

// Return the login UI to its initial "not connected" state.
function resetLoginUI() {
    stopStatusPolling();
    loggedIn = false;
    services = [];
    selectedServiceId = null;
    setStatus('offline', 'Not connected');
    document.getElementById('loginProgress').classList.add('hidden');
    document.getElementById('stepLogin').classList.remove('completed');
    const btn = document.getElementById('loginBtn');
    btn.textContent = 'Open Synergy login';
    btn.disabled = false;
    document.getElementById('authActions').classList.add('hidden');
    document.getElementById('stepService').classList.add('disabled');
    document.getElementById('serviceSelector').replaceChildren();
    // Make sure the connect step is visible for a live fetch.
    if (!document.getElementById('dataSource').value) {
        document.getElementById('connectCard').classList.remove('hidden');
    }
    document.getElementById('analyseBtn').disabled = !canAnalyse();
}

// "Log out / reconnect": drop the server-side browser session and start over.
async function resetLogin() {
    try {
        const res = await fetch('/api/auth/reset', { method: 'POST' });
        if (!res.ok) throw new Error(await responseError(res, 'Could not reset the session'));
        resetLoginUI();
        refreshRates();
        toast('Logged out of Synergy', 'success');
    } catch (e) {
        toast('Log out failed: ' + e.message, 'error');
    }
}

function populateServices() {
    const step = document.getElementById('stepService');
    step.classList.remove('disabled');
    const sel = document.getElementById('serviceSelector');
    sel.replaceChildren();
    services.forEach(s => {
        const opt = document.createElement('option');
        opt.value = s.service_id;
        opt.textContent = `${s.account_number} — ${s.address}${s.tariff ? ' (' + s.tariff + ')' : ''}`;
        opt.dataset.tariff = s.tariff || '';
        sel.appendChild(opt);
    });
    if (services.length) {
        sel.value = services[0].service_id;
        onServiceChange();
    }
}

async function fetchPlans() {
    try {
        const res = await fetch('/api/plans');
        if (!res.ok) return;
        const data = await res.json();
        planDetails = data.details || {};
        ['planSelector', 'resultsPlanSelector'].forEach(id => {
            const sel = document.getElementById(id);
            if (!sel) return;
            sel.replaceChildren();
            // "" = use the plan matching the data's own tariff (sends plan_id null).
            sel.appendChild(el('option', { value: '' }, 'My current plan (from data)'));
            (data.plans || []).forEach(name => sel.appendChild(el('option', { value: name }, name)));
            sel.value = '';
        });
    } catch (e) {
        // plans are optional; ignore
    }
}

// Switch the projected plan from the results view and re-run in place.
async function onResultsPlanChange() {
    const plan = document.getElementById('resultsPlanSelector').value;
    document.getElementById('planSelector').value = plan;
    // Load the new plan's rates into the (hidden) rate fields before re-running,
    // otherwise the previous plan's rates would be sent.
    await refreshRates({ force: true });
    runAnalysis();
}

async function onServiceChange() {
    const sel = document.getElementById('serviceSelector');
    selectedServiceId = sel.value || null;
    await refreshRates();
    document.getElementById('analyseBtn').disabled = !canAnalyse();
}

async function onPlanChange() {
    await refreshRates({ force: true });
    // If we've already analysed, re-run to project the newly selected plan.
    // This reads from cache (set after the first fetch), so it's instant.
    if (analysisResult) runAnalysis();
}

function canAnalyse() {
    const hasCachedFile = !!document.getElementById('dataSource').value;
    return hasCachedFile || (loggedIn && !!selectedServiceId);
}

// === Analysis ===

// Parse a numeric input; blank/invalid -> null (never silently 0).
function numOrNull(input) {
    if (!input || input.value.trim() === '') return null;
    const v = parseFloat(input.value);
    return Number.isFinite(v) ? v : null;
}

async function runAnalysis() {
    const btn = document.getElementById('analyseBtn');
    const btnText = btn.querySelector('.btn-text');
    const spinner = btn.querySelector('.btn-spinner');

    const cachedFile = document.getElementById('dataSource').value || null;

    // Dates: required for a live fetch; optional filters for a saved dataset.
    clampDates();
    let startDate = document.getElementById('startDate').value || null;
    let endDate = document.getElementById('endDate').value || null;
    if (!cachedFile && (!startDate || !endDate)) {
        toast('Choose a start and end date to fetch.', 'error');
        return;
    }
    if (startDate && endDate && endDate < startDate) {
        toast('End date must be on or after the start date.', 'error');
        return;
    }
    if (cachedFile) {
        const file = dataFiles[cachedFile];
        const fullRange = file && startDate === file.start && endDate === file.end;
        // Only filter when both are set and actually narrow the dataset.
        if (fullRange || !startDate || !endDate) startDate = endDate = null;
    }

    // Editable per-period rates. A blank field is "not overridden" — the
    // backend falls back to scraped/default rates (and warns if none exist).
    const rateOverrides = {};
    document.querySelectorAll('#tariffFields input[data-period]').forEach(input => {
        const v = numOrNull(input);
        if (v !== null) rateOverrides[input.dataset.period] = v;
    });

    const effPct = numOrNull(document.getElementById('roundTripEfficiency'));
    const efficiency = effPct === null ? 0.9 : Math.min(Math.max(effPct, 1), 100) / 100;

    const payload = {
        service_id: cachedFile ? null : selectedServiceId,
        // For a saved dataset the backend uses the label stored with the data.
        tariff_label: cachedFile ? null : currentTariffLabel(),
        plan_id: document.getElementById('planSelector').value || null,
        start_date: startDate,
        end_date: endDate,
        battery_capacity_kwh: numOrNull(document.getElementById('batteryCapacity')) ?? 15,
        inverter_capacity_kw: numOrNull(document.getElementById('inverterCapacity')) || 5,
        round_trip_efficiency: efficiency,
        battery_cost_dollars: numOrNull(document.getElementById('batteryCost')) || null,
        grid_topup: document.getElementById('gridTopup').checked,
        feed_in_peak_cents: numOrNull(document.getElementById('feedInPeak')) ?? 0,
        feed_in_off_peak_cents: numOrNull(document.getElementById('feedInOffPeak')) ?? 0,
        supply_charge_cents: numOrNull(document.getElementById('tariffSupplyCharge')),
        rate_overrides: Object.keys(rateOverrides).length ? rateOverrides : null,
        cached_file: cachedFile,
    };

    btn.disabled = true;
    btnText.textContent = 'Analysing...';
    spinner.classList.remove('hidden');
    showProgressModal();

    try {
        const res = await fetch('/api/analyse', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload),
        });
        if (!res.ok) {
            const err = new Error(await responseError(res, 'Could not start analysis'));
            if (res.status === 401) err.code = 'not_logged_in';
            throw err;
        }

        const job = await pollAnalysis();
        if (job.status === 'error') {
            const err = new Error(job.error || 'Analysis failed');
            err.code = job.error_code || null;
            throw err;
        }

        analysisResult = job.result;
        const daySet = new Set(analysisResult.intervals.map(iv => iv.timestamp.split('T')[0]));
        batteryDayIndex = daySet.size - 1;
        hideProgressModal();
        showResults();
        toast(cachedFile ? 'Loaded from cache' : 'Analysis complete — data cached', 'success');
        if (!cachedFile) {
            // Point subsequent runs (e.g. switching plans) at the cached file so
            // they're instant instead of re-fetching from Synergy.
            const cacheName = analysisResult.cache_file;
            await fetchDataFiles();
            const ds = document.getElementById('dataSource');
            if (cacheName && dataFiles[cacheName]) {
                ds.value = cacheName;
                onDataSourceChange();
            }
        }
    } catch (e) {
        hideProgressModal();
        if (e.code === 'not_logged_in') {
            // Session expired or the browser was closed — start the login over.
            resetLoginUI();
            if (!document.getElementById('resultsSection').classList.contains('hidden')) showSetup();
            toast('Synergy session expired — please log in again. (' + e.message + ')', 'error');
        } else {
            toast('Analysis failed: ' + e.message, 'error');
        }
    } finally {
        btn.disabled = !canAnalyse();
        btnText.textContent = document.getElementById('dataSource').value ? 'Analyse' : 'Fetch & Analyse';
        spinner.classList.add('hidden');
    }
}

// === Progress modal ===

function pollAnalysis() {
    return new Promise((resolve) => {
        const iv = setInterval(async () => {
            try {
                const res = await fetch('/api/analyse/progress');
                const job = await res.json();
                updateProgressModal(job);
                if (job.status === 'done' || job.status === 'error') {
                    clearInterval(iv);
                    resolve(job);
                }
            } catch (e) {
                // transient — keep polling
            }
        }, 700);
    });
}

function showProgressModal() {
    document.getElementById('progressModal').classList.remove('hidden');
    updateProgressModal({ phase: 'Starting…', done: 0, total: 0 });
}

function hideProgressModal() {
    document.getElementById('progressModal').classList.add('hidden');
}

function updateProgressModal(job) {
    document.getElementById('progressPhase').textContent = job.phase || 'Working…';
    const total = job.total || 0;
    const done = job.done || 0;
    const pct = total ? Math.round((done / total) * 100) : 0;
    document.getElementById('progressBar').style.width = pct + '%';
    document.getElementById('progressCount').textContent = total ? `${done} / ${total} days retrieved` : '';
    document.getElementById('progressDay').textContent = job.current_day ? `Latest day: ${job.current_day}` : '';
}

function showResults() {
    document.getElementById('setupSection').classList.add('hidden');
    document.getElementById('resultsSection').classList.remove('hidden');
    // Keep the results plan switcher in sync with the analysed plan
    // ("" = "My current plan (from data)").
    document.getElementById('resultsPlanSelector').value = document.getElementById('planSelector').value;
    renderPlanBanner();
    renderWarnings();
    renderSummary();
    renderChart();
}

function showSetup() {
    document.getElementById('setupSection').classList.remove('hidden');
    document.getElementById('resultsSection').classList.add('hidden');
    if (currentChart) {
        currentChart.dispose();
        currentChart = null;
    }
}

// === Plan Banner ===

function renderPlanBanner() {
    const plan = analysisResult.plan;
    const s = analysisResult.summary;
    const periods = Object.entries(plan.rates || {}).map(([name, rate]) =>
        el('span', { class: 'plan-period' },
            el('span', { class: 'dot', style: `background:${getTariffColor(name)}` }),
            `${name} ${typeof rate === 'number' ? rate.toFixed(1) : '—'}c/kWh`,
        ));
    if (typeof plan.supply_charge_cents === 'number') {
        periods.push(el('span', { class: 'plan-period' },
            el('span', { class: 'dot', style: `background:${getTariffColor('Supply')}` }),
            `Supply ${plan.supply_charge_cents.toFixed(1)}c/day`));
    }
    const range = s.data_start && s.data_end
        ? el('span', { class: 'plan-range' }, `${s.data_start} → ${s.data_end} · ${s.num_days} days`)
        : null;

    document.getElementById('planBanner').replaceChildren(
        el('span', { class: 'plan-name' }, plan.name || 'Plan'),
        el('div', { class: 'plan-periods' }, periods),
        range,
    );
}

// === Warnings ===

function renderWarnings() {
    const banner = document.getElementById('warningBanner');
    const warnings = (analysisResult && analysisResult.warnings) || [];
    banner.classList.toggle('hidden', !warnings.length);
    if (!warnings.length) {
        banner.replaceChildren();
        return;
    }
    banner.replaceChildren(
        el('div', { class: 'warning-title' },
            warnings.length === 1 ? 'Check this before trusting the numbers' : `Check these ${warnings.length} things before trusting the numbers`),
        el('ul', {}, warnings.map(w => el('li', {}, String(w)))),
    );
}

// === Summary Cards ===

function renderSummary() {
    const s = analysisResult.summary;
    const cards = [
        { label: 'Grid Import', value: `${s.total_consumption_kwh.toLocaleString()} kWh`, sub: `${s.num_days} days`, cls: 'neutral' },
        { label: 'Grid Export', value: `${s.total_export_kwh.toLocaleString()} kWh`, sub: `earned $${s.export_credits_dollars} feed-in`, cls: 'positive' },
        { label: 'Net Cost (no battery)', value: `$${s.total_cost_without_battery_dollars.toLocaleString()}`, sub: `after feed-in credits`, cls: 'negative' },
        { label: 'Net Cost (with battery)', value: `$${s.total_cost_with_battery_dollars.toLocaleString()}`, sub: `save $${s.period_savings_dollars}/period`, cls: 'positive' },
        { label: 'Payback', value: s.payback_years ? `${s.payback_years} yrs` : '—', sub: s.payback_years ? 'estimated' : 'enter battery cost', cls: s.payback_years ? 'positive' : 'neutral' },
    ];

    document.getElementById('summaryGrid').replaceChildren(...cards.map(c =>
        el('div', { class: `summary-card ${c.cls}` },
            el('div', { class: 'summary-label' }, c.label),
            el('div', { class: 'summary-value' }, c.value),
            c.sub ? el('div', { class: 'summary-sub' }, c.sub) : null,
        )));
}

// === Charts ===

function renderChart() {
    const container = document.getElementById('chartContainer');
    const controls = document.getElementById('chartControls');
    controls.innerHTML = '';

    if (currentChart) {
        currentChart.dispose();
        currentChart = null;
    }

    container.style.height = '480px';

    currentChart = echarts.init(container, null, { renderer: 'canvas' });

    const renderers = {
        daily: renderDailyUsage,
        cost: renderCostBreakdown,
        solar: renderSolarVsConsumption,
        battery: renderBatterySimulation,
        roi: renderROI,
        heatmap: renderHeatmap,
    };

    (renderers[currentTab] || renderers.daily)();
}

// Common ECharts theme overrides
function cssVar(name) {
    return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

function baseOption() {
    return {
        backgroundColor: 'transparent',
        textStyle: { fontFamily: "'DM Mono', monospace", color: cssVar('--text-secondary') },
        grid: { left: 60, right: 24, top: 48, bottom: 60, containLabel: false },
        tooltip: {
            backgroundColor: cssVar('--chart-tooltip-bg'),
            borderColor: cssVar('--chart-tooltip-border'),
            textStyle: { color: cssVar('--chart-tooltip-text'), fontFamily: "'DM Mono', monospace", fontSize: 12 },
        },
    };
}

// --- Chart 1: Daily Usage ---

function renderDailyUsage() {
    const intervals = analysisResult.intervals;

    // Group by day
    const days = {};
    intervals.forEach(iv => {
        const day = iv.timestamp.split('T')[0];
        if (!days[day]) days[day] = {};
        if (!days[day][iv.tariff_name]) days[day][iv.tariff_name] = 0;
        days[day][iv.tariff_name] += iv.consumption_kwh;
        if (!days[day]._exp) days[day]._exp = 0;
        days[day]._exp += iv.export_kwh;
    });

    const dates = Object.keys(days).sort();
    const tariffNames = [...new Set(intervals.map(iv => iv.tariff_name))];

    const series = tariffNames.map(name => ({
        name,
        type: 'bar',
        stack: 'usage',
        data: dates.map(d => +(days[d][name] || 0).toFixed(2)),
        itemStyle: { color: getTariffColor(name) },
        barMaxWidth: 8,
    }));

    series.push({
        name: 'Solar Export',
        type: 'line',
        data: dates.map(d => +(days[d]._exp || 0).toFixed(2)),
        lineStyle: { color: '#facc15', width: 1.5 },
        itemStyle: { color: '#facc15' },
        symbol: 'none',
        smooth: true,
    });

    currentChart.setOption({
        ...baseOption(),
        legend: { data: [...tariffNames, 'Solar Export'], textStyle: { color: cssVar('--text-secondary'), fontSize: 11 }, top: 4 },
        xAxis: { type: 'category', data: dates, axisLabel: { fontSize: 10, rotate: 45 } },
        yAxis: { type: 'value', name: 'kWh', nameTextStyle: { fontSize: 11 } },
        series,
        dataZoom: [{ type: 'slider', bottom: 4, height: 20, borderColor: cssVar('--border'), fillerColor: cssVar('--accent-green-bg') }],
        grid: { ...baseOption().grid, bottom: 100 },
        tooltip: { ...baseOption().tooltip, trigger: 'axis' },
    });
}

// --- Chart 2: Cost Breakdown ---

function renderCostBreakdown() {
    const intervals = analysisResult.intervals;
    const supplyPerDay = analysisResult.plan.supply_charge_cents;
    // Supply is charged per distinct day present in the data — the same basis
    // as summary.num_days — so the monthly bars sum to the summary totals.
    const hasCredits = intervals.some(iv => typeof iv.export_credit_cents === 'number');

    // Group by month
    const months = {};
    intervals.forEach(iv => {
        const month = iv.timestamp.slice(0, 7);
        if (!months[month]) months[month] = { _days: new Set(), _credit: 0 };
        if (!months[month][iv.tariff_name]) months[month][iv.tariff_name] = 0;
        months[month][iv.tariff_name] += iv.cost_cents;
        months[month]._credit += iv.export_credit_cents || 0;
        months[month]._days.add(iv.timestamp.split('T')[0]);
    });

    const monthKeys = Object.keys(months).sort();
    const tariffNames = [...new Set(intervals.map(iv => iv.tariff_name))];

    const series = tariffNames.map(name => ({
        name,
        type: 'bar',
        stack: 'cost',
        data: monthKeys.map(m => +((months[m][name] || 0) / 100).toFixed(2)),
        itemStyle: { color: getTariffColor(name) },
        barMaxWidth: 32,
    }));

    // Supply charge bar
    series.push({
        name: 'Supply',
        type: 'bar',
        stack: 'cost',
        data: monthKeys.map(m => +((months[m]._days.size * supplyPerDay) / 100).toFixed(2)),
        itemStyle: { color: '#64748b' },
        barMaxWidth: 32,
    });

    // Feed-in credits (negative), so the total is net like the summary's
    // "Net Cost (no battery)".
    if (hasCredits) {
        series.push({
            name: 'Feed-in credit',
            type: 'bar',
            stack: 'cost',
            data: monthKeys.map(m => -+(months[m]._credit / 100).toFixed(2)),
            itemStyle: { color: '#facc15' },
            barMaxWidth: 32,
        });
    }

    // Total line
    const totalName = hasCredits ? 'Net total' : 'Total';
    series.push({
        name: totalName,
        type: 'line',
        data: monthKeys.map(m => {
            let total = (months[m]._days.size * supplyPerDay) - months[m]._credit;
            tariffNames.forEach(n => total += (months[m][n] || 0));
            return +(total / 100).toFixed(2);
        }),
        lineStyle: { color: cssVar('--text-primary'), width: 1.5, type: 'dashed' },
        itemStyle: { color: cssVar('--text-primary') },
        symbol: 'circle',
        symbolSize: 4,
    });

    currentChart.setOption({
        ...baseOption(),
        legend: { data: [...tariffNames, 'Supply', ...(hasCredits ? ['Feed-in credit'] : []), totalName], textStyle: { color: cssVar('--text-secondary'), fontSize: 11 }, top: 4 },
        xAxis: { type: 'category', data: monthKeys },
        yAxis: { type: 'value', name: 'AUD ($)', nameTextStyle: { fontSize: 11 } },
        series,
        tooltip: { ...baseOption().tooltip, trigger: 'axis' },
    });
}

// --- Chart 3: Grid Import vs Export ---

function renderSolarVsConsumption() {
    const intervals = analysisResult.intervals;

    const days = {};
    intervals.forEach(iv => {
        const day = iv.timestamp.split('T')[0];
        if (!days[day]) days[day] = { imported: 0, exported: 0 };
        days[day].imported += iv.consumption_kwh;
        days[day].exported += iv.export_kwh;
    });

    const dates = Object.keys(days).sort();

    currentChart.setOption({
        ...baseOption(),
        legend: { data: ['Grid Import', 'Grid Export'], textStyle: { color: cssVar('--text-secondary'), fontSize: 11 }, top: 4 },
        xAxis: { type: 'category', data: dates, axisLabel: { fontSize: 10, rotate: 45 } },
        yAxis: { type: 'value', name: 'kWh', nameTextStyle: { fontSize: 11 } },
        series: [
            {
                name: 'Grid Import',
                type: 'line',
                data: dates.map(d => +days[d].imported.toFixed(2)),
                areaStyle: { color: 'rgba(239,68,68,0.15)' },
                lineStyle: { color: '#ef4444', width: 1.5 },
                itemStyle: { color: '#ef4444' },
                symbol: 'none',
                smooth: true,
            },
            {
                name: 'Grid Export',
                type: 'line',
                data: dates.map(d => +days[d].exported.toFixed(2)),
                areaStyle: { color: 'rgba(250,204,21,0.12)' },
                lineStyle: { color: '#facc15', width: 1.5 },
                itemStyle: { color: '#facc15' },
                symbol: 'none',
                smooth: true,
            },
        ],
        dataZoom: [{ type: 'slider', bottom: 4, height: 20, borderColor: cssVar('--border'), fillerColor: cssVar('--accent-green-bg') }],
        grid: { ...baseOption().grid, bottom: 100 },
        tooltip: { ...baseOption().tooltip, trigger: 'axis' },
    });
}

// --- Chart 4: Battery Simulation (single day view) ---

function renderBatterySimulation() {
    const intervals = analysisResult.intervals;

    // Get unique days
    const dayMap = {};
    intervals.forEach(iv => {
        const day = iv.timestamp.split('T')[0];
        if (!dayMap[day]) dayMap[day] = [];
        dayMap[day].push(iv);
    });
    const allDays = Object.keys(dayMap).sort();

    if (batteryDayIndex >= allDays.length) batteryDayIndex = allDays.length - 1;
    if (batteryDayIndex < 0) batteryDayIndex = 0;

    const dayKey = allDays[batteryDayIndex];
    const dayData = dayMap[dayKey];

    // Render day selector
    const controls = document.getElementById('chartControls');
    controls.replaceChildren(el('div', { class: 'day-selector' },
        el('button', { class: 'btn btn-outline', onclick: () => changeBatteryDay(-1), disabled: batteryDayIndex === 0 }, '←'),
        el('input', {
            type: 'date', class: 'day-picker', value: dayKey,
            min: allDays[0], max: allDays[allDays.length - 1],
            onchange: (e) => jumpToBatteryDay(e.target.value),
        }),
        el('button', { class: 'btn btn-outline', onclick: () => changeBatteryDay(1), disabled: batteryDayIndex >= allDays.length - 1 }, '→'),
        el('span', { style: 'color:var(--text-muted);font-size:0.78rem;margin-left:8px' }, `${batteryDayIndex + 1} / ${allDays.length}`),
    ));

    const times = dayData.map(iv => iv.timestamp.split('T')[1].slice(0, 5));
    const capacity = parseFloat(document.getElementById('batteryCapacity').value) || 15;

    const opt = baseOption();
    opt.grid.right = 80;
    currentChart.clear();
    currentChart.setOption({
        ...opt,
        legend: { data: ['Battery Level', 'Consumption', 'Solar Export', 'Discharge'], textStyle: { color: cssVar('--text-secondary'), fontSize: 11 }, top: 4 },
        xAxis: { type: 'category', data: times, axisLabel: { fontSize: 10 } },
        yAxis: [
            { type: 'value', name: 'kWh', nameTextStyle: { fontSize: 11 }, max: Math.max(capacity, 5) },
            { type: 'value', name: 'kWh', nameTextStyle: { fontSize: 11 }, splitLine: { show: false } },
        ],
        series: [
            {
                name: 'Battery Level',
                type: 'line',
                data: dayData.map(d => +d.battery_kwh.toFixed(2)),
                areaStyle: { color: 'rgba(74,222,148,0.15)' },
                lineStyle: { color: '#4ade94', width: 2 },
                itemStyle: { color: '#4ade94' },
                symbol: 'none',
                smooth: true,
            },
            {
                name: 'Consumption',
                type: 'bar',
                yAxisIndex: 1,
                data: dayData.map(d => +d.consumption_kwh.toFixed(3)),
                itemStyle: { color: 'rgba(239,68,68,0.5)' },
                barMaxWidth: 12,
            },
            {
                name: 'Solar Export',
                type: 'bar',
                yAxisIndex: 1,
                data: dayData.map(d => +d.export_kwh.toFixed(3)),
                itemStyle: { color: 'rgba(250,204,21,0.5)' },
                barMaxWidth: 12,
            },
            {
                name: 'Discharge',
                type: 'bar',
                yAxisIndex: 1,
                data: dayData.map(d => +d.discharge_kwh.toFixed(3)),
                itemStyle: { color: 'rgba(139,92,246,0.6)' },
                barMaxWidth: 12,
            },
        ],
        tooltip: { ...baseOption().tooltip, trigger: 'axis' },
    });
}

function changeBatteryDay(delta) {
    batteryDayIndex += delta;
    renderChart();
}

function jumpToBatteryDay(dateStr) {
    const intervals = analysisResult.intervals;
    const daySet = new Set();
    intervals.forEach(iv => daySet.add(iv.timestamp.split('T')[0]));
    const allDays = [...daySet].sort();
    const idx = allDays.indexOf(dateStr);
    if (idx >= 0) {
        batteryDayIndex = idx;
        renderChart();
    }
}

// --- Chart 5: ROI ---

function renderROI() {
    const s = analysisResult.summary;
    const batteryCost = parseFloat(document.getElementById('batteryCost').value) || 0;
    const annualSavings = s.period_savings_dollars;

    if (!batteryCost || annualSavings <= 0) {
        currentChart.setOption({
            ...baseOption(),
            title: {
                text: batteryCost ? 'No positive savings to project' : 'Enter battery cost to see ROI',
                left: 'center', top: 'center',
                textStyle: { color: cssVar('--text-muted'), fontSize: 14, fontWeight: 400 },
            },
        });
        return;
    }

    // Scale savings to annual rate
    const daysInPeriod = s.num_days;
    const dailySavings = annualSavings / daysInPeriod;
    const yearlySavings = dailySavings * 365;

    const years = [];
    const savings = [];
    const cost = [];
    const maxYears = Math.min(Math.ceil(batteryCost / yearlySavings) * 2, 25);

    for (let y = 0; y <= maxYears; y++) {
        years.push(`Year ${y}`);
        savings.push(+(yearlySavings * y).toFixed(0));
        cost.push(batteryCost);
    }

    currentChart.setOption({
        ...baseOption(),
        legend: { data: ['Cumulative Savings', 'Battery Cost'], textStyle: { color: cssVar('--text-secondary'), fontSize: 11 }, top: 4 },
        xAxis: { type: 'category', data: years },
        yAxis: { type: 'value', name: 'AUD ($)', nameTextStyle: { fontSize: 11 } },
        series: [
            {
                name: 'Cumulative Savings',
                type: 'line',
                data: savings,
                areaStyle: { color: 'rgba(74,222,148,0.12)' },
                lineStyle: { color: '#4ade94', width: 2 },
                itemStyle: { color: '#4ade94' },
                smooth: true,
            },
            {
                name: 'Battery Cost',
                type: 'line',
                data: cost,
                lineStyle: { color: '#ef4444', width: 1.5, type: 'dashed' },
                itemStyle: { color: '#ef4444' },
                symbol: 'none',
            },
        ],
        tooltip: { ...baseOption().tooltip, trigger: 'axis' },
    });
}

// --- Chart 6: Heatmap ---

function renderHeatmap() {
    const intervals = analysisResult.intervals;

    // Daily totals
    const days = {};
    intervals.forEach(iv => {
        const day = iv.timestamp.split('T')[0];
        if (!days[day]) days[day] = 0;
        days[day] += iv.consumption_kwh;
    });

    const data = Object.entries(days).map(([d, v]) => [d, +v.toFixed(2)]);
    const values = data.map(d => d[1]);
    const minVal = Math.min(...values);
    const maxVal = Math.max(...values);

    // Determine year range
    const allDates = data.map(d => d[0]).sort();
    const startYear = parseInt(allDates[0].slice(0, 4));
    const endYear = parseInt(allDates[allDates.length - 1].slice(0, 4));

    const calendarAndSeries = [];
    let calIdx = 0;
    for (let yr = startYear; yr <= endYear; yr++) {
        const rangeStart = allDates.find(d => d.startsWith(yr.toString())) || `${yr}-01-01`;
        const yearDates = allDates.filter(d => d.startsWith(yr.toString()));
        const rangeEnd = yearDates.length ? yearDates[yearDates.length - 1] : `${yr}-12-31`;

        calendarAndSeries.push({
            calendar: {
                range: [rangeStart, rangeEnd],
                top: 60 + calIdx * 180,
                left: 60,
                right: 40,
                cellSize: ['auto', 16],
                splitLine: { lineStyle: { color: cssVar('--border') } },
                yearLabel: { color: cssVar('--text-secondary'), fontSize: 12 },
                monthLabel: { color: cssVar('--text-secondary'), fontSize: 10 },
                dayLabel: { color: cssVar('--text-muted'), fontSize: 10 },
                itemStyle: { borderColor: cssVar('--bg-primary'), borderWidth: 2 },
            },
            series: {
                type: 'heatmap',
                coordinateSystem: 'calendar',
                calendarIndex: calIdx,
                data: data.filter(d => d[0].startsWith(yr.toString())),
            },
        });
        calIdx++;
    }

    const chartHeight = 60 + calIdx * 180 + 40;
    document.getElementById('chartContainer').style.height = chartHeight + 'px';
    currentChart.resize();

    currentChart.setOption({
        ...baseOption(),
        visualMap: {
            min: minVal,
            max: maxVal,
            calculable: true,
            orient: 'horizontal',
            left: 'center',
            top: 4,
            inRange: {
                color: [cssVar('--bg-card'), '#14532d', '#22c55e', '#facc15', '#ef4444'],
            },
            textStyle: { color: cssVar('--text-secondary'), fontSize: 11 },
        },
        calendar: calendarAndSeries.map(c => c.calendar),
        series: calendarAndSeries.map(c => c.series),
        tooltip: {
            ...baseOption().tooltip,
            formatter: (p) => `${escapeHtml(p.data[0])}<br/>${escapeHtml(p.data[1])} kWh`,
        },
    });
}

// === CSV Export ===

function exportCsv() {
    if (!analysisResult || !analysisResult.intervals || !analysisResult.intervals.length) {
        toast('No analysis data to export', 'error');
        return;
    }

    const intervals = analysisResult.intervals;
    const headers = Object.keys(intervals[0]);
    const rows = intervals.map(row =>
        headers.map(h => {
            const val = row[h];
            if (typeof val === 'string' && val.includes(',')) return `"${val}"`;
            return val;
        }).join(',')
    );

    const csv = [headers.join(','), ...rows].join('\n');
    const blob = new Blob([csv], { type: 'text/csv;charset=utf-8;' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = 'synergy_analysis.csv';
    a.click();
    URL.revokeObjectURL(url);
    toast('CSV downloaded', 'success');
}
