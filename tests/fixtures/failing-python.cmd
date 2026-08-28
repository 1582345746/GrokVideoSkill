@echo off
if /I "%~2"=="components-install" exit /b 31
python %*
