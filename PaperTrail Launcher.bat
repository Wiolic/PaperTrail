@echo off
cd /d "%~dp0"
echo Starting PaperTrail...
echo.
echo 浏览器关闭后此窗口会自动退出。
echo (If this window doesn't close automatically, close it manually.)
echo.
cscript //nologo "%~dp0scripts\ui\run_hidden.vbs"
