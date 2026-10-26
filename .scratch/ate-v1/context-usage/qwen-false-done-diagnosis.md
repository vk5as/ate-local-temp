# Why Qwen workers report "done" when they aren't (2026-10-24)

Evidence from the OpenCode session "Fix packet 16b: Haiku review findings" (Qwen3.5 9B FP8, coder2, variant high, 179 steps). It reported all 13 items fixed; the test file still had 11 tests and the packet was unticked.

## Findings

1. **The model barely thinks in agent turns.** With tools offered, Qwen3.5 9B reasons about 30–75 tokens a step (median 32, max 75 in the session). The same server reasons 2,000–3,000 tokens on the same kind of question without tools, so vLLM's reasoning parser is working. A direct request with tools gave 75 reasoning tokens by default and 151 with `reasoning_effort: "high"`. The session never exceeded 75. (Checked later with a logging proxy: OpenCode *does* send `reasoning_effort: "high"` for `#high`, even before explicit variants were configured. The short thinking is the model's own agentic behaviour, not a config fault.) For comparison, Ornith had a median of 167 reasoning tokens a step, with peaks of 32k.
2. **It never tried half the list.** Of 42 edit calls, 0 added a new test function. The six "TEST" items were never attempted, not attempted-and-failed.
3. **Edit failures pulled it off the plan.** 9 of 42 edits failed (21%): 6 "oldString not found" and 3 "oldString and newString identical". It also made 5 grep calls with invalid arguments. Each failure triggered a re-read and a change of direction ("simplify the test"), and the original item was dropped.
4. **No working checklist.** The core tools (shell, read, edit, grep) were called directly, but Code Mode added an `execute` tool with an ~8.7k-char catalog of browser and OpenCode-management tools, and no todo tool was offered (`todowrite` sits behind `experimental.todo`), so the 13-item list lived only in the brief, which was by then far back in context. It also used `execute` as a Python REPL.
5. **The final report was rewritten from the brief, not checked.** The last four steps were: commit, run the suite three times, get the SHA, write the summary. It saw "304 passed" both before and after (no new tests) and didn't notice. Its ✅ list follows the brief's numbering and wording, describing intended changes.

Not a server fault: vLLM serves reasoning and tool calls correctly. The behaviour comes from a small model with very short agentic thinking, a long multi-item brief, and nothing forcing it to check its claims against evidence. Ornith showed the same false-done pattern earlier, with more thinking but far more context.

## Counter-measures

Brief-side (orchestrator, applied from packet 16b onward):
- At most ~5 items per run; split longer lists over several runs.
- The worker keeps a checklist file outside the repo (`/tmp/<packet>-checklist.md`), one line per item, and updates it after each commit with the SHA and test name.
- The final reply must paste evidence produced by commands, not prose: `git diff --stat integration/ate-v1...HEAD`, `grep -c '^def test_' <test file>` before and after, `grep -n '\- \[' <packet file>`, the pytest lines. Any item without evidence is reported NOT DONE.

Config-side (user's call):
- Define explicit variants for the coder2 model in `~/.config/opencode/opencode.json`, so `high` actually sends `reasoning_effort: "high"` (or `chat_template_kwargs`) to vLLM, and check the session's reasoning tokens afterwards.
- Consider turning off OpenCode Code Mode for these agents, so `todowrite`, `edit` and `grep` are called directly. Fewer argument errors, and a real todo list.

## Config changes applied (2026-10-24, user-approved)

`~/.config/opencode/opencode.json` (backup: `opencode.json.bak-2026-10-24`):
- coder2 model `variants`: `low`/`medium`/`high` → `reasoningEffort`. Same request body as before (verified via proxy); makes the mapping explicit.
- `permissions: [{"action":"execute","resource":"*","effect":"deny"}]`, which should drop the Code Mode `execute` tool and its ~8.7k-char catalog (browser and opencode-management tools) from T3-run sessions. Standalone `opencode run` never loads that catalog, so this has to be confirmed on the next T3-dispatched worker session.
- `experimental.todo: true`: enables the `todowrite` tool (confirmed working in a standalone trial).

## vLLM tool-call parsing (user updated coder2's vLLM config, 2026-10-24)

Re-tested after the change:
- 64 synthetic requests (stream and non-stream, default and `reasoning_effort: high`), with prompts like the commands that broke before (env-var prefixed pytest commands, multi-line edits with backslash escapes, regex greps): 0 malformed names, 0 non-JSON arguments, 0 tool-call markup leaked into content or reasoning.
- Replayed the real "Fix packet 16b: missing read/write tests" conversation up to each of its 5 malformed steps, 3 tries each: 14/15 gave clean tool calls; 1 ended with a plain text reply (`finish=stop`), not malformed markup.
- Replayed the session that ended with its tool call inside the reasoning text (packet 16b r3, step 2), 5 tries: 5/5 gave structured tool calls. Some named tools that only existed in that session's old Code Mode catalog (`execute`, `webfetch`), which the Code Mode removal should stop.

## Code Mode: left on (user's choice, 2026-10-24)

The `permissions` deny-`execute` rule had no effect: T3 gives each OpenCode session its own permission ruleset starting `allow *`, which overrides config rules. The rule was removed from `opencode.json`. Code Mode stays on. `experimental.todo` and the explicit variants remain.
