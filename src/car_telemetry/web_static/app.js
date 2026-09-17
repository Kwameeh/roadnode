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
let wifiNetworks = [];
let selectedWifi = null;
let wifiBusy = false;
let networkPollBusy = false;
let wifiConnecting = false;

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
  // The engine reports the cloud link as `publisher` and the outbox as `frame`.
  const publisher = latest.publisher || {};
  const frame = latest.frame || {};
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
  badge('badge-mqtt', publisher.connected, publisher.enabled);

  $('sys-cpu').textContent = system.cpuPercent == null ? '--' : `${system.cpuPercent}%`;
  $('sys-temp').textContent = system.temperatureC == null ? '--' : `${system.temperatureC} °C`;
  $('sys-ram').textContent = system.memoryAvailableMb == null ? '--' : `${system.memoryAvailableMb} MB`;
  $('sys-disk').textContent = system.diskFreeGb == null ? '--' : `${system.diskFreeGb} GB`;
  $('sys-host').textContent = system.hostname || '--';
  $('sys-ip').textContent = system.ipAddress || '--';
  $('sys-uptime').textContent = fmtUptime(system.uptimeSeconds);
  $('sys-agent').textContent = latest.agent || '--';
  $('cloud-status').textContent = !publisher.enabled
    ? `Disabled${publisher.error ? ` (${publisher.error})` : ''}`
    : publisher.connected ? 'Connected' : `Offline${publisher.error ? `: ${publisher.error}` : ', connecting…'}`;
  $('cloud-broker').textContent = publisher.broker ? `${publisher.broker}${publisher.tls ? ' (TLS)' : ' (NO TLS)'}` : '--';
  $('cloud-client').textContent = publisher.clientId || '--';
  $('cloud-username').textContent = publisher.username || '--';
  $('cloud-topic').textContent = publisher.topic || '--';
  $('cloud-frames').textContent = `${frame.queueDepth ?? publisher.queueDepth ?? 0} queued · ${publisher.published ?? 0} sent · ${frame.droppedMessages ?? 0} dropped`;
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

async function getJson(url, timeoutMs = 0) {
  const controller = new AbortController();
  const timer = timeoutMs ? setTimeout(() => controller.abort(), timeoutMs) : null;
  try {
    const response = await fetch(url, { cache: 'no-store', signal: controller.signal });
    const body = await response.json();
    if (!response.ok) throw new Error(body.detail || body.error || response.statusText);
    return body;
  } finally { if (timer) clearTimeout(timer); }
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
  const policy = data.policy || {};
  const query = $('signal-search').value.trim().toLowerCase();
  // The device decides tier, state and wording. The UI never re-derives them,
  // so a browser left open on an old page cannot offer a stale choice.
  const decisions = new Map((policy.decisions || []).map((item) => [item.name, item]));
  const explanation = policy.explanation || [];
  $('signal-summary').innerHTML = policy.connected
    ? explanation.map((line) => `<p>${escapeHtml(line)}</p>`).join('')
    : '<p>Connect to the vehicle to see which signals it offers.</p>';

  const rows = signalCatalog.filter((signal) => !query || `${signal.name} ${signal.description}`.toLowerCase().includes(query));
  $('signal-list').innerHTML = rows.length ? rows.map((signal) => {
    const decision = decisions.get(signal.name) || {};
    const isCore = decision.tier === 'core';
    const isSelected = decision.state === 'selected';
    const purpose = decision.purpose || signal.description || '';
    const note = isCore ? 'Always on' : decision.state === 'rejected' ? escapeHtml(decision.reason || 'Not added') : '';
    const control = isCore || decision.state === 'unavailable'
      ? `<span class="small">${note || 'Not offered by this vehicle'}</span>`
      : `<button data-signal="${escapeHtml(signal.name)}" data-selected="${isSelected}">${isSelected ? 'Remove' : 'Add'}</button>`;
    return `<div class="list-row"><div class="meta"><b>${escapeHtml(signal.name)} ${isCore ? '<small>CORE</small>' : ''}</b><small>${escapeHtml(purpose)}</small></div>${control}</div>`;
  }).join('') : '<div class="empty">No matching signals.</div>';

  $('signal-list').querySelectorAll('button[data-signal]').forEach((button) => {
    button.onclick = async () => {
      button.disabled = true;
      try {
        await postJson('/api/signals/select', { name: button.dataset.signal, selected: button.dataset.selected !== 'true' });
      } catch (error) { toast(error.message); }
      await loadSignals();
    };
  });
}

async function loadSetup() {
  loadNetworkStatus();
  try {
    const [ports, bluetooth] = await Promise.all([getJson('/api/obd/ports'), getJson('/api/bluetooth/status')]);
    $('port-info').textContent = `Mode: ${ports.mode}\nSelected: ${ports.selected?.kind || 'none'} ${ports.selected?.port || ''}\nUSB: ${(ports.usb || []).join(', ') || 'none'}\nBluetooth serial: ${ports.bluetoothPort}`;
    document.querySelectorAll('.transport-button').forEach((button) => button.classList.toggle('active', button.dataset.transport === ports.mode));
    renderBluetooth(bluetooth);
  } catch (error) { toast(error.message); }
}

function wifiMessage(message) {
  $('wifi-message').textContent = message;
}

function updateWifiControls() {
  const busy = wifiBusy || wifiConnecting;
  $('scan-wifi').disabled = busy;
  $('connect-wifi').disabled = busy;
  $('wifi-list').querySelectorAll('button').forEach((button) => {
    const network = wifiNetworks[Number(button.dataset.network)];
    button.disabled = busy || network.connected || !network.supported;
  });
}

function renderWifiNetworks() {
  $('wifi-list').innerHTML = wifiNetworks.length ? wifiNetworks.map((network, index) => `
    <div class="list-row"><div class="meta">
      <b>${escapeHtml(network.ssid)}</b>
      <small>${network.signal}% signal · ${escapeHtml(network.security || 'Open network')} · ${escapeHtml(network.interface)}</small>
    </div><button type="button" data-network="${index}">${network.connected ? 'Connected' : network.supported ? 'Select' : 'Unsupported'}</button></div>
  `).join('') : '<div class="empty">No networks found. Check that the hotspot is nearby and Wi-Fi is enabled on the Pi.</div>';
  $('wifi-list').querySelectorAll('button').forEach((button) => {
    button.onclick = () => {
      selectedWifi = wifiNetworks[Number(button.dataset.network)];
      $('wifi-selection').textContent = `Connect to ${selectedWifi.ssid}`;
      $('wifi-password').value = '';
      $('wifi-password').disabled = !selectedWifi.security;
      $('wifi-password-help').textContent = selectedWifi.security
        ? 'Leave blank to use a password already saved on the Pi.' : 'This is an open network. No password is needed.';
      $('wifi-form').hidden = false;
      (selectedWifi.security ? $('wifi-password') : $('connect-wifi')).focus();
    };
  });
  updateWifiControls();
}

async function loadNetworkStatus() {
  if (networkPollBusy) return;
  networkPollBusy = true;
  try {
    const data = await getJson('/api/network/status', 30000);
    const labels = { online: 'Online', offline: 'No internet', limited: 'Limited access', unknown: 'Unknown' };
    const label = labels[data.internet] || 'Unknown';
    $('badge-internet').textContent = `Internet: ${label}`;
    badge('badge-internet', data.internet === 'online', data.internet !== 'online');
    const wifi = (data.wifi || []).map((network) => network.ssid).join(', ');
    $('network-status').textContent = `Internet: ${label}. Wi-Fi: ${wifi || (data.wifiEnabled ? 'Not connected' : 'Off')}.${data.error ? ` ${data.error}` : ''}`;
    $('network-addresses').textContent = (data.interfaces || []).map((item) =>
      `${item.interface}: ${item.state}${item.addresses.length ? ` · ${item.addresses.join(', ')}` : ''}`
    ).join('\n');
    const operation = data.operation || {};
    // A status request started before submission may still report an idle job.
    if (!wifiBusy) {
      wifiConnecting = operation.state === 'connecting';
      if (wifiConnecting) wifiMessage(`Connecting to ${operation.ssid}... This page may disconnect while the Pi changes networks.`);
      else if (operation.state === 'failed') wifiMessage(operation.error || 'Connection failed. Check the password and try again.');
      else if (operation.state === 'connected') wifiMessage(`Connection attempt to ${operation.ssid} completed. Internet: ${label}.`);
    }
    if (wifiNetworks.length) {
      wifiNetworks.forEach((network) => {
        network.connected = (data.wifi || []).some((active) => active.ssid === network.ssid && active.interface === network.interface);
      });
      renderWifiNetworks();
    }
    updateWifiControls();
  } catch {
    $('badge-internet').textContent = 'Internet: unknown';
    badge('badge-internet', false, true);
    $('network-status').textContent = 'Cannot reach the Pi. Internet status is unknown. If Wi-Fi changed, join the new network and reopen the Pi web page.';
  } finally { networkPollBusy = false; }
}

function initWifi() {
  $('scan-wifi').onclick = async () => {
    wifiBusy = true;
    updateWifiControls();
    wifiMessage('Scanning for Wi-Fi networks...');
    try {
      const data = await postJson('/api/network/scan');
      wifiNetworks = data.networks || [];
      renderWifiNetworks();
      wifiMessage(`${wifiNetworks.length} networks found. Select a network to connect. Enterprise and WEP networks require setup on the Pi.`);
    } catch (error) { wifiMessage(error.message); }
    finally { wifiBusy = false; updateWifiControls(); }
  };
  $('cancel-wifi').onclick = () => {
    $('wifi-form').hidden = true;
    $('wifi-password').value = '';
    selectedWifi = null;
  };
  $('wifi-form').onsubmit = async (event) => {
    event.preventDefault();
    if (!selectedWifi || wifiBusy || wifiConnecting) return;
    wifiBusy = true;
    updateWifiControls();
    wifiMessage(`Connecting to ${selectedWifi.ssid}... If this page disconnects, join that network and reopen the Pi web page.`);
    const body = { ssid: selectedWifi.ssid, interface: selectedWifi.interface, password: $('wifi-password').value };
    $('wifi-password').value = '';
    try {
      await postJson('/api/network/connect', body);
      wifiConnecting = true;
      $('wifi-form').hidden = true;
    } catch (error) {
      wifiMessage(`${error.message}. If the Pi changed networks, reconnect to it and check status before retrying.`);
    } finally {
      body.password = '';
      wifiBusy = false;
      updateWifiControls();
      loadNetworkStatus();
    }
  };
  loadNetworkStatus();
  setInterval(loadNetworkStatus, 10000);
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
        if (action === 'pair') await postJson('/api/bluetooth/pair', { mac, pin: prompt('Bluetooth PIN (ELM327 is usually 1234 or 1111). Leave blank if none.') || '' });
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
  $('show-oled-qr').onclick = async () => { try { const result = await postJson('/api/oled/qr', { seconds: 60 }); toast(`Display shows a QR for ${result.url} for 60 s`); } catch (error) { toast(error.message); } };
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
  initTabs(); initActions(); initWifi();
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
