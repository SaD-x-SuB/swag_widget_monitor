@echo off
chcp 65001 >nul
setlocal EnableDelayedExpansion

:: --- Проверка прав администратора и перезапуск с UAC ---
net session >nul 2>&1
if %errorLevel% neq 0 (
    echo Запрос прав администратора...
    powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
    exit /b
)

:: --- Мы уже с правами администратора ---
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install_monitor.ps1"

pause