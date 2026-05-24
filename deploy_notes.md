# Deploy Notes — Make the app accessible externally

## CURRENT STATE
- Flask app entry point: `app.py` (local dev)
- Runs on `http://localhost:5000`
- SQLite DB: `data/mappings.db`
- Logs: `logs/app.log`
- Images folder: `data/S.S IMAGE/`

## OPTION A: Cloudflare Tunnel (RECOMMENDED)
Zero port-forwarding. Works behind NAT/firewall. HTTPS handled by Cloudflare.

1. Download `cloudflared.exe` from: https://github.com/cloudflare/cloudflared/releases
   Save to: `C:\tally_stock\cloudflared.exe`
2. Run: `cloudflared.exe tunnel login` and complete interactive login.
3. Create tunnel: `cloudflared.exe tunnel create tally-stock` and note TUNNEL_ID.
4. Create `C:\Users\<you>\.cloudflared\config.yml` with:

```
tunnel: <TUNNEL_ID>
credentials-file: C:\Users\<you>\.cloudflared\<TUNNEL_ID>.json
ingress:
  - hostname: stock.yourdomain.com
    service: http://localhost:5000
  - service: http_status:404
```

5. Run: `cloudflared.exe tunnel run tally-stock`

Notes:
- Set `FLASK_DEBUG=0` in production and secure `SECRET_KEY`.
- Use `SESSION_COOKIE_SECURE=1` behind HTTPS.

## OPTION B: ngrok (quick test)
1. Download ngrok and run: `ngrok http 5000`
2. Share the generated https URL.

## OPTION C: Router Port Forwarding
1. Forward external port 443 to internal `192.168.x.x:5000`.
2. Use a reverse proxy (nginx) with SSL or Cloudflare SSL proxy.

## SECURITY CHECKLIST
- Set `FLASK_DEBUG=0`.
- Ensure `SECRET_KEY` is strong and not checked into source control.
- Do not expose Tally's port (9000) externally.
- Ensure `data/` and `logs/` are not served by Flask; use proper static hosting rules.

## ENVIRONMENT VARIABLES (suggested)
- `FLASK_DEBUG=0`
- `FLASK_SECRET_KEY=<random-32-chars>`
- `SESSION_COOKIE_SECURE=1`
- `DB_PATH=C:\tally_stock\data\mappings.db`
- `LOG_FILE=C:\tally_stock\logs\app.log`
- `TALLY_URL=http://localhost:9000`

## RUNNING BEHIND A PRODUCTION WSGI
Consider using `gunicorn` (Linux) or `waitress` (Windows) behind a reverse proxy for better stability.

## BACKUP & MAINTENANCE
- Regularly backup `data/mappings.db`.
- Rotate logs in `logs/app.log` and monitor disk.
- Keep `.venv` and dependencies updated on a maintenance cycle.
