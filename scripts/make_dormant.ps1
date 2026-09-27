<#
Turns a copied-over full tally_stock folder into a clearly-marked DORMANT backup
so it can never be started by accident next to feeder.py.

Run it ONCE, on the new PC, from the ROOT of the copied folder (the folder that
contains app.py):

    cd C:\tally_stock
    powershell -ExecutionPolicy Bypass -File scripts\make_dormant.ps1

What it does (and nothing else):
  1. refuses to run unless app.py is in the current folder
  2. renames app.py, serve.py, launcher.pyw  ->  <name>.dormant
  3. removes any HKCU\...\Run autostart entry that points at THIS folder's launcher.pyw
  4. writes DORMANT_README.md with the reverse (revival) steps
  5. prints exactly what it changed

It deletes nothing and touches no data, images or database.
#>

$ErrorActionPreference = "Stop"
$root = (Get-Location).Path
$changes = New-Object System.Collections.Generic.List[string]
$warnings = New-Object System.Collections.Generic.List[string]

# ---- 1. Safety check: must be the root of a copied full-site folder ----
if (-not (Test-Path -LiteralPath (Join-Path $root "app.py") -PathType Leaf)) {
    Write-Host ""
    Write-Host "REFUSING TO RUN: app.py was not found in $root" -ForegroundColor Red
    if (Test-Path -LiteralPath (Join-Path $root "app.py.dormant")) {
        Write-Host "This folder already looks dormant (app.py.dormant exists). Nothing to do." -ForegroundColor Yellow
    } else {
        Write-Host "Open PowerShell IN the copied tally_stock folder (the one that contains app.py) and run this again." -ForegroundColor Yellow
    }
    exit 1
}

# Refuse (before changing anything) if a rename target already exists.
$targets = @("app.py", "serve.py", "launcher.pyw")
foreach ($name in $targets) {
    if (Test-Path -LiteralPath (Join-Path $root "$name.dormant")) {
        Write-Host "REFUSING TO RUN: $name.dormant already exists in $root. Nothing was changed." -ForegroundColor Red
        exit 1
    }
}

Write-Host ""
Write-Host "Making this folder dormant: $root"

# ---- 2. Rename the entry-point files ----
foreach ($name in $targets) {
    $path = Join-Path $root $name
    if (Test-Path -LiteralPath $path -PathType Leaf) {
        Rename-Item -LiteralPath $path -NewName "$name.dormant"
        $changes.Add("Renamed $name -> $name.dormant")
    } else {
        $warnings.Add("$name was not found here, so it was skipped")
    }
}

# ---- 3. Remove autostart entries that point at THIS folder's launcher.pyw ----
$launcherPath = Join-Path $root "launcher.pyw"
$runKey = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run"
$removedEntries = @()
if (Test-Path $runKey) {
    $props = Get-ItemProperty -Path $runKey
    foreach ($prop in $props.PSObject.Properties) {
        if ($prop.Name -like "PS*") { continue }   # PowerShell's own bookkeeping properties
        if ($prop.Value -is [string] -and $prop.Value.IndexOf($launcherPath, [System.StringComparison]::OrdinalIgnoreCase) -ge 0) {
            Remove-ItemProperty -Path $runKey -Name $prop.Name
            $removedEntries += $prop.Name
            $changes.Add("Removed autostart entry '$($prop.Name)' (it pointed at $launcherPath)")
        }
    }
}
if ($removedEntries.Count -eq 0) {
    $changes.Add("Checked the autostart key: no entry referenced $launcherPath (nothing to remove)")
}

# ---- 4. Explain everything for whoever finds this folder later ----
$stamp = Get-Date -Format "yyyy-MM-dd HH:mm"
$readme = @"
# DORMANT backup - do not run

This folder is an **intentionally deactivated backup snapshot** of the full
Tally Stock Viewer site, made on migration day ($stamp).

The live site now runs on **Render**, and the Tally machine only runs the small
``feeder.py`` (see ``FEEDER_SETUP.md``). To make sure this old full-site copy can
never start by accident and push data to Render at the same time as the feeder
(two machines pushing conflicting data), ``scripts\make_dormant.ps1`` did this:

- renamed ``app.py`` -> ``app.py.dormant``
- renamed ``serve.py`` -> ``serve.py.dormant``
- renamed ``launcher.pyw`` -> ``launcher.pyw.dormant``
- removed any Windows autostart entry (HKCU ``Run`` key) that pointed at this folder's ``launcher.pyw``

Nothing was deleted. Your data, images and ``mappings.db`` are untouched.

## Reviving it (genuine emergency fallback only)

**1. Stop the feeder first.** Two machines pushing to Render at once overwrite each
other. On the feeder machine:

    schtasks /End /TN TallyFeeder
    schtasks /Change /TN TallyFeeder /DISABLE

**2. Rename the files back** (PowerShell, in this folder):

    Rename-Item app.py.dormant app.py
    Rename-Item serve.py.dormant serve.py
    Rename-Item launcher.pyw.dormant launcher.pyw

**3. Start it** by double-clicking ``launcher.pyw`` (or run
``.venv\Scripts\pythonw.exe launcher.pyw``). See ``MASTER_SETUP.md`` for the full
legacy guide.

**4. Only if you want it to start with Windows again**, restore the autostart entry
(this uses the path of the folder it was made dormant in):

    reg add "HKCU\Software\Microsoft\Windows\CurrentVersion\Run" /v TallyStockViewer /t REG_SZ /d "\"$root\.venv\Scripts\pythonw.exe\" \"$root\launcher.pyw\"" /f

**When you are done,** make it dormant again (``scripts\make_dormant.ps1``), then
re-enable the feeder (``schtasks /Change /TN TallyFeeder /ENABLE`` and
``schtasks /Run /TN TallyFeeder``).
"@
Set-Content -LiteralPath (Join-Path $root "DORMANT_README.md") -Value $readme -Encoding UTF8
$changes.Add("Created DORMANT_README.md")

# ---- 5. Confirmation ----
Write-Host ""
Write-Host "DONE. This folder is now dormant. What changed:" -ForegroundColor Green
foreach ($line in $changes) { Write-Host "  - $line" }
foreach ($line in $warnings) { Write-Host "  ! $line" -ForegroundColor Yellow }
Write-Host ""
Write-Host "Nothing was deleted. See DORMANT_README.md for how to reverse this."
