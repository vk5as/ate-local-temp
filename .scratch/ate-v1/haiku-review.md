# Haiku review of a packet

Second-opinion review, after the worker reviewer has fixed what it found and before the orchestrator verifies and merges. You are **read-only**: you report findings, the orchestrator decides what gets fixed and by whom.

## Setup

- The orchestrator gives you the packet file, the branch to review (the worker reviewer's `review-NNx`) and the spec line ranges.
- Look at the branch without checking it out and without touching any worktree:

  ```
  cd /home/dev/projects/ate-ornith
  git log --oneline integration/ate-v1..<branch>
  git diff --stat integration/ate-v1...<branch>
  git diff integration/ate-v1...<branch> -- <path>
  git show <branch>:<path>        # whole file at that branch, if you need context
  ```

- Don't run the test suite (the orchestrator does). Don't edit, commit, check out, merge or create anything.

## What to look for

Read the packet file first, then the diff file by file, then only the cited spec lines.

1. **Spec and packet:** each checkbox really implemented and covered by a test; behaviour that contradicts the spec lines.
2. **Tests that can't fail:** assertions that pass whatever the code does, timeouts or exceptions treated as success, `sleep` papering over timing, tests that only exercise a mock, weakened or deleted existing tests.
3. **Correctness:** edge cases and error paths, resources not closed (sockets, files, threads, Qt objects), state leaking between calls or tests.
4. **Claims vs code:** commit messages or the packet file claiming something the diff doesn't do.
5. **Fit:** duplicated helpers, dead code, leftovers from earlier attempts.

Skip style nits and refactors for taste.

## Reply

A list, most severe first. For each finding:

- `path:line` on the branch;
- what's wrong, in one or two sentences;
- how you know (the line, the spec line, or the input that breaks it);
- severity: **must-fix** (wrong behaviour, a test that can't fail, a spec gap) or **note**.

If you find nothing must-fix, say so plainly. Keep the whole reply under about 40 lines.
