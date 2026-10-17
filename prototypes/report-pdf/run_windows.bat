@echo off
rem PROTOTYPE, throwaway. Run from this folder on 64-bit Windows with CPython 3.13 or 3.14 on PATH as "py".
py -m venv .venv || exit /b 1
.venv\Scripts\pip install -q PyQt6 PyQt6-WebEngine xhtml2pdf jinja2 pypdf || exit /b 1
if not exist out mkdir out
echo. > out\results.txt
for %%E in (qwe x2p-thread x2p-process) do (
  .venv\Scripts\python render.py --engine %%E >> out\results.txt 2>&1
  .venv\Scripts\python render.py --engine %%E --steps 500 --logs 5000 >> out\results.txt 2>&1
)
.venv\Scripts\python render.py --engine qwe --steps 1000 --logs 10000 >> out\results.txt 2>&1
.venv\Scripts\python render.py --engine qwe --paper Letter >> out\results.txt 2>&1
.venv\Scripts\python render.py --engine x2p-process --paper Letter >> out\results.txt 2>&1
rem install footprint of each engine
powershell -NoProfile -Command "$sp='.venv\Lib\site-packages'; $we=(gci $sp\PyQt6\Qt6 -Recurse -File | ?{ $_.FullName -match 'WebEngine|qtwebengine|resources' } | measure Length -Sum).Sum/1MB; $x=(gci $sp\xhtml2pdf,$sp\reportlab,$sp\html5lib,$sp\svglib,$sp\lxml,$sp\PIL,$sp\pypdf -Recurse -File | measure Length -Sum).Sum/1MB; \"WebEngine MB: {0:N0}  xhtml2pdf+deps MB: {1:N0}\" -f $we,$x" >> out\results.txt
type out\results.txt | findstr /i "engine= MB:"
echo Open out\*.pdf and compare. Send back out\results.txt.
