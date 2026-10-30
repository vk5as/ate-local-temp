# 30d: Choice before an unsandboxed PDF

**Parent:** none. User decision, 2026-10-30. It replaces the 27c behaviour, which turned off Chromium's sandbox without asking when ATE ran as root or an elevated administrator.

**What to build:** Chromium's sandbox does not work as root or elevated, so a PDF there has to render without it. ATE must never do that silently. An elevated user chooses before each PDF, and a start-up flag presets the choice for headless use. Ordinary users are unaffected: they keep the sandbox and see no prompt.

**Blocked by:** 28c (Linux release CI: its smoke runs as root and must pass the new flag)

**Status:** ready-for-agent

- [ ] No code path turns the sandbox off automatically. `_sandbox_flags`/`_child_env` and `render_pdf` in `src/ate/report/pdf.py`, and `packaging/pyinstaller/ate_entry.py`, disable it only when the choice below was made. An unprivileged process never disables it, even with the flag.
- [ ] GUI: when elevated, before each PDF is generated (at run end, and on History's Regenerate), a dialog explains the risk in one or two plain lines and asks whether to generate the PDF without the sandbox. Default is No. No: no PDF is generated, the HTML Test Report is still written, and the skip is logged. Yes: this PDF renders unsandboxed, and that is logged. The HTML is written before the question, and the dialog never blocks the run's recording.
- [ ] Start-up flag `--allow-unsandboxed-pdf` on `ate` (documented in `--help` and `docs/building-from-source.md`) presets Yes with no dialog, and logs that it did. Without the flag and with no way to ask (headless, `_selftest --run`, a dismissed dialog), the answer is No.
- [ ] The hidden `_pdf` helper receives the decision explicitly from its parent (an argument, not an inherited guess). Run directly as root without that argument, it refuses with a clear message and a non-zero exit.
- [ ] `ate _selftest --run` and release CI: `packaging/linux/smoke_linux.py` and `packaging/windows/smoke_installer.py` pass the flag, because their runners are privileged.
- [ ] Tests (elevation simulated by monkeypatching; never really run as root on the host): ordinary user with and without the flag keeps the sandbox with no prompt; elevated + Yes / No / dismissed; elevated + flag; run-end and History paths; `_pdf` refusal; the environment handed to the helper has no sandbox-off settings unless the choice was Yes.

Definitions live in `.scratch/ate-v1-wayfinding/assets/` (`test-report-format.md`); the spec is `.scratch/ate-v1/spec.md`. Keep to this packet's checkboxes.
