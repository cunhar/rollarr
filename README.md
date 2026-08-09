# Rolarr — Media Recycler & Power Saver 🎬⚡

**Rolarr** is an automated media recycler and server power saver for **Sonarr**, **Radarr**, **Plex**, and **NZBGet**, designed to aggressively save disk space while automating media downloads and server power management.

---

## Key Features 🚀

- 📦 **Rolling Window Episode Manager (Sonarr)**: Drip-feeds TV show episodes based on your configured rolling window size (e.g. keep 3 unwatched episodes). When Maintainerr or Plex marks an episode as watched/deleted, Rolarr automatically monitors and searches for the next episode in the sequence.
- 🍿 **Movie Unmonitor & Cleanup (Radarr)**: Automatically unmonitors and cleans up watched movies from Radarr to save storage.
- ⚡ **AES-128 Fernet Encrypted Config**: Sensitive settings (API keys, passwords, tokens, URLs) are stored in `/config/settings.enc` encrypted using AES-128 Fernet. Manage all settings through the interactive **Config** tab in the Web UI — no container restarts required.
- 🔒 **DOM Security & Mask Sentinel**: Passwords and API keys are never exposed in HTML DOM attributes or client-side Javascript. Inspecting elements in browser DevTools only shows mask sentinels (`••••••••`).
- 📺 **Plex Active Stream Watcher**: Real-time display of active Plex streams with user avatars, device details, resolution, direct play / transcode state, remaining time, and progress bars.
- 📥 **NZBGet Active Downloads Card**: Live NZBGet status pill showing download speeds directly in the topbar, plus an Active Downloads card with progress bars, ETA, and download speeds on the Dashboard.
- 🔌 **Host SSH Auto-Shutdown Watcher**: Monitors Plex for active streams. If the host remains idle for a configured number of consecutive poll cycles, Rolarr issues an SSH shutdown command (`sudo shutdown -h now`) to power down your media server.
- 🔴 **Instant Manual Shutdown Button**: Manual **Shutdown Now** button in the UI header with confirmation prompt and live feedback toasts.
- 📊 **Activity History & Live Status**: Dedicated Activity tab showing full audit logs of processed episodes, webhooks, and manual triggers with persistent counters and clear log controls.

---

## Configuration & Management ⚙️

Rolarr features an **Encrypted UI Settings Manager**. You can configure all services directly from the **Config** tab in the web interface (`http://your-server:5000`):

### 1. Sonarr Settings
- **Sonarr URL**: Base URL of your Sonarr instance (e.g. `http://localhost:8989`).
- **API Key**: Sonarr API key.
- **Rolling Window**: Number of sequential episodes to monitor ahead (e.g. `3`).

### 2. Radarr Settings
- **Radarr URL**: Base URL of your Radarr instance (e.g. `http://localhost:7878`).
- **API Key**: Radarr API key.

### 3. NZBGet Settings
- **NZBGet URL**: Base URL of your NZBGet instance (e.g. `http://localhost:6789`).
- **Username**: NZBGet RPC username (default `nzbget`).
- **Password / API Key**: NZBGet password or API key.

### 4. Plex & Polling Settings
- **Plex URL**: Base URL of your Plex instance (e.g. `http://plex:32400`).
- **Plex Token**: `X-Plex-Token` for Plex API authentication.
- **Media Poll Interval**: Interval in seconds to poll Plex for watched media (default `3600`).
- **Shutdown Poll Interval**: Interval in seconds between idle checks (default `1200`).
- **Shutdown Idle Polls Threshold**: Number of consecutive idle checks required before triggering host shutdown (default `3`).
- **Enable Shutdown Dry Run**: Simulates host shutdown in logs without executing the command.

### 5. SSH Host Shutdown Settings
- **SSH Host**: Host IP or domain (e.g. `172.17.0.1` or LAN IP).
- **SSH Port**: SSH port (default `22`).
- **SSH User**: Linux user on host machine (e.g. `ricardo`).
- **SSH Password**: Optional SSH password (automatically feeds `sudo` if required).

---

## Deployment 🐳

### Docker Compose (Recommended)

```yaml
version: '3.8'

services:
  rolarr:
    image: ghcr.io/cunhar/rollarr:latest
    container_name: rolarr
    network_mode: "service:gluetun" # or ports: ["5000:5000"]
    depends_on:
      - gluetun
    volumes:
      - ./config/rolarr:/config
    environment:
      - TZ=Europe/Lisbon
    restart: unless-stopped
```

### Docker Run

```bash
docker run -d \
  --name rolarr \
  -p 5000:5000 \
  -v /your/host/config:/config \
  -e TZ="Europe/Lisbon" \
  ghcr.io/cunhar/rollarr:latest
```

---

## Maintainerr Webhook Integration 🔗

To trigger automatic episode monitoring when Maintainerr deletes watched media:

1. In **Maintainerr**, navigate to **Settings** -> **Notifications**.
2. Create a new **Webhook** notification agent.
3. Select the **Media Handled** event type.
4. Set the Webhook URL to: `http://<rolarr-ip>:5000/webhook`.
5. Set the JSON Payload to:
   ```json
   {
     "subject": "{{subject}}",
     "message": "{{message}}"
   }
   ```

*Rolarr automatically parses the show title, season, and episode number from standard payloads (e.g. `Clarkson's Farm - 1x01 - Tractoring`) and queues the next sequential episode in Sonarr.*

---

## License 📜

MIT License. Developed for the *Arr ecosystem.
