# Tally Stock Viewer Git Workflow (Detailed)

This guide is for your current situation:
- The project is already on Git.
- The remote (`origin`) is already configured.
- Going forward you mainly do `commit` + `push` from laptop.
- Production machine pulls updates and restarts the app safely.

---

## 1. Goal and Safety Model

You want code updates to sync, but never overwrite live business data.

Your protection model is:
1. `data/` is ignored by Git.
2. `.venv/`, `logs/`, and `*.pid` are ignored by Git.
3. Production updates only change tracked code files.
4. Production local files in ignored paths remain local.

Important:
- `.gitignore` prevents new tracking.
- If a file was tracked in older commits, it can still be changed by pull.
- So you must do a one-time verification that sensitive files are not tracked.

---

## 2. One-Time Safety Verification (Do This Once)

Run these in project root on your laptop:

```powershell
git ls-files data
git ls-files logs
git ls-files *.pid
```

Expected result:
- Ideally no output (or at least no sensitive production files).

If you see files listed under `data/` or `logs/`, untrack them (keeps local files):

```powershell
git rm -r --cached data logs
```

If any PID files are tracked:

```powershell
git ls-files | Where-Object { $_ -match '\.pid$' } | ForEach-Object { git rm --cached -- $_ }
```

Then commit and push this cleanup:

```powershell
git add .gitignore
git commit -m "Stop tracking runtime/data files; enforce safe deploy ignores"
git push origin main
```

After this, production data is much safer from accidental overwrite.

---

## 3. Daily Laptop Workflow (Your Normal Routine)

Use this every time you make code changes.

### Step 1: Check what changed

```powershell
git status
```

Review that only intended source files are modified.

### Step 2: Stage changes

Stage all tracked changes:

```powershell
git add .
```

Or stage specific files only:

```powershell
git add app.py database.py templates/index.html
```

### Step 3: Commit

```powershell
git commit -m "Describe what changed clearly"
```

Good commit message examples:
- `Fix stock search edge case for empty item codes`
- `Improve launcher recovery when pid file is stale`
- `Add validation for Excel import mapping`

### Step 4: Push

```powershell
git push origin main
```

This publishes your latest code for production to pull.

---

## 4. Production First-Time Setup

Use this once on live machine if not already done.

### Step 1: Clone repo

```powershell
git clone <YOUR_REPO_URL> C:\tally_stock
```

### Step 2: Ensure runtime folders exist

Create or restore local-only paths:
- `C:\tally_stock\data\`
- `C:\tally_stock\logs\`

Copy your real live files into `data\`:
- `mappings.db`
- Excel files (`.xlsx`, `.xls`)
- `S.S IMAGE\` folder

### Step 3: Python environment

```powershell
cd C:\tally_stock
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

If you use a different requirements file, install that one instead.

### Step 4: Place update script

Ensure this file exists:
- `C:\tally_stock\update_app.bat`

It should:
1. Stop server (`stop_server.py`)
2. Pull latest (`git pull origin main`)
3. Start silently (`pythonw.exe launcher.pyw`)

---

## 5. Production Update Routine (Every Release)

After you push from laptop:

### Step 1: Optional quick backup (recommended)

```powershell
Copy-Item C:\tally_stock\data\mappings.db C:\tally_stock\data\mappings.db.bak_$(Get-Date -Format yyyyMMdd_HHmmss)
```

### Step 2: Run update script

```powershell
cd C:\tally_stock
.\update_app.bat
```

What happens:
1. Old app process is stopped.
2. Git pulls latest code.
3. App restarts in background via `pythonw.exe`.

### Step 3: Verify app is live

- Open browser and test key pages.
- Confirm expected new feature/fix is visible.
- Check `logs\` for startup errors.

---

## 6. Handling Common Errors

### Error: `git pull` says local changes would be overwritten

Cause:
- Production has edited tracked files locally.

Fix options:
1. Discard local tracked edits (only if safe):
   ```powershell
   git reset --hard HEAD
   git pull origin main
   ```
2. Or stash, pull, re-apply:
   ```powershell
   git stash
   git pull origin main
   git stash pop
   ```

Best practice:
- Avoid editing tracked source files directly on production.
- Make code changes on laptop, then push.

### Error: `stop_server.py` fails

If stop step fails but app is still running, manually end process then rerun update.

### Error: app does not restart

Manual restart test:

```powershell
pythonw.exe C:\tally_stock\launcher.pyw
```

Then inspect logs and PID behavior.

---

## 7. Quick Command Cheat Sheet

Laptop (daily):

```powershell
cd <project-folder>
git status
git add .
git commit -m "Your message"
git push origin main
```

Production (deploy):

```powershell
cd C:\tally_stock
.\update_app.bat
```

---

## 8. Recommended Release Discipline

1. Test locally before push.
2. Keep commits small and descriptive.
3. Push to `main` only when stable.
4. Run production update immediately after push.
5. Validate critical flows after deploy:
   - Search
   - Data reads from DB
   - Any Excel/image dependent path

---

## 9. Final Checklist Before Every Deploy

On laptop:
1. `git status` clean after commit.
2. `git push origin main` successful.

On production:
1. Optional DB backup taken.
2. `update_app.bat` run successfully.
3. App opened and smoke-tested.
4. Logs checked for errors.

If all four pass, deploy is complete.
