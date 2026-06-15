let activeScanner = 'ema';
let latestResults = { ema: [], volume: [] };
let scanInProgress = false;
let activeAbortController = null;

const $ = (id) => document.getElementById(id);

function applyTheme(mode) {
  const isLight = mode === 'light';
  document.body.classList.toggle('lm', isLight);
  const icon = $('themeIcon');
  const label = $('themeLabel');
  if (icon) icon.textContent = isLight ? '☀️' : '🌙';
  if (label) label.textContent = isLight ? 'Light' : 'Dark';
  localStorage.setItem('scannerTheme', isLight ? 'light' : 'dark');
}

function toggleTheme() {
  const isLight = document.body.classList.contains('lm');
  applyTheme(isLight ? 'dark' : 'light');
}


function setStatus(message) {
  $('status').textContent = message;
}

function setProgress({ percent = 0, symbol = '-', scanned = 0, total = 0, matches = 0 } = {}) {
  const safePercent = Math.max(0, Math.min(100, Number(percent) || 0));
  $('scanProgressBar').style.width = `${safePercent}%`;
  $('progressText').textContent = `${safePercent.toFixed(safePercent % 1 ? 1 : 0)}%`;
  $('currentTicker').textContent = `Ticker: ${symbol || '-'}`;
  $('scanCounts').textContent = `Scanned: ${scanned || 0} / ${total || 0} | Matches: ${matches || 0}`;
}

function tradingViewUrl(symbol, interval) {
  const clean = String(symbol || '').replace('.NS', '').replace('.BO', '').replace('NSE:', '');
  return `https://www.tradingview.com/chart/?symbol=${encodeURIComponent(clean)}&interval=${interval}`;
}

async function initSheets() {
  try {
    const res = await fetch('/screener/api/sheets');
    const data = await res.json();
    $('sheetSelect').innerHTML = '';
    (data.sheets || []).forEach((name) => {
      const opt = document.createElement('option');
      opt.value = name;
      opt.textContent = name;
      $('sheetSelect').appendChild(opt);
    });
    if ((data.sheets || []).length) await loadSymbols();
  } catch (err) {
    setStatus(`Failed to load sheet names: ${err.message}`);
  }
}

async function loadSymbols() {
  const sheet = $('sheetSelect').value;
  if (!sheet) return;
  setStatus(`Loading symbols from ${sheet}...`);
  try {
    const res = await fetch(`/screener/api/symbols?sheet=${encodeURIComponent(sheet)}`);
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || 'Unable to load symbols');
    $('symbolCount').textContent = data.count;
    setProgress({ percent: 0, symbol: '-', scanned: 0, total: data.count, matches: 0 });
    setStatus(`Loaded ${data.count} symbols from ${sheet}.`);
  } catch (err) {
    setStatus(`Error: ${err.message}`);
  }
}

function getEmaConfig() {
  return {
    timeframe: $('emaTimeframe').value,
    lookback_days: Number($('emaLookback').value || 20),
    ema1: Number($('ema1').value || 9),
    ema2: Number($('ema2').value || 18),
    ema3: Number($('ema3').value || 27),
  };
}

function getVolumeConfig() {
  return {
    interval: $('volInterval').value,
    volume_threshold: Number($('volThreshold').value || 2),
    price_threshold: Number($('priceThreshold').value || 3),
    min_price: Number($('minPrice').value || 100),
    rsi_threshold: Number($('rsiThreshold').value || 55),
    rsi_length: Number($('rsiLength').value || 14),
  };
}

function clearScannerResults(scanner = activeScanner) {
  latestResults[scanner] = [];
  const tableId = scanner === 'ema' ? 'emaResults' : 'volumeResults';
  $(tableId).querySelector('tbody').innerHTML = '';
  if (!scanInProgress) {
    setProgress({ percent: 0, symbol: '-', scanned: 0, total: 0, matches: 0 });
    setStatus(`${scanner.toUpperCase()} results cleared.`);
  }
}

function numberValue(value) {
  const n = Number(String(value ?? '').replace('%', ''));
  return Number.isFinite(n) ? n : 0;
}

function appendEmaResult(row) {
  latestResults.ema.push(row);
  const tbody = $('emaResults').querySelector('tbody');
  const tr = document.createElement('tr');
  const interval = $('emaTimeframe').value === 'Weekly' ? 'W' : ($('emaTimeframe').value === 'Daily' ? 'D' : '60');
  const ema1Diff = numberValue(row.ema1_diff_pct);
  const ema2Diff = numberValue(row.ema2_diff_pct);
  const ema3Diff = numberValue(row.ema3_diff_pct);

  if (ema1Diff > 3 && ema2Diff > 3 && ema3Diff > 3) {
    tr.classList.add('ema-strong-row');
  }

  tr.innerHTML = `
    <td>${row.symbol}</td>
    <td>${row.current_price}</td>
    <td>${row.rsi14}</td>
    <td>${row.ema1_diff_pct}%</td>
    <td>${row.ema2_diff_pct}%</td>
    <td>${row.ema3_diff_pct}%</td>
    <td><a class="chart-link" href="${tradingViewUrl(row.symbol, interval)}" target="_blank" rel="noopener">Open</a></td>
  `;
  tbody.appendChild(tr);
}

function volumeRowClass(bbPosition) {
  const pos = String(bbPosition || '').trim();
  if (['Upper Band', 'Above Band', 'At Upper'].includes(pos)) return 'bb-upper-row';
  if (['Above Mid', 'Mid Band', 'Below Mid', 'At Middle'].includes(pos)) return 'bb-mid-row';
  if (['Lower Band', 'Below Band', 'At Lower'].includes(pos)) return 'bb-lower-row';
  return '';
}

function appendVolumeResult(row) {
  latestResults.volume.push(row);
  const tbody = $('volumeResults').querySelector('tbody');
  const interval = $('volInterval').value === '1d' ? 'D' : '60';
  const tr = document.createElement('tr');
  const rowClass = volumeRowClass(row.bb_position);
  if (rowClass) tr.classList.add(rowClass);
  tr.innerHTML = `
    <td>${row.symbol}</td>
    <td>${Number(row.prev_5_vol || 0).toLocaleString()}</td>
    <td>${Number(row.curr_5_vol || 0).toLocaleString()}</td>
    <td>${row.current_price}</td>
    <td>${row.volume_ratio}</td>
    <td>${row.price_change_pct}%</td>
    <td>${row.rsi}</td>
    <td>${row.bb_position}</td>
    <td><a class="chart-link" href="${tradingViewUrl(row.symbol, interval)}" target="_blank" rel="noopener">Open</a></td>
  `;
  tbody.appendChild(tr);
}

function setScanControls(running) {
  scanInProgress = running;
  document.querySelectorAll('.run-btn').forEach((btn) => btn.disabled = running);
  document.querySelectorAll('.stop-btn').forEach((btn) => btn.disabled = !running);
  $('loadSymbolsBtn').disabled = running;
  $('sheetSelect').disabled = running;
}

function stopScan() {
  if (!scanInProgress || !activeAbortController) return;
  activeAbortController.abort();
  setStatus('Stopping scan...');
}

async function runScan(scanner) {
  if (scanInProgress) return;
  const sheet = $('sheetSelect').value;
  if (!sheet) {
    setStatus('Select a sheet first.');
    return;
  }

  const config = scanner === 'ema' ? getEmaConfig() : getVolumeConfig();
  clearScannerResults(scanner);
  setProgress({ percent: 0, symbol: '-', scanned: 0, total: 0, matches: 0 });
  setStatus(`Starting ${scanner.toUpperCase()} scan on ${sheet}...`);

  activeAbortController = new AbortController();
  setScanControls(true);

  try {
    const res = await fetch(`/screener/api/scan_stream/${scanner}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ sheet, config }),
      signal: activeAbortController.signal,
    });
    if (!res.ok) {
      let data = {};
      try { data = await res.json(); } catch (_) {}
      throw new Error(data.error || 'Scan failed');
    }

    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';

    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const lines = buffer.split('\n');
      buffer = lines.pop() || '';

      for (const line of lines) {
        if (!line.trim()) continue;
        const event = JSON.parse(line);

        if (event.type === 'start') {
          setProgress(event);
          setStatus(`Scanning ${event.total} symbols from ${event.sheet}...`);
        } else if (event.type === 'progress') {
          setProgress(event);
          setStatus(event.message || `Scanning ${event.symbol}...`);
        } else if (event.type === 'result') {
          if (scanner === 'ema') appendEmaResult(event.row);
          else appendVolumeResult(event.row);
          setProgress(event);
          setStatus(`Match found: ${event.row.symbol}. Continuing scan...`);
        } else if (event.type === 'done') {
          setProgress(event);
          const errText = event.errors?.length ? ` ${event.errors.length} symbol errors captured.` : '';
          setStatus(`${event.message}${errText}`);
        }
      }
    }
  } catch (err) {
    if (err.name === 'AbortError') {
      setStatus('Scan stopped by user. Existing results are kept.');
    } else {
      setStatus(`Error: ${err.message}`);
    }
  } finally {
    activeAbortController = null;
    setScanControls(false);
  }
}

function exportCsv() {
  const scanner = activeScanner;
  const rows = latestResults[scanner] || [];
  if (!rows.length) {
    setStatus('No active results to export.');
    return;
  }
  const headers = Object.keys(rows[0]);
  const csv = [headers.join(',')]
    .concat(rows.map((row) => headers.map((h) => JSON.stringify(row[h] ?? '')).join(',')))
    .join('\n');
  const blob = new Blob([csv], { type: 'text/csv;charset=utf-8;' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = `${scanner}_scanner_results.csv`;
  a.click();
  URL.revokeObjectURL(url);
}

function setupTabs() {
  document.querySelectorAll('.tab-btn').forEach((btn) => {
    btn.addEventListener('click', () => {
      document.querySelectorAll('.tab-btn').forEach((b) => b.classList.remove('active'));
      document.querySelectorAll('.tab-pane').forEach((p) => p.classList.remove('active'));
      btn.classList.add('active');
      $(btn.dataset.tab).classList.add('active');
      activeScanner = btn.dataset.tab === 'emaTab' ? 'ema' : 'volume';
      setStatus(`Active scanner: ${activeScanner.toUpperCase()}.`);
    });
  });
}

window.addEventListener('DOMContentLoaded', () => {
  applyTheme(localStorage.getItem('scannerTheme') || 'dark');
  const themeToggle = $('themeToggle');
  if (themeToggle) themeToggle.addEventListener('click', toggleTheme);
  setupTabs();
  initSheets();
  $('loadSymbolsBtn').addEventListener('click', loadSymbols);
  $('sheetSelect').addEventListener('change', loadSymbols);
  document.querySelectorAll('.run-btn').forEach((btn) => btn.addEventListener('click', () => runScan(btn.dataset.scanner)));
  document.querySelectorAll('.stop-btn').forEach((btn) => btn.addEventListener('click', stopScan));
  document.querySelectorAll('.clear-btn').forEach((btn) => btn.addEventListener('click', () => clearScannerResults(btn.dataset.scanner)));
  $('exportCsvBtn').addEventListener('click', exportCsv);
});
