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

let selectedPremiseId = null;
let analysisResult = null;
let currentChart = null;
let currentTab = 'daily';
let batteryDayIndex = 0;
let rateSchedules = null;
let loginMethod = 'email';

const PLAN_TIME_WINDOWS = {
    'Home Plan (A1)': {
        'Anytime': [{ start: '00:00', end: '00:00' }],
    },
    'Midday Saver': {
        'Super Off Peak': [{ start: '09:00', end: '15:00' }],
        'Peak': [{ start: '15:00', end: '21:00' }],
        'Off Peak': [{ start: '21:00', end: '09:00' }],
    },
    'EV Add on': {
        'Super Off Peak': [{ start: '09:00', end: '15:00' }],
        'Peak': [{ start: '15:00', end: '21:00' }],
        'Off Peak': [{ start: '06:00', end: '09:00' }, { start: '21:00', end: '23:00' }],
        'Overnight': [{ start: '23:00', end: '06:00' }],
    },
};

// Tariff color map
const TARIFF_COLORS = {
    'Peak': '#ef4444',
    'Off Peak': '#3b82f6',
    'Super Off Peak': '#22c55e',
    'Overnight': '#8b5cf6',
    'Anytime': '#f59e0b',
};

function getTariffColor(name) {
    return TARIFF_COLORS[name] || '#64748b';
}

// === Init ===

document.addEventListener('DOMContentLoaded', () => {
    // Set default date range: 12 months prior to today
    const today = new Date();
    const yearAgo = new Date(today);
    yearAgo.setFullYear(yearAgo.getFullYear() - 1);
    document.getElementById('endDate').value = fmt(today);
    document.getElementById('startDate').value = fmt(yearAgo);

    // Fetch rate schedules and cached data files
    fetchRateSchedules();
    fetchDataFiles();

    // Plan selector change handler
    document.getElementById('planSelector').addEventListener('change', (e) => {
        const planName = e.target.value;
        const tariffFields = document.getElementById('tariffFields');
        const topupGroup = document.getElementById('gridTopupGroup');
        const topupCheckbox = document.getElementById('gridTopup');
        if (planName === 'auto' || !rateSchedules || !rateSchedules[planName]) {
            tariffFields.classList.add('hidden');
            tariffFields.innerHTML = '';
            topupGroup.classList.remove('hidden');
            return;
        }
        renderTariffFields(planName);
        tariffFields.classList.remove('hidden');

        const isFlatRate = Object.keys(PLAN_TIME_WINDOWS[planName] || {}).length <= 1;
        topupGroup.classList.toggle('hidden', isFlatRate);
        if (isFlatRate) topupCheckbox.checked = false;
    });

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
        if (!res.ok) return;
        rateSchedules = await res.json();

        const selector = document.getElementById('planSelector');
        Object.keys(rateSchedules).forEach(name => {
            const opt = document.createElement('option');
            opt.value = name;
            opt.textContent = name;
            selector.appendChild(opt);
        });
    } catch (e) {
        // Rate schedules are optional; silently ignore fetch errors
    }
}

function renderTariffFields(planName) {
    const plan = rateSchedules[planName];
    if (!plan) return;

    const container = document.getElementById('tariffFields');
    let html = `
        <div class="tariff-field">
            <label>Supply charge</label>
            <input type="number" class="input" id="tariffSupplyCharge" step="0.0001" value="${plan.supply_charge_cents}">
            <span class="tariff-unit">c/day</span>
        </div>
    `;

    Object.entries(plan.rates).forEach(([periodName, rate]) => {
        const fieldId = 'tariffRate_' + periodName.replace(/\s+/g, '_');
        html += `
            <div class="tariff-field">
                <label>${periodName}</label>
                <input type="number" class="input" id="${fieldId}" step="0.0001" value="${rate}" data-period="${periodName}">
                <span class="tariff-unit">c/kWh</span>
            </div>
        `;
    });

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

// === Auth Flow ===

async function searchPremise() {
    const addr = document.getElementById('addressInput').value.trim();
    if (!addr) return;

    const btn = document.getElementById('lookupBtn');
    btn.disabled = true;
    btn.textContent = '...';

    try {
        const res = await fetch('/api/premise/search', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ address: addr }),
        });
        const data = await res.json();

        const container = document.getElementById('premiseResults');
        container.innerHTML = '';
        container.classList.remove('hidden');

        if (!data.length) {
            container.innerHTML = '<div class="premise-item" style="color:var(--text-muted)">No results found</div>';
            return;
        }

        data.forEach(p => {
            const item = document.createElement('div');
            item.className = 'premise-item';
            item.innerHTML = `${p.label} <span class="premise-code">${p.code}</span>`;
            item.addEventListener('click', () => selectPremise(p));
            container.appendChild(item);
        });
    } catch (e) {
        toast('Failed to search: ' + e.message, 'error');
    } finally {
        btn.disabled = false;
        btn.textContent = 'Look up';
    }
}

function selectPremise(premise) {
    selectedPremiseId = premise.code;
    document.getElementById('addressInput').value = premise.label;
    document.getElementById('premiseResults').classList.add('hidden');
    document.getElementById('stepAddress').classList.add('completed');

    // Enable method step
    const stepMethod = document.getElementById('stepMethod');
    stepMethod.classList.remove('disabled');
    document.getElementById('emailInput').disabled = false;
    document.getElementById('otpBtn').disabled = false;
    document.getElementById('mobileInput').disabled = false;
    document.getElementById('smsBtn').disabled = false;
}

function setLoginMethod(method) {
    loginMethod = method;
    document.getElementById('methodEmail').classList.toggle('active', method === 'email');
    document.getElementById('methodSms').classList.toggle('active', method === 'sms');
    document.getElementById('emailField').classList.toggle('hidden', method !== 'email');
    document.getElementById('smsField').classList.toggle('hidden', method !== 'sms');
}

async function requestToken() {
    const email = document.getElementById('emailInput').value.trim();
    if (!email || !selectedPremiseId) return;

    const btn = document.getElementById('otpBtn');
    btn.disabled = true;
    btn.textContent = '...';

    try {
        const res = await fetch('/api/auth/request-token', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ email, premise_id: selectedPremiseId }),
        });

        if (!res.ok) throw new Error(await res.text());

        document.getElementById('stepMethod').classList.add('completed');
        toast('OTP sent — check your email', 'success');

        // Enable OTP step
        const stepOtp = document.getElementById('stepOtp');
        stepOtp.classList.remove('disabled');
        document.getElementById('otpInput').disabled = false;
        document.getElementById('loginBtn').disabled = false;
        document.getElementById('otpInput').focus();
    } catch (e) {
        toast('Failed to send OTP: ' + e.message, 'error');
    } finally {
        btn.disabled = false;
        btn.textContent = 'Send OTP';
    }
}

async function requestSmsCode() {
    const mobile = document.getElementById('mobileInput').value.trim();
    if (!mobile || !selectedPremiseId) return;

    const btn = document.getElementById('smsBtn');
    btn.disabled = true;
    btn.textContent = '...';

    try {
        const res = await fetch('/api/auth/request-sms', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ mobile, premise_id: selectedPremiseId }),
        });

        if (!res.ok) throw new Error(await res.text());

        document.getElementById('stepMethod').classList.add('completed');
        toast('SMS code sent — check your phone', 'success');

        // Enable OTP step
        const stepOtp = document.getElementById('stepOtp');
        stepOtp.classList.remove('disabled');
        document.getElementById('otpInput').disabled = false;
        document.getElementById('loginBtn').disabled = false;
        document.getElementById('otpInput').focus();
    } catch (e) {
        toast('Failed to send SMS: ' + e.message, 'error');
    } finally {
        btn.disabled = false;
        btn.textContent = 'Send SMS';
    }
}

async function loginWithToken() {
    const token = document.getElementById('otpInput').value.trim();
    if (!token) return;

    const btn = document.getElementById('loginBtn');
    btn.disabled = true;
    btn.textContent = '...';

    try {
        const res = await fetch('/api/auth/login', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ token, method: loginMethod }),
        });

        if (!res.ok) throw new Error('Login failed');

        document.getElementById('stepOtp').classList.add('completed');

        // Update header status
        const status = document.getElementById('authStatus');
        status.querySelector('.status-dot').className = 'status-dot online';
        status.querySelector('.status-text').textContent = 'Connected';

        document.getElementById('analyseBtn').disabled = !canAnalyse();

        toast('Logged in successfully', 'success');
    } catch (e) {
        toast('Login failed — check your code', 'error');
    } finally {
        btn.disabled = false;
        btn.textContent = 'Login';
    }
}

function canAnalyse() {
    const hasCachedFile = !!document.getElementById('dataSource').value;
    const isLoggedIn = !!document.querySelector('.status-dot.online');
    return hasCachedFile || isLoggedIn;
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
    const payload = {
        start_date: document.getElementById('startDate').value,
        end_date: document.getElementById('endDate').value,
        battery_capacity_kwh: parseFloat(document.getElementById('batteryCapacity').value) || 15,
        inverter_capacity_kw: parseFloat(document.getElementById('inverterCapacity').value) || 5,
        battery_cost_dollars: parseFloat(document.getElementById('batteryCost').value) || null,
        grid_topup: document.getElementById('gridTopup').checked,
        feed_in_peak_cents: parseFloat(document.getElementById('feedInPeak').value) || 0,
        feed_in_off_peak_cents: parseFloat(document.getElementById('feedInOffPeak').value) || 0,
        cached_file: cachedFile,
    };

    const selectedPlan = document.getElementById('planSelector').value;
    if (selectedPlan !== 'auto' && rateSchedules && rateSchedules[selectedPlan]) {
        const supplyInput = document.getElementById('tariffSupplyCharge');
        if (supplyInput) {
            payload.custom_supply_charge_cents = parseFloat(supplyInput.value);
        }

        const timeWindows = PLAN_TIME_WINDOWS[selectedPlan] || {};
        const tariffPeriods = [];
        const rateInputs = document.querySelectorAll('#tariffFields input[data-period]');
        rateInputs.forEach(input => {
            const periodName = input.dataset.period;
            const rateCents = parseFloat(input.value);
            const windows = timeWindows[periodName] || [{ start: '00:00', end: '00:00' }];
            windows.forEach(w => {
                tariffPeriods.push({
                    name: periodName,
                    rate_cents: rateCents,
                    start_time: w.start,
                    end_time: w.end,
                });
            });
        });
        payload.custom_tariff_periods = tariffPeriods;
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
        batteryDayIndex = 0;
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
    const periods = plan.tariff_periods.map(p =>
        `<span class="plan-period">
            <span class="dot" style="background:${getTariffColor(p.name)}"></span>
            ${p.name} ${p.rate_cents.toFixed(1)}c/kWh
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
