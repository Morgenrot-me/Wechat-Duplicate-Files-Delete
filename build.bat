@echo off
setlocal
cd /d "%~dp0"

set "PY_LAUNCHER=py -3.11"
set "VENV_DIR=%~dp0.venv-build"
set "VENV_PYTHON=%VENV_DIR%\Scripts\python.exe"

echo [1/4] Checking Python 3.11...
%PY_LAUNCHER% -c "import sys; print(sys.executable)" >nul 2>nul || goto :python_missing

echo [2/4] Checking local build environment...
if not exist "%VENV_PYTHON%" goto :venv_missing

"%VENV_PYTHON%" -c "import importlib.util, sys; mods=['PyQt5','send2trash','PyInstaller']; missing=[m for m in mods if importlib.util.find_spec(m) is None]; print('Missing dependencies: ' + ', '.join(missing)) if missing else print('All dependencies are present.'); sys.exit(1 if missing else 0)" || goto :missing

echo [3/4] Running verification...
"%VENV_PYTHON%" verify.py || goto :error

echo [4/4] Building executable...
"%VENV_PYTHON%" -m PyInstaller --noconfirm --clean --onefile --windowed --icon="%~dp0logo.ico" --add-data "%~dp0logo.png;." "%~dp0main.py" || goto :error

echo Build finished. Output folder: dist
pause
exit /b 0

:python_missing
echo Python 3.11 was not found through the py launcher.
echo Install official Python 3.11 and make sure "py -3.11" works, then run build.bat again.
pause
exit /b 1

:venv_missing
echo Local build environment .venv-build was not found.
echo Create it once with Python 3.11 and install PyQt5, send2trash, and PyInstaller, then run build.bat again.
pause
exit /b 1

:missing
echo Required packages are missing in .venv-build.
echo Install PyQt5, send2trash, and PyInstaller into .venv-build, then run build.bat again.
pause
exit /b 1

:error
echo Build failed. Check the messages above.
pause
exit /b 1
