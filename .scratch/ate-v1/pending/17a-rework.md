# Pending: packet 17a rework (Opus) — written 2026-10-25, not yet dispatched

Dispatch on resume with `delegate_task`: target `{"providerInstanceId":"claudeAgent","model":"claude-opus-5-5"}`, `isolation:"worktree"`, title `Fix packet 17a: receive-buffer locking and limits`, clientRequestId `ate-v1-packet-17a-fix-opus-r1`. Then run the three-reviewer round again.

**Why Opus:** the defects are interlocking lock and teardown races. That's the triage rule's concurrency case, and two reviewers reproduced them independently.

## Triage of round 1 (head 826e07a)

| Reviewer | Must-fix | Confirmed |
|---|---|---|
| Haiku | 5 | overflow state unlocked; exact-fit overflow; read timeout path outside the lock; `_failed` unlocked in flush/write; `receiveBufferBytes` ≤ 0 disables the cap |
| saluki | 1 | overflow state unlocked (deterministic probe: "WARNING LOST") |
| Ornith | 2 | close() sets `_teardown` after closing the socket (stress: a spurious `_failed` 1 in 400); overflow state unlocked |

Ornith's note that `read` "holds the lock for its whole wait so appends block" is wrong: `Condition.wait` releases the lock while waiting (checked on tcp.py ~128-142).

## Brief

You are reworking ONE work packet of the ATE v1 spec (a PyQt6 + lupa test-automation app), in your own git worktree. Three reviewers found locking and teardown races in its background receive buffer.

Rules: work only in this worktree. Never run git in /home/dev/projects/ate-ornith, never check out or modify `integration/ate-v1`, never create worktrees, never rebase, never push. Commit with the prefix `packet 17a: `, ending each message with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.

Start: `git checkout -b packet-17a-opus packet-17a-receive-buffer` (head 826e07a), then `git merge --no-edit integration/ate-v1`. Suite: `source /home/dev/projects/ate-ornith/.venv/bin/activate && QT_QPA_PLATFORM=offscreen pytest -q -p no:cacheprovider | tail -3` (366 passed). Some known flakes exist: a lone Qt-teardown or TCP-timing failure elsewhere means re-run once.

Context: packet `.scratch/ate-v1/issues/17a-receive-buffer.md`. Spec: `.scratch/ate-v1-wayfinding/assets/ate-table-reference.md` lines 19 and 62-91. Code: `src/ate/devices/tcp.py` (`_TcpDevice`: `_reader_loop`, `_append`, `_fail`, `read`, `flush`, `write`, `close`), `src/ate/engine/lua_host.py` `_do_read` (~490-525) and `_do_flush`, and `src/ate/config.py` (`receiveBufferBytes`). Tests are in `tests/test_receive_buffer.py`.

Fix the following. For each item, write a test that fails before the fix where a test can show it deterministically; otherwise say why.
1. **Overflow state is read and cleared outside the lock.** `_do_read` reads and clears `_overflow_pending` and `_overflow_dropped` with no lock, while the reader sets them under `_lock`, so a drop can be lost (WARNING lost, or "dropped 0"). Add a locked `_TcpDevice.take_overflow()` that returns and resets both, and use only that.
2. **Read's timeout path runs outside the lock.** After the `with self._cond:` block, `read` checks and takes `_buffer` unlocked, so a terminator arriving at the deadline can come back partial or as a timeout. Keep the timeout branch inside the lock.
3. **`_failed` is touched outside the lock.** `flush()` and `write()` read it, and `write` sets it, unlocked, and `write` doesn't `notify_all`. Route all `_failed` access through the lock and wake waiting readers.
4. **close() ordering.** Set `_teardown` (and stop) BEFORE closing the socket, so a clean close never records a spurious `_failed`. Also, if the reader join times out, don't silently drop the thread reference. Also, the reader's `except (OSError, ValueError): break` should call `_fail` when not tearing down, so a real error isn't swallowed.
5. **Limits.**
   - An exact-fit chunk (`len(chunk) == limit`, empty buffer) must not set `overflow`, since nothing was dropped.
   - `devices.receiveBufferBytes` ≤ 0 must be rejected at config validation, naming the key. Today 0 silently disables the cap.
   - Add a test for overflow from small chunks into a partly full buffer (the `_append` else-branch).

Also tidy: replace the timing sleeps in `tests/test_receive_buffer.py` (~181, ~247, and `ATE.sleep(0.1)` before `ATE.flush` ~345) with a deadline poll on the buffer under the lock; stop tests from setting `_overflow_pending` directly; and fix the test at ~193 that asserts `_overflow_pending is True` after a direct `dev.read()` (it passes for the wrong reason). Note in your reply: the WARNING is logged on the next read, not at overflow time. Keep that, and say how "one WARNING per overflow" is counted.

Finish: run the FULL suite 3 times.

Final reply (short, command output where possible): branch, head SHA, `git log --oneline packet-17a-receive-buffer..HEAD`, per item what changed and the covering test, the 3 full-suite pytest lines.

## Notes for review-notes.md at merge
- `flush` on a failed device keeps the buffered bytes, while `read` returns them first. Spec line 19 says flush "discards everything"; decide.
- There's no test that `devices.receiveBufferBytes` reaches a device through the headless session (the config → device path).
- `Driver.accepts_receive_buffer_bytes` is a special-case opt-in. The default limit is hard-coded in 4 places.
- `write` select-on-closed-fd ValueError window (pre-existing from 16b).
- The reader polls `select` every 50 ms even when idle.
