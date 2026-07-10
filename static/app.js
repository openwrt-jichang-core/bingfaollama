const hostForm = document.getElementById('hostForm');
const hostInput = document.getElementById('hostInput');
const hostList = document.getElementById('hostList');
const hostEmpty = document.getElementById('hostEmpty');

const startBtn = document.getElementById('startBtn');
const stopBtn = document.getElementById('stopBtn');
const statusPill = document.getElementById('statusPill');
const logEl = document.getElementById('log');
const resultsBody = document.getElementById('resultsBody');
const radar = document.getElementById('radar');

const concurrencyRange = document.getElementById('concurrencyRange');
const concurrencyNumber = document.getElementById('concurrencyNumber');
const concurrencyValue = document.getElementById('concurrencyValue');

let pollTimer = null;
let lastSeq = 0;
let wasRunning = false;

// ---------- Concurrency control ----------

function clampConcurrency(v) {
  v = parseInt(v, 10);
  if (isNaN(v)) v = 3;
  return Math.min(100, Math.max(1, v));
}

function setConcurrency(v) {
  v = clampConcurrency(v);
  concurrencyRange.value = v;
  concurrencyNumber.value = v;
  concurrencyValue.textContent = v;
  try { localStorage.setItem('ollama-scanner-concurrency', String(v)); } catch (e) {}
}

concurrencyRange.addEventListener('input', () => setConcurrency(concurrencyRange.value));
concurrencyNumber.addEventListener('input', () => setConcurrency(concurrencyNumber.value));

(function initConcurrency() {
  let saved = 3;
  try {
    const stored = localStorage.getItem('ollama-scanner-concurrency');
    if (stored) saved = clampConcurrency(stored);
  } catch (e) {}
  setConcurrency(saved);
})();

// ---------- Hosts ----------

async function fetchHosts() {
  const res = await fetch('/api/hosts');
  const hosts = await res.json();
  renderHosts(hosts);
}

function renderHosts(hosts) {
  hostList.innerHTML = '';
  hostEmpty.style.display = hosts.length ? 'none' : 'block';
  hosts.forEach((host) => {
    const { url, enabled, favorite } = host;
    const li = document.createElement('li');
    li.className = `host-item${enabled ? '' : ' host-item--disabled'}`;
    li.innerHTML = `
      <button class="host-item__star${favorite ? ' is-lit' : ''}" title="${favorite ? '取消收藏' : '收藏此地址'}" aria-pressed="${favorite}">★</button>
      <span class="host-item__url">${escapeHtml(url)}</span>
      <label class="host-item__toggle" title="${enabled ? '启用中，参与扫描' : '已禁用，不参与扫描'}">
        <input type="checkbox" ${enabled ? 'checked' : ''} aria-label="启用 ${escapeHtml(url)}" />
        <span class="host-item__toggle-track"><span class="host-item__toggle-thumb"></span></span>
      </label>
      <button class="host-item__remove" title="移除" aria-label="移除 ${escapeHtml(url)}">×</button>
    `;
    li.querySelector('.host-item__star').addEventListener('click', () => patchHost(url, { favorite: !favorite }));
    li.querySelector('.host-item__toggle input').addEventListener('change', (e) => patchHost(url, { enabled: e.target.checked }));
    li.querySelector('.host-item__remove').addEventListener('click', () => removeHost(url));
    hostList.appendChild(li);
  });
}

async function patchHost(url, changes) {
  const res = await fetch('/api/hosts', {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ url, ...changes }),
  });
  if (res.ok) {
    const hosts = await res.json();
    renderHosts(hosts);
  }
}

async function addHost(url) {
  const res = await fetch('/api/hosts', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ url }),
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    alert(err.detail || '添加失败');
    return;
  }
  const hosts = await res.json();
  renderHosts(hosts);
}

async function removeHost(url) {
  const res = await fetch('/api/hosts', {
    method: 'DELETE',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ url }),
  });
  if (res.ok) {
    const hosts = await res.json();
    renderHosts(hosts);
  }
}

hostForm.addEventListener('submit', (e) => {
  e.preventDefault();
  const val = hostInput.value.trim();
  if (!val) return;
  addHost(val);
  hostInput.value = '';
});

// ---------- Scan control ----------

async function startScan() {
  const concurrency = clampConcurrency(concurrencyNumber.value);
  const res = await fetch('/api/scan/start', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ concurrency }),
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    alert(err.detail || '无法开始扫描');
    return;
  }
  logEl.innerHTML = '';
  lastSeq = 0;
  setRunningUI(true);
  poll();
}

async function stopScan() {
  await fetch('/api/scan/stop', { method: 'POST' });
  setStatusPill('stopping', '停止中…');
  stopBtn.disabled = true;
}

startBtn.addEventListener('click', startScan);
stopBtn.addEventListener('click', stopScan);

function setRunningUI(running) {
  startBtn.disabled = running;
  stopBtn.disabled = !running;
  concurrencyRange.disabled = running;
  concurrencyNumber.disabled = running;
  radar.classList.toggle('is-active', running);
  if (running) setStatusPill('running', '扫描中…');
}

function setStatusPill(kind, text) {
  statusPill.className = `status-pill status-pill--${kind}`;
  statusPill.textContent = text;
}

// ---------- Polling ----------

async function poll() {
  clearTimeout(pollTimer);
  try {
    const res = await fetch(`/api/scan/status?since=${lastSeq}`);
    const data = await res.json();

    appendLogs(data.logs);
    setRunningUI(data.running);

    if (!data.running) {
      if (data.results) {
        setStatusPill('done', '已完成');
        renderResults(data.results);
      } else if (wasRunning) {
        setStatusPill('idle', '待机');
      }
    }
    wasRunning = data.running;

    if (data.running) {
      pollTimer = setTimeout(poll, 1200);
    }
  } catch (e) {
    pollTimer = setTimeout(poll, 2000);
  }
}

function appendLogs(logs) {
  if (!logs || !logs.length) return;
  const atBottom = logEl.scrollHeight - logEl.scrollTop - logEl.clientHeight < 40;
  logs.forEach((l) => {
    lastSeq = Math.max(lastSeq, l.seq);
    const div = document.createElement('div');
    div.className = 'log__line';
    div.innerHTML = `<span class="log__ts">${l.ts}</span>${escapeHtml(l.text)}`;
    logEl.appendChild(div);
  });
  if (atBottom) logEl.scrollTop = logEl.scrollHeight;
}

// ---------- Results ----------

function renderResults(results) {
  if (!results || !results.advanced || Object.keys(results.advanced).length === 0) {
    resultsBody.innerHTML = '<p class="results-empty">本次扫描没有可用模型进入高级测试阶段。</p>';
    return;
  }

  resultsBody.innerHTML = '';
  Object.entries(results.advanced).forEach(([key, tests]) => {
    const [host, model] = key.split('|');
    const passCount = tests.filter((t) => t.status === 'PASS').length;
    const allPass = tests.length > 0 && passCount === tests.length;

    const card = document.createElement('div');
    card.className = 'result-card';
    card.innerHTML = `
      <div class="result-card__head">
        <span>${escapeHtml(model)} <span style="color:var(--dim)">@ ${escapeHtml(host)}</span></span>
        <span class="result-card__badge ${allPass ? 'result-card__badge--pass' : 'result-card__badge--fail'}">
          ${passCount}/${tests.length} 通过
        </span>
      </div>
      <div class="result-card__tests">
        ${tests.map((t) => `
          <div class="result-test">
            <span>${escapeHtml(t.test)}</span>
            <span class="result-test__status--${t.status}">${t.status} (${t.elapsed.toFixed(1)}s)</span>
          </div>
        `).join('')}
      </div>
    `;
    resultsBody.appendChild(card);
  });
}

function escapeHtml(str) {
  const div = document.createElement('div');
  div.textContent = str;
  return div.innerHTML;
}

// ---------- Init ----------

async function init() {
  await fetchHosts();
  try {
    const res = await fetch('/api/scan/status?since=0');
    const data = await res.json();
    appendLogs(data.logs);
    setRunningUI(data.running);
    if (data.results) renderResults(data.results);
    if (data.running) poll();
  } catch (e) {
    // backend not reachable yet
  }
}

init();
