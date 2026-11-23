@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo Registering yarophoto:// protocol (HKCU, no admin needed)...
regedit /s register-yarophoto.reg
echo Done. You can now use the Start button in index.html.
pause
