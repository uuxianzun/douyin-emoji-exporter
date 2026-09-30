@echo off
setlocal enabledelayedexpansion
chcp 65001 >nul 2>nul
cd /d "%~dp0"
title Douyin Emoji Exporter

rem ---------------------------------------------------------------
rem  Douyin Emoji Exporter - launcher
rem
rem  This file is intentionally pure ASCII. Windows cmd parses .bat
rem  using the system ANSI codepage (GBK on Chinese systems), so any
rem  non-ASCII text here would corrupt and break the script.
rem  All Chinese output is produced by run.py instead.
rem
rem  Logic (system Python takes priority - no needless 11MB download):
rem    1. If a system Python exists      - use it.
rem    2. Else if a HEALTHY portable one - use it.
rem    3. Else download a portable one   - then use it.
rem
rem  A "healthy" portable runtime means python.exe AND the standard
rem  library (python311.zip) are both present. A half-deleted .runtime
rem  would otherwise break every import.
rem ---------------------------------------------------------------

set "RUNTIME=%~dp0.runtime\python.exe"
set "STDLIB=%~dp0.runtime\python311.zip"

rem --- 1. prefer a system Python ---
python --version >nul 2>nul
if not errorlevel 1 goto runpy

py -3 --version >nul 2>nul
if not errorlevel 1 goto runpy2

rem --- 2. fall back to a healthy portable runtime ---
if not exist "%RUNTIME%" goto bootstrap
if not exist "%STDLIB%" goto broken
if exist "%~dp0.runtime\.cleanup-pending" goto broken

"%RUNTIME%" run.py
goto done

:runpy
python "%~dp0run.py"
goto done

:runpy2
py -3 "%~dp0run.py"
goto done

rem --- 3. portable runtime is incomplete: wipe and re-download ---
:broken
echo ============================================================
echo   Runtime looks incomplete, repairing...
echo ============================================================
echo.
rmdir /s /q "%~dp0.runtime" >nul 2>nul
if exist "%RUNTIME%" goto locked

:bootstrap
echo ============================================================
echo   Preparing Python runtime (one-time, about 11 MB)
echo ============================================================
echo.
echo   Please wait...
echo.

set "BOOTDIR=%~dp0.runtime"
if not exist "%BOOTDIR%" mkdir "%BOOTDIR%"
set "BOOTZIP=%BOOTDIR%\bootstrap.zip"
set "URL=https://mirrors.huaweicloud.com/python/3.11.9/python-3.11.9-embed-amd64.zip"

powershell -NoProfile -ExecutionPolicy Bypass -Command "$ErrorActionPreference='Stop'; try { [Net.ServicePointManager]::SecurityProtocol=[Net.SecurityProtocolType]::Tls12; Invoke-WebRequest -Uri '%URL%' -OutFile '%BOOTZIP%' -UseBasicParsing } catch { exit 1 }"

if errorlevel 1 goto dlfail
if not exist "%BOOTZIP%" goto dlfail

powershell -NoProfile -ExecutionPolicy Bypass -Command "Expand-Archive -Path '%BOOTZIP%' -DestinationPath '%BOOTDIR%' -Force"
if errorlevel 1 goto dlfail

del /q "%BOOTZIP%" >nul 2>nul

if not exist "%RUNTIME%" goto dlfail

echo   Runtime ready.
echo.
"%RUNTIME%" run.py
goto done

:locked
echo.
echo   [ERROR] Cannot repair the runtime: files are in use.
echo.
echo   Please close all other windows of this tool, then run
echo   start.bat again.
echo.
echo   If it still fails, delete this folder manually:
echo       %~dp0.runtime
echo.
pause
exit /b 1

:dlfail
echo.
echo   [ERROR] Download or extraction failed.
echo   Please check your network connection and try again.
echo.
echo   If the problem persists, install Python manually from:
echo       https://www.python.org/downloads/
echo   and CHECK "Add Python to PATH" during installation.
echo.
pause
exit /b 1

:done
echo.
echo   Service stopped.
pause
exit /b 0
