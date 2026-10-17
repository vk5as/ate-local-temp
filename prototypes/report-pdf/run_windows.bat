@echo off
rem PROTOTYPE, throwaway. Run from this folder on 64-bit Windows with CPython 3.13 or 3.14 on PATH as "py".
setlocal
pushd "%~dp0"
rem the venv lives on the local disk: installing a few hundred MB onto a network share is very slow
set VENV=%LOCALAPPDATA%\ate-report-pdf-prototype\.venv
set PY=%VENV%\Scripts\python.exe
if not exist out mkdir out
echo ATE report PDF prototype > out\results.txt

echo.
echo [1/4] Creating venv in %VENV%
if not exist "%PY%" (
  py -m venv "%VENV%" || goto :fail
) else (
  echo       already there, reusing it
)

echo.
echo [2/4] Installing PyQt6, PyQt6-WebEngine, xhtml2pdf, jinja2, pypdf (a few hundred MB, takes a while)
"%PY%" -m pip install --disable-pip-version-check --progress-bar on PyQt6 PyQt6-WebEngine xhtml2pdf jinja2 pypdf || goto :fail

echo.
echo [3/4] Rendering. xhtml2pdf runs of 500 steps take about 35 s each; QWebEngine is much quicker.
call :render 1  qwe         40   200
call :render 2  x2p-thread  40   200
call :render 3  x2p-process 40   200
call :render 4  qwe         500  5000
call :render 5  x2p-thread  500  5000
call :render 6  x2p-process 500  5000
call :render 7  qwe         1000 10000
call :render 8  qwe         40   200   --paper Letter
call :render 9  x2p-process 40   200   --paper Letter

echo.
echo [4/4] Measuring install footprint
powershell -NoProfile -Command "$sp='%VENV%\Lib\site-packages'; $we=(gci $sp\PyQt6\Qt6 -Recurse -File | ?{ $_.FullName -match 'WebEngine|qtwebengine|resources' } | measure Length -Sum).Sum/1MB; $x=(gci $sp\xhtml2pdf,$sp\reportlab,$sp\html5lib,$sp\svglib,$sp\lxml,$sp\PIL,$sp\pypdf -Recurse -File -EA SilentlyContinue | measure Length -Sum).Sum/1MB; \"WebEngine MB: {0:N0}  xhtml2pdf+deps MB: {1:N0}\" -f $we,$x" > out\size.txt
type out\size.txt
type out\size.txt >> out\results.txt

echo.
echo Done. Results are in %CD%\out\results.txt and the PDFs are in %CD%\out\
popd
pause
exit /b 0

:render
rem %1 run number, %2 engine, %3 steps, %4 log lines, rest passed through
echo.
echo   (%1/9) %2, %3 steps, %4 log lines %5 %6 ... started %TIME%
"%PY%" render.py --engine %2 --steps %3 --logs %4 %5 %6 > out\last.txt 2>&1
type out\last.txt >> out\results.txt
findstr /c:"engine=" out\last.txt || (echo   FAILED, output follows: & type out\last.txt)
exit /b 0

:fail
echo.
echo FAILED at the step above.
popd
pause
exit /b 1
