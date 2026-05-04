/* ============================================================
   DCF Dashboard — App JavaScript
   Handles: tab switching, theme toggle, Chart.js charts,
   sensitivity heatmap, assumption overrides, AJAX recompute,
   confidence gauge, reverse DCF chart.
   ============================================================ */

/* ── Theme ───────────────────────────────────────────────── */
function toggleTheme() {
  const html = document.documentElement;
  const isDark = html.getAttribute('data-theme') === 'dark';
  html.setAttribute('data-theme', isDark ? 'light' : 'dark');
  document.getElementById('themeIcon').textContent = isDark ? '☽' : '☀';
  localStorage.setItem('dcf-theme', isDark ? 'light' : 'dark');
  if (window._chartsInitialized) {
    window.location.reload();
  }
}

(function initTheme() {
  const saved = localStorage.getItem('dcf-theme');
  if (saved) {
    document.documentElement.setAttribute('data-theme', saved);
    const icon = document.getElementById('themeIcon');
    if (icon) icon.textContent = saved === 'light' ? '☽' : '☀';
  }
})();

/* ── Tab switching ───────────────────────────────────────── */
function switchTab(name) {
  document.querySelectorAll('.tab-btn').forEach(btn => btn.classList.remove('active'));
  document.querySelectorAll('.tab-pane').forEach(sec => sec.classList.remove('active'));

  const section = document.getElementById('tab-' + name);
  if (section) section.classList.add('active');

  document.querySelectorAll('.tab-btn').forEach(btn => {
    if (btn.getAttribute('onclick') === `switchTab('${name}')`) {
      btn.classList.add('active');
    }
  });

  if (!window._chartsInitialized) {
    initCharts();
    window._chartsInitialized = true;
  }
}

/* ── Chart defaults ──────────────────────────────────────── */
function getChartColors() {
  const isDark = document.documentElement.getAttribute('data-theme') !== 'light';
  return {
    text:        isDark ? '#8b9cb8' : '#4a5568',
    textPrimary: isDark ? '#e8edf5' : '#1a2332',
    grid:        isDark ? '#1e2d45' : '#e4e8ec',
    accent:      '#2563eb',
    green:       '#10b981',
    red:         '#ef4444',
    amber:       '#f59e0b',
    orange:      '#f97316',
    purple:      '#7c3aed',
    bg:          isDark ? '#111827' : '#ffffff',
  };
}

Chart.defaults.font.family = "'Inter', -apple-system, sans-serif";
Chart.defaults.font.size   = 11;

function baseOptions(c) {
  return {
    responsive: true,
    maintainAspectRatio: false,
    plugins: {
      legend: {
        labels: { color: c.text, boxWidth: 12, padding: 16 }
      },
      tooltip: {
        backgroundColor: c.bg,
        titleColor: c.textPrimary,
        bodyColor:  c.text,
        borderColor: c.grid,
        borderWidth: 1,
        padding: 10,
      },
    },
    scales: {
      x: { ticks: { color: c.text }, grid: { color: c.grid } },
      y: { ticks: { color: c.text }, grid: { color: c.grid } },
    },
  };
}

function _money(value, digits = 2, symbol = '$') {
  return symbol + Number(value).toLocaleString('en-US', {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  });
}

function _moneyCompactMillions(value, symbol = '$') {
  const amount = Number(value);
  if (Math.abs(amount) >= 1000) {
    return `${symbol}${(amount / 1000).toFixed(0)}B`;
  }
  return `${symbol}${amount.toFixed(0)}M`;
}

/* ── Charts ──────────────────────────────────────────────── */
let _charts = {};

function initCharts() {
  const raw = document.getElementById('chartData');
  if (!raw) return;
  const D = JSON.parse(raw.textContent);
  const c = getChartColors();
  const moneySymbol = D.display_currency_symbol || D.currency_symbol || '$';
  const currencyCode = D.display_currency || D.currency || 'USD';

  /* ── Confidence gauge ─────────────────────────────────── */
  const ctxConf = document.getElementById('confGaugeChart');
  if (ctxConf) {
    const score  = D.confidence_score || 0;
    const remain = 100 - score;
    let gaugeColor = c.green;
    if (score < 50) gaugeColor = c.red;
    else if (score < 70) gaugeColor = c.amber;

    _charts.confGauge = new Chart(ctxConf, {
      type: 'doughnut',
      data: {
        datasets: [{
          data: [score, remain],
          backgroundColor: [gaugeColor + 'dd', c.grid],
          borderWidth: 0,
          borderRadius: 4,
        }],
      },
      options: {
        responsive: true, maintainAspectRatio: false,
        cutout: '72%', rotation: -90, circumference: 180,
        plugins: { legend: { display: false }, tooltip: { enabled: false } },
      },
    });
  }

  /* ── Revenue area chart ───────────────────────────────── */
  const ctxRev = document.getElementById('revenueChart');
  if (ctxRev) {
    _charts.revenue = new Chart(ctxRev, {
      type: 'line',
      data: {
        labels: D.historical_years,
        datasets: [{
          label: `Revenue (${currencyCode} M)`,
          data: D.historical_revenue,
          borderColor: c.accent,
          backgroundColor: c.accent + '22',
          fill: true, tension: .35, pointRadius: 3, pointBackgroundColor: c.accent,
        }],
      },
      options: {
        ...baseOptions(c),
        plugins: { ...baseOptions(c).plugins, legend: { display: false } },
        scales: {
          x: { ticks: { color: c.text }, grid: { color: c.grid } },
          y: { ticks: { color: c.text, callback: v => _moneyCompactMillions(v, moneySymbol) }, grid: { color: c.grid } },
        },
      },
    });
  }

  /* ── Margin line chart ────────────────────────────────── */
  const ctxMargin = document.getElementById('marginChart');
  if (ctxMargin) {
    _charts.margin = new Chart(ctxMargin, {
      type: 'line',
      data: {
        labels: D.historical_years,
        datasets: [
          { label: 'Gross Margin %', data: D.historical_gross_margin, borderColor: c.green, backgroundColor: 'transparent', tension: .3, pointRadius: 3, pointBackgroundColor: c.green },
          { label: 'EBIT Margin %',  data: D.historical_ebit_margin,  borderColor: c.amber, backgroundColor: 'transparent', tension: .3, pointRadius: 3, pointBackgroundColor: c.amber },
        ],
      },
      options: {
        ...baseOptions(c),
        scales: {
          x: { ticks: { color: c.text }, grid: { color: c.grid } },
          y: { ticks: { color: c.text, callback: v => v + '%' }, grid: { color: c.grid } },
        },
      },
    });
  }

  /* ── FCF bar chart ────────────────────────────────────── */
  const ctxFcf = document.getElementById('fcfChart');
  if (ctxFcf) {
    _charts.fcf = new Chart(ctxFcf, {
      type: 'bar',
      data: {
        labels: D.historical_years,
        datasets: [{
          label: `Free Cash Flow (${currencyCode} M)`,
          data: D.historical_fcf,
          backgroundColor: D.historical_fcf.map(v => v >= 0 ? c.green + 'bb' : c.red + 'bb'),
          borderRadius: 4,
        }],
      },
      options: {
        ...baseOptions(c),
        plugins: { ...baseOptions(c).plugins, legend: { display: false } },
        scales: {
          x: { ticks: { color: c.text }, grid: { color: c.grid } },
          y: { ticks: { color: c.text, callback: v => _moneyCompactMillions(v, moneySymbol) }, grid: { color: c.grid } },
        },
      },
    });
  }

  /* ── ROIC line chart ──────────────────────────────────── */
  const ctxRoic = document.getElementById('roicChart');
  if (ctxRoic) {
    _charts.roic = new Chart(ctxRoic, {
      type: 'line',
      data: {
        labels: D.historical_years,
        datasets: [{
          label: 'ROIC %', data: D.historical_roic,
          borderColor: c.accent, backgroundColor: 'transparent',
          tension: .3, pointRadius: 3, pointBackgroundColor: c.accent,
        }],
      },
      options: {
        ...baseOptions(c),
        plugins: { ...baseOptions(c).plugins, legend: { display: false } },
        scales: {
          x: { ticks: { color: c.text }, grid: { color: c.grid } },
          y: { ticks: { color: c.text, callback: v => v + '%' }, grid: { color: c.grid } },
        },
      },
    });
  }

  /* ── TV Donut chart ───────────────────────────────────── */
  const ctxDonut = document.getElementById('tvDonutChart');
  if (ctxDonut) {
    const ufcfPct = parseFloat((100 - D.tv_pct).toFixed(1));
    _charts.donut = new Chart(ctxDonut, {
      type: 'doughnut',
      data: {
        labels: ['PV of UFCFs', 'PV Terminal Value'],
        datasets: [{
          data: [ufcfPct, D.tv_pct],
          backgroundColor: [c.accent + 'cc', c.amber + 'cc'],
          borderColor: [c.accent, c.amber],
          borderWidth: 2, hoverOffset: 6,
        }],
      },
      options: {
        responsive: true, maintainAspectRatio: false,
        cutout: '68%',
        plugins: {
          legend: { position: 'right', labels: { color: c.text, padding: 16, boxWidth: 12 } },
          tooltip: { callbacks: { label: ctx => ` ${ctx.label}: ${ctx.parsed.toFixed(1)}% of EV` } },
        },
      },
    });
  }

  /* ── Reverse DCF sensitivity chart ───────────────────── */
  const ctxRdcf = document.getElementById('rdcfSensChart');
  if (ctxRdcf && D.rdcf_sensitivity && D.rdcf_sensitivity.length) {
    const gVals  = D.rdcf_sensitivity.map(p => p.g.toFixed(2) + '%');
    const ivVals = D.rdcf_sensitivity.map(p => p.iv);
    _charts.rdcf = new Chart(ctxRdcf, {
      type: 'line',
      data: {
        labels: gVals,
        datasets: [
          {
            label: `Intrinsic Value (${currencyCode})`,
            data: ivVals,
            borderColor: c.accent,
            backgroundColor: c.accent + '22',
            fill: true, tension: .4, pointRadius: 3, pointBackgroundColor: c.accent,
          },
          {
            label: 'Current Price',
            data: ivVals.map(() => D.price),
            borderColor: c.red,
            borderDash: [5, 3],
            borderWidth: 2,
            pointRadius: 0,
          },
        ],
      },
      options: {
        ...baseOptions(c),
        plugins: {
          ...baseOptions(c).plugins,
          annotation: {},
          tooltip: {
            ...baseOptions(c).plugins.tooltip,
            callbacks: {
              label: ctx => ctx.dataset.label === 'Current Price'
                ? ` Current Price: ${_money(D.price, 2, moneySymbol)}`
                : ` IV = ${_money(ctx.parsed.y, 2, moneySymbol)}`,
            },
          },
        },
        scales: {
          x: { ticks: { color: c.text }, grid: { color: c.grid }, title: { display: true, text: 'Terminal Growth Rate (g)', color: c.text } },
          y: { ticks: { color: c.text, callback: v => _money(v, 0, moneySymbol) }, grid: { color: c.grid } },
        },
      },
    });

    // Mark implied-g on chart with vertical line plugin
    if (D.rdcf_implied_g !== null) {
      const impliedG = D.rdcf_implied_g.toFixed(2) + '%';
      const impliedIdx = gVals.indexOf(impliedG);
      if (impliedIdx >= 0) {
        _charts.rdcf.data.datasets.push({
          label: 'Implied g',
          data: gVals.map((_, i) => i === impliedIdx ? D.price : null),
          pointRadius: 8,
          pointBackgroundColor: c.orange,
          pointBorderColor: c.orange,
          showLine: false,
        });
        _charts.rdcf.update();
      }
    }
  }

  /* ── Football field chart ─────────────────────────────── */
  const ctxFootball = document.getElementById('footballChart');
  if (ctxFootball) {
    const price = D.price;
    const rows = [
      { label: 'DCF Bear–Bull Range',    lo: D.dcf_bear, hi: D.dcf_bull, color: c.accent },
      { label: '52-Week Range',           lo: D.fifty_two_week_low, hi: D.fifty_two_week_high, color: c.green },
      { label: 'Analyst Price Targets',   lo: D.analyst_low, hi: D.analyst_high, color: c.amber },
      { label: 'DCF Intrinsic Value',     lo: D.iv * .985, hi: D.iv * 1.015, color: c.purple },
    ];
    const allVals = rows.flatMap(r => [r.lo, r.hi]).concat(price);
    const minV = Math.min(...allVals) * 0.88;
    const maxV = Math.max(...allVals) * 1.08;

    const datasets = rows.flatMap((r, i) => [
      {
        label: r.label + ' (base)',
        data: rows.map((_, j) => j === i ? r.lo : null),
        backgroundColor: 'transparent', borderColor: 'transparent',
        barPercentage: 0.6, categoryPercentage: 0.85, stack: 'stack',
      },
      {
        label: r.label,
        data: rows.map((_, j) => j === i ? (r.hi - r.lo) : null),
        backgroundColor: r.color + '88', borderColor: r.color, borderWidth: 1, borderRadius: 4,
        barPercentage: 0.6, categoryPercentage: 0.85, stack: 'stack',
      },
    ]);

    _charts.football = new Chart(ctxFootball, {
      type: 'bar',
      data: { labels: rows.map(r => r.label), datasets },
      options: {
        indexAxis: 'y', responsive: true, maintainAspectRatio: false,
        plugins: {
          legend: { display: false },
          tooltip: {
            filter: item => !item.dataset.label.endsWith('(base)'),
            callbacks: {
              label: ctx => {
                const r = rows[ctx.dataIndex];
                return ` ${_money(r.lo, 2, moneySymbol)} – ${_money(r.hi, 2, moneySymbol)}`;
              },
            },
          },
        },
        scales: {
          x: { stacked: true, min: minV, max: maxV, ticks: { color: c.text, callback: v => _money(v, 0, moneySymbol) }, grid: { color: c.grid } },
          y: { stacked: true, ticks: { color: c.text }, grid: { display: false } },
        },
      },
      plugins: [{
        id: 'priceLine',
        afterDraw(chart) {
          const ctx2 = chart.ctx;
          const xScale = chart.scales.x;
          const area   = chart.chartArea;
          const xPos   = xScale.getPixelForValue(price);
          ctx2.save();
          ctx2.beginPath();
          ctx2.moveTo(xPos, area.top);
          ctx2.lineTo(xPos, area.bottom);
          ctx2.strokeStyle = c.red;
          ctx2.lineWidth   = 2;
          ctx2.setLineDash([5, 3]);
          ctx2.stroke();
          ctx2.fillStyle = c.red;
          ctx2.font = 'bold 11px Inter, sans-serif';
          ctx2.fillText(_money(price, 2, moneySymbol), xPos + 4, area.top + 14);
          ctx2.restore();
        },
      }],
    });
  }

  /* ── Sensitivity heatmap colouring ───────────────────── */
  colourSensitivityTable();

  /* ── DuPont decomposition chart ──────────────────────── */
  const ctxDupont = document.getElementById('dupontChart');
  if (ctxDupont && D.dupont_years && D.dupont_years.length) {
    _charts.dupont = new Chart(ctxDupont, {
      type: 'line',
      data: {
        labels: D.dupont_years,
        datasets: [
          {
            label: 'Net Margin %',
            data: D.dupont_net_margin,
            borderColor: c.accent,
            backgroundColor: 'transparent',
            tension: 0.3,
            yAxisID: 'yPct',
            pointRadius: 3,
            pointBackgroundColor: c.accent,
          },
          {
            label: 'Asset Turnover ×',
            data: D.dupont_asset_turnover,
            borderColor: c.green,
            backgroundColor: 'transparent',
            tension: 0.3,
            yAxisID: 'yMult',
            pointRadius: 3,
            pointBackgroundColor: c.green,
          },
          {
            label: 'Leverage ×',
            data: D.dupont_leverage,
            borderColor: c.amber,
            backgroundColor: 'transparent',
            tension: 0.3,
            yAxisID: 'yMult',
            pointRadius: 3,
            pointBackgroundColor: c.amber,
          },
        ],
      },
      options: {
        ...baseOptions(c),
        scales: {
          x:    { ticks: { color: c.text }, grid: { color: c.grid } },
          yPct: { type: 'linear', position: 'left',  ticks: { color: c.text, callback: v => v.toFixed(1) + '%' }, grid: { color: c.grid } },
          yMult:{ type: 'linear', position: 'right', ticks: { color: c.text, callback: v => v.toFixed(2) + '×' }, grid: { display: false } },
        },
      },
    });
  }

  /* ── Earnings quality chart ───────────────────────────── */
  const ctxEQ = document.getElementById('earningsQualityChart');
  if (ctxEQ && D.eq_years && D.eq_years.length) {
    _charts.earningsQuality = new Chart(ctxEQ, {
      type: 'bar',
      data: {
        labels: D.eq_years,
        datasets: [
          {
            label: 'Net Income',
            data: D.eq_net_income,
            backgroundColor: c.accent + '99',
            borderRadius: 3,
            barPercentage: 0.8,
          },
          {
            label: 'Operating CF',
            data: D.eq_operating_cf,
            backgroundColor: c.green + '99',
            borderRadius: 3,
            barPercentage: 0.8,
          },
          {
            label: 'FCF',
            data: D.eq_fcf,
            backgroundColor: c.amber + '99',
            borderRadius: 3,
            barPercentage: 0.8,
          },
        ],
      },
      options: {
        ...baseOptions(c),
        scales: {
          x: { ticks: { color: c.text }, grid: { color: c.grid } },
          y: {
            ticks: {
              color: c.text,
              callback: v => _moneyCompactMillions(v, moneySymbol),
            },
            grid: { color: c.grid },
          },
        },
      },
    });
  }
}

/* ── Sensitivity heatmap ─────────────────────────────────── */
function colourSensitivityTable() {
  const cells = document.querySelectorAll('#sensitivityTable td[data-val]');
  if (!cells.length) return;

  const vals = Array.from(cells)
    .map(c => parseFloat(c.dataset.val))
    .filter(v => !isNaN(v));

  const min = Math.min(...vals);
  const max = Math.max(...vals);

  cells.forEach(cell => {
    const v = parseFloat(cell.dataset.val);
    if (isNaN(v)) return;
    const pct = (v - min) / (max - min);
    let r, g, b;
    if (pct < 0.5) {
      const t = pct * 2;
      r = Math.round(239 + t * (245 - 239));
      g = Math.round(68  + t * (158 - 68));
      b = Math.round(68  + t * (11  - 68));
    } else {
      const t = (pct - 0.5) * 2;
      r = Math.round(245 + t * (16  - 245));
      g = Math.round(158 + t * (185 - 158));
      b = Math.round(11  + t * (129 - 11));
    }
    cell.style.backgroundColor = `rgba(${r},${g},${b},0.25)`;
    cell.style.color            = `rgb(${r},${g},${b})`;
  });
}

/* ── Assumption mode toggle ──────────────────────────────── */
function toggleMode(idx) {
  const badge   = document.getElementById('modebadge-' + idx);
  const wrap    = document.getElementById('active-' + idx);
  const input   = document.getElementById('input-' + idx);
  if (!wrap || !input) return;

  const staticSpan = wrap.querySelector('span');
  const isManual   = badge.textContent === 'AUTO';

  if (isManual) {
    badge.textContent = 'MANUAL';
    badge.className   = 'mode-badge MANUAL';
    if (staticSpan) staticSpan.style.display = 'none';
    input.style.display = 'inline-block';
    input.focus();
  } else {
    badge.textContent = 'AUTO';
    badge.className   = 'mode-badge AUTO';
    input.style.display = 'none';
    if (staticSpan) { staticSpan.style.display = ''; staticSpan.textContent = input.value + input.dataset.unit; }
  }
}

/* ── Recompute model via AJAX ────────────────────────────── */
function recomputeModel() {
  const raw    = document.getElementById('chartData');
  const baseData = raw ? JSON.parse(raw.textContent) : { ticker: 'NKE' };
  const ticker = baseData.ticker;
  const baseMoneySymbol = baseData.display_currency_symbol || baseData.currency_symbol || '$';

  const overrides = {};
  document.querySelectorAll('.assumption-input').forEach(inp => {
    const badge = inp.closest('[id^="assump-"]')
      ? inp.closest('[id^="assump-"]').querySelector('.mode-badge')
      : null;
    if (badge && badge.textContent === 'MANUAL') {
      const driver = inp.dataset.driver || '';
      const keyMap = {
        'wacc': 'wacc', 'terminal_growth': 'g',
        'revenue_growth_near': 'revenue_growth_near',
        'ebit_margin_target': 'ebit_margin_target',
        'da': 'da_pct', 'capex': 'capex_pct', 'sbc': 'sbc_pct',
        'tax_rate': 'tax_rate', 'beta': 'beta',
      };
      for (const [k, v] of Object.entries(keyMap)) {
        if (driver.startsWith(k)) {
          overrides[v] = parseFloat(inp.value);
          break;
        }
      }
    }
  });

  const status = document.getElementById('recomputeStatus');
  if (status) status.textContent = 'Recomputing…';

  fetch('/api/recompute', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ ticker, overrides }),
  })
  .then(r => r.json())
  .then(d => {
    const moneySymbol = d.display_currency_symbol || baseMoneySymbol;
    _updateEl('kpiIV',        _money(d.intrinsic_value, 2, moneySymbol));
    _updateEl('kpiUpside',    (d.upside_pct >= 0 ? '+' : '') + d.upside_pct.toFixed(1) + '%');
    _updateEl('kpiRec',       `<span class="rec-pill ${d.recommendation_class}">${d.recommendation}</span>`);
    _updateEl('dcfWacc',      d.wacc + '%');
    _updateEl('dcfWaccKpi',   d.wacc + '%');
    _updateEl('dcfG',         d.terminal_growth + '%');
    _updateEl('dcfTvPct',     d.tv_pct.toFixed(1) + '%');
    _updateEl('dcfTvPctKpi',  d.tv_pct.toFixed(1) + '%');
    _updateEl('bridgePvTV',   _moneyCompactMillions(d.pv_terminal, moneySymbol));
    _updateEl('bridgeTvPct',  d.tv_pct.toFixed(1) + '% of EV');
    _updateEl('bridgeEV',     _moneyCompactMillions(d.enterprise_value, moneySymbol));
    _updateEl('bridgeEquity', _moneyCompactMillions(d.equity_value, moneySymbol));
    _updateEl('bridgeIV',     _money(d.intrinsic_value, 2, moneySymbol));
    if (status) status.textContent = '✓ Updated at ' + new Date().toLocaleTimeString();
    colourSensitivityTable();
  })
  .catch(err => {
    if (status) status.textContent = '✗ Error: ' + err.message;
  });
}

function resetAssumptions() {
  document.querySelectorAll('.assumption-input').forEach((inp, idx) => {
    const badge = document.getElementById('modebadge-' + idx);
    if (badge && badge.textContent === 'MANUAL') {
      toggleMode(idx);
    }
  });
}

/* ── Helpers ─────────────────────────────────────────────── */
function _updateEl(id, html) {
  const el = document.getElementById(id);
  if (el) el.innerHTML = html;
}

function _fmt(n) {
  return typeof n === 'number' ? n.toLocaleString('en-US', { maximumFractionDigits: 0 }) : n;
}

function _dashboardSymbolPayload() {
  const raw = document.getElementById('chartData');
  if (!raw) return null;
  const data = JSON.parse(raw.textContent);
  return {
    ticker: data.ticker,
    company_name: data.company_name,
    exchange: data.exchange,
    sector: data.sector,
    industry: data.industry,
  };
}

const _DEVICE_WATCHLIST_KEY = 'nelix-device-watchlist-v1';
const _DEVICE_MANUAL_COMPARE_KEY = 'nelix-device-manual-compare-v1';
const _DEVICE_WATCHLIST_LIMIT = 30;
const _DEVICE_MANUAL_COMPARE_LIMIT = 48;

function _storageAvailable() {
  try {
    localStorage.setItem('__nelix_probe__', '1');
    localStorage.removeItem('__nelix_probe__');
    return true;
  } catch (_error) {
    return false;
  }
}

function _readDeviceStore(key, fallback) {
  if (!_storageAvailable()) return fallback;
  try {
    const raw = localStorage.getItem(key);
    if (!raw) return fallback;
    const parsed = JSON.parse(raw);
    return Array.isArray(fallback) ? (Array.isArray(parsed) ? parsed : fallback) : (parsed || fallback);
  } catch (_error) {
    return fallback;
  }
}

function _writeDeviceStore(key, value) {
  if (!_storageAvailable()) return false;
  try {
    localStorage.setItem(key, JSON.stringify(value));
    return true;
  } catch (_error) {
    return false;
  }
}

function _nowIso() {
  return new Date().toISOString();
}

function _tickerKey(value) {
  return String(value || '').trim().toUpperCase();
}

function _manualCompareEventId(subjectTicker, peerTicker) {
  return `device:${_tickerKey(subjectTicker)}:${_tickerKey(peerTicker)}`;
}

function _normaliseWatchlistItem(item) {
  const payload = item || {};
  const ticker = _tickerKey(payload.ticker || payload.symbol || payload.code);
  if (!ticker) return null;
  const timestamp = String(payload.last_touched_at || payload.added_at || _nowIso());
  return {
    ticker,
    company_name: String(payload.company_name || payload.name || '').trim(),
    exchange: String(payload.exchange || '').trim().toUpperCase(),
    country: String(payload.country || '').trim(),
    sector: String(payload.sector || '').trim(),
    industry: String(payload.industry || '').trim(),
    added_at: String(payload.added_at || timestamp),
    last_touched_at: timestamp,
  };
}

function _deviceWatchlistItems() {
  return _readDeviceStore(_DEVICE_WATCHLIST_KEY, [])
    .map(_normaliseWatchlistItem)
    .filter(Boolean)
    .sort((left, right) => String(right.last_touched_at || '').localeCompare(String(left.last_touched_at || '')))
    .slice(0, _DEVICE_WATCHLIST_LIMIT);
}

function _saveDeviceWatchlistItems(items) {
  const byTicker = new Map();
  (items || []).forEach(item => {
    const normalised = _normaliseWatchlistItem(item);
    if (!normalised) return;
    const current = byTicker.get(normalised.ticker);
    if (!current || String(normalised.last_touched_at || '').localeCompare(String(current.last_touched_at || '')) >= 0) {
      byTicker.set(normalised.ticker, normalised);
    }
  });
  const ordered = Array.from(byTicker.values())
    .sort((left, right) => String(right.last_touched_at || '').localeCompare(String(left.last_touched_at || '')))
    .slice(0, _DEVICE_WATCHLIST_LIMIT);
  _writeDeviceStore(_DEVICE_WATCHLIST_KEY, ordered);
  return ordered;
}

function _upsertDeviceWatchlistItem(item) {
  const normalised = _normaliseWatchlistItem(item);
  if (!normalised) return null;
  const current = _deviceWatchlistItems();
  const existing = current.find(entry => entry.ticker === normalised.ticker);
  normalised.added_at = existing ? existing.added_at : (normalised.added_at || _nowIso());
  normalised.last_touched_at = _nowIso();
  _saveDeviceWatchlistItems([normalised, ...current.filter(entry => entry.ticker !== normalised.ticker)]);
  return normalised;
}

function _removeDeviceWatchlistItem(ticker) {
  const tickerText = _tickerKey(ticker);
  _saveDeviceWatchlistItems(_deviceWatchlistItems().filter(item => item.ticker !== tickerText));
}

function _mergeWatchlistItems(serverItems, deviceItems) {
  return _saveDeviceWatchlistItems([...(serverItems || []), ...(deviceItems || [])]);
}

function _normaliseManualCompareItem(item, subjectHint) {
  const payload = item || {};
  const ticker = _tickerKey(payload.ticker || payload.peer_ticker || payload.symbol || payload.code);
  const subjectTicker = _tickerKey(payload.subject_ticker || subjectHint || (payload.subject || {}).ticker);
  if (!ticker || !subjectTicker) return null;
  const eventId = String(payload.event_id || _manualCompareEventId(subjectTicker, ticker)).trim();
  return {
    event_id: eventId,
    ticker,
    company_name: String(payload.company_name || payload.name || '').trim(),
    exchange: String(payload.exchange || '').trim().toUpperCase(),
    sector: String(payload.sector || '').trim(),
    industry: String(payload.industry || '').trim(),
    subject_ticker: subjectTicker,
    subject_company_name: String(payload.subject_company_name || (payload.subject || {}).company_name || '').trim(),
    subject_exchange: String(payload.subject_exchange || (payload.subject || {}).exchange || '').trim().toUpperCase(),
    subject_sector: String(payload.subject_sector || (payload.subject || {}).sector || '').trim(),
    subject_industry: String(payload.subject_industry || (payload.subject || {}).industry || '').trim(),
    created_at: String(payload.created_at || _nowIso()),
  };
}

function _deviceManualCompareItems() {
  return _readDeviceStore(_DEVICE_MANUAL_COMPARE_KEY, [])
    .map(item => _normaliseManualCompareItem(item))
    .filter(Boolean)
    .sort((left, right) => String(right.created_at || '').localeCompare(String(left.created_at || '')))
    .slice(0, _DEVICE_MANUAL_COMPARE_LIMIT);
}

function _saveDeviceManualCompareItems(items) {
  const byEvent = new Map();
  (items || []).forEach(item => {
    const normalised = _normaliseManualCompareItem(item);
    if (!normalised) return;
    const current = byEvent.get(normalised.event_id);
    if (!current || String(normalised.created_at || '').localeCompare(String(current.created_at || '')) >= 0) {
      byEvent.set(normalised.event_id, normalised);
    }
  });
  const ordered = Array.from(byEvent.values())
    .sort((left, right) => String(right.created_at || '').localeCompare(String(left.created_at || '')))
    .slice(0, _DEVICE_MANUAL_COMPARE_LIMIT);
  _writeDeviceStore(_DEVICE_MANUAL_COMPARE_KEY, ordered);
  return ordered;
}

function _upsertDeviceManualCompareItem(subject, peer) {
  const subjectTicker = _tickerKey((subject || {}).ticker);
  const peerTicker = _tickerKey((peer || {}).ticker || (peer || {}).symbol || (peer || {}).code);
  if (!subjectTicker || !peerTicker) return null;
  const current = _deviceManualCompareItems();
  const eventId = _manualCompareEventId(subjectTicker, peerTicker);
  const normalised = _normaliseManualCompareItem({
    event_id: eventId,
    ticker: peerTicker,
    company_name: (peer || {}).company_name || (peer || {}).name || '',
    exchange: (peer || {}).exchange || '',
    sector: (peer || {}).sector || '',
    industry: (peer || {}).industry || '',
    subject_ticker: subjectTicker,
    subject_company_name: (subject || {}).company_name || '',
    subject_exchange: (subject || {}).exchange || '',
    subject_sector: (subject || {}).sector || '',
    subject_industry: (subject || {}).industry || '',
    created_at: _nowIso(),
  });
  if (!normalised) return null;
  const remaining = current.filter(item => item.event_id !== normalised.event_id);
  _saveDeviceManualCompareItems([normalised, ...remaining]);
  return normalised;
}

function _manualComparesForSubject(subjectTicker) {
  const subjectKey = _tickerKey(subjectTicker);
  return _deviceManualCompareItems()
    .filter(item => item.subject_ticker === subjectKey)
    .sort((left, right) => String(right.created_at || '').localeCompare(String(left.created_at || '')));
}

function _mergeManualCompareItems(subjectTicker, serverItems, deviceItems) {
  const subjectKey = _tickerKey(subjectTicker);
  const saved = _saveDeviceManualCompareItems([...(serverItems || []).map(item => ({
    ...item,
    subject_ticker: item.subject_ticker || subjectKey,
  })), ...(deviceItems || [])]);
  return saved.filter(item => item.subject_ticker === subjectKey);
}

async function _fetchJson(url, options) {
  try {
    const response = await fetch(url, options);
    const payload = await response.json();
    return { ok: response.ok, payload };
  } catch (_error) {
    return { ok: false, payload: null };
  }
}

async function _syncDeviceWatchlist(serverItems, deviceItems) {
  const serverTickers = new Set((serverItems || []).map(item => _tickerKey(item.ticker)));
  const missing = (deviceItems || []).filter(item => !serverTickers.has(_tickerKey(item.ticker)));
  for (const item of missing) {
    await _fetchJson('/api/watchlist', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ...item, _device_sync_mode: 'client-replay' }),
    });
  }
}

async function _syncDeviceManualCompares(subjectTicker, serverItems, deviceItems) {
  const subjectKey = _tickerKey(subjectTicker);
  const serverEventIds = new Set((serverItems || []).map(item => String(item.event_id || '').trim()).filter(Boolean));
  const missing = (deviceItems || []).filter(item => item.subject_ticker === subjectKey && !serverEventIds.has(String(item.event_id || '').trim()));
  for (const item of missing) {
    await _fetchJson('/api/manual-compare', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        event_id: item.event_id,
        subject: {
          ticker: item.subject_ticker,
          company_name: item.subject_company_name,
          exchange: item.subject_exchange,
          sector: item.subject_sector,
          industry: item.subject_industry,
        },
        peer: {
          ticker: item.ticker,
          company_name: item.company_name,
          exchange: item.exchange,
          sector: item.sector,
          industry: item.industry,
        },
      }),
    });
  }
}

function _watchlistPanelHtml(items) {
  if (!Array.isArray(items) || !items.length) {
    return '<div style="font-size:.74rem;color:var(--text-secondary);">No symbols pinned yet.</div>';
  }
  return items.map(item => `
    <div style="display:flex;justify-content:space-between;gap:.6rem;align-items:center;padding:.55rem .65rem;border:1px solid var(--border);border-radius:10px;background:var(--bg-secondary);">
      <div>
        <div style="font-size:.76rem;font-weight:700;color:var(--text-primary);">${item.company_name || item.ticker}</div>
        <div style="font-size:.7rem;color:var(--text-secondary);margin-top:.14rem;">${item.ticker}${item.exchange ? ' · ' + item.exchange : ''}${item.sector ? ' · ' + item.sector : ''}</div>
      </div>
      <div style="display:flex;gap:.35rem;flex-wrap:wrap;justify-content:flex-end;">
        <a class="btn btn-outline btn--sm" href="/dashboard/${encodeURIComponent(item.ticker)}">Open</a>
        <button class="btn btn--ghost btn--sm" onclick="removeWatchlistItem('${item.ticker}')">Remove</button>
      </div>
    </div>
  `).join('');
}

function _manualComparePanelHtml(items) {
  if (!Array.isArray(items) || !items.length) {
    return '<div style="font-size:.74rem;color:var(--text-secondary);">No manual compare activity yet.</div>';
  }
  return items.map(item => `
    <div style="display:flex;justify-content:space-between;gap:.6rem;align-items:center;padding:.55rem .65rem;border:1px solid var(--border);border-radius:10px;background:var(--bg-secondary);">
      <div>
        <div style="font-size:.76rem;font-weight:700;color:var(--text-primary);">${item.company_name || item.ticker}</div>
        <div style="font-size:.7rem;color:var(--text-secondary);margin-top:.14rem;">${item.ticker}${item.exchange ? ' · ' + item.exchange : ''}${item.sector ? ' · ' + item.sector : ''}</div>
      </div>
      <a class="btn btn-outline btn--sm" href="/dashboard/${encodeURIComponent(item.ticker)}">Open</a>
    </div>
  `).join('');
}

async function loadWatchlistPanels() {
  const panels = Array.from(document.querySelectorAll('[data-watchlist-panel]'));
  if (!panels.length) return;
  const deviceItems = _deviceWatchlistItems();
  const response = await _fetchJson('/api/watchlist', { headers: { 'Accept': 'application/json' } });
  const serverItems = Array.isArray((response.payload || {}).items) ? response.payload.items : [];
  const mergedItems = _mergeWatchlistItems(serverItems, deviceItems);
  const html = _watchlistPanelHtml(mergedItems || []);
  panels.forEach(panel => {
    panel.innerHTML = html;
  });
  if (response.ok) {
    _syncDeviceWatchlist(serverItems, mergedItems).catch(() => {});
  }
}

async function addWatchlistFromButton(button) {
  if (!button) return;
  const payload = JSON.parse(button.dataset.watchlistItem || '{}');
  const saved = _upsertDeviceWatchlistItem(payload);
  if (!saved) return;
  button.disabled = true;
  const previous = button.textContent;
  button.textContent = 'Tracking…';
  try {
    await _fetchJson('/api/watchlist', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(saved),
    });
    button.textContent = 'Tracked';
    await loadWatchlistPanels();
  } catch (_error) {
    button.textContent = previous;
  } finally {
    button.disabled = false;
  }
}

async function removeWatchlistItem(ticker) {
  _removeDeviceWatchlistItem(ticker);
  await _fetchJson(`/api/watchlist/${encodeURIComponent(ticker)}`, { method: 'DELETE' });
  await loadWatchlistPanels();
}

async function loadManualComparePanel() {
  const panel = document.querySelector('[data-manual-compare-panel]');
  const subject = _dashboardSymbolPayload();
  if (!panel || !subject || !subject.ticker) return;
  const deviceItems = _manualComparesForSubject(subject.ticker);
  const response = await _fetchJson(`/api/manual-compare?subject=${encodeURIComponent(subject.ticker)}`, {
    headers: { 'Accept': 'application/json' },
  });
  const serverItems = Array.isArray((response.payload || {}).items) ? response.payload.items : [];
  const mergedItems = _mergeManualCompareItems(subject.ticker, serverItems, deviceItems);
  panel.innerHTML = _manualComparePanelHtml(mergedItems || []);
  if (response.ok) {
    _syncDeviceManualCompares(subject.ticker, serverItems, mergedItems).catch(() => {});
  }
}

async function recordManualCompare(button) {
  if (!button) return;
  const subject = _dashboardSymbolPayload();
  if (!subject || !subject.ticker) return;
  const peer = JSON.parse(button.dataset.comparePeer || '{}');
  const deviceItem = _upsertDeviceManualCompareItem(subject, peer);
  if (!deviceItem) return;
  button.disabled = true;
  const previous = button.textContent;
  button.textContent = 'Learning…';
  try {
    await _fetchJson('/api/manual-compare', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ event_id: deviceItem.event_id, subject, peer }),
    });
    button.textContent = 'Queued';
    await loadManualComparePanel();
  } catch (_error) {
    button.textContent = previous;
  } finally {
    button.disabled = false;
  }
}

function initDiscoveryUi() {
  loadWatchlistPanels().catch(() => {});
  loadManualComparePanel().catch(() => {});
}

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', initDiscoveryUi);
} else {
  initDiscoveryUi();
}
