// State
let currentStation = null;
let isScanning = false;
let stations = [];
let metadataInterval = null;
let scanInterval = null;

const audio = document.getElementById('audio-player');

// DOM references
const statusIndicator = document.getElementById('status-indicator');
const nowPlaying = document.getElementById('now-playing');
const stationName = document.getElementById('station-name');
const ensembleName = document.getElementById('ensemble-name');
const dlsText = document.getElementById('dls-text');
const snrValue = document.getElementById('snr-value');
const bitrateValue = document.getElementById('bitrate-value');
const modeValue = document.getElementById('mode-value');
const slideImage = document.getElementById('slide-image');
const scanProgress = document.getElementById('scan-progress');
const progressFill = document.getElementById('progress-fill');
const scanStatus = document.getElementById('scan-status');
const stationsContainer = document.getElementById('stations-container');
const sdrStatus = document.getElementById('sdr-status');
const welleStatus = document.getElementById('welle-status');
const outputMode = document.getElementById('output-mode');
const scanPopularBtn = document.getElementById('scan-popular-btn');
const scanAllBtn = document.getElementById('scan-all-btn');
const stopBtn = document.getElementById('stop-btn');

// ---- API helpers ----

async function apiFetch(url, options = {}) {
    try {
        const resp = await fetch(url, options);
        if (!resp.ok) {
            const body = await resp.text().catch(() => '');
            throw new Error(body || `HTTP ${resp.status}`);
        }
        return await resp.json().catch(() => null);
    } catch (err) {
        if (err.name === 'TypeError') {
            // Network error
            throw new Error('Network error - server unreachable');
        }
        throw err;
    }
}

// ---- Status ----

async function fetchStatus() {
    try {
        const data = await apiFetch('/api/status');
        if (!data) return;

        const sdrOk = data.sdr_connected || data.sdrConnected || false;
        const welleOk = data.welle_running || data.welleRunning || false;

        sdrStatus.innerHTML = `<span class="status-dot ${sdrOk ? 'connected' : 'disconnected'}"></span> SDR`;
        welleStatus.innerHTML = `<span class="status-dot ${welleOk ? 'connected' : 'disconnected'}"></span> welle.io`;

        statusIndicator.innerHTML = sdrOk && welleOk
            ? '<span class="status-dot connected"></span> Connected'
            : '<span class="status-dot disconnected"></span> Offline';
    } catch {
        sdrStatus.innerHTML = '<span class="status-dot disconnected"></span> SDR';
        welleStatus.innerHTML = '<span class="status-dot disconnected"></span> welle.io';
        statusIndicator.innerHTML = '<span class="status-dot disconnected"></span> Offline';
    }
}

// ---- Scanning ----

async function startScan(mode) {
    if (isScanning) return;
    isScanning = true;

    scanPopularBtn.disabled = true;
    scanAllBtn.disabled = true;
    scanProgress.classList.remove('hidden');
    progressFill.style.width = '0%';
    scanStatus.textContent = 'Starting scan...';

    try {
        const url = mode === 'popular' ? '/api/scan?mode=popular' : '/api/scan';
        await apiFetch(url, { method: 'POST' });
        pollScanProgress();
    } catch (err) {
        showError('Failed to start scan: ' + err.message);
        resetScanUI();
    }
}

function pollScanProgress() {
    if (scanInterval) clearInterval(scanInterval);

    scanInterval = setInterval(async () => {
        try {
            const data = await apiFetch('/api/scan/progress');
            if (!data) return;

            const pct = data.progress_percent || data.progress || 0;
            progressFill.style.width = pct + '%';
            const ch = data.current_channel ? ` (${data.current_channel})` : '';
            scanStatus.textContent = `Scanning${ch}... ${Math.round(pct)}% — ${data.stations_found || 0} stations found`;

            const scanning = data.scanning !== undefined ? data.scanning : data.isScanning;
            if (!scanning) {
                clearInterval(scanInterval);
                scanInterval = null;
                resetScanUI();
                await loadStations();
            }
        } catch (err) {
            clearInterval(scanInterval);
            scanInterval = null;
            showError('Scan progress error: ' + err.message);
            resetScanUI();
        }
    }, 1000);
}

function resetScanUI() {
    isScanning = false;
    scanPopularBtn.disabled = false;
    scanAllBtn.disabled = false;
    setTimeout(() => {
        scanProgress.classList.add('hidden');
    }, 1500);
}

// ---- Stations ----

async function loadStations() {
    try {
        const data = await apiFetch('/api/stations');
        stations = Array.isArray(data) ? data : (data && data.stations ? data.stations : []);
        renderStations(stations);
    } catch {
        // Silently fail on load - stations may not exist yet
    }
}

function renderStations(stationList) {
    if (!stationList || stationList.length === 0) {
        stationsContainer.innerHTML = '<p style="color:var(--text-muted);font-size:0.9rem;">No stations found. Run a scan to discover stations.</p>';
        return;
    }

    stationsContainer.innerHTML = stationList.map(s => {
        const id = s.id || s.serviceId || s.service_id || s.sid || '';
        const name = s.name || s.stationName || 'Unknown';
        const ensemble = s.ensemble || s.ensembleName || '';
        const channel = s.channel || '';
        const bitrate = s.bitrate ? s.bitrate + ' kbps' : '';
        const mode = s.mode || s.audioMode || '';
        const dls = s.dls || '';
        const isActive = currentStation && String(currentStation) === String(id);

        return `
            <div class="station-card ${isActive ? 'active' : ''}" data-service-id="${id}" onclick="playStation('${id}')">
                <div class="station-card-name">${escapeHtml(name)}</div>
                ${ensemble ? `<div class="station-card-ensemble">${escapeHtml(ensemble)}</div>` : ''}
                <div class="station-card-meta">
                    ${channel ? `<span>${escapeHtml(channel)}</span>` : ''}
                    ${bitrate ? `<span>${escapeHtml(bitrate)}</span>` : ''}
                    ${mode ? `<span>${escapeHtml(mode)}</span>` : ''}
                </div>
                ${dls ? `<div class="station-card-dls">${escapeHtml(dls)}</div>` : ''}
            </div>
        `;
    }).join('');
}

// ---- Playback ----

async function playStation(serviceId) {
    try {
        const mode = outputMode.value;
        await apiFetch(`/api/play/${serviceId}`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ output: mode }),
        });

        currentStation = serviceId;
        nowPlaying.classList.remove('hidden');

        // Set station info from cached station list
        const station = stations.find(s =>
            String(s.id || s.serviceId || s.service_id || s.sid) === String(serviceId)
        );
        if (station) {
            stationName.textContent = station.name || station.stationName || 'Unknown';
            ensembleName.textContent = station.ensemble || station.ensembleName || '';
        } else {
            stationName.textContent = 'Loading...';
            ensembleName.textContent = '';
        }

        dlsText.textContent = '';
        slideImage.innerHTML = '';
        snrValue.textContent = '';
        bitrateValue.textContent = '';
        modeValue.textContent = '';

        // Browser audio
        if (mode === 'browser' || mode === 'both') {
            audio.src = `/api/stream/${serviceId}`;
            audio.play().catch(() => {
                // Autoplay may be blocked
            });
        } else {
            audio.pause();
            audio.removeAttribute('src');
        }

        // Re-render stations to highlight active
        renderStations(stations);

        // Start metadata polling
        startMetadataPolling();
    } catch (err) {
        showError('Failed to play station: ' + err.message);
    }
}

async function stopPlayback() {
    try {
        await apiFetch('/api/play', { method: 'DELETE' });
    } catch {
        // Ignore errors on stop
    }

    audio.pause();
    audio.removeAttribute('src');
    currentStation = null;
    nowPlaying.classList.add('hidden');
    stopMetadataPolling();
    renderStations(stations);
}

// ---- Metadata ----

function startMetadataPolling() {
    stopMetadataPolling();
    // Fetch immediately, then every 2 seconds
    pollMetadata();
    metadataInterval = setInterval(pollMetadata, 2000);
}

function stopMetadataPolling() {
    if (metadataInterval) {
        clearInterval(metadataInterval);
        metadataInterval = null;
    }
}

async function pollMetadata() {
    if (!currentStation) return;

    try {
        const data = await apiFetch('/api/metadata');
        if (data) updateNowPlaying(data);
    } catch {
        // Silently fail - metadata is non-critical
    }
}

function updateNowPlaying(meta) {
    if (meta.dls !== undefined) {
        dlsText.textContent = meta.dls || '';
    }

    if (meta.snr !== undefined) {
        snrValue.textContent = `SNR: ${meta.snr} dB`;
    }

    if (meta.bitrate !== undefined) {
        bitrateValue.textContent = `${meta.bitrate} kbps`;
    }

    if (meta.mode !== undefined || meta.audioMode !== undefined) {
        modeValue.textContent = meta.mode || meta.audioMode || '';
    }

    if (meta.station_name || meta.stationName || meta.name) {
        stationName.textContent = meta.station_name || meta.stationName || meta.name;
    }

    if (meta.ensemble || meta.ensembleName) {
        ensembleName.textContent = meta.ensemble || meta.ensembleName;
    }

    // Slide image
    const slideUrl = meta.mot_image || meta.slide || meta.slideUrl || meta.slideImage || null;
    if (slideUrl) {
        if (!slideImage.querySelector('img') || slideImage.querySelector('img').src !== slideUrl) {
            slideImage.innerHTML = `<img src="${escapeHtml(slideUrl)}" alt="Slide">`;
        }
    } else {
        slideImage.innerHTML = '';
    }
}

// ---- Output mode change ----

async function handleOutputChange() {
    if (!currentStation) return;

    const mode = outputMode.value;
    try {
        await apiFetch(`/api/play/${currentStation}`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ output: mode }),
        });

        if (mode === 'browser' || mode === 'both') {
            audio.src = `/api/stream/${currentStation}`;
            audio.play().catch(() => {});
        } else {
            audio.pause();
            audio.removeAttribute('src');
        }
    } catch (err) {
        showError('Failed to switch output: ' + err.message);
    }
}

// ---- Error notification ----

function showError(message) {
    // Remove existing toast
    const existing = document.querySelector('.error-toast');
    if (existing) existing.remove();

    const toast = document.createElement('div');
    toast.className = 'error-toast';
    toast.textContent = message;
    document.body.appendChild(toast);

    setTimeout(() => {
        toast.remove();
    }, 4000);
}

// ---- Utilities ----

function escapeHtml(str) {
    const el = document.createElement('span');
    el.textContent = str;
    return el.innerHTML;
}

// ---- Event Listeners ----

scanPopularBtn.addEventListener('click', () => startScan('popular'));
scanAllBtn.addEventListener('click', () => startScan('all'));
stopBtn.addEventListener('click', stopPlayback);
outputMode.addEventListener('change', handleOutputChange);

// ---- Initialization ----

document.addEventListener('DOMContentLoaded', () => {
    fetchStatus();
    loadStations();
    setInterval(fetchStatus, 5000);
});
