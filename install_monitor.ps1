# ============================================================
#  Установка Monitor PC для ESP32
#  Запуск: правый клик -> "Выполнить с помощью PowerShell"
#  (или: powershell -ExecutionPolicy Bypass -File install_monitor.ps1)
# ============================================================

$ErrorActionPreference = "Stop"
$OutputEncoding = [System.Text.Encoding]::UTF8

Write-Host "============================================" -ForegroundColor Cyan
Write-Host " Установка Monitor PC для ESP32" -ForegroundColor Cyan
Write-Host "============================================" -ForegroundColor Cyan
Write-Host ""

# --- Проверка прав администратора ---
$isAdmin = ([Security.Principal.WindowsPrincipal] `
    [Security.Principal.WindowsIdentity]::GetCurrent()
).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)

if (-not $isAdmin) {
    Write-Host "[ОШИБКА] Запустите скрипт от имени администратора." -ForegroundColor Red
    Write-Host "Правый клик по файлу -> 'Выполнить с помощью PowerShell'." -ForegroundColor Yellow
    Pause
    exit 1
}

# --- Определяем папку скрипта ---
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$MonitorPy = Join-Path $ScriptDir "monitor_pc.py"

if (-not (Test-Path $MonitorPy)) {
    Write-Host "[ОШИБКА] Файл monitor_pc.py не найден в папке:" -ForegroundColor Red
    Write-Host "  $ScriptDir" -ForegroundColor Yellow
    Pause
    exit 1
}

# --- Шаг 1: проверка Python ---
Write-Host "[1/4] Проверка Python..." -ForegroundColor Cyan

$pythonCmd = $null
foreach ($candidate in @("py", "python", "python3")) {
    try {
        $cmd = Get-Command $candidate -ErrorAction SilentlyContinue -CommandType Application
        if ($cmd) {
            # Проверяем, что это Python, а не заглушка Microsoft Store
            $versionOutput = & $candidate --version 2>&1
            if ($versionOutput -match "Python 3\.\d+") {
                $pythonCmd = $candidate
                Write-Host "  Python найден: $versionOutput (команда: $candidate)" -ForegroundColor Green
                break
            }
        }
    } catch {
        continue
    }
}

if (-not $pythonCmd) {
    Write-Host "  Python не найден. Устанавливаю через winget..." -ForegroundColor Yellow

    # Проверяем, есть ли winget
    $winget = Get-Command winget -ErrorAction SilentlyContinue
    if (-not $winget) {
        Write-Host "[ОШИБКА] winget не найден. Установите Python вручную:" -ForegroundColor Red
        Write-Host "  https://www.python.org/downloads/" -ForegroundColor Yellow
        Pause
        exit 1
    }

    # Устанавливаем Python 3.12 системно, с автоматическим добавлением в PATH
    # --scope machine + override с PrependPath=1 гарантируют, что PATH обновится
    $wingetArgs = @(
        "install", "--id", "Python.Python.3.12", "-e",
        "--scope", "machine",
        "--silent",
        "--accept-source-agreements",
        "--accept-package-agreements",
        "--override", "/quiet InstallAllUsers=1 PrependPath=1 Include_test=0 Include_pip=1 Include_launcher=1 InstallLauncherAllUsers=1"
    )

    Write-Host "  Выполняю: winget $($wingetArgs -join ' ')" -ForegroundColor DarkGray
    & winget @wingetArgs

    if ($LASTEXITCODE -ne 0) {
        Write-Host "[ОШИБКА] winget не смог установить Python (код $LASTEXITCODE)." -ForegroundColor Red
        Write-Host "Установите Python вручную и запустите скрипт снова." -ForegroundColor Yellow
        Pause
        exit 1
    }

    Write-Host "  Python установлен." -ForegroundColor Green

    # Обновляем PATH для текущей сессии
    $machinePath = [Environment]::GetEnvironmentVariable("Path", "Machine")
    $userPath    = [Environment]::GetEnvironmentVariable("Path", "User")
    $env:Path = "$machinePath;$userPath"

    # Перепроверяем, что Python теперь доступен
    $pythonCmd = $null
    foreach ($candidate in @("py", "python", "python3")) {
        try {
            $versionOutput = & $candidate --version 2>&1
            if ($versionOutput -match "Python 3\.\d+") {
                $pythonCmd = $candidate
                break
            }
        } catch { continue }
    }

    if (-not $pythonCmd) {
        Write-Host "[ПРЕДУПРЕЖДЕНИЕ] Python установлен, но не виден в PATH текущей сессии." -ForegroundColor Yellow
        Write-Host "  Возможно, потребуется перезагрузка или новый терминал." -ForegroundColor Yellow
        Write-Host "  Продолжаю с командой 'python'..." -ForegroundColor Yellow
        $pythonCmd = "python"
    }
}

# --- Шаг 2: установка зависимостей ---
Write-Host ""
Write-Host "[2/4] Установка Python-библиотек..." -ForegroundColor Cyan

$deps = @("pyserial", "requests", "wmi", "pywin32")
foreach ($dep in $deps) {
    Write-Host "  Устанавливаю $dep..." -ForegroundColor DarkGray
    & $pythonCmd -m pip install --quiet --upgrade $dep
    if ($LASTEXITCODE -ne 0) {
        Write-Host "[ОШИБКА] Не удалось установить $dep." -ForegroundColor Red
        Pause
        exit 1
    }
}
Write-Host "  Все библиотеки установлены." -ForegroundColor Green

# --- Шаг 3: настройка автозагрузки ---
Write-Host ""
Write-Host "[3/4] Настройка автозагрузки..." -ForegroundColor Cyan

$StartupFolder = [Environment]::GetFolderPath("Startup")
Write-Host "  Папка автозагрузки: $StartupFolder" -ForegroundColor DarkGray

# Создаём .bat, который запускает Python без окна консоли (pythonw)
# pythonw.exe — windows-subsystem, окно не открывается [citation:5]
$BatPath = Join-Path $ScriptDir "start_monitor.bat"
$BatContent = @"
@echo off
cd /d "$ScriptDir"
start "" "pythonw" "monitor_pc.py"
"@
Set-Content -Path $BatPath -Value $BatContent -Encoding ASCII
Write-Host "  Создан: $BatPath" -ForegroundColor Green

# Создаём ярлык в папке автозагрузки через COM WScript.Shell
$ShortcutPath = Join-Path $StartupFolder "MonitorPC.lnk"
$WshShell = New-Object -ComObject WScript.Shell
$Shortcut = $WshShell.CreateShortcut($ShortcutPath)
$Shortcut.TargetPath = $BatPath
$Shortcut.WorkingDirectory = $ScriptDir
$Shortcut.WindowStyle = 7   # 7 = свёрнутое окно (без вспышки)
$Shortcut.Description = "Monitor PC для ESP32"
$Shortcut.Save()
Write-Host "  Ярлык создан: $ShortcutPath" -ForegroundColor Green

# --- Шаг 4: проверка ---
Write-Host ""
Write-Host "[4/4] Проверка..." -ForegroundColor Cyan

$checkCmd = @"
import sys
import serial
import requests
import wmi
import pythoncom
print('OK')
"@

$checkFile = Join-Path $env:TEMP "monitor_check.py"
Set-Content -Path $checkFile -Value $checkCmd -Encoding UTF8

$result = & $pythonCmd $checkFile 2>&1
Remove-Item $checkFile -Force -ErrorAction SilentlyContinue

if ($result -match "OK") {
    Write-Host "  Все библиотеки импортируются корректно." -ForegroundColor Green
} else {
    Write-Host "[ПРЕДУПРЕЖДЕНИЕ] Проверка выявила проблемы:" -ForegroundColor Yellow
    Write-Host $result -ForegroundColor DarkGray
}

# --- Итог ---
Write-Host ""
Write-Host "============================================" -ForegroundColor Cyan
Write-Host " Готово!" -ForegroundColor Green
Write-Host "============================================" -ForegroundColor Cyan
Write-Host ""
Write-Host "Автозагрузка настроена. При следующем входе в Windows" -ForegroundColor White
Write-Host "скрипт monitor_pc.py запустится автоматически." -ForegroundColor White
Write-Host ""
Write-Host "Логи: $ScriptDir\logs\" -ForegroundColor DarkGray
Write-Host ""
Pause