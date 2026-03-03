@echo off
:: ═══════════════════════════════════════════════════════════════════════
:: Hivemind Installer — Windows
::
:: Creates an isolated environment at %USERPROFILE%\.hivemind-env\ and
:: adds the `hivemind` command to your PATH.
::
:: Usage:
::   install.cmd              Install or upgrade Hivemind
::   install.cmd --uninstall  Remove Hivemind completely
:: ═══════════════════════════════════════════════════════════════════════
setlocal enabledelayedexpansion

set "INSTALL_DIR=%USERPROFILE%\.hivemind-env"
set "BIN_NAME=hivemind"
set "SCRIPT_DIR=%~dp0"

echo.
echo   * Hivemind Installer
echo.

:: ── Uninstall ─────────────────────────────────────────────────────────
if "%~1"=="--uninstall" (
    echo   Uninstalling Hivemind...

    :: Remove from PATH
    set "USER_PATH="
    for /f "tokens=2*" %%a in ('reg query "HKCU\Environment" /v Path 2^>nul') do set "USER_PATH=%%b"
    if defined USER_PATH (
        set "NEW_PATH=!USER_PATH:%INSTALL_DIR%\Scripts;=!"
        if not "!NEW_PATH!"=="!USER_PATH!" (
            reg add "HKCU\Environment" /v Path /t REG_EXPAND_SZ /d "!NEW_PATH!" /f >nul 2>nul
            echo   Removed from PATH
        )
    )

    if exist "%INSTALL_DIR%" (
        rmdir /s /q "%INSTALL_DIR%"
        echo   Removed %INSTALL_DIR%
    )

    echo.
    echo   Hivemind uninstalled.
    echo   Config files remain at %USERPROFILE%\.hivemind\ ^(delete manually if desired^)
    echo.
    goto :eof
)

:: ── Check Python ──────────────────────────────────────────────────────
set "PYTHON="
for %%p in (python3 python py) do (
    where %%p >nul 2>nul && (
        for /f "tokens=*" %%v in ('%%p -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" 2^>nul') do (
            %%p -c "import sys; exit(0 if sys.version_info >= (3, 12) else 1)" 2>nul && (
                set "PYTHON=%%p"
                set "PYTHON_VER=%%v"
                goto :found_python
            )
        )
    )
)

echo   [ERROR] Python 3.12+ is required but not found.
echo   Install from: https://www.python.org/downloads/
echo.
exit /b 1

:found_python
echo   Found Python %PYTHON_VER% (%PYTHON%)

:: ── Find wheel or source ─────────────────────────────────────────────
set "INSTALL_SRC="
for %%w in ("%SCRIPT_DIR%dist\hivemind_ai-*.whl") do (
    if exist "%%w" set "INSTALL_SRC=%%w"
)

if not defined INSTALL_SRC (
    if exist "%SCRIPT_DIR%pyproject.toml" (
        set "INSTALL_SRC=%SCRIPT_DIR%"
        echo   Installing from source directory
    ) else (
        echo   [ERROR] No wheel found in dist\ and no pyproject.toml found.
        echo   Run build.sh first, or run from the project directory.
        echo.
        exit /b 1
    )
) else (
    echo   Installing from wheel
)

:: ── Create isolated environment ──────────────────────────────────────
if exist "%INSTALL_DIR%" (
    echo   Upgrading existing installation...
) else (
    echo   Creating isolated environment at %INSTALL_DIR%...
)

%PYTHON% -m venv "%INSTALL_DIR%" --clear
if errorlevel 1 (
    echo   [ERROR] Failed to create virtual environment
    exit /b 1
)
echo   Virtual environment ready

:: ── Install package ──────────────────────────────────────────────────
echo   Installing Hivemind and dependencies...
"%INSTALL_DIR%\Scripts\pip.exe" install --upgrade pip -q 2>nul
"%INSTALL_DIR%\Scripts\pip.exe" install "%INSTALL_SRC%" -q 2>nul

if not exist "%INSTALL_DIR%\Scripts\hivemind.exe" (
    echo   [ERROR] Installation failed: hivemind.exe not found
    exit /b 1
)

for /f "tokens=*" %%v in ('"%INSTALL_DIR%\Scripts\python.exe" -c "import hivemind; print(hivemind.__version__)" 2^>nul') do set "VERSION=%%v"
echo   Hivemind v%VERSION% installed

:: ── Add to PATH ──────────────────────────────────────────────────────
echo   Adding to PATH...

set "BIN_PATH=%INSTALL_DIR%\Scripts"
set "USER_PATH="
for /f "tokens=2*" %%a in ('reg query "HKCU\Environment" /v Path 2^>nul') do set "USER_PATH=%%b"

echo !USER_PATH! | findstr /i /c:"%BIN_PATH%" >nul 2>nul
if errorlevel 1 (
    if defined USER_PATH (
        reg add "HKCU\Environment" /v Path /t REG_EXPAND_SZ /d "%BIN_PATH%;!USER_PATH!" /f >nul
    ) else (
        reg add "HKCU\Environment" /v Path /t REG_EXPAND_SZ /d "%BIN_PATH%" /f >nul
    )
    :: Broadcast environment change
    powershell -Command "[Environment]::SetEnvironmentVariable('Path', [Environment]::GetEnvironmentVariable('Path','User'), 'User')" 2>nul
    echo   Added %BIN_PATH% to user PATH
) else (
    echo   Already in PATH
)

:: ── Done ──────────────────────────────────────────────────────────────
echo.
echo   Hivemind installed successfully!
echo.
echo   Run:        hivemind
echo   Demo mode:  hivemind --demo
echo   Uninstall:  %SCRIPT_DIR%install.cmd --uninstall
echo.
echo   NOTE: You may need to restart your terminal for PATH changes.
echo.
