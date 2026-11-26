/**
 * static/js/app.js
 * ----------------
 * Main UI logic, tab switching, polling loop, DOM rendering, and toast alerts.
 */

const VALID_TABS = ['dashboard', 'shows', 'movies', 'disks', 'activity', 'config'];
const REFRESH_MS = 10000;

let shutdownConfirmTimer = null;
let isConfirmingShutdown = false;

function el(id) { return document.getElementById(id); }
function setText(id, val) { const e = el(id); if (e) e.textContent = val; }

const SVG_ICONS = {
    trash: '<svg class="ui-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="3 6 5 6 21 6"/><path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6"/><path d="M10 11v6"/><path d="M14 11v6"/><path d="M9 6V4a1 1 0 0 1 1-1h4a1 1 0 0 1 1 1v2"/></svg>',
};

function escapeHtml(str) {
    if (!str) return '';
    return String(str).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

const MONTH_NAMES = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];

function formatDate(raw) {
    if (!raw) return '';
    const str = String(raw).trim();
    const m = str.match(/^(\d{4})-(\d{2})-(\d{2})(?:\s+(.+))?$/);
    if (!m) return str;
    const year = m[1];
    const monthIdx = parseInt(m[2], 10) - 1;
    const day = parseInt(m[3], 10);
    const timePart = m[4] ? ' • ' + m[4] : '';
    if (monthIdx < 0 || monthIdx > 11 || isNaN(day)) return str;
    return `${MONTH_NAMES[monthIdx]} ${day}, ${year}${timePart}`;
}

function switchTab(name, updateHash = true) {
    if (!VALID_TABS.includes(name)) name = 'dashboard';

    document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
    document.querySelectorAll('.tab-panel').forEach(p => p.classList.remove('active'));

    const btn = document.getElementById('tab-' + name);
    const panel = document.getElementById('panel-' + name);
    if (btn) btn.classList.add('active');
    if (panel) panel.classList.add('active');

    if (name === 'shows' || name === 'movies') {
        loadLibrary();
    }

    if (updateHash && window.location.hash !== '#' + name) {
        history.pushState(null, null, '#' + name);
    }
}


// ── Template Renderers ───────────────────────────────────────────────────────

function renderStreamCard(st) {
    const decBadge = st.decision === 'DIRECT PLAY' ? 'badge-green' : (st.decision === 'DIRECT STREAM' ? 'badge-blue' : 'badge-amber');
    const remText = st.remaining_mins > 0 ? st.remaining_mins + ' min left' : '0 min left';
    return `
    <div class="stream-card">
        <div class="stream-header">
            <div class="stream-user">
                <span class="stream-user-name">${escapeHtml(st.user)}</span>
                <span class="stream-device">${escapeHtml(st.device)}</span>
            </div>
            <div class="stream-badges">
                <span class="badge ${decBadge}">${escapeHtml(st.decision)}</span>
                <span class="badge badge-dim">${escapeHtml(st.location)}</span>
                <span class="stream-bw">${escapeHtml(st.bandwidth)}</span>
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
}

function renderDownloadCard(d, mIdx, mTotal) {
    const statusBadge = d.status === 'DOWNLOADING' ? 'badge-green' : (d.status === 'PAUSED' ? 'badge-amber' : 'badge-dim');
    const meta = d.size_mb > 0
        ? (d.size_mb - d.remaining_mb).toFixed(1) + ' / ' + d.size_mb + ' MB'
        : 'Starting...';
    const isMovable = mIdx >= 0;
    const reorderBtns = (isMovable && mTotal > 1)
        ? `<button class="btn-clear btn-sm" title="Move Up" ${mIdx === 0 ? 'disabled' : ''} onclick="moveDownload(${d.id}, 'up')">↑</button>
           <button class="btn-clear btn-sm" title="Move Down" ${mIdx === mTotal - 1 ? 'disabled' : ''} onclick="moveDownload(${d.id}, 'down')">↓</button>`
        : '';
    return `
    <div class="stream-card">
        <div class="stream-header">
            <div class="stream-user">
                <span class="stream-user-name">${escapeHtml(d.media_label || d.name)}</span>
            </div>
            <div class="stream-badges">
                <span class="badge ${statusBadge}">${escapeHtml(d.status)}</span>
                ${d.category ? `<span class="badge badge-dim">${escapeHtml(d.category)}</span>` : ''}
                ${reorderBtns}
                <button class="btn-clear btn-sm" onclick="cancelDownload(${d.id})">Cancel</button>
            </div>
        </div>
        <div class="download-nzb-name" title="${escapeHtml(d.name)}">${escapeHtml(d.name)}</div>
        <div class="stream-progress-wrap">
            <div class="stream-progress-meta">
                <span>${meta}</span>
                <span>${escapeHtml(d.eta)} (${d.progress_pct}%)</span>
            </div>
            <div class="stream-progress-track">
                <div class="stream-progress-fill" style="width: ${Math.min(100, Math.max(0, d.progress_pct))}%;"></div>
            </div>
        </div>
    </div>`;
}

function renderDiskSpaceCell(d) {
    const badgeColor = d.status === 'ok' ? 'green' : (d.status === 'warning' ? 'amber' : 'red');
    const fillColor = d.status === 'critical' ? '#e05252' : (d.status === 'warning' ? '#e5a00d' : '#3ecf8e');
    const folderFmt = d.folder_formatted || '0.0 GB';
    const folderPct = d.folder_pct !== undefined ? d.folder_pct : 0;
    return `
    <div class="stat-cell" style="flex:1;">
        <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:6px;">
            <span class="stat-label" style="font-weight:600; text-transform:uppercase;">${escapeHtml(d.label)}</span>
            <span class="badge badge-${badgeColor}">${d.free_pct}% Free</span>
        </div>
        <div class="stat-value ${badgeColor}" style="margin-bottom:8px;">${escapeHtml(folderFmt)} <span style="font-size:13px; font-weight:normal; color:var(--text-sub);">in folder (${escapeHtml(d.free_formatted)} free)</span></div>
        
        <div class="idle-track" style="margin-bottom:8px;">
            <div class="idle-fill" style="width: ${d.used_pct}%; background: ${fillColor};"></div>
        </div>
        
        <div class="stat-sub">
            <span>Folder Share: ${folderPct}% of drive</span>
            <span>Drive Used: ${escapeHtml(d.used_formatted)} / ${escapeHtml(d.total_formatted)}</span>
        </div>
        <div class="stat-sub" style="margin-top:8px;">
            <span>Path: <code>${escapeHtml(d.configured_path)}</code></span>
            <span style="opacity:0.7;">${escapeHtml(d.source)}</span>
        </div>
    </div>`;
}

function renderLogEntry(log, isOpen, logKey) {
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
                <span class="log-ts">${escapeHtml(formatDate(log.timestamp))}</span>
                <span class="log-tag ${gutter}">${escapeHtml(displayStatus)}</span>
            </div>
            <div class="log-msg">${escapeHtml(log.message)}</div>
            ${payloadHtml}
        </div>
    </div>`;
}

// ── Event Handlers & Core Updates ────────────────────────────────────────────

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
        const streamStr = s.stream_count === 1 ? '1 active stream' : `${s.stream_count} active streams`;
        label = `0 of ${idleNeeded} idle polls (Paused)`;
        detail = `Shutdown paused — ${streamStr}`;
    } else if (s.nzbget_active) {
        label = `0 of ${idleNeeded} idle polls (Paused)`;
        detail = `Shutdown paused — ${s.nzbget_detail || 'Usenet download active'}`;
    } else if (s.plex_activity_active) {
        label = `0 of ${idleNeeded} idle polls (Paused)`;
        detail = `Shutdown paused — ${s.plex_activity_detail || 'Media server background task running'}`;
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
            : s.idle_streak > 0 ? '#e5a00d'
                : '#3ecf8e';
    }

    const container = el('pw-streams-container');
    if (container) {
        if (!s.active_streams || s.active_streams.length === 0) {
            container.innerHTML = '';
        } else {
            const html = ['<div class="dash-section"><div class="dash-section-header"><span class="section-title" style="margin-bottom:0;">Active Streams</span></div>'];
            s.active_streams.forEach(st => html.push(renderStreamCard(st)));
            html.push('</div>');
            container.innerHTML = html.join('');
        }
    }
}

function _shortTime(ts) {
    if (!ts) return '—';
    const parts = String(ts).split(' ');
    return parts.length > 1 ? parts[1].substring(0, 5) : String(ts).substring(0, 5);
}

function updateEpisodePoller(s) {
    if (!s) return;
    const cnt = el('ep-count');
    if (cnt) {
        cnt.textContent = s.episodes_session || 0;
        cnt.className = (s.episodes_session > 0) ? 'green' : '';
    }

    setText('ep-scan-meta', `Last scan ${_shortTime(s.last_check)} • Next ${_shortTime(s.next_check)}`);

    let pct = 0;
    let label = 'Next scan: —';

    if (s.next_check && s.poll_interval) {
        const nextTime = new Date(s.next_check.replace(' ', 'T')).getTime();
        const now = Date.now();
        const diffMs = nextTime - now;

        if (diffMs <= 0) {
            pct = 100;
            label = 'Scanning now...';
        } else {
            const totalMs = s.poll_interval * 1000;
            const elapsedMs = totalMs - diffMs;
            pct = Math.min(100, Math.max(0, (elapsedMs / totalMs) * 100));

            const diffMins = Math.ceil(diffMs / 60000);
            if (diffMins > 60) {
                const h = Math.floor(diffMins / 60);
                const m = diffMins % 60;
                label = `Next scan in ~${h}h ${m}m`;
            } else {
                label = `Next scan in ~${diffMins} min`;
            }
        }
    }

    setText('ep-next-label', label);
    const bar = el('ep-bar');
    if (bar) {
        bar.style.width = pct + '%';
    }
}

function updateConnectionPills(d) {
    if (!d) return;
    ['nzbgeek', 'usenet', 'plex', 'jellyfin'].forEach(k => {
        if (d[k]) {
            const p = el(`pip-${k}`);
            if (p) {
                p.className = 'conn-pip ' + (d[k].ok ? 'ok' : ((d[k].status || '').includes('Error') ? 'warn' : 'err'));
            }
        }
    });
}

function updateDownloads(s) {
    if (!s) return;
    const pip = el('pip-usenet');
    if (pip) {
        pip.className = 'conn-pip ' + (s.enabled ? 'ok' : 'err');
    }

    const banner = el('disk-guard-banner');
    if (banner) {
        if (s.disk_guard_paused) {
            banner.innerHTML = `<div class="disk-guard-alert">⚠️ <strong>Disk Space Guard Active:</strong> ${escapeHtml(s.disk_guard_reason || 'Low disk space — downloads paused')}</div>`;
        } else {
            banner.innerHTML = '';
        }
    }

    const container = el('downloads-container');
    if (container) {
        if (!s.downloads || s.downloads.length === 0) {
            container.innerHTML = '';
        } else {
            const toggleBtn = s.download_paused
                ? `<button class="btn-clear" onclick="resumeDownloads()">Resume</button>`
                : `<button class="btn-clear" onclick="pauseDownloads()">Pause</button>`;
            const html = [
                `<div class="dash-section"><div class="dash-section-header">
                    <span class="section-title" style="margin-bottom:0;">Active Downloads</span>
                    <div style="display:flex; align-items:center; gap:10px;">
                        <span class="dl-rate">${escapeHtml(s.download_rate)}</span>
                        ${toggleBtn}
                    </div>
                </div>`
            ];
            const movableList = s.downloads.filter(d => d.status === 'DOWNLOADING' || d.status === 'QUEUED' || d.status === 'PAUSED');
            s.downloads.forEach(d => {
                const mIdx = movableList.indexOf(d);
                html.push(renderDownloadCard(d, mIdx, movableList.length));
            });
            html.push('</div>');
            container.innerHTML = html.join('');
        }
    }
}

function moveDownload(id, direction) {
    Api.moveDownload(id, direction).then(() => pollAll());
}

function pauseDownloads() {
    Api.pauseDownloads().then(() => {
        showToast('Downloads paused', 'info');
        pollAll();
    });
}

function resumeDownloads() {
    Api.resumeDownloads().then(() => {
        showToast('Downloads resumed', 'ok');
        pollAll();
    });
}

function cancelDownload(id) {
    Api.cancelDownload(id).then(() => {
        showToast('Download cancelled', 'info');
        pollAll();
    });
}

window.pauseDownloads = pauseDownloads;
window.resumeDownloads = resumeDownloads;
window.cancelDownload = cancelDownload;
window.moveDownload = moveDownload;


// ── Search & Library ─────────────────────────────────────────────────────────

let _lastSearchResults = [];

function _renderSearchCardActions(item, idx) {
    const isTv = item.type === 'tv';
    if (item.library_id) {
        const goLabel = isTv ? 'Go to Show' : 'Go to Movie';
        return `
            <button class="btn-clear btn-ep" onclick="goToSearchLibraryItem(${idx})">${goLabel}</button>
            <button type="button" class="btn-clear btn-ep btn-icon-trash" title="Remove from Library" onclick="removeSearchLibraryItem(${idx}, this)">${SVG_ICONS.trash}<span>Remove</span></button>
        `;
    }
    const btnLabel = isTv ? 'Add Show' : 'Add Movie';
    return `<button class="btn-clear btn-ep" onclick="addLibraryItem('${escapeHtml(item.type)}', ${idx}, this)">${btnLabel}</button>`;
}

function _renderSearchCard(item, idx) {
    const isTv = item.type === 'tv';
    const typeBadge = isTv
        ? '<span class="badge badge-blue" style="margin-left:6px;">TV</span>'
        : '<span class="badge badge-amber" style="margin-left:6px;">MOVIE</span>';
    return `
        <div class="media-card">
            ${item.poster_url ? `<img class="media-poster" src="${escapeHtml(item.poster_url)}" alt="">` : `<div class="media-poster media-poster-placeholder">?</div>`}
            <div class="media-card-info">
                <div class="media-card-title">${escapeHtml(item.title)} ${item.year ? `<span class="media-year">(${item.year})</span>` : ''}${typeBadge}</div>
                <div class="media-card-overview">${escapeHtml((item.overview || '').slice(0, 120))}</div>
                <div id="search-card-actions-${idx}" style="display:flex; align-items:center; gap:8px; flex-wrap:nowrap;">
                    ${_renderSearchCardActions(item, idx)}
                </div>
            </div>
        </div>`;
}

function clearSearch() {
    const input = el('search-media-query');
    const container = el('search-results-container');
    const clearBtn = el('btn-clear-search');
    if (input) input.value = '';
    if (container) container.innerHTML = '';
    if (clearBtn) clearBtn.style.display = 'none';
    _lastSearchResults = [];
}

function searchMedia() {
    const query = (el('search-media-query').value || '').trim();
    const btn = el('btn-search-media');
    const clearBtn = el('btn-clear-search');
    const container = el('search-results-container');
    if (!query) {
        clearSearch();
        return;
    }

    if (clearBtn) clearBtn.style.display = '';
    if (btn) {
        btn.disabled = true;
        btn.textContent = 'Searching...';
    }
    container.innerHTML = '<div class="no-logs">Searching movies & TV shows...</div>';

    Api.searchMedia(query, 'all')
        .then(({ ok, data }) => {
            if (!ok) {
                showToast(data.message || 'Search failed', 'err');
                container.innerHTML = '';
                return;
            }
            const movies = (data.movies || []).map(m => ({ ...m, type: 'movie' }));
            const tv = (data.tv || []).map(t => ({ ...t, type: 'tv' }));
            _lastSearchResults = [...movies, ...tv];

            if (_lastSearchResults.length === 0) {
                container.innerHTML = '<div class="no-logs">No movies or TV shows found.</div>';
                return;
            }

            let html = '';
            if (movies.length > 0) {
                html += `<p class="section-title" style="margin:12px 0 10px;">Movies (${movies.length})</p>`;
                html += `<div class="media-grid">${movies.map((m, i) => _renderSearchCard(m, i)).join('')}</div>`;
            }
            if (tv.length > 0) {
                const offset = movies.length;
                html += `<p class="section-title" style="margin:20px 0 10px;">TV Shows (${tv.length})</p>`;
                html += `<div class="media-grid">${tv.map((t, i) => _renderSearchCard(t, offset + i)).join('')}</div>`;
            }
            container.innerHTML = html;
        })
        .catch(() => {
            showToast('Search request failed', 'err');
            container.innerHTML = '';
        })
        .finally(() => {
            if (btn) {
                btn.disabled = false;
                btn.textContent = 'Search';
            }
        });
}

function addLibraryItem(type, idx, btnEl) {
    const item = _lastSearchResults[idx];
    if (!item) return;
    if (btnEl) {
        btnEl.disabled = true;
        btnEl.textContent = 'Adding...';
    }
    const payload = {
        id: item.id,
        title: item.title,
        year: item.year,
        imdb_id: item.imdb_id || '',
        poster_url: item.poster_url || ''
    };
    Api.addMedia(type, payload)
        .then(({ ok, data }) => {
            const isSuccess = ok && data && data.status === 'success';
            const toastType = isSuccess ? 'ok' : (ok && data && data.status === 'warning' ? 'warn' : 'err');
            showToast(data.message || 'Added to library', toastType);
            if (ok && data) {
                const addedObj = data.show || data.movie || null;
                item.library_id = (addedObj && addedObj.id) ? addedObj.id : true;
                const actionsWrap = el(`search-card-actions-${idx}`);
                if (actionsWrap) {
                    actionsWrap.innerHTML = _renderSearchCardActions(item, idx);
                }
            } else if (btnEl) {
                btnEl.disabled = false;
                btnEl.textContent = 'Retry';
            }
            loadLibrary();
            setTimeout(pollAll, 500);
        })
        .catch(() => {
            showToast('Failed to add media', 'err');
            if (btnEl) {
                btnEl.disabled = false;
                btnEl.textContent = item.type === 'tv' ? 'Add Show' : 'Add Movie';
            }
        });
}

function goToSearchLibraryItem(idx) {
    const item = _lastSearchResults[idx];
    if (!item) return;
    const isTv = item.type === 'tv';
    const targetTab = isTv ? 'shows' : 'movies';

    document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
    document.querySelectorAll('.tab-panel').forEach(p => p.classList.remove('active'));
    const btn = document.getElementById('tab-' + targetTab);
    const panel = document.getElementById('panel-' + targetTab);
    if (btn) btn.classList.add('active');
    if (panel) panel.classList.add('active');
    if (window.location.hash !== '#' + targetTab) {
        history.pushState(null, null, '#' + targetTab);
    }

    loadLibrary().then(() => {
        if (isTv && item.library_id && item.library_id !== true) {
            const det = document.querySelector(`#library-shows-container details[data-show-id="${item.library_id}"]`);
            if (det) {
                det.open = true;
                loadShowEpisodes(item.library_id, false);
                det.scrollIntoView({ behavior: 'smooth', block: 'start' });
            }
        } else if (!isTv && item.library_id && item.library_id !== true) {
            const row = document.querySelector(`#library-movies-container .library-row[data-movie-id="${item.library_id}"]`);
            if (row) {
                row.scrollIntoView({ behavior: 'smooth', block: 'center' });
            }
        }
    });
}

function removeSearchLibraryItem(idx, btnEl) {
    const item = _lastSearchResults[idx];
    if (!item || !item.library_id) return;
    if (btnEl && btnEl.dataset.confirming !== '1') {
        const origHtml = btnEl.innerHTML;
        btnEl.dataset.confirming = '1';
        btnEl.dataset.origHtml = origHtml;
        btnEl.innerHTML = `${SVG_ICONS.trash}<span>Confirm?</span>`;
        btnEl.style.background = 'var(--red-dim)';
        btnEl.style.borderColor = 'var(--red)';
        btnEl.style.color = 'var(--red)';
        btnEl.style.fontWeight = '600';
        btnEl._confirmTimer = setTimeout(() => {
            btnEl.dataset.confirming = '';
            btnEl.innerHTML = btnEl.dataset.origHtml || `${SVG_ICONS.trash}<span>Remove</span>`;
            btnEl.style.background = '';
            btnEl.style.borderColor = '';
            btnEl.style.color = '';
            btnEl.style.fontWeight = '';
        }, 5000);
        return;
    }
    if (btnEl && btnEl._confirmTimer) clearTimeout(btnEl._confirmTimer);
    if (btnEl) btnEl.disabled = true;

    Api.deleteLibraryItem(item.type, item.library_id).then(() => {
        showToast('Removed from tracked library & disk', 'info');
        item.library_id = null;
        const actionsWrap = el(`search-card-actions-${idx}`);
        if (actionsWrap) {
            actionsWrap.innerHTML = _renderSearchCardActions(item, idx);
        }
        loadLibrary();
        Api.fetchDiskSpace().then(d => { if (d) renderDiskSpace(d); });
    });
}

function queueShowFrom(showId, startSeason, startEpisode, btnEl) {
    const s = startSeason || parseInt((el(`show-s-${showId}`) || {}).value) || 1;
    const e = startEpisode || parseInt((el(`show-e-${showId}`) || {}).value) || 1;
    if (btnEl) {
        btnEl.disabled = true;
        btnEl.textContent = 'Queueing...';
    }
    Api.queueShowFrom(showId, s, e)
        .then(({ ok, data }) => {
            showToast(data.message || 'Episodes queued', ok && data.status === 'success' ? 'ok' : 'info');
            loadShowEpisodes(showId, true);
            setTimeout(pollAll, 500);
        })
        .catch(() => showToast('Failed to queue episodes', 'err'))
        .finally(() => {
            if (btnEl) {
                btnEl.disabled = false;
                btnEl.textContent = 'Start Here';
            }
        });
}

function queueSingleEpisode(showId, season, episode, btnEl) {
    if (btnEl) {
        btnEl.disabled = true;
        btnEl.textContent = '...';
    }
    Api.queueSingleEpisode(showId, season, episode)
        .then(({ ok, data }) => {
            showToast(data.message || 'Episode queued', ok && data.status === 'success' ? 'ok' : 'info');
            loadShowEpisodes(showId, true);
            setTimeout(pollAll, 500);
        })
        .catch(() => showToast('Failed to queue episode', 'err'))
        .finally(() => {
            if (btnEl) {
                btnEl.disabled = false;
                btnEl.textContent = 'Get';
            }
        });
}

const _openSeasonsByShow = {};

function onSeasonToggle(detailsEl, showId, seasonNum) {
    const key = String(showId);
    if (!_openSeasonsByShow[key]) _openSeasonsByShow[key] = new Set();
    if (detailsEl.open) {
        _openSeasonsByShow[key].add(String(seasonNum));
    } else {
        _openSeasonsByShow[key].delete(String(seasonNum));
    }
}

function onShowToggle(detailsEl, showId) {
    if (detailsEl.open) {
        loadShowEpisodes(showId, false);
    }
}

function loadShowEpisodes(showId, forceReload = false) {
    const body = el(`show-episodes-${showId}`);
    if (!body) return;
    if (!forceReload && body.dataset.loaded === '1') return;

    const showKey = String(showId);
    if (!_openSeasonsByShow[showKey]) _openSeasonsByShow[showKey] = new Set();
    const openSeasons = _openSeasonsByShow[showKey];
    body.querySelectorAll('details.season-card[data-season]').forEach(d => {
        if (d.open) openSeasons.add(d.getAttribute('data-season'));
    });

    if (!forceReload) {
        body.innerHTML = '<div style="font-size:11px; color:var(--text-sub); font-family:var(--mono); padding:6px 0;">Loading seasons & episodes...</div>';
    }
    Api.fetchShowEpisodes(showId).then(data => {
        if (!data || data.status !== 'success') {
            body.innerHTML = '<div style="font-size:11px; color:var(--red);">Failed to load episodes.</div>';
            return;
        }
        body.dataset.loaded = '1';
        const seasons = data.seasons || [];
        if (seasons.length === 0) {
            body.innerHTML = '<div style="font-size:11px; color:var(--text-sub);">No episodes found on TVMaze.</div>';
            return;
        }
        body.innerHTML = seasons.map(sec => `
            <details class="season-card" data-season="${sec.season}" ${openSeasons.has(String(sec.season)) ? 'open' : ''} ontoggle="onSeasonToggle(this, ${showId}, ${sec.season})">
                <summary class="season-summary">
                    <span>Season ${sec.season}</span>
                    <span style="display:flex; align-items:center; gap:8px;">
                        <span class="badge ${sec.downloaded > 0 ? 'badge-green' : 'badge-dim'}">${sec.downloaded} / ${sec.total} on disk</span>
                        ${sec.downloaded > 0 ? `<button type="button" class="btn-clear btn-icon-trash" title="Delete all on-disk episodes in Season ${sec.season}" onclick="event.preventDefault(); event.stopPropagation(); deleteSeasonFiles(${showId}, ${sec.season}, this)">${SVG_ICONS.trash}</button>` : ''}
                    </span>
                </summary>
                <div class="season-episodes">
                    ${sec.episodes.map(ep => {
            const tag = `S${String(ep.season).padStart(2, '0')}E${String(ep.episode).padStart(2, '0')}`;
            const st = ep.status;
            const badgeCls = st === 'downloaded' ? 'badge-green'
                : st === 'queued' ? 'badge-blue'
                    : st === 'missing' ? 'badge-amber'
                        : 'badge-dim';
            const badgeTxt = st === 'downloaded' ? 'ON DISK' : st.toUpperCase();
            let actions = '';
            if (st === 'downloaded') {
                actions = `<button class="btn-clear btn-ep" onclick="openEpisodePicker(${showId}, ${ep.season}, ${ep.episode})">Pick</button>
                           <button type="button" class="btn-clear btn-icon-trash" title="Delete episode file from disk" onclick="deleteEpisodeFile(${showId}, ${ep.season}, ${ep.episode}, this)">${SVG_ICONS.trash}</button>`;
            } else if (st === 'queued') {
                actions = `<button type="button" class="btn-clear btn-icon-trash" title="Cancel queued episode download" onclick="deleteEpisodeFile(${showId}, ${ep.season}, ${ep.episode}, this)">${SVG_ICONS.trash}</button>`;
            } else if (st === 'missing' || st === 'watched' || st === 'upcoming') {
                actions = `<button class="btn-clear btn-ep" onclick="queueSingleEpisode(${showId}, ${ep.season}, ${ep.episode}, this)">Get</button>
                           <button class="btn-clear btn-ep" onclick="openEpisodePicker(${showId}, ${ep.season}, ${ep.episode})">Pick</button>
                           <button class="btn-clear btn-ep" onclick="queueShowFrom(${showId}, ${ep.season}, ${ep.episode}, this)">Start Here</button>`;
            }
            return `
                        <div class="ep-row">
                            <div class="ep-row-left">
                                <span class="ep-tag">${tag}</span>
                                <span class="ep-title">${escapeHtml(ep.title)}</span>
                                ${ep.airdate ? `<span class="ep-airdate">${escapeHtml(formatDate(ep.airdate))}</span>` : ''}
                            </div>
                            <div class="ep-row-right">
                                <span class="badge ${badgeCls}">${badgeTxt}</span>
                                ${actions}
                            </div>
                        </div>`;
        }).join('')}
                </div>
            </details>
        `).join('');
    });
}

function deleteEpisodeFile(showId, season, episode, btnEl) {
    if (btnEl && btnEl.dataset.confirming !== '1') {
        const origHtml = btnEl.innerHTML;
        btnEl.dataset.confirming = '1';
        btnEl.dataset.origHtml = origHtml;
        btnEl.innerHTML = `${SVG_ICONS.trash}<span>Confirm?</span>`;
        btnEl.style.background = 'var(--red-dim)';
        btnEl.style.borderColor = 'var(--red)';
        btnEl.style.color = 'var(--red)';
        btnEl.style.fontWeight = '600';
        btnEl._confirmTimer = setTimeout(() => {
            btnEl.dataset.confirming = '';
            btnEl.innerHTML = btnEl.dataset.origHtml || SVG_ICONS.trash;
            btnEl.style.background = '';
            btnEl.style.borderColor = '';
            btnEl.style.color = '';
            btnEl.style.fontWeight = '';
        }, 5000);
        return;
    }
    if (btnEl && btnEl._confirmTimer) clearTimeout(btnEl._confirmTimer);
    if (btnEl) btnEl.disabled = true;

    Api.deleteSingleEpisode(showId, season, episode)
        .then(({ ok, data }) => {
            showToast((data && data.message) || 'Episode deleted', ok ? 'info' : 'err');
            loadLibrary();
            setTimeout(pollAll, 500);
        })
        .catch(() => {
            showToast('Failed to delete episode', 'err');
            if (btnEl) btnEl.disabled = false;
        });
}

function deleteSeasonFiles(showId, season, btnEl) {
    if (btnEl && btnEl.dataset.confirming !== '1') {
        const origHtml = btnEl.innerHTML;
        btnEl.dataset.confirming = '1';
        btnEl.dataset.origHtml = origHtml;
        btnEl.innerHTML = `${SVG_ICONS.trash}<span>Confirm?</span>`;
        btnEl.style.background = 'var(--red-dim)';
        btnEl.style.borderColor = 'var(--red)';
        btnEl.style.color = 'var(--red)';
        btnEl.style.fontWeight = '600';
        btnEl._confirmTimer = setTimeout(() => {
            btnEl.dataset.confirming = '';
            btnEl.innerHTML = btnEl.dataset.origHtml || SVG_ICONS.trash;
            btnEl.style.background = '';
            btnEl.style.borderColor = '';
            btnEl.style.color = '';
            btnEl.style.fontWeight = '';
        }, 5000);
        return;
    }
    if (btnEl && btnEl._confirmTimer) clearTimeout(btnEl._confirmTimer);
    if (btnEl) btnEl.disabled = true;

    Api.deleteSeasonEpisodes(showId, season)
        .then(({ ok, data }) => {
            showToast((data && data.message) || 'Season files deleted', ok ? 'info' : 'err');
            loadLibrary();
            setTimeout(pollAll, 500);
        })
        .catch(() => {
            showToast('Failed to delete season files', 'err');
            if (btnEl) btnEl.disabled = false;
        });
}

function deleteLibraryItem(type, id, btnEl) {
    if (btnEl && btnEl.dataset.confirming !== '1') {
        const origHtml = btnEl.innerHTML;
        btnEl.dataset.confirming = '1';
        btnEl.dataset.origHtml = origHtml;
        btnEl.innerHTML = `${SVG_ICONS.trash}<span>Confirm?</span>`;
        btnEl.style.background = 'var(--red-dim)';
        btnEl.style.borderColor = 'var(--red)';
        btnEl.style.color = 'var(--red)';
        btnEl.style.fontWeight = '600';
        btnEl._confirmTimer = setTimeout(() => {
            btnEl.dataset.confirming = '';
            btnEl.innerHTML = btnEl.dataset.origHtml || SVG_ICONS.trash;
            btnEl.style.background = '';
            btnEl.style.borderColor = '';
            btnEl.style.color = '';
            btnEl.style.fontWeight = '';
        }, 5000);
        return;
    }
    if (btnEl && btnEl._confirmTimer) {
        clearTimeout(btnEl._confirmTimer);
    }
    if (btnEl) {
        btnEl.disabled = true;
    }
    Api.deleteLibraryItem(type, id).then(() => {
        showToast('Removed from tracked library & disk', 'info');
        loadLibrary();
        Api.fetchDiskSpace().then(d => { if (d) renderDiskSpace(d); });
    });
}

function loadLibrary() {
    return Api.fetchLibrary().then(data => {
        if (!data) return;
        const showsEl = el('library-shows-container');
        const moviesEl = el('library-movies-container');

        if (showsEl) {
            const shows = data.shows || [];
            if (shows.length === 0) {
                showsEl.innerHTML = '<div class="no-logs">No tracked TV shows yet.</div>';
            } else {
                const openIds = new Set();
                showsEl.querySelectorAll('details[data-show-id]').forEach(d => {
                    const sid = d.getAttribute('data-show-id');
                    if (d.open) openIds.add(sid);
                    const openSec = new Set();
                    d.querySelectorAll('details.season-card[data-season]').forEach(sd => {
                        if (sd.open) openSec.add(sd.getAttribute('data-season'));
                    });
                    if (openSec.size > 0) _openSeasonsByShow[String(sid)] = openSec;
                });
                showsEl.innerHTML = shows.map(s => {
                    const st = (s.status || 'idle').toLowerCase();
                    const sBadge = st === 'downloading' ? 'badge-blue'
                        : (st.includes('on disk') || st === 'active' || st.startsWith('caught up')) ? 'badge-green'
                            : 'badge-dim';
                    return `
                    <details class="config-card" data-show-id="${s.id}" ${openIds.has(String(s.id)) ? 'open' : ''} ontoggle="onShowToggle(this, ${s.id})" style="margin-bottom:0;">
                        <summary class="config-card-summary">
                            <span style="display:flex; align-items:center; gap:10px; min-width:0;">
                                ${s.poster_url ? `<img class="lib-thumb" src="${escapeHtml(s.poster_url)}" alt="">` : `<span class="lib-thumb lib-thumb-placeholder">TV</span>`}
                                <strong>${escapeHtml(s.title)}</strong>
                                ${s.year ? `<span class="media-year">(${s.year})</span>` : ''}
                                <span class="badge ${sBadge}">${escapeHtml(s.status || 'idle')}</span>
                            </span>
                            <button type="button" class="btn-clear btn-icon-trash" style="margin-left:auto; margin-right:12px;" title="Remove Show" onclick="event.preventDefault(); event.stopPropagation(); deleteLibraryItem('tv', ${s.id}, this)">${SVG_ICONS.trash}</button>
                        </summary>
                        <div class="config-card-body">
                            <div id="show-episodes-${s.id}"></div>
                        </div>
                    </details>
                `;
                }).join('');
                openIds.forEach(id => loadShowEpisodes(parseInt(id), true));
            }
        }

        if (moviesEl) {
            const movies = data.movies || [];
            if (movies.length === 0) {
                moviesEl.innerHTML = '<div class="no-logs">No tracked movies yet.</div>';
            } else {
                moviesEl.innerHTML = movies.map(m => {
                    const st = (m.status || 'wanted').toLowerCase();
                    const badgeCls = st === 'downloaded' ? 'badge-green'
                        : st === 'wanted' ? 'badge-amber'
                            : st === 'downloading' ? 'badge-blue'
                                : 'badge-dim';
                    return `
                    <div class="library-row" data-movie-id="${m.id}">
                        <div style="display:flex; align-items:center; gap:10px;">
                            ${m.poster_url ? `<img class="lib-thumb" src="${escapeHtml(m.poster_url)}" alt="">` : `<span class="lib-thumb lib-thumb-placeholder">MOV</span>`}
                            <strong>${escapeHtml(m.title)}</strong>
                            ${m.year ? `<span class="media-year">(${m.year})</span>` : ''}
                            <span class="badge ${badgeCls}">${escapeHtml(m.status || 'wanted')}</span>
                        </div>
                        <div class="ep-controls-row">
                            ${st === 'wanted' ? `<button class="btn-clear btn-ep" onclick="queueSingleMovie(${m.id}, this)">Get</button><button class="btn-clear btn-ep" onclick="openMoviePicker(${m.id})">Pick</button>` : ''}
                            <button type="button" class="btn-clear btn-icon-trash" title="Remove Movie" onclick="deleteLibraryItem('movie', ${m.id}, this)">${SVG_ICONS.trash}</button>
                        </div>
                    </div>`;
                }).join('');
            }
        }
    });
}

let _pickerContext = null;

function _renderPickerModal(data) {
    const body = el('release-modal-body');
    if (!body) return;
    if (!data || data.status !== 'success') {
        body.innerHTML = '<div class="no-logs">Failed to search NZBgeek.</div>';
        return;
    }
    _pickerContext = data;
    const rels = data.releases || [];
    if (rels.length === 0) {
        body.innerHTML = '<div class="no-logs">No matching releases found on NZBgeek.</div>';
        return;
    }
    body.innerHTML = rels.map((r, idx) => `
        <div class="release-row">
            <div style="min-width:0; flex:1;">
                <div class="release-title">${escapeHtml(r.nzb_title)}</div>
                <div class="release-meta">
                    <span class="badge badge-blue">${escapeHtml(r.resolution)}</span>
                    <span class="badge badge-dim">${escapeHtml(r.source)}</span>
                    <span class="badge badge-dim">${escapeHtml(r.audio)}</span>
                    <span>${r.size_mb >= 1024 ? (r.size_mb / 1024).toFixed(2) + ' GB' : r.size_mb + ' MB'}</span>
                    <span>Score: ${r.score}</span>
                    ${r.grabs ? `<span>Grabs: ${r.grabs}</span>` : ''}
                    ${r.blocklisted ? '<span class="badge badge-red">BLOCKLISTED</span>' : ''}
                </div>
            </div>
            <div style="display:flex; align-items:center; gap:8px; flex-shrink:0;">
                <button class="btn-clear" onclick="toggleBlocklistPickedRelease(${idx}, this)">${r.blocklisted ? 'Unblock' : 'Blocklist'}</button>
                <button class="btn-clear" onclick="grabPickedRelease(${idx}, this)">Grab</button>
            </div>
        </div>
    `).join('');
}

function queueSingleMovie(movieId, btnEl) {
    if (btnEl) {
        btnEl.disabled = true;
        btnEl.textContent = '...';
    }
    Api.queueMovie(movieId)
        .then(({ ok, data }) => {
            showToast(data.message || 'Movie queued', ok && data.status === 'success' ? 'ok' : 'warn');
            loadLibrary();
            setTimeout(pollAll, 500);
        })
        .catch(() => showToast('Failed to queue movie', 'err'))
        .finally(() => {
            if (btnEl) {
                btnEl.disabled = false;
                btnEl.textContent = 'Get';
            }
        });
}

function openMoviePicker(movieId) {
    const modal = el('release-modal');
    if (!modal) return;
    setText('release-modal-title', 'Manual Release Search — Movie');
    el('release-modal-body').innerHTML = '<div class="no-logs">Searching NZBgeek...</div>';
    modal.style.display = 'flex';
    Api.fetchMovieReleases(movieId).then(_renderPickerModal);
}

function openEpisodePicker(showId, season, episode) {
    const modal = el('release-modal');
    if (!modal) return;
    const tag = `S${String(season).padStart(2, '0')}E${String(episode).padStart(2, '0')}`;
    setText('release-modal-title', `Manual Release Search — ${tag}`);
    el('release-modal-body').innerHTML = '<div class="no-logs">Searching NZBgeek...</div>';
    modal.style.display = 'flex';
    Api.fetchEpisodeReleases(showId, season, episode).then(_renderPickerModal);
}

function grabPickedRelease(idx, btnEl) {
    if (!_pickerContext || !_pickerContext.releases[idx]) return;
    const r = _pickerContext.releases[idx];
    if (btnEl) {
        btnEl.disabled = true;
        btnEl.textContent = 'Queueing...';
    }
    Api.grabManualRelease({
        media_type: _pickerContext.media_type,
        media_id: _pickerContext.media_id,
        title: _pickerContext.title,
        year: _pickerContext.year,
        season: _pickerContext.season,
        episode: _pickerContext.episode,
        nzb_title: r.nzb_title,
        nzb_url: r.nzb_url,
        nzb_guid: r.nzb_guid,
        size_bytes: r.size_bytes
    }).then(({ ok, data }) => {
        showToast(data.message || 'Release queued', ok ? 'ok' : 'err');
        if (ok) {
            closeReleaseModal();
            if (_pickerContext.media_type === 'episode') loadShowEpisodes(_pickerContext.media_id, true);
            else loadLibrary();
            setTimeout(pollAll, 500);
        } else if (btnEl) {
            btnEl.disabled = false;
            btnEl.textContent = 'Grab';
        }
    });
}

function toggleBlocklistPickedRelease(idx, btnEl) {
    if (!_pickerContext || !_pickerContext.releases || !_pickerContext.releases[idx]) return;
    const r = _pickerContext.releases[idx];
    const shouldBlock = !r.blocklisted;
    const origText = btnEl ? btnEl.textContent : '';
    if (btnEl) {
        btnEl.disabled = true;
        btnEl.textContent = '...';
    }
    Api.toggleBlocklistRelease({
        nzb_guid: r.nzb_guid || r.nzb_url || r.nzb_title,
        nzb_title: r.nzb_title
    }, shouldBlock).then(({ ok, data }) => {
        if (ok && data && data.status === 'success') {
            r.blocklisted = Boolean(data.blocklisted);
            showToast(data.message || (r.blocklisted ? 'Release blocklisted' : 'Release unblocklisted'), r.blocklisted ? 'warn' : 'info');
            _renderPickerModal(_pickerContext);
            setTimeout(pollAll, 500);
        } else {
            showToast((data && data.message) || 'Failed to update blocklist', 'err');
            if (btnEl) {
                btnEl.disabled = false;
                btnEl.textContent = origText;
            }
        }
    }).catch(() => {
        showToast('Failed to update blocklist', 'err');
        if (btnEl) {
            btnEl.disabled = false;
            btnEl.textContent = origText;
        }
    });
}

function closeReleaseModal() {
    const modal = el('release-modal');
    if (modal) modal.style.display = 'none';
}

function loadCalendar() {
    const container = el('calendar-container');
    if (!container) return;
    Api.fetchCalendar().then(data => {
        const items = (data && data.upcoming) || [];
        if (items.length === 0) {
            container.innerHTML = '';
            return;
        }
        const rows = items.map(it => {
            const tag = `S${String(it.season).padStart(2, '0')}E${String(it.episode).padStart(2, '0')}`;
            const when = it.days_away === 0 ? 'Today' : (it.days_away === 1 ? 'Tomorrow' : `In ${it.days_away}d`);
            const whenCls = it.days_away === 0 ? 'badge-green' : (it.days_away === 1 ? 'badge-amber' : 'badge-dim');
            return `
            <div class="cal-row">
                <div style="display:flex; align-items:center; gap:10px; min-width:0;">
                    ${it.poster_url ? `<img class="lib-thumb" src="${escapeHtml(it.poster_url)}" alt="">` : `<span class="lib-thumb lib-thumb-placeholder">TV</span>`}
                    <div style="min-width:0;">
                        <div><strong>${escapeHtml(it.show_title)}</strong> <span class="ep-tag">${tag}</span></div>
                        <div class="cal-sub">${escapeHtml(it.ep_title)}</div>
                    </div>
                </div>
                <div style="display:flex; align-items:center; gap:8px; flex-shrink:0;">
                    <span class="ep-airdate">${escapeHtml(formatDate(it.airdate))}</span>
                    <span class="badge ${whenCls}">${when}</span>
                    ${it.on_disk ? '<span class="badge badge-green">ON DISK</span>' : ''}
                </div>
            </div>`;
        }).join('');
        container.innerHTML = `<div class="dash-section"><div class="dash-section-header"><span class="section-title" style="margin-bottom:0;">Airing This Week</span></div><div class="media-list">${rows}</div></div>`;
    });
}

function runAutoGrab() {
    const btn = el('btn-run-autograb');
    if (btn) {
        btn.disabled = true;
        btn.textContent = 'Scanning...';
    }
    Api.runAutoGrab()
        .then(({ data }) => {
            showToast((data && data.message) || 'Auto-grab finished', 'info');
            setTimeout(pollAll, 500);
        })
        .catch(() => showToast('Auto-grab failed', 'err'))
        .finally(() => {
            if (btn) {
                btn.disabled = false;
                btn.textContent = 'Grab Wanted';
            }
        });
}

function focusDashboardSearch() {
    switchTab('dashboard');
    const input = el('search-media-query');
    if (input) {
        input.focus();
        input.scrollIntoView({ behavior: 'smooth', block: 'center' });
    }
}

window.focusDashboardSearch = focusDashboardSearch;
window.clearSearch = clearSearch;
window.searchMedia = searchMedia;
window.addLibraryItem = addLibraryItem;
window.queueShowFrom = queueShowFrom;
window.queueSingleEpisode = queueSingleEpisode;
window.onShowToggle = onShowToggle;
window.onSeasonToggle = onSeasonToggle;
window.deleteLibraryItem = deleteLibraryItem;
window.loadLibrary = loadLibrary;
window.openMoviePicker = openMoviePicker;
window.openEpisodePicker = openEpisodePicker;
window.grabPickedRelease = grabPickedRelease;
window.toggleBlocklistPickedRelease = toggleBlocklistPickedRelease;
window.closeReleaseModal = closeReleaseModal;
window.runAutoGrab = runAutoGrab;


// ── Disk, Activity & Polling ─────────────────────────────────────────────────

function renderDiskSpace(data) {
    const grid = el('disk-grid');
    if (!grid || !data || !data.disks) return;
    grid.innerHTML = Object.entries(data.disks).map(([key, d]) => renderDiskSpaceCell(d)).join('');

    const largestEl = el('disk-largest-container');
    if (largestEl) {
        const items = data.largest_items || [];
        if (items.length === 0) {
            largestEl.innerHTML = '<div class="no-logs">No media files found on disk.</div>';
        } else {
            largestEl.innerHTML = items.map(it => {
                const badgeCls = it.type === 'tv' ? 'badge-blue' : 'badge-amber';
                const badgeLbl = it.type === 'tv' ? 'TV' : 'MOVIE';
                const delBtn = it.id
                    ? `<button type="button" class="btn-clear btn-icon-trash" title="Delete from disk & library" onclick="deleteLibraryItem('${escapeHtml(it.type)}', ${it.id}, this)">${SVG_ICONS.trash}</button>`
                    : '';
                return `
                <div class="library-row">
                    <div style="display:flex; align-items:center; gap:10px; min-width:0;">
                        <span class="badge ${badgeCls}">${badgeLbl}</span>
                        <strong>${escapeHtml(it.title)}</strong>
                        ${it.year ? `<span class="media-year">(${it.year})</span>` : ''}
                        <span style="font-size:12px; color:var(--text-sub);">${escapeHtml(it.detail || '')}</span>
                    </div>
                    <div class="ep-controls-row" style="display:flex; align-items:center; gap:10px;">
                        <span class="badge badge-dim" style="font-family:var(--mono);">${escapeHtml(it.size_formatted)} (${it.pct_of_disk}%)</span>
                        ${delBtn}
                    </div>
                </div>`;
            }).join('');
        }
    }
}

function updateActivityLog(logs) {
    const container = el('activity-log-container');
    if (!container) return;

    if (!logs || logs.length === 0) {
        container.className = 'no-logs';
        container.innerHTML = 'No activity recorded yet.';
        return;
    }

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
        return renderLogEntry(log, isOpen, logKey);
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
    Api.fetchDownloadsStatus().then(d => { if (d) updateDownloads(d); });
    Api.fetchActivityLogs().then(d => { if (d) updateActivityLog(d); });
    Api.fetchDiskSpace().then(d => { if (d) renderDiskSpace(d); });
}

function pollDiskSpace() {
    const btn = document.querySelector('#panel-disks .btn-clear');
    const origText = btn ? btn.textContent : 'Refresh Storage';
    if (btn) {
        btn.disabled = true;
        btn.textContent = 'Refreshing...';
    }
    Api.fetchDiskSpace()
        .then(d => {
            if (d) {
                renderDiskSpace(d);
                showToast('Storage space refreshed', 'info');
            }
        })
        .catch(() => showToast('Failed to refresh storage space', 'err'))
        .finally(() => {
            if (btn) {
                btn.disabled = false;
                btn.textContent = origText;
            }
        });
}

window.pollDiskSpace = pollDiskSpace;
window.pollAll = pollAll;

function clearLogs() {
    const btn = el('btn-clear-logs');
    if (btn) btn.disabled = true;
    Api.clearLogs()
        .then(() => {
            updateActivityLog([]);
            showToast('Activity logs cleared', 'info');
        })
        .catch(() => showToast('Failed to clear logs', 'err'))
        .finally(() => { if (btn) btn.disabled = false; });
}

window.clearLogs = clearLogs;

function runPoller() {
    const btn = el('btn-run-poller');
    if (btn) {
        btn.disabled = true;
        btn.textContent = 'Scanning...';
    }
    Api.runPoller()
        .then(({ ok, data }) => {
            showToast(data.message || 'Watched scan triggered', ok ? 'info' : 'err');
            setTimeout(pollAll, 1000);
        })
        .catch(() => showToast('Failed to trigger scan', 'err'))
        .finally(() => {
            if (btn) {
                btn.disabled = false;
                btn.textContent = 'Scan Watched';
            }
        });
}

window.runPoller = runPoller;

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
        btn.textContent = 'Confirm Shutdown?';
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
        NZBGEEK_URL: el('cfg-NZBGEEK_URL').value,
        NZBGEEK_API_KEY: el('cfg-NZBGEEK_API_KEY').value,
        NNTP_HOST: el('cfg-NNTP_HOST').value,
        NNTP_PORT: parseInt(el('cfg-NNTP_PORT').value) || 563,
        NNTP_SSL: el('cfg-NNTP_SSL').value === 'true',
        NNTP_USERNAME: el('cfg-NNTP_USERNAME').value,
        NNTP_PASSWORD: el('cfg-NNTP_PASSWORD').value,
        NNTP_CONNECTIONS: parseInt(el('cfg-NNTP_CONNECTIONS').value) || 8,
        TMDB_API_KEY: el('cfg-TMDB_API_KEY').value,
        PREFERRED_RESOLUTION: el('cfg-PREFERRED_RESOLUTION').value,
        PREFERRED_AUDIO: el('cfg-PREFERRED_AUDIO').value,
        MAX_EPISODE_SIZE_GB: parseInt(el('cfg-MAX_EPISODE_SIZE_GB').value) || 8,
        MAX_MOVIE_SIZE_GB: parseInt(el('cfg-MAX_MOVIE_SIZE_GB').value) || 25,
        REJECT_DV_WITHOUT_HDR: el('cfg-REJECT_DV_WITHOUT_HDR').value === 'true',
        REJECT_KEYWORDS: el('cfg-REJECT_KEYWORDS').value,
        AUTO_GRAB_INTERVAL_HOURS: parseInt(el('cfg-AUTO_GRAB_INTERVAL_HOURS').value) || 0,
        MIN_FREE_DISK_GB: parseInt(el('cfg-MIN_FREE_DISK_GB').value) || 0,
        SUBTITLES_ENABLED: el('cfg-SUBTITLES_ENABLED').value === 'true',
        SUBTITLE_LANGUAGES: el('cfg-SUBTITLE_LANGUAGES').value,
        ROLLING_WINDOW: parseInt(el('cfg-ROLLING_WINDOW').value) || 3,
        DELETE_WATCHED_EPISODES: el('cfg-DELETE_WATCHED_EPISODES').value === 'true',
        PLEX_URL: el('cfg-PLEX_URL').value,
        PLEX_TOKEN: el('cfg-PLEX_TOKEN').value,
        JELLYFIN_URL: el('cfg-JELLYFIN_URL').value,
        JELLYFIN_API_KEY: el('cfg-JELLYFIN_API_KEY').value,
        PLEX_WATCH_INTERVAL: parseInt(el('cfg-PLEX_WATCH_INTERVAL').value) || 3600,
        PLEX_POLL_INTERVAL: parseInt(el('cfg-PLEX_POLL_INTERVAL').value) || 1200,
        PLEX_IDLE_POLLS: parseInt(el('cfg-PLEX_IDLE_POLLS').value) || 3,
        PLEX_SHUTDOWN_MODE: el('cfg-PLEX_SHUTDOWN_MODE') ? el('cfg-PLEX_SHUTDOWN_MODE').value : 'dry_run',
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

window.triggerShutdownNow = triggerShutdownNow;
window.saveConfiguration = saveConfiguration;

function testConnection(service) {
    const btn = el(`btn-test-${service}`);
    const origText = btn ? btn.textContent : 'Test Connection';
    if (btn) {
        btn.disabled = true;
        btn.textContent = 'Testing...';
    }

    let params = {};
    if (service === 'nzbgeek') {
        params = { url: el('cfg-NZBGEEK_URL').value, api_key: el('cfg-NZBGEEK_API_KEY').value };
    } else if (service === 'usenet') {
        params = {
            host: el('cfg-NNTP_HOST').value,
            port: parseInt(el('cfg-NNTP_PORT').value) || 563,
            ssl: el('cfg-NNTP_SSL').value === 'true',
            username: el('cfg-NNTP_USERNAME').value,
            password: el('cfg-NNTP_PASSWORD').value
        };
    } else if (service === 'tmdb') {
        params = { api_key: el('cfg-TMDB_API_KEY').value };
    } else if (service === 'plex') {
        params = { url: el('cfg-PLEX_URL').value, token: el('cfg-PLEX_TOKEN').value };
    } else if (service === 'jellyfin') {
        params = { url: el('cfg-JELLYFIN_URL').value, api_key: el('cfg-JELLYFIN_API_KEY').value };
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
        plex: urls.PLEX_URL || urls.plex,
        jellyfin: urls.JELLYFIN_URL || urls.jellyfin,
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
    loadLibrary();
    loadCalendar();
    setInterval(pollAll, REFRESH_MS);
});
