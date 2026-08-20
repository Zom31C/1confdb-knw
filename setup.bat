@echo off
rem confdb environment setup: create .venv and install the package.
rem Run by double-click or from a console: setup.bat
rem Pure ASCII: readable in any console code page (866/1251/65001); all other
rem user-facing text is printed by the Python layer in UTF-8.
setlocal
cd /d "%~dp0"

if exist ".venv\Scripts\python.exe" (
    echo venv already exists: .venv
    goto install
)

echo Creating virtual environment in .venv ...
python -m venv .venv 2>nul
if not exist ".venv\Scripts\python.exe" py -3 -m venv .venv 2>nul
if not exist ".venv\Scripts\python.exe" (
    echo Error: Python 3.9+ not found in PATH. Install Python and re-run.
    exit /b 1
)

:install
echo Installing confdb into venv ...
".venv\Scripts\python.exe" -m pip install --upgrade pip
".venv\Scripts\python.exe" -m pip install .
if errorlevel 1 (
    echo Retrying install without build isolation ...
    ".venv\Scripts\python.exe" -m pip install --no-build-isolation .
)
if errorlevel 1 (
    echo Package installation failed.
    exit /b 1
)

echo.
echo Done. Usage:
echo   confdb.bat extract file.cf --db out.sqlite [--dump dir]
echo   confdb-ui.bat              text console interface
echo   1confdb-knw.bat out.db     MCP server for LLM clients
endlocal
