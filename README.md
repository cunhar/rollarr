# Rolarr

Rolarr is a lightweight companion application for **Maintainerr** within the *Arr ecosystem (Sonarr/Radarr). 

## The Goal
Rolarr helps aggressively save disk space by creating a "rolling window" of downloaded episodes. Instead of downloading an entire season, Rolarr drip-feeds episodes. When you watch an episode and Maintainerr deletes it to save space, Rolarr intercepts the deletion webhook and automatically triggers Sonarr to monitor and search for the next sequential episode.

## How It Works
1. **Trigger**: Maintainerr sends a webhook payload to Rolarr upon the successful deletion of a watched episode.
2. **Action**: Rolarr parses the payload, fetches all episodes from the Sonarr API, filters out specials (Season 0), and sorts them chronologically.
3. **Sequential Automation**: Rolarr locates the next episode in the sequence, marks it as **Monitored**, and tells Sonarr to search for/download it immediately.

---

## Configuration

Rolarr is configured via environment variables:

| Environment Variable | Description | Example |
|---|---|---|
| `SONARR_URL` | The base URL of your Sonarr instance. | `http://sonarr:8989` |
| `SONARR_API_KEY` | Your Sonarr API key. | `abcdef0123456789...` |
| `ROLLING_WINDOW` | Optional. How many episodes to monitor ahead. Defaults to `3`. | `3` |
| `PORT` | Optional. The port Rolarr will listen on. Defaults to `5000`. | `5000` |

---

## Deployment

### Docker Run
You can pull the prebuilt package from GHCR or run it locally:
```bash
docker run -d \
  --name rolarr \
  -p 5000:5000 \
  -e SONARR_URL="http://your-sonarr-ip:8989" \
  -e SONARR_API_KEY="your-sonarr-api-key" \
  ghcr.io/cunhar/rollarr:latest
```

### Docker Compose
```yaml
version: '3.8'

services:
  rolarr:
    image: ghcr.io/cunhar/rollarr:latest
    container_name: rolarr
    ports:
      - 5000:5000
    environment:
      - SONARR_URL=http://sonarr:8989
      - SONARR_API_KEY=your-sonarr-api-key
    restart: unless-stopped
```

---

## Webhook Setup in Maintainerr

Configure Maintainerr to send a webhook notification when a rule processes an item:

1. In Maintainerr, navigate to **Settings** -> **Notifications**.
2. Add a new **Webhook** notification agent.
3. Check the **Media Handled** event type.
4. Set the Webhook URL to: `http://<rolarr-ip-or-container-name>:5000/webhook`.
5. Configure the **JSON Payload** using the standard variables Maintainerr exposes:
   ```json
   {
     "subject": "{{subject}}",
     "message": "{{message}}"
   }
   ```

*Note: Rolarr is intelligent. When it receives this standard payload, it automatically parses the Show Name, Season Number, and Episode Number directly out of the `subject` text (e.g. `Clarkson's Farm - 1x01 - Tractoring`) and automatically resolves the matching show and episode sequence in Sonarr.*
