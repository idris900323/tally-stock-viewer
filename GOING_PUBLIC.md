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
stock.carxone.com
    -> Cloudflare
    -> Cloudflare Tunnel
    -> office PC on localhost:5000
    -> Tally Prime on localhost:9000
```

Important:
- the Flask app should stay bound to localhost only
- Tally stays local to the office PC
- your main website on MilesWeb stays separate

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

To generate a secret key:

```powershell
python -c "import secrets; print(secrets.token_hex(32))"
```

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
  - hostname: stock.carxone.com
    service: http://localhost:5000
  - service: http_status:404
```

## 5. DNS

Point the stock subdomain to the tunnel.

Use the tunnel hostname:

```text
YOUR_TUNNEL_ID.cfargotunnel.com
```

Create a `CNAME` record for:

```text
stock.carxone.com -> YOUR_TUNNEL_ID.cfargotunnel.com
```

Keep all existing records for the main website unchanged.

## 6. Test before relying on auto-start

Run this manually first:

```powershell
.\cloudflared.exe tunnel run tally-stock
```

Then test:
- `https://stock.carxone.com`
- `https://stock.carxone.com/health`

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
- `https://stock.carxone.com` loads externally
- `https://stock.carxone.com/health` loads externally
- Tally refresh still works on the office PC
- images still load after public access is enabled
- `.env` has `FLASK_DEBUG=0`
- `.env` has `SESSION_COOKIE_SECURE=1`
- default seeded credentials are no longer in active use

## 10. If the public site goes down

Check these in order:

1. the office PC is powered on
2. the office PC is logged into Windows
3. `http://localhost:5000` still works locally
4. `C:\tally_stock\cloudflared.exe` still exists
5. `C:\tally_stock\logs\app.log` has no startup failure
6. Cloudflare DNS still points `stock.carxone.com` at the tunnel

If local access works but public access fails, the problem is usually:
- the tunnel is not running
- the Cloudflare login expired
- the DNS record is wrong
- the PC restarted but the user never logged in, so the autostart entry never ran
