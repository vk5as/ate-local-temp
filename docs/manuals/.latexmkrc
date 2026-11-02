# .latexmkrc - how every ATE manual is compiled (packets 34a, 34b and 34c).
#
# Run through Docker, because the TeX toolchain is not installed on the build machine:
#   docs/manuals/build.py [test-writer]
# compiles each manual's main.tex from this directory, with the shared preamble in common/
# on TEXINPUTS and the PDFs in build/<name>/ (gitignored).

$pdf_mode = 5;   # 5 = lualatex
$lualatex = 'lualatex -interaction=nonstopmode -file-line-error %O %s';
