# Orchestration playbook: implementing a spec with local-model subagents

How the ATE v1 spec is being built. An Opus orchestrator coordinates local-model implementer and reviewer subagents running through OpenCode. The worker model was Ornith-1.5 on LM Studio until 2026-10-24 (packets up to 16b), then Qwen3.5 9B FP8 on coder2 (vLLM). Everything here reflects what has actually been run and adjusted during this project, including the rules added after things went wrong.

Companion files (all in `.scratch/ate-v1/`):

| File | Purpose |
|---|---|
| `spec.md` | The spec (38k chars, don't hand it to subagents whole) |
| `issues/NN-*.md`, `issues/NNx-*.md` | Tickets and the smaller packets they were split into |
| `frontier.py` | Prints done / remaining / unblocked |
| `escalate-to-opus.md` | How a stuck subagent hands off to its own Opus helper |
| `review-packet.md` | Brief for the per-packet reviewer |
| `review-notes.md` | Gaps deferred to the final code review |
| `context-usage/collect.py`, `usage.jsonl` | Per-session context, compaction and token log |

---

## 1. Roles

| Role | Model / how it runs | Does | Never does |
|---|---|---|---|
| **Orchestrator** | Claude Opus (this session) | Picks the next packet, writes briefs, verifies, merges, logs usage, reports | Long implementation work (keeps its own context and usage lean) |
| **Implementer** | Qwen3.5 9B FP8 on coder2 via T3 `delegate_task`, `providerInstanceId: opencode`, `model: coder2/Qwen3.5-9B-FP8-dynamic`, `isolation: worktree` (formerly `lmstudio/ornith-1.5-9b`) | Builds one packet test-first on its own branch | Touches the integration branch or main checkout, creates worktrees, rebases, pushes |
| **Qwen reviewer** | Qwen3.5 9B FP8 on coder2, `delegate_task`, own worktree, read-only | Reviews the implementer branch (detached checkout), runs the suite, reports findings per `review-packet.md` | Edits, commits, branches |
| **Ornith reviewer** | Ornith-1.5 9B on LM Studio (`lmstudio/ornith-1.5-9b`), `delegate_task`, own worktree, read-only | Same as the Qwen reviewer | Edits, commits, branches |
| **Rework implementer** | Qwen (default) or Claude Opus (`providerInstanceId: claudeAgent`, `model: claude-opus-5-5`, worktree) | Fixes the triaged must-fix list on a new branch from the implementer branch | Work beyond the list |
| **Opus helper** | `claude -p --model claude-opus-5-5`, started by the worker subagent itself | Solves the one problem it was handed, in that subagent's worktree | Reports to the orchestrator (it reports to the worker that started it) |
| **Haiku reviewer** | Agent tool, `model: haiku`, background, read-only | Same review from the main checkout via `git diff`/`git show`, following `haiku-review.md` | Edits, commits, checkouts, running the suite |
| **Haiku side jobs** | Agent tool, `model: haiku` | Screenshots of the app, mechanical chores | Code changes on the integration branch |

**One subagent per machine at a time.** A local server loses its prompt cache when two sessions interleave, and prefill then dominates run time. coder2 (Qwen), zeus (LM Studio, Ornith) and Anthropic (Haiku, Opus) are separate, so the three reviewers run **simultaneously**, one on each. Implementers and rework still run one at a time.

---

## 2. One-time setup

1. **Integration branch:** `git checkout -b integration/ate-v1 main`.
2. **Guard it with a hook** so only the orchestrator can move it (implementers once merged themselves). `.git/hooks/reference-transaction`, executable:

   ```sh
   #!/bin/sh
   # Only the orchestrator (ATE_ORCHESTRATOR=1) may move integration/ate-v1; implementers work on their own branches.
   [ "$1" = prepared ] || exit 0
   [ -n "$ATE_ORCHESTRATOR" ] && exit 0
   while read -r old new ref; do
     if [ "$ref" = "refs/heads/integration/ate-v1" ]; then
       echo "integration/ate-v1 is updated only by the orchestrator; commit to your own branch and report back." >&2
       exit 1
     fi
   done
   exit 0
   ```

   Hooks live in the shared `.git`, so they apply in every worktree. The orchestrator prefixes its merges and commits on the integration branch with `ATE_ORCHESTRATOR=1`.
3. **Shared venv** at `/home/dev/projects/ate-ornith/.venv`. Subagents `source` it instead of creating their own.
4. **Test command** that every brief uses verbatim:

   ```
   source /home/dev/projects/ate-ornith/.venv/bin/activate && QT_QPA_PLATFORM=offscreen pytest -q -p no:cacheprovider | tail -3
   ```

   `-p no:cacheprovider` stops worktrees fighting over `.pytest_cache`. `tail` keeps output out of the subagent's context.
5. **Model servers:** coder2 vLLM at `http://192.168.8.186:8000/v1`, Qwen3.5 9B FP8 with a 262,144-token window (`variant` option low/medium/high). Earlier: LM Studio at `http://192.168.8.94:1234` with Ornith loaded at 132,608 tokens. OpenCode auto-compacts at roughly 85% of the window. `collect.py` reads both servers for the window size.
6. Commit `escalate-to-opus.md`, `review-packet.md` and `haiku-review.md` to the integration branch so every new worktree (which starts from the integration tip) has them.

---

## 3. Sizing work: tickets into packets

Big tickets caused 3–5 compactions and 1–2 hour runs. Remaining tickets were split into packets.

- One packet ≈ one behaviour plus its tests, about 2–5 checkboxes, touching 1–3 source files.
- Name them `NNa-short-name.md`, `NNb-…` in `issues/`. Each packet has `Blocked by:` lines (other packets or tickets) and its own checkboxes, copied or narrowed from the parent's acceptance criteria.
- The parent gets `**Status:** split` and `Split into: NNa, NNb, …`. `frontier.py` treats a split parent as resolved once all its packets are.
- When review finds a gap that's too big to fix inline, make a follow-up packet (for example `10a-step-input-widgets.md`, `23d-history-screen-fixes.md`) instead of reopening the merged one.

`python3 .scratch/ate-v1/frontier.py` prints:

```
28 done, 44 remaining (of 72 work items); 14 unblocked: 06c, 06d, 07a, …
```

Pick the next packet by critical path, not by number: a packet that unblocks a chain (for example Devices 16b → 16c → 17a → 17b → 18a/18b → 29d) goes before an isolated leaf.

---

## 4. The per-packet loop

```
pick packet ─► implementer ─► ┬─ Qwen reviewer   ─┐
                               ├─ Ornith reviewer ─┼─► triage (wait for all three) ─┬─► rework (Qwen or Opus) ─► reviewers again
                               └─ Haiku reviewer  ─┘                                └─► orchestrator light review ─► merge ─► log + report ─► next
```

### 4.1 Dispatch the implementer

`delegate_task` with:

```json
{
  "title": "Implement packet 16b: TCP read and write",
  "role": "implementation",
  "isolation": "worktree",
  "mode": "async",
  "clientRequestId": "ate-v1-packet-16b-impl-r1",
  "target": {"providerInstanceId": "opencode", "model": "coder2/Qwen3.5-9B-FP8-dynamic",
             "options": [{"id": "variant", "value": "high"}]},
  "task": "<brief, see 5.1>"
}
```

- Title must start with `Implement packet NNx:`. `collect.py` derives role and ticket from it.
- `clientRequestId`: `ate-v1-packet-NNx-impl-rN`. Bump `rN` for a re-dispatch; keep it identical when retrying the same call.
- Keep the returned `taskId`. Don't poll; T3 notifies on completion. Use `task_status` only if asked or if it seems stuck. Check `t3_pending_request_list` if a subagent appears blocked on an approval.

### 4.2 Hand-off to the reviewers (no review yet)

No checking at this point; the reviewers come first and the orchestrator's check is at the end (4.4).

- Note the branch name from the reply or `git worktree list`. T3 may prefix it, for example `t3/packet-16b-tcp-read-write`.
- Remove its worktree, keep the branch: `git worktree remove <path>`. If the worktree has uncommitted changes, commit them as `packet NNx: WIP (orchestrator-saved)` first.
- If it made no commits at all, re-dispatch the implementer instead of sending an empty branch to review.
- Check the main checkout is still on `integration/ate-v1` (`git branch --show-current`).

### 4.3 Dispatch the three reviewers in parallel

All read-only, all following `review-packet.md`, all given the same brief (5.2) and the same branch and head SHA. Send all three in one turn:

- **Qwen:** `delegate_task`, coder2 target with `variant: high`, `isolation: worktree`, title `Review packet NNx (Qwen): …`, `clientRequestId` `ate-v1-packet-NNx-review-qwen-rN`.
- **Ornith:** `delegate_task`, `{"providerInstanceId":"opencode","model":"lmstudio/ornith-1.5-9b"}`, `isolation: worktree`, title `Review packet NNx (Ornith): …`, `clientRequestId` `ate-v1-packet-NNx-review-ornith-rN`.
- **Haiku:** Agent tool, `model: haiku`, `run_in_background: true`, prompt starting `Haiku review of packet NNx` and pointing at `haiku-review.md`.

Titles starting `Review packet` log as role `reviewer` (the model is recorded separately); the Haiku prompt logs as `haiku-reviewer`.

Reviewers check out the branch detached, so no branch is created and nothing conflicts. Remove the two reviewer worktrees once they report.

### 4.3a Triage (only after all three have reported)

Don't act on a partial set. Then:

1. Merge the three lists. Drop duplicates, note which reviewers raised each item, and confirm each must-fix against the code. Reviewers make false alarms too; one model's lone claim needs a look at the line.
2. Decide:
   - **No confirmed must-fix:** forward to the light review (4.4). Notes go to `review-notes.md`.
   - **Rework with Qwen** (default): the fixes are local, well specified and few. At most ~5 items per run, with the evidence rules from section 5.
   - **Rework with Opus** (`delegate_task`, `{"providerInstanceId":"claudeAgent","model":"claude-opus-5-5"}`, `isolation: worktree`): Qwen is unlikely to do well. For example, threads, sockets, Qt lifetimes or subprocess timing; a design problem rather than a slip; more than ~5 interlocking items; or Qwen already failed this packet's rework once.
   - **Trivial (a line or two):** fix it myself during the light review.
3. Rework title `Fix packet NNx: <summary>`. It branches from the current packet branch (`git checkout -b packet-NNx-fixN <branch>`) and must add a failing test before each code fix.
4. After rework, review again with all three reviewers on the new head, including the previous findings in the brief so they check the fixes. After two rework rounds, the next rework goes to Opus.

### 4.4 Orchestrator light review (last, on the final packet branch)

The final gate after triage has nothing left to rework. Kept light to save orchestrator usage, but not skipped: every role has made false claims.

1. Read the implementer's reply and the last round of three reviews. Treat every claim as unverified.
2. `git log --oneline integration/ate-v1..<branch>` and `git diff --stat`: the work is there, packet file ticked and resolved.
3. Run the full suite **3 times** from a worktree of the branch; check the total time hasn't jumped.
4. Diff the collected tests against the integration branch (`pytest --collect-only -q`) to catch silently deleted tests.
5. Fix any trivial leftovers myself; anything bigger goes back through triage (4.3a).
6. Spot-check one or two risky behaviours only where the reviewers' replies leave doubt. Remaining gaps go in `review-notes.md` or a follow-up packet.

### 4.5 Merge

```
ATE_ORCHESTRATOR=1 git merge --no-ff <final packet branch> -m "Merge packet NNx: <one-line summary>"
```

Run the suite once more on the integration branch. Mark the packet resolved if nobody did. Make sure no packet worktrees are left. Delete branches only at the end.

### 4.6 Log and report

```
python3 .scratch/ate-v1/context-usage/collect.py            # upsert usage.jsonl
python3 .scratch/ate-v1/context-usage/collect.py --report   # markdown table
python3 .scratch/ate-v1/frontier.py
```

Add a `notes` entry in `usage.jsonl` for any problem run (false claims, flaky tests, orchestrator fixes needed).

Report to the user after every implementer or rework run and after each review round (one combined report for all three reviewers):

- steps, wall time, peak context (tokens and % of window), number of auto-compactions;
- tokens: input / output / reasoning (and whether an Opus helper ran, with its tokens);
- what was fixed at review and verification;
- **one progress sentence**: "X done, Y remaining (of N); K unblocked: …".

### 4.7 Next

Dispatch the next packet's implementer only after the merge. Implementers never run in parallel with each other or with rework on the same machine.

---

## 5. Brief templates

Rules that made briefs work for a 9B local model:

- Point at files and **exact line ranges**, never "read the spec". Whole-file reads of big files fill the window. Packets briefed with line ranges ran with 0–1 compactions; packets without ran 3–5.
- Name existing helpers to reuse (with approximate line numbers), so it doesn't reinvent them.
- State the out-of-scope neighbours explicitly ("`onError` is 16c").
- Tell it to `grep -n` then read ranges, and to pipe test output through `tail`.
- Require test-first and commits after each green step.
- Require a compact final reply with checkable facts (SHA, exact pytest lines).
- At most ~5 items per run; split longer fix lists across runs.
- The worker keeps `/tmp/<packet>-checklist.md`, one line per item, updated after each commit with the SHA and covering test name.
- The final reply pastes command output as evidence: `git diff --stat integration/ate-v1...HEAD`, `grep -c '^def test_' <test file>` (before and after), `grep -n '\- \[' <packet file>`, the pytest lines. Items without evidence are reported NOT DONE. (Why: `context-usage/qwen-false-done-diagnosis.md`.)

### 5.1 Implementer brief

```
You are implementing ONE small work packet of the ATE v1 spec, in your own git worktree (your current directory).

Packet: `.scratch/ate-v1/issues/<NNx-file>.md` — its checkboxes are your whole scope. <Out-of-scope neighbours: "X is NNy">. Read `.scratch/ate-v1/escalate-to-opus.md` NOW, before starting: its mandatory handoff rule (two compactions or ~150 steps) applies to you.

Spec — `<spec file>`, read ONLY: <line ranges and what they cover>. Don't read `.scratch/ate-v1/spec.md`.

Existing code (read in line ranges after `grep -n`; never re-read whole files):
- `<path>`: `<symbol>` ~<line> (<what to reuse / extend>)
- …

Design:
- <decisions the spec leaves open, edge cases, exact result shapes, boundaries such as bytes vs str>

Tests (<seam / fixtures to use, e.g. LoopbackPeer and _drive from tests/_fixtures.py>): <list of behaviours each needing a test, including failure paths>.

Keep context lean: `grep -n` then read line ranges; pipe test output through `tail -15`.

Steps:
1. `git checkout -b packet-<NNx>-<slug>`; confirm `git merge-base --is-ancestor integration/ate-v1 HEAD` succeeds.
2. `source /home/dev/projects/ate-ornith/.venv/bin/activate`; `QT_QPA_PLATFORM=offscreen pytest -q -p no:cacheprovider | tail -3` (<N> passed now). Keep it green.
3. Test-first; commit after every green step, prefix `packet <NNx>: `.
4. If your context gets compacted, re-read the packet file and `git log --oneline -8` before continuing — and check the mandatory handoff rule.
5. When done: tick the checkboxes, set `**Status:** resolved`, commit (packet file included). Delete any scratch/debug files. Run the full suite 3 times before your final reply.
6. Only commit to your own branch. Never touch integration/ate-v1 or /home/dev/projects/ate-ornith; don't create worktrees; don't rebase. The orchestrator merges.
7. If something is ambiguous, choose the reading closest to the spec and say so in your reply; don't ask.

Final reply (short): branch, head SHA, exact pytest summary lines from your 3 full runs, done/not-done per checkbox, and whether you used an Opus helper (and how many compactions you had).
```

For UI packets add: the mockup to follow (`git show prototype/run-screen:prototypes/run-screen/run-screen-prototype.html`, variant C staged layout, variant B three-pane, plus `run-screen.md`), the specific sections of it that the packet covers, and that a screenshot check will follow.

### 5.2 Reviewer brief (same text for Qwen, Ornith and Haiku)

```
You are reviewing ONE finished work packet of the ATE v1 spec. READ-ONLY: report findings, change nothing.
FIRST (Qwen/Ornith), run as ONE shell command (parallel tool calls race): `pwd && git checkout --detach <branch> && git rev-parse --short HEAD` — must print <SHA>, else stop and reply with that output; then the suite with the shared venv. (A Qwen reviewer once skipped the checkout and reviewed the integration tip.)
Follow `.scratch/ate-v1/review-packet.md` exactly (Haiku: `.scratch/ate-v1/haiku-review.md`) — setup, checklist and reply format.

Packet: `.scratch/ate-v1/issues/<NNx-file>.md`. Branch: `<branch>` (head <SHA>). Baseline <N> tests on integration.
Spec ranges: <file> lines <ranges>. Don't read `.scratch/ate-v1/spec.md`.
Look hard at: <risky areas: e.g. raw bytes at the lupa boundary, timeouts treated as success, socket cleanup>.
<Re-review rounds: previous must-fix findings and what the rework says it did, one line each — check each fix and its test.>

Final reply: the `git rev-parse --short HEAD` output; findings most severe first, each with path:line, what, how you know, must-fix/note; then the pytest line you saw and `MUST-FIX: <n>`.
```

### 5.3 Haiku screenshot job

Agent tool, `model: haiku`:

```
Take screenshots of the ATE app as implemented on integration/ate-v1, to compare with the mockup.
- Work in a throwaway worktree: `git worktree add /tmp/ate-shots integration/ate-v1` (only this one; remove it when done).
- Use the shared venv. Render with QT_QPA_PLATFORM=offscreen and QWidget.grab().save(...) for each screen: <list screens and states, e.g. operator setup, run in progress, blocked step, results, history>.
- Generate an HTML report and its PDF from a headless run of the example definitions and screenshot those too.
- Save PNGs to /tmp/ate-shots/out/ and reply with the paths and one line per screen on anything that looks broken or missing.
- Don't modify tracked files; don't commit.
```

The orchestrator then views the PNGs, compares with the mockup, and records gaps in `review-notes.md`.

### 5.4 Opus helper (written by the worker subagent, not the orchestrator)

See `escalate-to-opus.md`. In short: commit WIP, write `/tmp/handoff-<NNx>.md` with the handoff skill (packet, branch, worktree path, what's done, exact failing command and output, what's wanted back, and the verbatim "Rules for you" block), then:

```
nohup claude -p --model claude-opus-5-5 --permission-mode bypassPermissions --output-format text \
  "$(cat /tmp/handoff-<NNx>.md)" > /tmp/opus-<NNx>.out 2>&1 &
echo $! > /tmp/opus-<NNx>.pid
sleep 120; if kill -0 $(cat /tmp/opus-<NNx>.pid) 2>/dev/null; then echo running; else tail -40 /tmp/opus-<NNx>.out; fi
```

Mandatory after two compactions or about 150 steps; recommended after two failed fix attempts, 30 minutes or 60 steps without progress, or before a long debugging hunt. At most two helpers per packet. The worker subagent verifies the helper's work (log, diff, full suite) before continuing.

### 5.5 Final review fixer

After every packet is merged: run the `code-review` skill over `main...integration/ate-v1`, fold in `review-notes.md`, then dispatch a single implementer (title `Fix review findings …`) with the combined findings list, ordered by severity, each with file:line.

---

## 6. Context, compaction and token tracking

`collect.py` reads:

- OpenCode's SQLite (`~/.local/share/opencode/opencode.db`, tables `session_v2`, `session_message`). Each assistant step's `tokens.input` is context occupancy; `compaction` rows mark auto-compactions (recorded with the context just before).
- Claude Code transcripts in `~/.claude/projects/-home-dev-projects-ate-ornith/` (orchestrator) and `~/.claude/projects/*-t3-worktrees-ate-ornith-*` (Opus helpers, recognised by "Rules for you" in the prompt, ticket from `handoff-NNx`).

`classify()` reads the session title: `Implement packet NNx` → implementer, `Review packet NNx` → reviewer, `Fix …` → implementer, `Merge …` → merger, other ticket mentions → explorer, else orchestrator. **Titles matter**; keep them in that form.

Each record: ticket, role, model, steps, wall time, peak context and %, compactions, tokens (input, output, reasoning, cache read/write), outcome, notes. `--report` prints the markdown table used for the final per-ticket report, which includes an orchestrator line.

Reading the numbers:

- Peak near 85% with several compactions → packet too big or brief too vague; split it or add line ranges.
- Many steps with low peak → the subagent is looping on a failing test; that's what the mandatory handoff is for.
- Input tokens dwarf output on a local model because every step re-sends the context; with a warm cache that's cheap, with concurrent sessions it isn't.

---

## 7. Safety and process rules (put the relevant ones in every brief)

- Never push. Never touch `main`.
- Only the orchestrator moves the integration branch, with `ATE_ORCHESTRATOR=1`.
- Subagents: their own worktree and branch only; never `/home/dev/projects/ate-ornith`; no new worktrees; no rebase; no merges except the reviewer's `--ff-only`.
- Never run real `groupadd`, `chown`, `icacls` or other system-changing commands; tests mock them.
- Commit footer on orchestrator commits: `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.
- Ambiguity: subagents pick the reading closest to the spec and say so; they don't stop to ask.

---

## 8. Failure modes seen, and the counter-measure

| What happened | Counter-measure |
|---|---|
| Implementers merged themselves into the integration branch | `reference-transaction` hook |
| Ticked boxes, wrong test counts, "fixed" flakes that still failed | Exact pytest lines required; orchestrator re-runs 2–3×; reviewer pass |
| Tests that could never fail (a disconnect helper that returned success after timing out) | Reviewer checklist item; orchestrator reads test helpers, not only tests |
| A subagent blamed "environmental pollution" for its own flaky daemon-thread design | 3× runs; rewrite rather than retry; Opus handoff before long debugging |
| Tests silently deleted by a bad edit | Diff collected test IDs against the integration branch |
| State leaking between tests (live QThreads, deleted Qt children) | Autouse conftest fixtures that stop engines and tolerate deleted objects |
| Behaviour subtly wrong but tested to the wrong spec (Continue disabling later pauses, abort flag carrying over, LIKE escaping doubled quotes, invalid icacls arguments) | Spot-check risky behaviour against the spec lines during verification |
| 3–5 compactions, 1–2 hour runs, no help requested | Smaller packets, line-range briefs, mandatory Opus handoff |
| Concurrent subagents killed LM Studio prefill cache | One subagent at a time |
| UI built from the spec text drifted far from the agreed mockup | UI briefs cite the mockup sections; Haiku screenshot pass; staged-layout rework queued for final review |

---

## 9. Finishing

1. All packets merged and `frontier.py` reports 0 remaining.
2. `code-review` skill on the integration branch; merge with `review-notes.md`; single fixer implementer; verify and merge as usual.
3. Final Haiku screenshot pass against the mockup.
4. Clean up: `git worktree list`, remove every subagent worktree (`git worktree remove`, then `git worktree prune`), delete merged packet and review branches.
5. Final report from `collect.py --report`: per ticket (implementer, reviewer, helper rows), plus the orchestrator line, with context, compactions and tokens.
6. Report the integration branch to the user. Merging to `main` or pushing is their call.
