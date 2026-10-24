# Reviewing a finished packet (read-only)

For the reviewer subagents. An implementer has finished a packet on its own branch. Three reviewers look at it at the same time, each on a different model: Qwen (coder2), Ornith (LM Studio) and Haiku. You **report findings only**. You don't fix, commit or push anything, because the others are reviewing the same branch. The orchestrator collects all three reports and decides what gets reworked and by whom.

## Setup

Your current directory is your own worktree. Run `pwd` and stay there. Never `cd` to `/home/dev/projects/ate-ornith`, and never run `git worktree`, `git switch` or `git checkout`, except for this one command:

```
git checkout --detach <implementer branch>
```

Then:

- See the scope: `git log --oneline integration/ate-v1..HEAD` and `git diff --stat integration/ate-v1...HEAD`.
- Run the suite once: `source /home/dev/projects/ate-ornith/.venv/bin/activate && QT_QPA_PLATFORM=offscreen pytest -q -p no:cacheprovider | tail -3`. Add `--durations=5` on the packet's test file if timing looks off.

Haiku has no worktree and runs no tests. It inspects the branch from the main checkout with `git log`, `git diff integration/ate-v1...<branch>` and `git show <branch>:<path>`, and changes nothing.

## Review

Read the packet file, then the diff file by file (`git diff integration/ate-v1...HEAD -- <path>`), then only the spec line ranges in your brief. Look for:

1. **Packet criteria:** each checkbox really implemented and covered by a test; criteria quietly dropped; boxes ticked without the work.
2. **Correctness:** wrong behaviour against the spec lines; missed edge cases and error paths; resources not closed (sockets, files, threads, Qt objects); state carrying over between calls or tests.
3. **Tests that can't fail:** assertions that pass whatever the code does; timeouts or exceptions treated as success; `sleep` papering over timing; tests that only exercise a mock; existing tests weakened or deleted.
4. **Claims vs code:** commit messages or the implementer's reply claiming something the diff doesn't do.
5. **Fit:** helpers that already exist in the codebase being duplicated; dead code; leftovers from earlier attempts; scope creep.

Skip style nits and refactors for taste. You may write a throwaway test or script in `/tmp` to prove a bug, but don't add it to the repo.

## Final reply

A list, most severe first, under about 40 lines. For each finding:

- `path:line`;
- what's wrong, in one or two sentences;
- how you know: the line, the spec line, the input that breaks it, or a command and its output;
- severity: **must-fix** (wrong behaviour, a test that can't fail, a spec gap, a false claim) or **note**.

End with the pytest summary line you saw (Qwen and Ornith only), and the line `MUST-FIX: <n>`. If there are none, say `MUST-FIX: 0` plainly.

## Rules

- Read-only on the repo: no edits, commits, merges, rebases, branches, worktrees or pushes.
- Never run real `groupadd`, `chown` or `icacls` against the system.
