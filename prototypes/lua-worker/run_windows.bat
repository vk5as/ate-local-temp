@echo off
rem PROTOTYPE, throwaway. Checks the Lua worker design on Windows with CPython 3.13 and 3.14 ("py" launcher).
setlocal
pushd "%~dp0"
if not exist out mkdir out
echo ATE Lua worker check > out\results.txt
for %%V in (3.13 3.14) do call :check %%V
echo.
echo ===== Results for both Pythons (also in %CD%\out\results.txt) =====
findstr /b /c:"PASS" /c:"FAIL" /c:"ALL PASSED" /c:"SOME CHECKS" /c:"Python " /c:"lupa " /c:"SKIPPED" out\results.txt
echo.
echo Next: an interactive window opens. Try the buttons yourself: run "busy" and drag the window
echo around, run "input" then click Continue, run "abort_busy" or "abort_input" then click Abort.
echo Close the window when done.
pause
"%LOCALAPPDATA%\ate-lua-worker-prototype\venv3.14\Scripts\python.exe" lua_worker_check.py 2>nul || "%LOCALAPPDATA%\ate-lua-worker-prototype\venv3.13\Scripts\python.exe" lua_worker_check.py
popd
exit /b 0

:check
set VENV=%LOCALAPPDATA%\ate-lua-worker-prototype\venv%1
echo.
echo ===== Python %1 =====
py -%1 -c "import sys" 2>nul || (echo SKIPPED: Python %1 is not installed & echo SKIPPED: Python %1 is not installed >> out\results.txt & exit /b 0)
if not exist "%VENV%\Scripts\python.exe" (
  echo [1/3] Creating venv in %VENV%
  py -%1 -m venv "%VENV%" || exit /b 0
) else (
  echo [1/3] Reusing venv in %VENV%
)
echo [2/3] Installing PyQt6 and lupa
"%VENV%\Scripts\python.exe" -m pip install --disable-pip-version-check --progress-bar on PyQt6 lupa
echo [3/3] Running the unattended checks (about 15 s; a window flashes up while they run)
echo. >> out\results.txt
"%VENV%\Scripts\python.exe" -u lua_worker_check.py --auto > out\last.txt 2>&1
type out\last.txt
type out\last.txt >> out\results.txt
exit /b 0
