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

    async fetchNZBGetStatus() {
        const res = await fetch('/api/nzbget-status');
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
