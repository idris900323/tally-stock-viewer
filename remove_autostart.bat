@echo off
REG DELETE "HKCU\Software\Microsoft\Windows\CurrentVersion\Run" /v "TallyStockViewer" /f
echo Auto-start removed. Server will no longer start on login.
pause
