/**
 * static/js/app.js
 * ----------------
 * Main UI logic, tab switching, polling loop, DOM rendering, and toast alerts.
 */

const VALID_TABS = ['dashboard', 'disks', 'activity', 'config'];
const REFRESH_MS = 10000;

let shutdownConfirmTimer = null;
let isConfirmingShutdown = false;

function el(id) { return document.getElementById(id); }
function setText(id, val) { const e = el(id); if (e) e.textContent = val; }

function escapeHtml(str) {
    if (!str) return '';
    return String(str).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

function switchTab(name, updateHash = true) {
    if (!VALID_TABS.includes(name)) name = 'dashboard';

    document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
    document.querySelectorAll('.tab-panel').forEach(p => p.classList.remove('active'));

    const btn = document.getElementById('tab-' + name);
    const panel = document.getElementById('panel-' + name);
    if (btn) btn.classList.add('active');
    if (panel) panel.classList.add('active');

    if (updateHash && window.location.hash !== '#' + name) {
        history.pushState(null, null, '#' + name);
    }
}

function handleHashChange() {
    const hash = (window.location.hash || '').replace('#', '').trim();
    if (hash && VALID_TABS.includes(hash)) {
        switchTab(hash, false);
    }
}

function formatShutdownWatcher(s) {
    if (s.shutdown_mode === 'disabled' || s.status === 'disabled') {
        return {
            label: 'Disabled',
            detail: 'Power saver & idle host shutdown watcher is disabled'
        };
    }

    const idleNeeded = s.idle_needed || 3;
    const idleStreak = s.idle_streak || 0;
    const pollInterval = s.poll_interval || 1200;
    const pollMins = Math.max(1, Math.round(pollInterval / 60));
    
    let label = `${idleStreak} of ${idleNeeded} idle polls`;
    let detail = '';

    let nextCheckTime = '';
    if (s.next_check) {
        const parts = s.next_check.split(' ');
        nextCheckTime = parts.length > 1 ? parts[1].substring(0, 5) : s.next_check.substring(0, 5);
    }
    
    if (s.stream_count > 0) {
        const streamStr = s.stream_count === 1 ? '1 active Plex stream' : `${s.stream_count} active Plex streams`;
        label = `0 of ${idleNeeded} idle polls (Paused)`;
        detail = `Shutdown paused — ${streamStr}`;
    } else if (s.nzbget_active) {
        label = `0 of ${idleNeeded} idle polls (Paused)`;
        detail = `Shutdown paused — ${s.nzbget_detail || 'NZBGet active'}`;
    } else if (s.plex_activity_active) {
        label = `0 of ${idleNeeded} idle polls (Paused)`;
        detail = `Shutdown paused — ${s.plex_activity_detail || 'Plex background task running'}`;
    } else if (idleStreak >= idleNeeded) {
        label = `${idleNeeded} of ${idleNeeded} idle polls`;
        detail = `Shutdown threshold reached — SSH command executed.`;
    } else {
        const pollsLeft = Math.max(0, idleNeeded - idleStreak);
        const minsLeft = pollsLeft * pollMins;
        let timeStr = '';
        if (minsLeft >= 60) {
            const h = Math.floor(minsLeft / 60);
            const m = minsLeft % 60;
            timeStr = `${h}h${m > 0 ? ' ' + m + 'm' : ''}`;
        } else {
            timeStr = `${minsLeft} min`;
        }
        
        const pollWord = pollsLeft === 1 ? '1 poll left' : `${pollsLeft} polls left`;
        const nextPart = nextCheckTime ? ` • Next check: ${nextCheckTime}` : '';
        detail = `Est. shutdown in ~${timeStr} (${pollWord} @ ${pollMins}m)${nextPart}`;
    }
    
    return { label, detail };
}

function updateShutdownWatcher(s) {
    if (!s) return;
    const formatted = formatShutdownWatcher(s);
    setText('pw-streak-label', formatted.label);
    setText('pw-shutdown-detail', formatted.detail);

    const bar = el('pw-bar');
    if (bar) {
        const isPaused = (s.stream_count > 0 || s.nzbget_active);
        const pct = isPaused ? 0 : Math.min((s.idle_streak / s.idle_needed) * 100, 100);
        bar.style.width = pct + '%';
        bar.style.background = s.idle_streak >= s.idle_needed ? '#e05252'
                             : s.idle_streak > 0              ? '#e5a00d'
                             :                                  '#3ecf8e';
    }

    const container = el('pw-streams-container');
    if (container) {
        if (!s.active_streams || s.active_streams.length === 0) {
            container.innerHTML = '';
        } else {
            let html = '<p class="section-title" style="margin-bottom:12px;">Active Streams</p>';
            s.active_streams.forEach(st => {
                const decBadge = st.decision === 'DIRECT PLAY' ? 'badge-green' : (st.decision === 'DIRECT STREAM' ? 'badge-blue' : 'badge-amber');
                const remText = st.remaining_mins > 0 ? st.remaining_mins + ' min left' : '0 min left';
                html += `
                <div class="stream-card">
                    <div class="stream-header">
                        <div class="stream-user">
                            <span class="stream-user-name">${escapeHtml(st.user)}</span>
                            <span class="stream-device">${escapeHtml(st.device)}</span>
                        </div>
                        <div class="stream-badges">
                            <span class="badge ${decBadge}">${escapeHtml(st.decision)}</span>
                            <span class="badge badge-dim">${escapeHtml(st.location)}</span>
                            <span style="font-family:var(--mono); font-size:10px; color:var(--text-sub);">${escapeHtml(st.bandwidth)}</span>
                        </div>
                    </div>
                    <div class="stream-title">${escapeHtml(st.title)}</div>
                    <div class="stream-progress-wrap">
                        <div class="stream-progress-meta">
                            <span>${escapeHtml((st.state || 'PLAYING').toUpperCase())}</span>
                            <span>${remText} (${st.progress_pct}%)</span>
                        </div>
                        <div class="stream-progress-track">
                            <div class="stream-progress-fill" style="width: ${Math.min(100, Math.max(0, st.progress_pct))}%;"></div>
                        </div>
                    </div>
                </div>`;
            });
            container.innerHTML = html;
        }
    }
}

function updateEpisodePoller(s) {
    if (!s) return;
    const cnt = el('ep-count');
    if (cnt) {
        cnt.textContent = s.episodes_session;
        cnt.className = 'stat-value';
        if (s.episodes_session > 0) cnt.classList.add('green');
    }
    setText('ep-last',    s.last_check || '—');
    setText('ep-next',    s.next_check || '—');
    setText('ep-last-ep', s.last_episode || '');
}

function updateConnectionPills(d) {
    if (!d) return;
    if (d.sonarr) {
        const p = el('pip-sonarr');
        if (p) p.className = 'conn-pip ' + (d.sonarr.ok ? 'ok' : (d.sonarr.status.includes('Error') ? 'warn' : 'err'));
    }
    if (d.radarr) {
        const p = el('pip-radarr');
        if (p) p.className = 'conn-pip ' + (d.radarr.ok ? 'ok' : (d.radarr.status.includes('Error') ? 'warn' : 'err'));
    }
    if (d.plex) {
        const p = el('pip-plex');
        if (p) p.className = 'conn-pip ' + (d.plex.ok ? 'ok' : (d.plex.status.includes('Error') ? 'warn' : 'err'));
    }
}

function updateNZBGet(s) {
    if (!s) return;
    const pip = el('pip-nzbget');
    if (pip) {
        pip.className = 'conn-pip ' + (s.connected ? 'ok' : 'err');
    }

    const container = el('nzbget-container');
    if (container) {
        if (!s.enabled || !s.connected || !s.downloads || s.downloads.length === 0) {
            container.innerHTML = '';
        } else {
            let html = `
            <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:12px;">
                <p class="section-title" style="margin-bottom:0;">Active Downloads (NZBGet)</p>
                <span style="font-family:var(--mono); font-size:11px; color:var(--green); font-weight:600;">${escapeHtml(s.download_rate)}</span>
            </div>`;

            s.downloads.forEach(d => {
                const statusBadge = d.status === 'DOWNLOADING' ? 'badge-green' : (d.status === 'PAUSED' ? 'badge-amber' : 'badge-dim');
                html += `
                <div class="stream-card">
                    <div class="stream-header">
                        <div class="stream-user">
                            <span class="stream-user-name" style="word-break:break-all;">${escapeHtml(d.name)}</span>
                        </div>
                        <div class="stream-badges">
                            <span class="badge ${statusBadge}">${escapeHtml(d.status)}</span>
                            ${d.category ? `<span class="badge badge-dim">${escapeHtml(d.category)}</span>` : ''}
                        </div>
                    </div>
                    <div class="stream-progress-wrap">
                        <div class="stream-progress-meta">
                            <span>${d.remaining_mb > 0 ? (d.size_mb - d.remaining_mb).toFixed(1) + ' / ' + d.size_mb + ' MB' : d.size_mb + ' MB'}</span>
                            <span>${escapeHtml(d.eta)} (${d.progress_pct}%)</span>
                        </div>
                        <div class="stream-progress-track">
                            <div class="stream-progress-fill" style="width: ${Math.min(100, Math.max(0, d.progress_pct))}%;"></div>
                        </div>
                    </div>
                </div>`;
            });
            container.innerHTML = html;
        }
    }
}

function renderDiskSpace(data) {
    const grid = el('disk-grid');
    if (!grid || !data || !data.disks) return;

    grid.innerHTML = Object.entries(data.disks).map(([key, d]) => {
        const badgeColor = d.status === 'ok' ? 'green' : (d.status === 'warning' ? 'amber' : 'red');
        const fillColor = d.status === 'critical' ? '#e05252' : (d.status === 'warning' ? '#e5a00d' : '#3ecf8e');
        return `
        <div class="stat-cell" style="flex:1;">
            <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:6px;">
                <span class="stat-label" style="font-weight:600; text-transform:uppercase;">${escapeHtml(d.label)}</span>
                <span class="badge badge-${badgeColor}">${d.free_pct}% Free</span>
            </div>
            <div class="stat-value ${badgeColor}" style="margin-bottom:8px;">${escapeHtml(d.free_formatted)} <span style="font-size:13px; font-weight:normal; color:var(--text-sub);">free</span></div>
            
            <div class="idle-track" style="margin-bottom:8px;">
                <div class="idle-fill" style="width: ${d.used_pct}%; background: ${fillColor};"></div>
            </div>
            
            <div style="display:flex; justify-content:space-between; font-size:11px; font-family:var(--mono); color:var(--text-sub);">
                <span>Used: ${escapeHtml(d.used_formatted)} (${d.used_pct}%)</span>
                <span>Total: ${escapeHtml(d.total_formatted)}</span>
            </div>
            <div style="margin-top:8px; font-size:11px; font-family:var(--mono); color:var(--text-sub); display:flex; justify-content:space-between; align-items:center;">
                <span>Path: <code>${escapeHtml(d.configured_path)}</code></span>
                <span style="opacity:0.7;">${escapeHtml(d.source)}</span>
            </div>
        </div>`;
    }).join('');
}

function updateActivityLog(logs) {
    const container = el('activity-log-container');
    if (!container) return;

    if (!logs || logs.length === 0) {
        container.className = 'no-logs';
        container.innerHTML = 'No activity recorded yet.';
        return;
    }

    // Preserve open <details> state across refreshes
    const openDetailsKeys = new Set();
    container.querySelectorAll('.log-entry').forEach(entry => {
        const details = entry.querySelector('details');
        if (details && details.open) {
            const key = entry.getAttribute('data-log-key');
            if (key) openDetailsKeys.add(key);
        }
    });

    container.className = 'log-list';
    container.innerHTML = logs.map(log => {
        const logKey = (log.timestamp || '') + '::' + (log.message || '');
        const isOpen = openDetailsKeys.has(logKey);
        const rawStatus = (log.status || 'INFO').toUpperCase();
        const displayStatus = (rawStatus === 'OK') ? 'INFO' : rawStatus;
        const st = displayStatus.toLowerCase();
        const gutter = (st === 'success') ? 'success'
                     : (st === 'info') ? 'info'
                     : (st === 'warn' || st === 'warning') ? 'warn'
                     : 'err';
        const payloadHtml = log.payload
            ? `<details ${isOpen ? 'open' : ''}>
                <summary class="log-payload-toggle">View payload</summary>
                <pre class="log-payload-pre"><code>${escapeHtml(JSON.stringify(log.payload, null, 2))}</code></pre>
               </details>`
            : '';
        return `<div class="log-entry" data-log-key="${escapeHtml(logKey)}">
            <div class="log-gutter ${gutter}"></div>
            <div class="log-body">
                <div class="log-meta">
                    <span class="log-ts">${escapeHtml(log.timestamp)}</span>
                    <span class="log-tag ${gutter}">${escapeHtml(displayStatus)}</span>
                </div>
                <div class="log-msg">${escapeHtml(log.message)}</div>
                ${payloadHtml}
            </div>
        </div>`;
    }).join('');
}

function showToast(message, type = 'ok') {
    let container = el('toast-container');
    if (!container) {
        container = document.createElement('div');
        container.id = 'toast-container';
        container.className = 'toast-container';
        document.body.appendChild(container);
    }
    const toast = document.createElement('div');
    toast.className = `toast ${type}`;
    toast.innerHTML = `<span>${escapeHtml(message)}</span>`;
    container.appendChild(toast);

    setTimeout(() => {
        toast.style.animation = 'toastFadeOut .3s ease-in forwards';
        setTimeout(() => toast.remove(), 300);
    }, 3500);
}

function pollAll() {
    Api.fetchConnectionStatus().then(d => { if (d) updateConnectionPills(d); });
    Api.fetchPlexStatus().then(d => { if (d) updateShutdownWatcher(d); });
    Api.fetchPollerStatus().then(d => { if (d) updateEpisodePoller(d); });
    Api.fetchNZBGetStatus().then(d => { if (d) updateNZBGet(d); });
    Api.fetchActivityLogs().then(d => { if (d) updateActivityLog(d); });
    Api.fetchDiskSpace().then(d => { if (d) renderDiskSpace(d); });
}

function clearLogs() {
    const btn = el('btn-clear-logs');
    if (btn) btn.disabled = true;
    Api.clearLogs()
        .then(() => {
            updateActivityLog([]);
            showToast('Activity logs cleared', 'ok');
        })
        .catch(() => showToast('Failed to clear logs', 'err'))
        .finally(() => { if (btn) btn.disabled = false; });
}

function runPoller() {
    const btn = el('btn-run-poller');
    if (btn) {
        btn.disabled = true;
        btn.textContent = 'Re-checking...';
    }
    Api.runPoller()
        .then(({ ok, data }) => {
            showToast(data.message || 'Stateless re-check completed', ok ? 'ok' : 'err');
            setTimeout(pollAll, 1000);
        })
        .catch(() => showToast('Failed to trigger re-check', 'err'))
        .finally(() => {
            if (btn) {
                btn.disabled = false;
                btn.textContent = 'Re-check Now';
            }
        });
}

function resetShutdownBtn() {
    const btn = el('btn-shutdown-now');
    if (!btn) return;
    btn.textContent = 'Shutdown Now';
    btn.style.background = '';
    btn.style.borderColor = '';
    btn.style.color = '';
    btn.style.fontWeight = '';
    isConfirmingShutdown = false;
    if (shutdownConfirmTimer) {
        clearTimeout(shutdownConfirmTimer);
        shutdownConfirmTimer = null;
    }
}

function triggerShutdownNow() {
    const btn = el('btn-shutdown-now');
    if (!btn) return;

    if (!isConfirmingShutdown) {
        isConfirmingShutdown = true;
        btn.textContent = '⚠️ Confirm Shutdown?';
        btn.style.background = 'var(--red-dim)';
        btn.style.borderColor = 'var(--red)';
        btn.style.color = 'var(--red)';
        btn.style.fontWeight = '600';
        
        shutdownConfirmTimer = setTimeout(() => {
            resetShutdownBtn();
        }, 5000);
        return;
    }

    resetShutdownBtn();
    btn.disabled = true;
    btn.textContent = 'Shutting down...';

    Api.triggerShutdownNow()
        .then(({ ok, data }) => {
            if (ok && data.status === 'success') {
                showToast(data.message || 'Shutdown command sent to host', 'warn');
            } else {
                showToast('Shutdown failed: ' + (data.message || 'Unknown error'), 'err');
            }
            setTimeout(pollAll, 1000);
        })
        .catch(err => showToast('Shutdown request error: ' + err, 'err'))
        .finally(() => {
            if (btn) {
                btn.disabled = false;
                btn.textContent = 'Shutdown Now';
            }
        });
}

function saveConfiguration() {
    const btn = el('btn-save-cfg');
    if (btn) {
        btn.disabled = true;
        btn.textContent = 'Saving...';
    }

    const payload = {
        SONARR_URL: el('cfg-SONARR_URL').value,
        SONARR_API_KEY: el('cfg-SONARR_API_KEY').value,
        ROLLING_WINDOW: parseInt(el('cfg-ROLLING_WINDOW').value) || 3,
        DELETE_WATCHED_EPISODES: el('cfg-DELETE_WATCHED_EPISODES').value === 'true',
        RADARR_URL: el('cfg-RADARR_URL').value,
        RADARR_API_KEY: el('cfg-RADARR_API_KEY').value,
        NZBGET_URL: el('cfg-NZBGET_URL').value,
        NZBGET_USERNAME: el('cfg-NZBGET_USERNAME').value,
        NZBGET_PASSWORD: el('cfg-NZBGET_PASSWORD').value,
        PLEX_URL: el('cfg-PLEX_URL').value,
        PLEX_TOKEN: el('cfg-PLEX_TOKEN').value,
        PLEX_WATCH_INTERVAL: parseInt(el('cfg-PLEX_WATCH_INTERVAL').value) || 3600,
        PLEX_POLL_INTERVAL: parseInt(el('cfg-PLEX_POLL_INTERVAL').value) || 1200,
        PLEX_IDLE_POLLS: parseInt(el('cfg-PLEX_IDLE_POLLS').value) || 3,
        PLEX_SHUTDOWN_MODE: el('cfg-PLEX_SHUTDOWN_MODE') ? el('cfg-PLEX_SHUTDOWN_MODE').value : 'dry_run',
        PLEX_SHUTDOWN_DRY_RUN: el('cfg-PLEX_SHUTDOWN_MODE') ? el('cfg-PLEX_SHUTDOWN_MODE').value === 'dry_run' : true,
        SSH_HOST: el('cfg-SSH_HOST').value,
        SSH_PORT: parseInt(el('cfg-SSH_PORT').value) || 22,
        SSH_USER: el('cfg-SSH_USER').value,
        SSH_PASSWORD: el('cfg-SSH_PASSWORD').value,
        PATH_DOWNLOADS: el('cfg-PATH_DOWNLOADS').value || '/downloads',
        PATH_TV: el('cfg-PATH_TV').value || '/tv',
        PATH_MOVIES: el('cfg-PATH_MOVIES').value || '/movies',
    };

    Api.saveConfig(payload)
        .then(data => {
            if (data.status === 'success') {
                showToast('Configuration saved securely!', 'ok');
                if (data.config) updatePillLinks(data.config);
                setTimeout(pollAll, 500);
            } else {
                showToast('Error saving config: ' + (data.message || 'Unknown error'), 'err');
            }
        })
        .catch(() => showToast('Failed to save configuration', 'err'))
        .finally(() => {
            if (btn) {
                btn.disabled = false;
                btn.textContent = 'Save Configuration';
            }
        });
}

function testConnection(service) {
    const btn = el(`btn-test-${service}`);
    const origText = btn ? btn.textContent : 'Test Connection';
    if (btn) {
        btn.disabled = true;
        btn.textContent = 'Testing...';
    }

    let params = {};
    if (service === 'sonarr') {
        params = { url: el('cfg-SONARR_URL').value, api_key: el('cfg-SONARR_API_KEY').value };
    } else if (service === 'radarr') {
        params = { url: el('cfg-RADARR_URL').value, api_key: el('cfg-RADARR_API_KEY').value };
    } else if (service === 'plex') {
        params = { url: el('cfg-PLEX_URL').value, token: el('cfg-PLEX_TOKEN').value };
    } else if (service === 'nzbget') {
        params = { url: el('cfg-NZBGET_URL').value, username: el('cfg-NZBGET_USERNAME').value, password: el('cfg-NZBGET_PASSWORD').value };
    } else if (service === 'ssh') {
        params = {
            ssh_host: el('cfg-SSH_HOST').value,
            ssh_port: parseInt(el('cfg-SSH_PORT').value) || 22,
            ssh_user: el('cfg-SSH_USER').value,
            ssh_password: el('cfg-SSH_PASSWORD').value
        };
    }

    Api.testConnection(service, params)
        .then(data => {
            if (data && data.status === 'success') {
                showToast(data.message, 'ok');
            } else {
                showToast((data && data.message) ? data.message : 'Connection test failed', 'err');
            }
        })
        .catch(err => showToast('Test error: ' + err, 'err'))
        .finally(() => {
            if (btn) {
                btn.disabled = false;
                btn.textContent = origText;
            }
        });
}

window.testConnection = testConnection;

function buildServiceUrl(configuredUrl) {
    if (!configuredUrl || configuredUrl === 'Not Configured' || configuredUrl === 'Not configured') return '#';
    try {
        let raw = String(configuredUrl).trim();
        if (!/^https?:\/\//i.test(raw)) {
            raw = 'http://' + raw;
        }
        const parsed = new URL(raw);
        const currentProtocol = window.location.protocol;
        const currentHost = window.location.hostname;
        
        let port = parsed.port;
        if (!port) {
            if (parsed.protocol === 'https:') port = '443';
            else if (parsed.protocol === 'http:') port = '80';
        }
        
        const pathname = (parsed.pathname === '/' || !parsed.pathname) ? '' : parsed.pathname;
        const search = parsed.search || '';
        const hash = parsed.hash || '';

        const portSuffix = (port && port !== '80' && port !== '443') ? `:${port}` : '';
        return `${currentProtocol}//${currentHost}${portSuffix}${pathname}${search}${hash}`;
    } catch (e) {
        return '#';
    }
}

function updatePillLinks(urls) {
    const mappings = {
        sonarr: urls.SONARR_URL || urls.sonarr,
        radarr: urls.RADARR_URL || urls.radarr,
        plex:   urls.PLEX_URL || urls.plex,
        nzbget: urls.NZBGET_URL || urls.nzbget
    };
    for (const [key, rawUrl] of Object.entries(mappings)) {
        const pillEl = el(`pill-${key}`);
        if (pillEl) {
            const href = buildServiceUrl(rawUrl);
            pillEl.href = href;
            if (href === '#') {
                pillEl.removeAttribute('target');
            } else {
                pillEl.target = '_blank';
            }
        }
    }
}

// Global initialization
window.addEventListener('hashchange', handleHashChange);
window.addEventListener('DOMContentLoaded', () => {
    handleHashChange();
    pollAll();
    setInterval(pollAll, REFRESH_MS);
});
