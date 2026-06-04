# Tally Stock Viewer — Going Public
## Full Guide: GoDaddy Domain + MilesWeb cPanel + Cloudflare Tunnel


## YOUR SETUP AT A GLANCE

```
stock.yourdomain.com   ← subdomain you will create
        ↓
  Cloudflare DNS       ← routes the subdomain to your tunnel
        ↓
  Cloudflare Tunnel    ← running on your office PC (free)
        ↓
  Office PC :5000      ← Flask app served by waitress (not dev server)
        ↓
  Tally Prime :9000    ← local, never exposed externally
```


Your main website and the stock viewer run independently.
The stock viewer never touches MilesWeb's server — only the DNS record
for the subdomain points away from MilesWeb toward the Cloudflare Tunnel.


## PHASE 1 — BEFORE YOU TOUCH ANYTHING EXTERNAL

Complete these on the office PC first. Do not skip any.

### 1.1 Change all default passwords

Open `C:\tally_stock\.env` (create it if it doesn't exist) and set:

```
FLASK_DEBUG=0
FLASK_SECRET_KEY=<paste a 32-char random string here>
SESSION_COOKIE_SECURE=1
ADMIN_PASSWORD=<your new strong admin password>
DB_PATH=C:\tally_stock\data\mappings.db
LOG_FILE=C:\tally_stock\logs\app.log
TALLY_URL=http://localhost:9000
IMAGE_SCAN_ROOT=C:\tally_stock\data\S.S IMAGE
```

Generate a secret key (run once in PowerShell):
```powershell
python -c "import secrets; print(secrets.token_hex(32))"
```

Also log into the app at http://localhost:5000 and change:

### 1.2 Switch from Flask dev server to waitress

Flask's built-in server prints a warning: "Do not use the development
server in a production environment." It is not stable under real traffic.
waitress is a production-grade Windows WSGI server — drop-in replacement.

Install it:
```powershell
C:\tally_stock\.venv\Scripts\pip.exe install waitress
```

The Copilot prompt at the end of this document adds waitress support
to launcher.pyw automatically. After applying it, the app will serve
via waitress on port 5000 — no other change needed.

### 1.3 Verify the app works locally before going public

```
http://localhost:5000/health   → should return {"status": "ok"}
http://localhost:5000/login    → login page loads
http://localhost:5000/         → main page loads after login
```

Check C:\tally_stock\logs\app.log — no ERROR lines at startup.


## PHASE 2 — CLOUDFLARE ACCOUNT AND TUNNEL SETUP

You need a FREE Cloudflare account. This is separate from GoDaddy and
MilesWeb — Cloudflare sits in between and handles the tunnel.

### 2.1 Create a free Cloudflare account

Go to: https://dash.cloudflare.com/sign-up
Sign up with any email. Free plan is sufficient.

### 2.2 Download cloudflared on the office PC

Download the Windows 64-bit exe from:
https://github.com/cloudflare/cloudflared/releases/latest

File to download: cloudflared-windows-amd64.exe
Rename it to: cloudflared.exe
Save it to: C:\tally_stock\cloudflared.exe

### 2.3 Log in to Cloudflare from the office PC

Open PowerShell in C:\tally_stock and run:
```powershell
.\cloudflared.exe tunnel login
```

A browser window opens. Log in with your Cloudflare account.
A certificate file is saved automatically to:
C:\Users\<yourname>\.cloudflared\cert.pem

### 2.4 Create the tunnel

```powershell
.\cloudflared.exe tunnel create tally-stock
```

This prints a TUNNEL_ID (a long UUID like 9a2b3c4d-...).
Copy it — you need it in the next step.

A credentials file is saved to:
C:\Users\<yourname>\.cloudflared\<TUNNEL_ID>.json

### 2.5 Create the tunnel config file

Create this file exactly at:
C:\Users\<yourname>\.cloudflared\config.yml

Contents (replace YOUR_TUNNEL_ID and your actual subdomain):
```yaml
tunnel: YOUR_TUNNEL_ID
credentials-file: C:\Users\<yourname>\.cloudflared\YOUR_TUNNEL_ID.json

ingress:
  - hostname: stock.yourdomain.com
    service: http://localhost:5000
  - service: http_status:404
```

Replace:

### 2.6 Test the tunnel manually (before auto-start)

```powershell
.\cloudflared.exe tunnel run tally-stock
```

Leave this running. In another window, check:
```powershell
.\cloudflared.exe tunnel info tally-stock
```

It should show status: healthy.


## PHASE 3 — GODADDY DNS: POINT SUBDOMAIN TO CLOUDFLARE

Your domain is registered on GoDaddy. GoDaddy controls your DNS records.
You need to add ONE CNAME record so that stock.yourdomain.com points
to your Cloudflare Tunnel.

### 3.1 Get your tunnel's Cloudflare hostname

Run:
```powershell
.\cloudflared.exe tunnel info tally-stock
```

Look for the line that says something like:
  Connections: ... YOUR_TUNNEL_ID.cfargotunnel.com

That value (YOUR_TUNNEL_ID.cfargotunnel.com) is what you point DNS at.

### 3.2 Log into GoDaddy and add the CNAME

1. Go to https://dcc.godaddy.com
2. Click your domain → DNS → Add New Record
3. Fill in:

   Type  : CNAME
   Name  : stock          (this creates stock.yourdomain.com)
   Value : YOUR_TUNNEL_ID.cfargotunnel.com
   TTL   : 1 hour (or default)

4. Save.

IMPORTANT: Do NOT change any existing A records or MX records.
Your main website on MilesWeb is untouched. Only the stock subdomain
is being redirected.

### 3.3 Wait for DNS propagation

DNS changes take 5–30 minutes to work globally.
Check progress at: https://dnschecker.org
Search for: stock.yourdomain.com → should show your tunnel value.


## PHASE 4 — MILESWEB cPANEL: NOTHING TO DO

Because your stock viewer runs on the office PC (not on MilesWeb's
server), you do not need to touch cPanel at all for the stock viewer.

The only thing that changed is the GoDaddy DNS record for the subdomain.
MilesWeb still serves your main website exactly as before.

If MilesWeb asks why traffic for stock.yourdomain.com is going elsewhere:
that is controlled by GoDaddy DNS, not by MilesWeb. MilesWeb's cPanel
has no involvement in this setup.


## PHASE 5 — AUTO-START THE TUNNEL WITH THE APP

The tunnel must be running whenever the app is running. The Copilot
prompt below updates launcher.pyw to start and stop cloudflared.exe
automatically alongside the Flask app.

After applying the Copilot changes:


## PHASE 6 — FINAL VERIFICATION CHECKLIST

Run through these after everything is set up:

DNS

HTTPS

App

Security


## MAINTENANCE AFTER GOING PUBLIC

### Daily (automatic)

### Weekly

### Monthly

### When you push a code update (git workflow)
1. Push from laptop: git push origin main
2. On office PC: click the built-in Update button in the admin panel
   (the git update feature you are currently building)
3. App restarts automatically, tunnel stays connected


## COSTS

| Item | Cost |
|------|------|
| GoDaddy domain | Already paid |
| MilesWeb hosting | Already paid (main site unchanged) |
| Cloudflare account | Free |
| Cloudflare Tunnel | Free (up to unlimited bandwidth for HTTP) |
| waitress WSGI | Free (open source Python package) |
| Office PC electricity | ~Rs 500/month estimate |
| **Total new cost** | **Rs 0** |


## COPILOT PROMPT — PASTE THIS INTO COPILOT CHAT

```
I have a Flask project at C:\tally_stock with these files:

Make EXACTLY the following changes. Do not modify any other file.


## CHANGE 1: Add waitress to launcher.pyw

In launcher.pyw, find the _start_server() function.
Currently it launches:
  subprocess.Popen([PYTHONW_EXE, APP_ENTRY], ...)

Replace it so it launches a small wrapper script instead.
Create a new file: C:\tally_stock\serve.py with this content:

  from waitress import serve
  from app import app
  import os, logging

  logging.basicConfig(
      filename=r"C:\tally_stock\logs\app.log",
      level=logging.INFO,
      format="[%(asctime)s] %(levelname)s: %(message)s"
  )
  log = logging.getLogger("waitress")
  log.info("Starting waitress on port 5000")
  serve(app, host="127.0.0.1", port=5000, threads=8)

Then in launcher.pyw _start_server(), change the Popen call to:
  proc = subprocess.Popen(
      [PYTHONW_EXE, r"C:\tally_stock\serve.py"],
      cwd=APP_DIR,
      creationflags=subprocess.CREATE_NO_WINDOW,
  )

Add this constant at the top of launcher.pyw with the other constants:
  SERVE_SCRIPT = r"C:\tally_stock\serve.py"


## CHANGE 2: Add cloudflared tunnel management to launcher.pyw

Add these constants at the top of launcher.pyw (with the others):
  CLOUDFLARED_EXE    = r"C:\tally_stock\cloudflared.exe"
  TUNNEL_NAME        = "tally-stock"
  TUNNEL_PID_FILE    = r"C:\tally_stock\tunnel.pid"
  CLOUDFLARE_ENABLED = os.path.exists(r"C:\tally_stock\cloudflared.exe")

Add a new function _start_tunnel():
  def _start_tunnel():
      if not CLOUDFLARE_ENABLED:
          log.info("cloudflared.exe not found, skipping tunnel")
          return
      log.info("Starting Cloudflare tunnel...")
      try:
          proc = subprocess.Popen(
              [CLOUDFLARED_EXE, "tunnel", "run", TUNNEL_NAME],
              cwd=APP_DIR,
              creationflags=subprocess.CREATE_NO_WINDOW,
              stdout=subprocess.DEVNULL,
              stderr=subprocess.DEVNULL,
          )
          pathlib.Path(TUNNEL_PID_FILE).write_text(str(proc.pid))
          log.info("Cloudflare tunnel started with PID %s", proc.pid)
      except Exception as exc:
          log.error("Failed to start tunnel: %s", exc)

Add a new function _stop_tunnel():
  def _stop_tunnel():
      pid_path = pathlib.Path(TUNNEL_PID_FILE)
      if not pid_path.exists():
          return
      try:
          pid = int(pid_path.read_text().strip())
          _kill_pid(pid)
          pid_path.unlink(missing_ok=True)
          log.info("Cloudflare tunnel stopped")
      except Exception as exc:
          log.warning("Could not stop tunnel: %s", exc)

In the main() function, after _start_server() succeeds, add:
  _start_tunnel()

In _stop_server(), before returning, add:
  _stop_tunnel()

In _restart_server(), after _stop_server(), add a call to
_start_tunnel() after _start_server() succeeds:
  ok = _start_server()
  if ok:
      _start_tunnel()
      webbrowser.open(APP_URL)

In the watchdog loop _watchdog_loop(), after auto-restarting the
server, also restart the tunnel:
  log.warning("Server crashed — auto-restarting")
  _restart_server()
  # _restart_server() already calls _start_tunnel() internally

Add "Tunnel Status" to the tray menu:
  def on_tunnel_status(_icon, _item):
      pid_path = pathlib.Path(TUNNEL_PID_FILE)
      if pid_path.exists():
          pid = int(pid_path.read_text().strip())
          alive = _process_alive(pid)
          msg = f"Tunnel running (PID {pid})" if alive else "Tunnel PID found but process dead"
      else:
          msg = "Tunnel not running"
      import tkinter, tkinter.messagebox
      root = tkinter.Tk(); root.withdraw()
      tkinter.messagebox.showinfo("Tunnel Status", msg)
      root.destroy()

Add it to the pystray menu between "View Logs" and "Restart Server":
  pystray.MenuItem("Tunnel Status", on_tunnel_status),


## CHANGE 3: Update setup_office_pc.bat

At the end of STEP 5 (after installing pystray Pillow psutil),
add this line:
  "%INSTALL_DIR%\.venv\Scripts\pip.exe" install waitress --quiet

At the end of the summary printout, add:
  echo   Cloudflare Tunnel: place cloudflared.exe in %INSTALL_DIR%\
  echo   then run: cloudflared.exe tunnel login
  echo   then run: cloudflared.exe tunnel create tally-stock
  echo   See GOING_PUBLIC.md for full tunnel setup instructions.


## CONSTRAINTS
  missing, the app starts normally without the tunnel, no crash
  Cloudflare Tunnel handles external access — Flask must not be
  directly reachable from outside the office PC
  as existing functions (use the log = logging.getLogger("launcher"))

Generate the updated launcher.pyw, the new serve.py, and the
updated setup_office_pc.bat.
```

---

## QUICK REFERENCE CARD (print this and keep near the office PC)

```
START  : Double-click "Start Stock Viewer" on Desktop
STOP   : Double-click "Stop Stock Viewer" on Desktop
UPDATE : Log in as admin → Admin menu → Update App (git pull)
LOGS   : Right-click tray icon → View App Logs
HEALTH : https://stock.yourdomain.com/health

If site is down:
  1. Check office PC is on and logged in
  2. Check tray icon is visible (bottom right)
  3. If no tray icon: double-click Start Stock Viewer
  4. Check Tally Prime is open (for stock refresh)
  5. Check logs: C:\tally_stock\logs\app.log

Emergency manual start (open PowerShell):
  C:\tally_stock\.venv\Scripts\python.exe C:\tally_stock\serve.py
```
