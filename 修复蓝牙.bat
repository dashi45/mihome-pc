@echo off
setlocal
title Fix Bluetooth Adapter
fltmc >nul 2>&1
if not "%errorlevel%"=="0" (
  echo Requesting administrator rights...
  echo Please click YES on the UAC prompt.
  powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
  exit /b
)

set "BAD=USB\VID_0000&PID_0002\5&2FFDE795&0&14"

echo ==========================================================
echo   Fix Bluetooth: remove bad placeholder, rescan, restart
echo ==========================================================
echo.
echo [1/3] Removing the broken placeholder device (error 43)...
pnputil /remove-device "%BAD%"
echo.
echo [2/3] Rescanning hardware...
pnputil /scan-devices
echo.
echo [3/3] Restarting Bluetooth Support Service...
net stop bthserv /y >nul 2>&1
net start bthserv >nul 2>&1
echo.
echo ---- Bluetooth devices now ----
powershell -NoProfile -Command "Get-PnpDevice -Class Bluetooth -ErrorAction SilentlyContinue | Select-Object Status,FriendlyName | Format-Table -AutoSize"
echo.
echo If you still do not see [Intel(R) Wireless Bluetooth / OK],
echo do a FULL SHUTDOWN (not Restart) and power on again.
echo.
pause
