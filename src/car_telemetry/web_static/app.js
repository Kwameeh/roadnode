const $ = (id) => document.getElementById(id);
let latest = {};
let signalCatalog = [];
let socket = null;
let reconnectTimer = null;
let fallbackTimer = null;
let reconnectAttempt = 0;
let lastLiveMessageAt = 0;
let heartbeatSeconds = 5;
let fallbackPollSeconds = 1;

function toast(message) {
  const el = $('toast');
  el.textContent = message;
  el.classList.add('show');
  setTimeout(() => el.classList.remove('show'), 3000);
}

function valueOfSignal(name) {
  const raw = latest?.obd?.signals?.[name]?.value;
  if (raw && typeof raw === 'object' && 'value' in raw) return raw.value;
  return raw ?? null;
}

function displayValue(name, suffix = '') {
  const value = valueOfSignal(name);
  if (value === null || value === undefined) return '--';
  return `${typeof value === 'number' ? Math.round(value * 10) / 10 : value}${suffix}`;
}

function badge(id, ok, warn = false) {
  const el = $(id);
  el.classList.toggle('ok', Boolean(ok));
  el.classList.toggle('warn', !ok && Boolean(warn));
}

function streamStatus(label, state) {
  const el = $('badge-live');
  el.textContent = label;
  el.classList.toggle('ok', state === 'live');
  el.classList.toggle('warn', state === 'reconnecting' || state === 'stale');
}

function engineIsFresh(data) {
  const web = data?._web;
  if (!web) return true;
  if (!web.engineConnected || !web.lastEngineUpdateAt) return false;
  return Date.now() - Number(web.lastEngineUpdateAt) * 1000 <= (heartbeatSeconds + 2) * 1000;
}

function fmtUptime(seconds) {
  if (!seconds) return '--';
  const hours = Math.floor(seconds / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  return `${hours}h ${minutes}m`;
}

function escapeHtml(value) {
  return String(value ?? '').replace(/[&<>'"]/g, (char) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', "'": '&#39;', '"': '&quot;',
  }[char]));
}

function renderState(data) {
  latest = data || {};
  const obd = latest.obd || {};
  const gps = latest.gps || {};
  const mqtt = latest.mqtt || {};
  const system = latest.system || {};
  const oled = latest.oled || {};
  const vehicle = obd.vehicle || {};
  const dtc = obd.dtc || {};

  $('speed').textContent = displayValue('SPEED');
  $('rpm').textContent = displayValue('RPM');
  $('coolant').textContent = displayValue('COOLANT_TEMP', ' °C');
  $('load').textContent = displayValue('ENGINE_LOAD', ' %');
  $('throttle').textContent = displayValue('THROTTLE_POS', ' %');
  $('fuel').textContent = displayValue('FUEL_LEVEL', ' %');
  $('voltage').textContent = displayValue('CONTROL_MODULE_VOLTAGE', ' V');
  $('dtc-count').textContent = dtc.storedCount ?? 0;
  $('vin').textContent = vehicle.VIN || 'Not available';
  $('protocol').textContent = obd.protocolName || vehicle.protocolName || '--';
  $('transport').textContent = obd.transport || '--';
  $('gps-detail').textContent = gps.validFix
    ? `${gps.latitude ?? '--'}, ${gps.longitude ?? '--'} · ${gps.satellites ?? '--'} sats`
    : gps.received ? 'Data, waiting for fix' : 'Waiting';
  $('vehicle-subtitle').textContent = vehicle.VIN
    ? `VIN ${vehicle.VIN}`
    : obd.connected ? 'Vehicle connected' : 'Waiting for vehicle…';

  badge('badge-gps', gps.validFix, gps.received);
  badge('badge-obd', obd.connected, obd.connecting);
  badge('badge-mqtt', mqtt.connected, mqtt.enabled);

  $('sys-cpu').textContent = system.cpuPercent == null ? '--' : `${system.cpuPercent}%`;
  $('sys-temp').textContent = system.temperatureC == null ? '--' : `${system.temperatureC} °C`;
  $('sys-ram').textContent = system.memoryAvailableMb == null ? '--' : `${system.memoryAvailableMb} MB`;
  $('sys-disk').textContent = system.diskFreeGb == null ? '--' : `${system.diskFreeGb} GB`;
  $('sys-host').textContent = system.hostname || '--';
  $('sys-ip').textContent = system.ipAddress || '--';
  $('sys-uptime').textContent = fmtUptime(system.uptimeSeconds);
  $('sys-agent').textContent = latest.agent || '--';
  $('mqtt-buffer').textContent = `${mqtt.bufferedMessages ?? 0} queued / ${mqtt.droppedMessages ?? 0} dropped`;
  $('oled-status').textContent = oled.error
    ? `Error: ${oled.error}`
    : `${oled.driver || '--'} / ${oled.page || 'idle'}`;
  renderDtc(dtc, obd.dtcEvents || []);
}

function renderDtc(dtc, events) {
  renderDtcList('stored-dtcs', dtc.stored || []);
  renderDtcList('current-dtcs', dtc.currentCycle || []);
  renderDtcList('freeze-dtc', dtc.freezeFrameCode ? [dtc.freezeFrameCode] : []);
  const recent = [...events].reverse().slice(0, 15);
  $('dtc-events').innerHTML = recent.length
    ? recent.map((event) => `<div class="dtc-item"><b>${escapeHtml(event.event)} ${escapeHtml(event.code || '')}</b><small>${escapeHtml(event.scope || '')} · ${escapeHtml(event.timestamp || '')}</small></div>`).join('')
    : '<div class="empty">No DTC events yet.</div>';
}

function renderDtcList(id, items) {
  $(id).innerHTML = items.length
    ? items.map((item) => {
      const code = typeof item === 'string' ? item : item.code;
      const description = typeof item === 'string' ? 'Freeze-frame trigger' : item.description;
      return `<div class="dtc-item"><b>${escapeHtml(code)}</b><small>${escapeHtml(description || 'No description available')}</small></div>`;
    }).join('')
    : '<div class="empty">None</div>';
}

async function getJson(url) {
  const response = await fetch(url, { cache: 'no-store' });
  const body = await response.json();
  if (!response.ok) throw new Error(body.detail || body.error || response.statusText);
  return body;
}

async function postJson(url, body = {}) {
  const response = await fetch(url, {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
  });
  const payload = await response.json();
  if (!response.ok) throw new Error(payload.detail || payload.error || response.statusText);
  return payload;
}

async function loadSignals() {
  try {
    const data = await getJson('/api/signals');
    signalCatalog = data.supported || [];
    renderSignals(data);
  } catch (error) { toast(error.message); }
}

function renderSignals(data) {
  const query = $('signal-search').value.trim().toLowerCase();
  const core = new Set(data.core || []);
  const selected = new Set(data.selected || []);
  const rows = signalCatalog.filter((signal) => !query || `${signal.name} ${signal.description}`.toLowerCase().includes(query));
  $('signal-list').innerHTML = rows.length ? rows.map((signal) => {
    const isCore = core.has(signal.name);
    const isSelected = selected.has(signal.name);
    return `<div class="list-row"><div class="meta"><b>${escapeHtml(signal.name)} ${isCore ? '<small>CORE</small>' : ''}</b><small>${escapeHtml(signal.description || '')}</small></div>${isCore ? '<span class="small">Always on</span>' : `<button data-signal="${escapeHtml(signal.name)}" data-selected="${isSelected}">${isSelected ? 'Remove' : 'Add'}</button>`}</div>`;
  }).join('') : '<div class="empty">No matching signals.</div>';
  $('signal-list').querySelectorAll('button[data-signal]').forEach((button) => {
    button.onclick = async () => {
      try {
        await postJson('/api/signals/select', { name: button.dataset.signal, selected: button.dataset.selected !== 'true' });
        await loadSignals();
      } catch (error) { toast(error.message); }
    };
  });
}

async function loadSetup() {
  try {
    const [ports, bluetooth] = await Promise.all([getJson('/api/obd/ports'), getJson('/api/bluetooth/status')]);
    $('port-info').textContent = `Mode: ${ports.mode}\nSelected: ${ports.selected?.kind || 'none'} ${ports.selected?.port || ''}\nUSB: ${(ports.usb || []).join(', ') || 'none'}\nBluetooth serial: ${ports.bluetoothPort}`;
    document.querySelectorAll('.transport-button').forEach((button) => button.classList.toggle('active', button.dataset.transport === ports.mode));
    renderBluetooth(bluetooth);
  } catch (error) { toast(error.message); }
}

function renderBluetooth(data) {
  const controller = data.controller || {};
  $('bluetooth-controller').textContent = controller.available
    ? `Controller: ${controller.powered ? 'ON' : 'OFF'} · configured ELM: ${data.configuredElmMac || 'none'}`
    : `Bluetooth unavailable: ${controller.error || ''}`;
  const devices = data.devices || [];
  $('bluetooth-list').innerHTML = devices.length ? devices.map((device) => `<div class="list-row"><div class="meta"><b>${escapeHtml(device.name || device.mac)}</b><small>${escapeHtml(device.mac)} · ${device.paired ? 'paired' : 'not paired'} · ${device.connected ? 'connected' : 'disconnected'}</small></div><div class="button-row">${device.paired ? '' : `<button data-action="pair" data-mac="${device.mac}">Pair</button>`}<button data-action="use" data-mac="${device.mac}">Use as ELM</button>${device.connected ? `<button data-action="disconnect" data-mac="${device.mac}">Disconnect</button>` : ''}${device.paired ? `<button data-action="forget" data-mac="${device.mac}">Forget</button>` : ''}</div></div>`).join('') : '<div class="empty">No Bluetooth devices discovered yet.</div>';
  $('bluetooth-list').querySelectorAll('button').forEach((button) => {
    button.onclick = async () => {
      const { mac, action } = button.dataset;
      try {
        if (action === 'pair') await postJson('/api/bluetooth/pair', { mac, pin: prompt('Bluetooth PIN (usually 1234 or 0000). Leave blank if none.') || '' });
        else if (action === 'use') await postJson('/api/bluetooth/use-elm', { mac });
        else if (action === 'disconnect') await postJson('/api/bluetooth/disconnect', { mac });
        else if (action === 'forget' && confirm(`Forget ${mac}?`)) await postJson('/api/bluetooth/forget', { mac });
        await loadSetup(); toast('Bluetooth action completed');
      } catch (error) { toast(error.message); }
    };
  });
}

function initTabs() {
  document.querySelectorAll('.tabs button').forEach((button) => {
    button.onclick = () => {
      document.querySelectorAll('.tabs button').forEach((item) => item.classList.remove('active'));
      document.querySelectorAll('.tab').forEach((item) => item.classList.remove('active'));
      button.classList.add('active');
      $(`tab-${button.dataset.tab}`).classList.add('active');
      if (button.dataset.tab === 'signals') loadSignals();
      if (button.dataset.tab === 'setup') loadSetup();
    };
  });
}

function initActions() {
  $('signal-search').oninput = loadSignals;
  $('refresh-dtc').onclick = async () => { try { await postJson('/api/dtc/refresh'); toast('DTC scan complete'); } catch (error) { toast(error.message); } };
  $('clear-dtc').onclick = async () => {
    if (prompt('Engine must be OFF. Type CLEAR to clear diagnostic codes.') !== 'CLEAR') return;
    try { await postJson('/api/dtc/clear', { confirm: 'CLEAR_DTC_CONFIRMED' }); toast('DTC clear command completed'); } catch (error) { toast(error.message); }
  };
  document.querySelectorAll('.transport-button').forEach((button) => {
    button.onclick = async () => { try { await postJson('/api/obd/transport', { transport: button.dataset.transport }); await loadSetup(); toast(`OBD transport set to ${button.dataset.transport}`); } catch (error) { toast(error.message); } };
  });
  $('reconnect-obd').onclick = async () => { try { await postJson('/api/obd/reconnect'); toast('OBD reconnect requested'); } catch (error) { toast(error.message); } };
  $('scan-bluetooth').onclick = async () => { try { toast('Scanning Bluetooth…'); const result = await postJson('/api/bluetooth/scan'); renderBluetooth({ controller: (await getJson('/api/bluetooth/status')).controller, devices: result.devices }); toast('Bluetooth scan complete'); } catch (error) { toast(error.message); } };
}

async function pollFallback() {
  try { renderState(await getJson('/api/state')); } catch { streamStatus('Stale', 'stale'); }
}

function startFallback() {
  if (fallbackTimer) return;
  pollFallback();
  fallbackTimer = setInterval(pollFallback, Math.max(500, fallbackPollSeconds * 1000));
}

function stopFallback() {
  if (!fallbackTimer) return;
  clearInterval(fallbackTimer);
  fallbackTimer = null;
}

function scheduleReconnect() {
  if (reconnectTimer) return;
  const delay = Math.min(30000, 1000 * (2 ** Math.min(reconnectAttempt, 5))) + Math.floor(Math.random() * 500);
  reconnectAttempt += 1;
  reconnectTimer = setTimeout(() => { reconnectTimer = null; connectWs(); }, delay);
}

function connectWs() {
  if (socket && socket.readyState <= WebSocket.OPEN) return;
  streamStatus('Connecting', 'reconnecting');
  const protocol = location.protocol === 'https:' ? 'wss' : 'ws';
  socket = new WebSocket(`${protocol}://${location.host}/ws/telemetry`);
  socket.onopen = () => {
    reconnectAttempt = 0;
    lastLiveMessageAt = Date.now();
    streamStatus('Live', 'live');
    stopFallback();
  };
  socket.onmessage = (event) => {
    try {
      const data = JSON.parse(event.data);
      renderState(data);
      lastLiveMessageAt = Date.now();
      const fresh = engineIsFresh(data);
      streamStatus(fresh ? 'Live' : 'Engine stale', fresh ? 'live' : 'stale');
      stopFallback();
    } catch { /* ignore malformed frame */ }
  };
  socket.onclose = () => { socket = null; streamStatus('Reconnecting', 'reconnecting'); startFallback(); scheduleReconnect(); };
  socket.onerror = () => socket.close();
}

function watchStream() {
  if (socket?.readyState === WebSocket.OPEN && lastLiveMessageAt && Date.now() - lastLiveMessageAt > (heartbeatSeconds + 2) * 1000) {
    streamStatus('Stale', 'stale'); startFallback(); socket.close();
  }
}

async function initialize() {
  initTabs(); initActions();
  try {
    const config = await getJson('/api/web-config');
    heartbeatSeconds = Number(config.heartbeatSeconds) || 5;
    fallbackPollSeconds = Number(config.fallbackPollSeconds) || 1;
  } catch { /* defaults are safe */ }
  try { renderState(await getJson('/api/state')); } catch { /* WebSocket will retry */ }
  connectWs();
  setInterval(watchStream, 1000);
}

initialize();
