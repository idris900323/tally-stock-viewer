# Migration Day Instructions — Old PC → New PC + Render

**Before you start:** you've already copied the entire `tally_stock` folder from the old machine to the new machine, byte-for-byte. This document covers everything from here onward, in the correct order. Follow it top to bottom — don't skip ahead to shutting down the old machine until every step is confirmed.

---

## Step 0 — One more safety net (5 minutes)

Before touching anything else, take one more manual backup and store it somewhere completely separate from both machines (your laptop, a USB drive, cloud storage — anywhere that isn't either PC).

On the **old PC**, via its System Panel (`/admin/system`):
1. Click **Download Full Backup**
2. Save the resulting zip somewhere safe, off both machines

This costs almost nothing and gives you a clean fallback if anything goes wrong in the steps below.

---

## Step 1 — Get the real data onto Render (the important one)

This is the single most important step in this whole process. Everything your business has built — real images, mappings, categories, customer accounts — needs to land on Render's disk. Do this from the **new PC**, since it now has an identical copy of everything and is the machine you'll keep using going forward.

You'll need Render's SSH connection details — find these in your Render dashboard, on the `tally-stock-viewer` service's **Connect** tab (it'll look like `ssh srv-xxxxx@ssh.oregon.render.com` or similar).

From a PowerShell window on the new PC, in the `tally_stock` folder:

```powershell
scp "data\mappings.db" srv-xxxxx@ssh.oregon.render.com:/opt/render/project/src/data/mappings.db
```

```powershell
scp -r "data\S.S IMAGE" srv-xxxxx@ssh.oregon.render.com:/opt/render/project/src/data/
```

(Replace `srv-xxxxx@ssh.oregon.render.com` with your actual connection string from the dashboard. The image folder copy will likely take a while given the real file count — that's expected, let it run.)

**Once both transfers finish, verify they actually worked** — don't just trust that it completed:

```powershell
python scripts\verify_render_migration.py
```

This checks real row counts and file counts match between your local copy and what's now on Render. **Do not proceed until this reports a full pass.** If it reports any mismatch, stop and figure out what's missing before continuing.

---

## Step 2 — Move Google Drive backup authorization to Render

Copy the already-authorized token file from the new PC (which has it, from the full folder copy) onto Render's disk the same way:

```powershell
scp "data\gdrive_token.json" srv-xxxxx@ssh.oregon.render.com:/opt/render/project/src/data/gdrive_token.json
```

Then, in Render's dashboard, add the three Google Drive environment variables (`GDRIVE_OAUTH_CLIENT_SECRETS_PATH`, `GDRIVE_OAUTH_TOKEN_PATH`, `GDRIVE_BACKUP_FOLDER_ID`) pointing at these now-uploaded paths, matching exactly how they were configured on the old PC. You'll also need to `scp` the `oauth_client_secret.json` file over the same way.

Restart the Render service after adding these, then check its System Panel's Cloud Backup section — it should show "Authorized" without needing you to click anything, since the token is already valid.

---

## Step 3 — Restart Render with the real data loaded

Trigger a restart on Render (via its dashboard, or the System Panel's restart button) so it picks up the newly-arrived database and images. Once it's back up, open the live site and confirm:

- Real cars and designs show up (not the old test data)
- A few images you know well actually display correctly
- Categories show correctly on a few items you know were tagged

---

## Step 4 — Make the new PC's dormant copy safe

The new PC has a full copy of everything, kept intentionally as an emergency fallback. Before starting the feeder, make sure this copy can never accidentally run alongside it:

```powershell
cd C:\tally_stock
.\scripts\make_dormant.ps1
```

This renames the main app files so nothing can accidentally launch them, and leaves a `DORMANT_README.md` explaining what happened and how to reverse it if you ever genuinely need to.

---

## Step 5 — Set up and start the feeder

Follow `FEEDER_SETUP.md` from the top, on the new PC. Since Tally itself already lives on this machine, `TALLY_URL` should just be `http://localhost:9000`, unchanged from before.

At the end of that guide, confirm:
- `feeder.py` runs manually once without errors
- The Scheduled Task is registered correctly
- After a few minutes, check Render's live site — the "Last updated" timestamp should be advancing on its own, proving the feeder is genuinely pushing fresh data

---

## Step 6 — Point your real domain at Render

Once everything above is confirmed working, update `superseatings.carxone.com`'s DNS in Cloudflare to point at Render instead of the old PC's tunnel. This is the moment of brief downtime you already said is fine.

---

## Step 7 — Only now, shut down the old PC

With all of the above confirmed:
- Turn off the old PC's Cloudflare Tunnel
- Shut it down

It can stay off indefinitely. If you ever need it again, the manual backup from Step 0 and the dormant copy on the new PC (Step 4) are both there as fallbacks.

---

## If something goes wrong partway through

Don't panic and don't shut down the old PC until every step above is confirmed. The old PC remains your live, working site until you deliberately redirect the domain in Step 6 — nothing before that step is destructive or time-pressured.
