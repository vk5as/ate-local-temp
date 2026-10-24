# Reviewing a finished packet

For the reviewer subagent. An implementer has finished a packet on its own branch. You review its changes against the packet and the codebase, then fix what you find. The orchestrator merges your branch afterwards, so it must contain the implementer's commits plus your fixes.

## Setup

1. Your worktree starts at `integration/ate-v1`. Bring in the implementer's work with a fast-forward, so nothing is rewritten:

   ```
   git merge --ff-only <implementer branch>
   ```

   If that fails, stop and report it; don't rebase or merge any other way.
2. See the scope: `git log --oneline integration/ate-v1..HEAD` and `git diff --stat integration/ate-v1...HEAD`.
3. Run the suite once to get a baseline:
   `source /home/dev/projects/ate-ornith/.venv/bin/activate && QT_QPA_PLATFORM=offscreen pytest -q -p no:cacheprovider | tail -3`

## Review

Read the packet file, then the diff file by file (`git diff integration/ate-v1...HEAD -- <path>`). Read only the spec line ranges the packet cites, not whole spec files. Look for:

- **Packet criteria:** every ticked box really done and tested; unticked boxes or criteria quietly dropped.
- **Correctness:** wrong behaviour, missed edge cases and error paths, resource leaks (sockets, files, threads, Qt objects), state that carries over when it shouldn't.
- **Tests that can't fail:** assertions that always pass, timeouts treated as success, tests that check a mock rather than the behaviour, deleted or weakened existing tests.
- **Fit:** duplicated helpers that already exist in the codebase, names and idioms that don't match the surrounding code, scope creep beyond the packet.

Write each finding down before fixing anything: file:line, what's wrong, how you know.

## Fix

- Fix real defects and missing tests, with a test that fails before the fix where practical. Commit with the prefix `packet NNx: review: `.
- Don't refactor working code for taste, and don't expand the packet's scope. If something is out of scope or too big, list it as a note instead.
- Run the full suite after your fixes, three times if anything touched threads, timing, sockets or Qt.
- The same help rules apply as for implementers: read `.scratch/ate-v1/escalate-to-opus.md`.

## Rules

- Work only in your worktree and branch. Never check out, merge into or modify `integration/ate-v1`, never run git in `/home/dev/projects/ate-ornith`, never create worktrees, never rebase, never push.
- Never run real `groupadd`, `chown` or `icacls` against the system.

## Final reply

Keep it short:

- findings, each marked **fixed** (with commit SHA) or **noted** (with why it wasn't fixed);
- the exact pytest summary line, and how many runs;
- how many times your context was compacted, and whether an Opus helper was used.
