# Getting help from an Opus helper

For implementer subagents working an ATE v1 packet. If you are stuck, hand the problem to a fresh Claude Opus helper of your own. It works in your worktree and reports back to **you**, not the orchestrator. You stay responsible for the packet: verify what it did, then finish.

## When to ask

- The same test or error has survived two honest fix attempts, or
- about 30 minutes or 60 steps have passed without a commit that moves the packet forward, or
- you are about to start a long debugging hunt (threads, Qt lifetimes, subprocesses, timing) that would fill your context.

At most two helpers per packet.

## How

1. Commit what you have on your branch (prefix `packet NNx: WIP`), so the helper starts from a clean tree.
2. Follow the `handoff` skill (`~/.agents/skills/handoff/SKILL.md`) to write `/tmp/handoff-<packet>.md` (for example `/tmp/handoff-23a.md`). Reference files by path rather than pasting them. It must contain:
   - the packet file path, your branch name and your worktree path (`pwd`);
   - what is done and committed;
   - the exact problem: the failing command, the relevant output (trimmed), and what you already tried;
   - what you need back;
   - this section, copied verbatim:

     ```
     ## Rules for you
     - Work only in <worktree path>, on branch <branch>. Commit there with the prefix `packet NNx: `.
     - Never merge into, check out or modify `integration/ate-v1`, never run git in /home/dev/projects/ate-ornith, never create worktrees.
     - Run the test suite with: source /home/dev/projects/ate-ornith/.venv/bin/activate && QT_QPA_PLATFORM=offscreen pytest -q -p no:cacheprovider | tail -3
     - Fix the problem described; don't expand the packet's scope.
     - Finish with a short reply: what was wrong, what you changed, your commit SHAs, and the exact pytest summary line.
     ```

3. Start the helper in the background from your worktree, so your shell doesn't time out:

   ```
   nohup claude -p --model claude-opus-5-5 --permission-mode bypassPermissions --output-format text \
     "$(cat /tmp/handoff-<packet>.md)" > /tmp/opus-<packet>.out 2>&1 &
   echo $! > /tmp/opus-<packet>.pid
   ```

4. Wait for it, keeping your own context small:

   ```
   sleep 120; if kill -0 $(cat /tmp/opus-<packet>.pid) 2>/dev/null; then echo running; else tail -40 /tmp/opus-<packet>.out; fi
   ```

   Repeat until it has finished. Don't print the whole output file.

5. Verify, don't trust: read its reply, check `git log --oneline -6` and `git diff --stat HEAD~<n>`, read the changed lines, and run the full suite yourself (three times if the problem was flaky). If it's wrong, fix it yourself or, if you still have a helper left, hand off again with what you found.
6. Then finish the packet as usual. In your final reply, say that an Opus helper was used, why, and what it changed.
