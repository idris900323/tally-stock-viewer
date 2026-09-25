# Tally Stock Viewer - Going Public

This guide covers the public internet setup for the stock viewer.
It assumes the office PC is already working locally through `MASTER_SETUP.md`.

Current code status:
- `serve.py` already runs the app with `waitress`
- `launcher.pyw` already starts the local app on `127.0.0.1:5000`
- `launcher.pyw` already attempts to start `cloudflared.exe` when that file exists

No Copilot prompt or code-generation step is required before going public.

## 1. What changes when the app goes public

Public traffic should flow like this:

```text
superseatings.carxone.com
    -> Cloudflare
    -> Cloudflare Tunnel
    -> office PC on localhost:5000
    -> Tally Prime on localhost:9000
```

Important:
- the Flask app should stay bound to localhost only
- Tally stays local to the office PC
- your main website on MilesWeb stays separate

The app also already protects itself once public traffic reaches it, with nothing to configure: every response carries `X-Frame-Options`, `X-Content-Type-Options`, a restrictive `Permissions-Policy`, `Referrer-Policy: same-origin`, and `X-Robots-Tag: noindex, nofollow`, plus `Strict-Transport-Security` automatically once `SESSION_COOKIE_SECURE=1` is set (section 3). `https://superseatings.carxone.com/robots.txt` also returns a blanket `Disallow: /` — this is a private customer/admin catalog, not a marketing site, so it's meant to stay out of Google and other search indexes regardless of the public domain pointing at it.

## 2. Before touching DNS

Make sure all of these are already true on the office PC:
- the app opens at `http://localhost:5000`
- `http://localhost:5000/health` responds
- Tally refresh works locally
- image mappings work locally
- you can restart the app from the Desktop shortcut

If local setup is not stable, stop here and finish `MASTER_SETUP.md` first.

## 3. Lock down the local config first

Open `C:\tally_stock\.env` and verify these production values:

```env
FLASK_DEBUG=0
SESSION_COOKIE_SECURE=1
TALLY_URL=http://localhost:9000
DB_PATH=C:\tally_stock\data\mappings.db
LOG_FILE=C:\tally_stock\logs\app.log
IMAGE_SCAN_ROOT=C:\tally_stock\data\S.S IMAGE
```

Also make sure:
- the admin password is no longer the seeded default
- customer access codes are no longer the seeded defaults if those accounts are still active
- the Flask secret key is unique for this install
- `SYSTEM_ACCESS_TOKEN` is set to a long random value if you want the remote System panel (see section 10); leave it unset to keep the panel disabled
- `ACCOUNTS_ACCESS_PASSWORD` is set to a value only trusted admins know, if you want `Manage Accounts` protected by a second password on top of the admin login; leave it unset and that page stays fully locked, even to a valid admin session

To generate a secret key (the same command works for `SYSTEM_ACCESS_TOKEN`):

```powershell
python -c "import secrets; print(secrets.token_hex(32))"
```

Note: `.env` is deliberately NOT tracked in git, so a `git pull` never overwrites these values — but it also means `.env` must be backed up separately if the PC is rebuilt.

## 4. Cloudflare tunnel setup

### Step 1 - Download `cloudflared.exe`

1. Download the Windows 64-bit build from:
   `https://github.com/cloudflare/cloudflared/releases/latest`
2. Rename the file to `cloudflared.exe`
3. Place it in:

```text
C:\tally_stock\cloudflared.exe
```

This is the only file the launcher checks for before it attempts tunnel startup.

### Step 2 - Log in to Cloudflare from the office PC

Open PowerShell in `C:\tally_stock` and run:

```powershell
.\cloudflared.exe tunnel login
```

That browser flow should save a certificate under:

```text
C:\Users\<your-windows-user>\.cloudflared\cert.pem
```

### Step 3 - Create the named tunnel

```powershell
.\cloudflared.exe tunnel create tally-stock
```

Save the tunnel ID shown by Cloudflare.

You should also get a credentials file under:

```text
C:\Users\<your-windows-user>\.cloudflared\<TUNNEL_ID>.json
```

### Step 4 - Create the Cloudflare config file

Create this file:

```text
C:\Users\<your-windows-user>\.cloudflared\config.yml
```

Use this content and replace the placeholders:

```yaml
tunnel: YOUR_TUNNEL_ID
credentials-file: C:\Users\<your-windows-user>\.cloudflared\YOUR_TUNNEL_ID.json

ingress:
  - hostname: superseatings.carxone.com
    service: http://localhost:5000
  - service: http_status:404
```

## 5. DNS

Point the superseatings subdomain to the tunnel.

Use the tunnel hostname:

```text
YOUR_TUNNEL_ID.cfargotunnel.com
```

Create a `CNAME` record for:

```text
superseatings.carxone.com -> YOUR_TUNNEL_ID.cfargotunnel.com
```

Keep all existing records for the main website unchanged.

## 6. Test before relying on auto-start

Run this manually first:

```powershell
.\cloudflared.exe tunnel run tally-stock
```

Then test:
- `https://superseatings.carxone.com`
- `https://superseatings.carxone.com/health`

If both work, stop the manual test and return to the normal launcher-driven workflow.

## 7. How runtime startup works now

After public setup is complete, normal use is still simple:

1. Windows starts
2. the user logs in
3. the autostart entry launches `launcher.pyw`
4. `launcher.pyw` starts `serve.py`
5. `serve.py` hosts the app on `127.0.0.1:5000`
6. if `C:\tally_stock\cloudflared.exe` exists, the launcher also tries to start the tunnel

This is why the older Copilot prompt is no longer needed.

## 8. What to do with `first_time_setup.bat`

`first_time_setup.bat` is still useful for a brand-new office PC because it:
- copies the app into `C:\tally_stock`
- builds the virtual environment
- creates `.env`
- offers optional tunnel setup
- offers optional Git setup

But for public rollout, treat it as an installation helper, not as the public deployment guide.
This document is the public deployment guide.

## 9. Final checklist

- `http://localhost:5000` works on the office PC
- `http://localhost:5000/health` works on the office PC
- `https://superseatings.carxone.com` loads externally
- `https://superseatings.carxone.com/health` loads externally
- Tally refresh still works on the office PC
- images still load after public access is enabled
- `.env` has `FLASK_DEBUG=0`
- `.env` has `SESSION_COOKIE_SECURE=1` (this is also what turns on the `Strict-Transport-Security` header — see section 1)
- default seeded credentials are no longer in active use
- `ACCOUNTS_ACCESS_PASSWORD` is set if `Manage Accounts` should be usable in production (optional — leaving it unset just keeps that page locked)
- `https://superseatings.carxone.com/robots.txt` returns `Disallow: /` externally (confirms the site is opted out of search indexing — no `.env` setting involved, this is always on)

## 10. Remote management with the System panel

Once the site is public, most day-to-day maintenance can be done from anywhere through the System panel instead of remote desktop:

```text
https://superseatings.carxone.com/admin/system
```

Access requires two things:
1. an admin login session
2. a one-time device pairing per browser: open
   `https://superseatings.carxone.com/admin/system/authorize-device?token=<SYSTEM_ACCESS_TOKEN>`
   once with the token from the office PC's `.env`. This sets a long-lived cookie on that browser; the panel refuses unpaired devices even with a valid admin login.

From the panel you can:
- see the running code version and whether the office PC is behind `origin/main`
- pull the latest code and restart (`Pull Latest Code & Restart` — the restart is self-contained and does not depend on the tray launcher being healthy)
- restart the app without pulling code (`Restart App Only`)
- tail recent logs, download a database backup, check disk usage and uptime
- check Tally status (including the multiple-instances warning), run the Tally Performance Test, find duplicate images, and verify the Windows auto-start entry
- list every file actually on disk in `data/share_cache/` (with real modified times) and clear the cached share-image badges on demand — useful if a shared image's category badge looks stale after a category rename and a Cloudflare edge cache is suspected of still replaying an old response
- manage the Google Drive cloud backup (see section 11): authorize once, trigger a sync manually, watch live progress, stop a running sync, and see the last run's result and any recently skipped scheduled runs — all of it now survives a restart

If `SYSTEM_ACCESS_TOKEN` is not set in `.env`, all of these routes return `403` and the panel is effectively off.

## 11. Cloud backup (Google Drive)

Separate from public access, but usually set up around the same time: `cloud_backup.py` can incrementally back up `mappings.db`, the whole `S.S IMAGE` folder, and the three small JSON caches to a Google Drive folder, scheduled automatically. Off by default — nothing changes until it's configured.

### One-time setup

1. In the Google Cloud Console, create an OAuth Client ID of type "Desktop app" (a dedicated Google account for this is recommended, not your personal one) and download its JSON.
2. Add to `.env`:

```env
GDRIVE_OAUTH_CLIENT_SECRETS_PATH=C:\tally_stock\data\oauth_client_secret.json
GDRIVE_OAUTH_TOKEN_PATH=C:\tally_stock\data\gdrive_oauth_token.json
GDRIVE_BACKUP_FOLDER_ID=<the target Drive folder's id, from its URL>
CLOUD_BACKUP_INTERVAL=86400
```

(`CLOUD_BACKUP_INTERVAL` is in seconds — `86400` is once a day; the default if unset is `259200`, three days.)

3. Restart the app, open the System panel's Cloud Backup section, and click **Authorize Google Drive** — a browser opens once for a normal Google consent screen. After that, every run refreshes the token silently; no browser step is needed again unless access is later revoked.
4. Click **Sync Now** to confirm a first run works, or just wait for the schedule (first run 2 minutes after that restart, then every `CLOUD_BACKUP_INTERVAL`).

### Day to day

The Cloud Backup panel (part of `/admin/system`) shows: whether a sync is running (with a live progress log while it is), the last run's result (added/updated/deleted counts, a verification check against Drive's real contents, and how long each phase took), and any recently skipped scheduled runs (already running, or not yet authorized) with a plain-language reason. All of this now survives an app restart — it used to reset to blank on every restart even though the real backup on Drive was untouched.

A **Stop Sync** button appears while a sync is running — safe to use; the next run picks up exactly where it left off. The System panel's restart buttons (`Pull Latest Code & Restart`, `Restart App Only`) refuse with a clear message while a sync is actively running, rather than killing it mid-file.

## 12. Alternative: cloud deployment on Render (no Cloudflare Tunnel)

The Cloudflare Tunnel approach (sections 1-10) keeps the office PC itself as the public server. A separate option — useful when the public site should stay reachable even if the office PC or its internet connection is down — is hosting a second instance on Render (or a similar PaaS) that never talks to Tally at all. Instead, the office PC pushes fresh data to it after every local refresh.

This is additive, not a replacement: the office PC keeps running exactly as sections 1-10 describe; the Render instance is a second, independent deployment of the same codebase.

### On Render

1. Deploy this repository as a Render Web Service. Start command: `python serve.py` — it already reads Render's `PORT` env var and binds `0.0.0.0` automatically; no changes needed for that.
2. Set Render's **Health Check Path** to `/health` (this route is public/unauthenticated specifically so Render's own automated health check can read it — every other route requires login).
3. Set these environment variables on the Render service:

```env
DISABLE_TALLY_SCHEDULING=1
INTAKE_SYNC_TOKEN=<a long random secret you generate>
SYSTEM_ACCESS_TOKEN=<optional, only if you also want the System panel usable on this instance>
FLASK_SECRET_KEY=<a unique value, same as any other deployment>
```

`DISABLE_TALLY_SCHEDULING=1` stops this instance from ever attempting a doomed direct Tally connection (no startup full refresh, no item-export timer) — it shows a clear "receives data from the office system automatically" message instead of a Tally error if someone clicks a refresh button here.

### On the office PC

Add to `.env`:

```env
CLOUD_SYNC_URL=https://<your-render-app>.onrender.com/admin/intake/sync_data
CLOUD_SYNC_TOKEN=<the same INTAKE_SYNC_TOKEN value set on Render>
```

After this, every full refresh and every scheduled item-stock export also pushes the current car master / hierarchy / stock data to the Render instance in the background (a slow or failed push never delays the local job). If either value is missing, this is a clean no-op — nothing changes for a normal office-only setup.

### Verifying it end to end

- `https://<your-render-app>.onrender.com/health` should return `200` with `{"status": "ok", ...}`.
- After the office PC's next refresh (or by restarting it to trigger the startup push), `https://<your-render-app>.onrender.com/` should show the same cars/designs as the office PC.
- A real end-to-end test looks like: a POST to `/admin/intake/sync_data` with the right token returns `200` and `{"ok": true, "car_count": ..., "hierarchy_rows": ..., "stock_rows": ...}` matching the office PC's real counts.

### Known Render-specific gotcha

A free-tier (or otherwise suspended) Render service returns Render's own `503 "This service has been suspended by its owner"` page for every request, including the health check and the intake endpoint — this is Render's platform blocking the request before it ever reaches this app, not a bug here. If pushes are failing, check the service's own status in the Render dashboard first.

## 13. If the public site goes down

Check these in order:

1. the office PC is powered on
2. the office PC is logged into Windows
3. `http://localhost:5000` still works locally
4. `C:\tally_stock\cloudflared.exe` still exists
5. `C:\tally_stock\logs\app.log` has no startup failure
6. Cloudflare DNS still points `superseatings.carxone.com` at the tunnel

If local access works but public access fails, the problem is usually:
- the tunnel is not running
- the Cloudflare login expired
- the DNS record is wrong
- the PC restarted but the user never logged in, so the autostart entry never ran
