/**
 * static/js/api.js
 * ----------------
 * API client module for Rolarr backend endpoints.
 */

const Api = {
    async fetchConnectionStatus() {
        const res = await fetch('/api/connection-status');
        return res.ok ? await res.json() : null;
    },

    async fetchPlexStatus() {
        const res = await fetch('/api/plex-status');
        return res.ok ? await res.json() : null;
    },

    async fetchPollerStatus() {
        const res = await fetch('/api/poller-status');
        return res.ok ? await res.json() : null;
    },

    async fetchDownloadsStatus() {
        const res = await fetch('/api/downloads-status');
        return res.ok ? await res.json() : null;
    },

    async pauseDownloads() {
        const res = await fetch('/api/downloads/pause', { method: 'POST' });
        return res.ok ? await res.json() : null;
    },

    async resumeDownloads() {
        const res = await fetch('/api/downloads/resume', { method: 'POST' });
        return res.ok ? await res.json() : null;
    },

    async cancelDownload(id) {
        const res = await fetch(`/api/downloads/${id}/cancel`, { method: 'POST' });
        return res.ok ? await res.json() : null;
    },

    async searchMedia(query, type = 'all') {
        const params = new URLSearchParams({ type, q: query });
        const res = await fetch(`/api/search?${params.toString()}`);
        return { ok: res.ok, data: await res.json() };
    },

    async fetchLibrary() {
        const res = await fetch('/api/library');
        return res.ok ? await res.json() : null;
    },

    async addMedia(type, item) {
        const payload = (typeof item === 'object' && item !== null)
            ? { type, ...item }
            : { type, id: item };
        const res = await fetch('/api/library/add', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload)
        });
        return { ok: res.ok, data: await res.json() };
    },

    async fetchShowEpisodes(showId) {
        const res = await fetch(`/api/library/tv/${showId}/episodes`);
        return res.ok ? await res.json() : null;
    },

    async queueSingleEpisode(showId, season, episode) {
        const res = await fetch(`/api/library/tv/${showId}/episode`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ season, episode })
        });
        return { ok: res.ok, data: await res.json() };
    },

    async queueShowFrom(showId, startSeason, startEpisode) {
        const res = await fetch(`/api/library/tv/${showId}/queue`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ start_season: startSeason, start_episode: startEpisode })
        });
        return { ok: res.ok, data: await res.json() };
    },

    async deleteLibraryItem(type, id) {
        const res = await fetch(`/api/library/${type}/${id}`, { method: 'DELETE' });
        return res.ok ? await res.json() : null;
    },

    async fetchActivityLogs() {
        const res = await fetch('/api/activity');
        return res.ok ? await res.json() : null;
    },

    async fetchDiskSpace() {
        const res = await fetch('/api/disk-space');
        return res.ok ? await res.json() : null;
    },

    async clearLogs() {
        const res = await fetch('/api/clear-logs', { method: 'POST' });
        return res.ok ? await res.json() : null;
    },

    async runPoller() {
        const res = await fetch('/api/poller-run', { method: 'POST' });
        return { ok: res.ok, data: await res.json() };
    },

    async triggerShutdownNow() {
        const res = await fetch('/api/shutdown-now', { method: 'POST' });
        return { ok: res.ok, data: await res.json() };
    },

    async saveConfig(payload) {
        const res = await fetch('/api/config/save', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload)
        });
        return await res.json();
    },

    async testConnection(service, params) {
        const res = await fetch('/api/test-connection', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ service, ...params })
        });
        return await res.json();
    }
};

window.Api = Api;
