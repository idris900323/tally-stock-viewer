# Shop visit: update the shop PC (about 10 minutes)

You need: the shop PC with internet. You do **not** need to type any commands.
The updater is a short text file. You paste it into Notepad, save it in the feeder folder, and double-click it.

## The 5 steps

1. **Copy the updater text.** On the shop PC open this link in the browser:
   `https://raw.githubusercontent.com/idris900323/tally-stock-viewer/main/tools/shop/SHOP_UPDATE.bat`
   Click on the page, press **Ctrl+A** (select all), then **Ctrl+C** (copy). Do not "download" or "save page as".
   *(No internet on the shop PC? Open the same link on your phone or laptop, copy it, and send the text to yourself in an email or WhatsApp message. Then copy it again on the shop PC.)*
2. **Paste it into Notepad.** Open **Notepad**, press **Ctrl+V**. The first line should start with `rem SHOP_UPDATE.bat  version 2026-10-21`. Press **Ctrl+End**: the very last line must be `rem END-OF-SHOP-UPDATE`.
3. **Save it in the feeder folder.** File, **Save As**. Open the folder that contains `feeder.py` and `.env` (probably `C:\tally_stock`). Set **Save as type** to **All files**, **File name** to `SHOP_UPDATE.bat` (exactly), and click Save. If Notepad shows an **Encoding** box, choose **UTF-8** or **ANSI**.
4. **Open Tally with the company loaded first, then double-click `SHOP_UPDATE.bat`.** (If Tally is closed the window says so and waits; press a key to go on anyway, or close the window to stop.) A black window opens and shows 6 steps. It takes 2 to 6 minutes. **Do not close it and do not click inside it.** If it says **"The file looks cut off. Copy it again and paste the whole thing."**, go back to step 1 (nothing was changed).
5. **Read the result and press a key.** The window stays open until you do.

## What you will see

**PASS** (green window, big letters `PASS`): the feeder was updated and is sending stock to the site. Nothing else to do. You can close the window.

**FAIL** (red window, big letters `FAIL`): the update did not work. The program already put the previous version back and restarted the old feeder, and your `.env` and data are safe (backups are in the `SHOP_UPDATE_BACKUP_...` folder next to the file).

## If it says FAIL

1. **Take a photo of the whole window** (all the text). If the text is cut off, scroll up and take a second photo.
2. **Touch nothing else.** Do not run anything else, do not delete files, do not re-run it.
3. Send the photo(s). The full details are in `SHOP_UPDATE_log.txt` in the same folder; it holds no passwords or tokens.

## Notes

* The updater never prints `.env` (it contains the secret token). It adds the line `MACHINE_ROLE=feeder` to `.env` if it is missing; that line keeps the web app from ever running on this PC.
* The website stays up during the update. Only stock pushes pause for a few minutes.
* After the update, **`refresh_feeder_now.bat`** appears in the feeder folder: double-click it any time to make the feeder re-read the car list and items from Tally right away (new items now also appear by themselves within about 3 to 10 minutes).
* The new feeder log lines look like `Full refresh starting: reason=startup` and `Full refresh from Tally completed: reason=..., Tally export 30.0s ...` in `logs\feeder.log`.
