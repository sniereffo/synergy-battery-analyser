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
};

function getTariffColor(name) {
    return TARIFF_COLORS[name] || '#64748b';
}

// === Init ===

document.addEventListener('DOMContentLoaded', () => {
    // Default date range: last 3 months (a full day-by-day year fetch is slow —
    // the portal only serves one day of half-hourly data per request).
    const today = new Date();
    const start = new Date(today);
    start.setMonth(start.getMonth() - 3);
    document.getElementById('endDate').value = fmt(today);
    document.getElementById('startDate').value = fmt(start);

    // Fetch rate schedules and cached data files
    fetchRateSchedules();
    fetchDataFiles();

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
});

function fmt(d) {
    return d.toISOString().split('T')[0];
}

// === Rate Schedules ===

async function fetchRateSchedules() {
    try {
        const res = await fetch('/api/rate-schedules');
        if (res.ok) rateSchedules = await res.json();
    } catch (e) {
        // Rate schedules are optional prefill; silently ignore fetch errors.
    }
    // Render defaults so the panel isn't empty before a service is chosen.
    renderRateFields(null);
}

// Fetch current rates for a tariff (live from Synergy, falling back to
// defaults server-side) and render the editable fields.
async function fetchAndRenderRates(tariffLabel) {
    let data = null;
    try {
        const res = await fetch('/api/rates?tariff=' + encodeURIComponent(tariffLabel || ''));
        if (res.ok) data = await res.json();
    } catch (e) {
        // fall back to local prefill below
    }
    renderRateFields(tariffLabel, data);
}

// Render editable per-period rate fields. Uses server-fetched rates when
// provided (live from Synergy or default), else the local prefill schedule.
function renderRateFields(tariffLabel, fetched) {
    const container = document.getElementById('tariffFields');
    const key = (tariffLabel || '').trim().toUpperCase();

    let supply, rates, source;
    if (fetched && fetched.rates && Object.keys(fetched.rates).length) {
        supply = fetched.supply_charge_cents;
        rates = fetched.rates;
        source = fetched.source;
    } else {
        // Fall back to the first known schedule so the panel is never all-zero.
        let schedule = rateSchedules && rateSchedules[key];
        if (!schedule && rateSchedules) {
            const firstKey = Object.keys(rateSchedules)[0];
            schedule = firstKey ? rateSchedules[firstKey] : null;
        }
        supply = schedule ? schedule.supply_charge_cents : 116.0505;
        rates = schedule ? schedule.rates : {};
        source = rateSchedules && rateSchedules[key] ? 'default' : 'none';
    }
    const periods = Object.keys(rates).length ? Object.keys(rates) : ALL_PERIODS;

    let html = `
        <div class="tariff-field">
            <label>Supply charge</label>
            <input type="number" class="input" id="tariffSupplyCharge" step="0.0001" value="${supply != null ? supply : ''}">
            <span class="tariff-unit">c/day</span>
        </div>`;
    periods.forEach(p => {
        const r = rates[p] !== undefined ? rates[p] : 0;
        html += `
            <div class="tariff-field">
                <label>${PERIOD_LABELS[p] || p}</label>
                <input type="number" class="input" step="0.0001" value="${r}" data-period="${p}">
                <span class="tariff-unit">c/kWh</span>
            </div>`;
    });
    if (source === 'synergy') {
        html += `<div class="field-hint">✓ Rates sourced live from Synergy — adjust if your plan differs.</div>`;
    } else if (tariffLabel && source !== 'default') {
        html += `<div class="field-hint">No preset for "${tariffLabel}" — showing defaults; adjust to your plan's rates.</div>`;
    }
    container.innerHTML = html;
}

async function fetchDataFiles() {
    try {
        const res = await fetch('/api/data/files');
        if (!res.ok) return;
        const files = await res.json();
        const selector = document.getElementById('dataSource');
        // Clear existing file options (keep first "Fetch live" option)
        while (selector.options.length > 1) selector.remove(1);
        if (files.length) {
            const divider = document.createElement('option');
            divider.disabled = true;
            divider.textContent = '— Saved —';
            selector.appendChild(divider);
        }
        files.forEach(name => {
            const opt = document.createElement('option');
            opt.value = name;
            opt.textContent = name.replace('.json', '').replace(/_/g, ' ');
            selector.appendChild(opt);
        });
    } catch (e) {
        // Silently ignore
    }
}

function onDataSourceChange() {
    const cached = document.getElementById('dataSource').value;
    const authCard = document.querySelector('.auth-card');
    authCard.classList.toggle('disabled-card', !!cached);
    document.getElementById('analyseBtn').disabled = !canAnalyse();
}

// === Toast ===

function toast(msg, type = '') {
    const el = document.getElementById('toast');
    el.textContent = msg;
    el.className = 'toast ' + type + ' visible';
    setTimeout(() => el.classList.remove('visible'), 3500);
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
        if (!res.ok) throw new Error(await res.text());
        document.getElementById('loginProgress').classList.remove('hidden');
        setStatus('connecting', 'Waiting for login…');
        startStatusPolling();
    } catch (e) {
        toast('Could not open login: ' + e.message, 'error');
        btn.disabled = false;
        btn.textContent = 'Open Synergy login';
    }
}

function startStatusPolling() {
    if (statusPoll) clearInterval(statusPoll);
    statusPoll = setInterval(pollStatus, 2000);
    pollStatus();
}

async function pollStatus() {
    try {
        const res = await fetch('/api/auth/status');
        const data = await res.json();
        if (data.logged_in) {
            loggedIn = true;
            services = data.services || [];
            clearInterval(statusPoll);
            statusPoll = null;
            onLoggedIn();
        }
    } catch (e) {
        // transient — keep polling
    }
}

function onLoggedIn() {
    setStatus('online', 'Connected');
    document.getElementById('loginProgress').classList.add('hidden');
    document.getElementById('stepLogin').classList.add('completed');
    const btn = document.getElementById('loginBtn');
    btn.textContent = 'Logged in';
    btn.disabled = true;
    populateServices();
    toast('Logged in successfully', 'success');
}

function populateServices() {
    const step = document.getElementById('stepService');
    step.classList.remove('disabled');
    const sel = document.getElementById('serviceSelector');
    sel.innerHTML = '';
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

function onServiceChange() {
    const sel = document.getElementById('serviceSelector');
    selectedServiceId = sel.value;
    const tariff = sel.selectedOptions[0] ? sel.selectedOptions[0].dataset.tariff : '';
    fetchAndRenderRates(tariff);
    document.getElementById('analyseBtn').disabled = !canAnalyse();
}

function canAnalyse() {
    const hasCachedFile = !!document.getElementById('dataSource').value;
    return hasCachedFile || (loggedIn && !!selectedServiceId);
}

// === Analysis ===

async function runAnalysis() {
    const btn = document.getElementById('analyseBtn');
    const btnText = btn.querySelector('.btn-text');
    const spinner = btn.querySelector('.btn-spinner');

    btn.disabled = true;
    btnText.textContent = 'Analysing...';
    spinner.classList.remove('hidden');

    const cachedFile = document.getElementById('dataSource').value || null;

    // Collect editable per-period rates and supply charge.
    const rateOverrides = {};
    document.querySelectorAll('#tariffFields input[data-period]').forEach(input => {
        rateOverrides[input.dataset.period] = parseFloat(input.value) || 0;
    });
    const supplyInput = document.getElementById('tariffSupplyCharge');
    const serviceSel = document.getElementById('serviceSelector');
    const tariffLabel = serviceSel && serviceSel.selectedOptions[0]
        ? serviceSel.selectedOptions[0].dataset.tariff : null;

    const payload = {
        service_id: selectedServiceId,
        tariff_label: tariffLabel || null,
        start_date: document.getElementById('startDate').value,
        end_date: document.getElementById('endDate').value,
        battery_capacity_kwh: parseFloat(document.getElementById('batteryCapacity').value) || 15,
        inverter_capacity_kw: parseFloat(document.getElementById('inverterCapacity').value) || 5,
        battery_cost_dollars: parseFloat(document.getElementById('batteryCost').value) || null,
        grid_topup: document.getElementById('gridTopup').checked,
        feed_in_peak_cents: parseFloat(document.getElementById('feedInPeak').value) || 0,
        feed_in_off_peak_cents: parseFloat(document.getElementById('feedInOffPeak').value) || 0,
        supply_charge_cents: supplyInput ? parseFloat(supplyInput.value) : null,
        rate_overrides: rateOverrides,
        cached_file: cachedFile,
    };

    if (!cachedFile) {
        btnText.textContent = 'Fetching (one day at a time)…';
    }

    try {
        const res = await fetch('/api/analyse', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload),
        });

        if (!res.ok) {
            const err = await res.json().catch(() => ({ detail: 'Unknown error' }));
            throw new Error(err.detail || 'Analysis failed');
        }

        analysisResult = await res.json();
        const daySet = new Set(analysisResult.intervals.map(iv => iv.timestamp.split('T')[0]));
        batteryDayIndex = daySet.size - 1;
        showResults();
        toast(cachedFile ? 'Loaded from cache' : 'Analysis complete — data cached', 'success');
        if (!cachedFile) fetchDataFiles();
    } catch (e) {
        toast('Analysis failed: ' + e.message, 'error');
    } finally {
        btn.disabled = !canAnalyse();
        btnText.textContent = 'Fetch & Analyse';
        spinner.classList.add('hidden');
    }
}

function showResults() {
    document.getElementById('setupSection').classList.add('hidden');
    document.getElementById('resultsSection').classList.remove('hidden');
    renderPlanBanner();
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
    const periods = Object.entries(plan.rates).map(([name, rate]) =>
        `<span class="plan-period">
            <span class="dot" style="background:${getTariffColor(name)}"></span>
            ${name} ${rate.toFixed(1)}c/kWh
        </span>`
    ).join('');

    document.getElementById('planBanner').innerHTML = `
        <span class="plan-name">${plan.name}</span>
        <div class="plan-periods">${periods}</div>
    `;
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

    document.getElementById('summaryGrid').innerHTML = cards.map(c => `
        <div class="summary-card ${c.cls}">
            <div class="summary-label">${c.label}</div>
            <div class="summary-value">${c.value}</div>
            ${c.sub ? `<div class="summary-sub">${c.sub}</div>` : ''}
        </div>
    `).join('');
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

    // Group by month
    const months = {};
    intervals.forEach(iv => {
        const month = iv.timestamp.slice(0, 7);
        if (!months[month]) months[month] = { _days: new Set() };
        if (!months[month][iv.tariff_name]) months[month][iv.tariff_name] = 0;
        months[month][iv.tariff_name] += iv.cost_cents;
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

    // Total line
    series.push({
        name: 'Total',
        type: 'line',
        data: monthKeys.map(m => {
            let total = (months[m]._days.size * supplyPerDay);
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
        legend: { data: [...tariffNames, 'Supply', 'Total'], textStyle: { color: cssVar('--text-secondary'), fontSize: 11 }, top: 4 },
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
    controls.innerHTML = `
        <div class="day-selector">
            <button class="btn btn-outline" onclick="changeBatteryDay(-1)" ${batteryDayIndex === 0 ? 'disabled' : ''}>&larr;</button>
            <input type="date" class="day-picker" value="${dayKey}" min="${allDays[0]}" max="${allDays[allDays.length - 1]}" onchange="jumpToBatteryDay(this.value)">
            <button class="btn btn-outline" onclick="changeBatteryDay(1)" ${batteryDayIndex >= allDays.length - 1 ? 'disabled' : ''}>&rarr;</button>
            <span style="color:var(--text-muted);font-size:0.78rem;margin-left:8px">${batteryDayIndex + 1} / ${allDays.length}</span>
        </div>
    `;

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
            formatter: (p) => `${p.data[0]}<br/>${p.data[1]} kWh`,
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
