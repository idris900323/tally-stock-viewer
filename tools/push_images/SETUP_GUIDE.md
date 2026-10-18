# Image push tool: first-time setup (plain language)

This tool copies **new design photos** from a PC to the live site. It is careful by design:

- The **first time** it runs it only **compares** and uploads **nothing**.
- It **never deletes** anything on the server.
- It **never replaces** a photo that is already on the server (unless you deliberately add `--overwrite-changed`).
- If it would upload **more than 200** photos in one go, it stops and asks you to check.
- If anything is missing or looks wrong, it **stops before changing anything**.

You do not need to know programming. You need to do steps 1-6 **once**.

---

## What you need

1. A Windows PC with the photos in one folder (the same layout as on the server: one sub-folder per car).
2. **Python 3** installed (python.org, tick "Add Python to PATH"). The tool uses nothing else.
3. **OpenSSH Client** (already on most Windows 10/11 PCs). To check, open **Start**, type `cmd`, press Enter, type `ssh -V`, press Enter. If you see a version number, you're fine. If not: **Settings > Apps > Optional features > Add a feature > OpenSSH Client**.
4. A Render account login (to paste one line into the dashboard).

---

## Step 1: Get the tool

The tool is the folder `tools\push_images` inside the project. Nothing needs installing.

## Step 2: Check the tool works (no internet needed)

Open **Start**, type `cmd`, press Enter. Then type (change the path if your folder is elsewhere):

```
cd C:\tally_stock\tools\push_images
py -3 push_images.py selftest
```

You should see a list of `PASS` lines and `Self-test: 14/14 passed`. If not, stop and ask for help.

## Step 3: Create your SSH key (your PC's "key card")

In the same black window type:

```
ssh-keygen -t ed25519 -f "%USERPROFILE%\.ssh\superseatings_push" -C "image-push"
```

- When it asks for a passphrase just press **Enter** twice (leave it empty).
- This makes **two files** in `C:\Users\<you>\.ssh\`:
  - `superseatings_push` is the **private** key. **Never send it to anyone.**
  - `superseatings_push.pub` is the **public** key. This one is safe to share, and goes to Render.

## Step 4: Give Render the public key

1. In the black window type: `type "%USERPROFILE%\.ssh\superseatings_push.pub"` and press Enter. One long line appears starting with `ssh-ed25519`. Select it and copy it (right-click copies in the black window).
2. Open the Render dashboard > **Account Settings** (your profile, top right) > **SSH Public Keys** > **Add SSH Public Key**. Paste the line, give it a name like "Shop PC images", and save.

## Step 5: Find the server's address

Render dashboard > your service **tally-stock-viewer** > **Connect** (top right) > **SSH**. Copy the address that looks like:

```
srv-xxxxxxxxxxxxxxxxxxxx@ssh.singapore.render.com
```

## Step 6: Tell the tool your settings and test the connection

Double-click **`push_images.bat`** in the `tools\push_images` folder. The first time it asks questions:

| It asks | Type |
|---|---|
| Server SSH address | the line from step 5 |
| Server image folder | just press Enter (the usual one is filled in) |
| Image folder on THIS PC | the folder that holds the car folders, e.g. `D:\Photos\S.S IMAGE` |
| SSH private key file | `C:\Users\<you>\.ssh\superseatings_push` |
| Rescan URL | press Enter |
| Rescan token | the secret you were given for "rescan" (it is stored on this PC only and never shown again). Leave blank if you don't have it: new photos will then appear after the site's next scan |

Then it automatically does the **first run**: it logs in, compares the two folders and prints a summary. **It uploads nothing.** If the login fails, the message says what to fix (usually step 3-5).

> The first time you connect, Windows may remember the server's identity automatically. That is normal.

---

## Everyday use

1. Put the new photos in the right car folder on this PC.
2. Double-click **`push_images.bat`**.
3. Read the summary ("New on this PC (would be uploaded): N"). Type `yes` when asked. Wait for **Finished OK**.

If you see **"Stopped safely. NOTHING was changed"**, read the message above it. It tells you what to fix.

### The two safety switches (use only if you are sure)

- More than 200 photos at once: run the tool from a black window with `py -3 push_images.py push --allow-more-than-200`.
- Replace a photo whose **size changed**: `py -3 push_images.py push --overwrite-changed`.

---

## Where things are kept

Settings, the list of what has been checked, and the log are in:

```
%LOCALAPPDATA%\SuperSeatings\push_images\
```

(paste that into the Windows Explorer address bar). It is **outside** the project folder, so updating or cleaning the project can never erase it. The tool never reads the project's `.env` file.

## If something goes wrong

- **"Could not log in to the server over SSH"**: redo steps 3-5; check the address and the key path (`push_images.bat status` shows them, never the token).
- **"No image files found"**: the image folder is wrong. Run `push_images.bat setup` again.
- **Some files FAILED**: run it again; only the missing ones are retried. Leftover files ending in `.partupload` on the server are harmless and ignored.
- To start fresh: delete only the `manifest.json` file in the folder above; the next run will just compare again.
