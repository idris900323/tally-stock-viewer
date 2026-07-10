# Measures how long each Tally XML request used by the app's stock refresh takes,
# plus a proposed lighter single-request replacement, so they can be compared on
# real data. Read-only: only sends Export requests, changes nothing in Tally.
#
# Run on the office PC while Tally Prime is open with the company loaded:
#   powershell -ExecutionPolicy Bypass -File .\scripts\measure_tally.ps1
#
# Requests A/B/C are byte-identical to what fetch_item_stock_flat() in app.py
# sends on every 3-minute cycle today. Request D is the proposed replacement.

param([string]$TallyUrl = "http://localhost:9000")

$ErrorActionPreference = "Stop"

function Invoke-TallyTimed {
    param([string]$Label, [string]$Xml)
    $sw = [System.Diagnostics.Stopwatch]::StartNew()
    try {
        $resp = Invoke-WebRequest -Uri $TallyUrl -Method Post -Body $Xml -ContentType "text/xml;charset=utf-8" -UseBasicParsing -TimeoutSec 300
        $sw.Stop()
        $content = $resp.Content
        Write-Host ("{0,-44} {1,7:N2}s   {2,12:N0} chars" -f $Label, $sw.Elapsed.TotalSeconds, $content.Length)
        return $content
    } catch {
        $sw.Stop()
        Write-Host ("{0,-44} {1,7:N2}s   FAILED: {2}" -f $Label, $sw.Elapsed.TotalSeconds, $_.Exception.Message)
        return ""
    }
}

# --- A: Collection "List of Stock Items" (master names) — as in app.py _fetch_stock_item_master_names
$xmlA = @'
<ENVELOPE>
    <HEADER>
        <VERSION>1</VERSION>
        <TALLYREQUEST>Export Data</TALLYREQUEST>
        <TYPE>Collection</TYPE>
        <ID>List of Stock Items</ID>
    </HEADER>
    <BODY>
        <DESC>
            <STATICVARIABLES>
                <SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>
            </STATICVARIABLES>
        </DESC>
    </BODY>
</ENVELOPE>
'@

# --- B: Stock Summary, non-detailed — as in app.py _fetch_rows(detailed=False)
$xmlB = @'
<ENVELOPE>
    <HEADER>
        <VERSION>1</VERSION>
        <TALLYREQUEST>Export</TALLYREQUEST>
        <TYPE>Data</TYPE>
        <ID>Stock Summary</ID>
    </HEADER>
    <BODY>
        <DESC>
            <STATICVARIABLES>
                <SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>
            </STATICVARIABLES>
        </DESC>
    </BODY>
</ENVELOPE>
'@

# --- C: Stock Summary, detailed + exploded — as in app.py _fetch_rows(detailed=True)
$xmlC = @'
<ENVELOPE>
    <HEADER>
        <VERSION>1</VERSION>
        <TALLYREQUEST>Export</TALLYREQUEST>
        <TYPE>Data</TYPE>
        <ID>Stock Summary</ID>
    </HEADER>
    <BODY>
        <DESC>
            <STATICVARIABLES>
                <SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>
                <SVSTOCKGROUP>Primary</SVSTOCKGROUP>
                <ISDETAILED>Yes</ISDETAILED>
                <EXPLODEFLAG>Yes</EXPLODEFLAG>
                <SVSHOWALLITEMS>Yes</SVSHOWALLITEMS>
                <SVSHOWZEROBALANCES>Yes</SVSHOWZEROBALANCES>
            </STATICVARIABLES>
        </DESC>
    </BODY>
</ENVELOPE>
'@

# --- D: PROPOSED single replacement — TDL collection walk over StockItem masters
#     fetching Name + ClosingBalance directly (no report rendering / explode).
$xmlD = @'
<ENVELOPE>
    <HEADER>
        <VERSION>1</VERSION>
        <TALLYREQUEST>Export Data</TALLYREQUEST>
        <TYPE>Collection</TYPE>
        <ID>Item Names With Closing</ID>
    </HEADER>
    <BODY>
        <DESC>
            <STATICVARIABLES>
                <SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>
            </STATICVARIABLES>
            <TDL>
                <TDLMESSAGE>
                    <COLLECTION NAME="Item Names With Closing" ISMODIFY="No">
                        <TYPE>StockItem</TYPE>
                        <FETCH>NAME, CLOSINGBALANCE</FETCH>
                    </COLLECTION>
                </TDLMESSAGE>
            </TDL>
        </DESC>
    </BODY>
</ENVELOPE>
'@

Write-Host ("Tally URL: " + $TallyUrl)
Write-Host "Two passes, so a warm-cache second run is visible."

foreach ($pass in 1..2) {
    Write-Host ""
    Write-Host ("=== PASS " + $pass + " ===")

    $a = Invoke-TallyTimed "A. Collection: List of Stock Items" $xmlA
    if ($a) {
        $countA = ([regex]::Matches($a, '<STOCKITEM')).Count
        Write-Host ("   A sanity: STOCKITEM nodes = " + $countA)
    }

    $b = Invoke-TallyTimed "B. Stock Summary (non-detailed)" $xmlB
    if ($b) {
        $countB = ([regex]::Matches($b, '<DSPACCNAME')).Count
        Write-Host ("   B sanity: DSPACCNAME rows = " + $countB)
    }

    $c = Invoke-TallyTimed "C. Stock Summary (detailed+exploded)" $xmlC
    if ($c) {
        $countC = ([regex]::Matches($c, '<DSPACCNAME')).Count
        Write-Host ("   C sanity: DSPACCNAME rows = " + $countC)
    }

    $d = Invoke-TallyTimed "D. PROPOSED: item collection w/ closing" $xmlD
    if ($d) {
        $countD = ([regex]::Matches($d, '<STOCKITEM')).Count
        $bal = [regex]::Matches($d, '<CLOSINGBALANCE[^>]*>([^<]*)</CLOSINGBALANCE>')
        $nonEmpty = @($bal | Where-Object { $_.Groups[1].Value.Trim() -ne '' })
        Write-Host ("   D sanity: STOCKITEM nodes = " + $countD + ", CLOSINGBALANCE values = " + $bal.Count + " (non-empty = " + $nonEmpty.Count + ")")
        $samples = $bal | Select-Object -First 5 | ForEach-Object { $_.Groups[1].Value.Trim() }
        Write-Host ("   D sample balances: " + ($samples -join " | "))
    }
}

Write-Host ""
Write-Host "Done. Paste this whole output back."
