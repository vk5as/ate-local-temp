# 34: Three manuals in LaTeX

**Parent:** none. User request, 2026-11-01.

**What to build:** Three LaTeX manuals and three matching Markdown manuals for LLMs, written by Strata, then reviewed by Opus and Haiku. Start only when the CI fixes (31e, 32b) are done, because the manuals describe the PySide6 application.

**Blocked by:** 32b, 31e

**Status:** queued

## Manuals

Sources under `docs/manuals/<name>/`, with a shared preamble in `docs/manuals/common/`. Use LaTeX compiled with `lualatex` through `latexmk`. Use `GLOSSARY.md`'s terms everywhere.

- [ ] **`test-writer`, for Lua test writers.** Writing a Test Schedule from nothing: Media Releases and `.atedef`, the schedule format, steps, Step Inputs, Sub-schedules, modules and `require`, Pre-test and Post-test Actions, retries and Stop on Fail, the `ATE` table, Devices (TCP, UDP, SERIAL, VISA) and error handling, logging, results and reports, checksums and mismatches, and what the sandbox forbids. Every example must really run; build them from `examples/`.
- [ ] **`admin`, for ATE system administrators.** Installing on Windows and Linux, `ate setup` and permissions, the config file and every key, database migration and backup, Station setup, Media Release deployment, `storeMismatchedFiles`, logs and where they are, reports and PDF including `--allow-unsandboxed-pdf`, upgrade and uninstall, troubleshooting by symptom, and third-party licences.
- [ ] **`developer`, for ATE developers.** Architecture and module map, the run engine and threading rules, the run session, UI layouts, storage and schema, the Lua host and sandbox, Device drivers, reports, packaging and release CI, test suite seams and fixtures, PySide6 traps found in 32a, ADRs, and how to add a Device type or a schema version.

## Markdown manuals for LLMs

User request, 2026-11-01. Strata also writes a Markdown manual for each of the same three jobs, under `docs/manuals/llm/` (`test-writer.md`, `admin.md`, `developer.md`), so an LLM agent can do that job. Opus and Haiku review them with the LaTeX manuals.

- [ ] Written for an agent, not converted from the PDF text: it says what to do, in order, with exact commands, file paths, formats and constraints, how to check the result worked, and what never to do. Follow `.scratch/ate-v1`'s conventions and the `writing-for-agents` guidance. Compact, with plain structure and no screenshots.
- [ ] Same facts as the LaTeX manual, and the same rules below: names checked against the code, and examples that really run. The test that catches a missing config key or `ATE` function covers these files too.
- [ ] Each is usable alone, without the PDF. An agent with only that file and the repo or installed application can do the job. The reviewers check this by following it for a small real task: writing and running a Test Schedule, setting up a Station in a temporary directory with `ate setup --root`, and tracing where a code change would go.

## Rules

- [ ] Facts come from the code and `.scratch/ate-v1-wayfinding/assets/`. Read the code when they disagree, and list the disagreements in the final reply rather than guessing. Nothing is described that the code does not do. Mark limits plainly, such as Windows checks that have only run headless.
- [ ] Every command, config key and API name is checked against the code. Every Lua example runs headless through ATE in a test or a script kept beside the manual.
- [ ] A test fails if a config key or `ATE` function in the code is missing from the relevant manual, so the manuals can't silently fall behind.
- [ ] Each manual builds without errors or undefined references, with `latexmk -lualatex`. The toolchain is not on this machine, and installing system packages isn't allowed: build in Docker (`texlive/texlive`) or say what else was used. The PDFs are not committed; say how to build them in `docs/building-from-source.md`.
- [ ] Screenshots, if any, come from the real application and are said to come from offscreen grabs. The interface may change after the appearance work (33), so keep them few.

## Review

Opus and Haiku both review all three manuals, reading the rendered PDFs as well as the sources: accuracy against the code, whether the intended reader could actually do the job from the manual, examples that don't run, gaps, and layout. Rework, then the orchestrator merges.
