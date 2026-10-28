# Model evaluation — ATE v1 build

Generated 2026-10-29 08:24 by `model_eval.py`, after 57 merged packets/tickets. Regenerated every 5 merged packets.

## Summary

Only Claude costs money; the local models are free but slow and less reliable. Ranked by implementation quality so far (17a needed an Opus rework for its lock races; the rework was clean in one pass): **Strata ≈ saluki ≥ Qwen-long > Ornith > Qwen 9B**, with **Opus** as the reliable rework fallback (Strata is about 3× faster than saluki; 19's VISA I/O needed Opus). As reviewers, **Strata and Haiku** together: Haiku reads carefully against spec and mockup; Strata's long runtime probes (repeat runs, load, hang probes, wheel builds) found bugs Haiku missed (06d broken wheel, 19 blocking write, 18b/31b flakes). The user kept both on 2026-10-27. Qwen-long and saluki were good reviewers; Ornith was mixed; Qwen 9B was poor. The largest paid cost is the Opus orchestrator itself, not the subagents.

## Assessment by model

### Ornith 1.5 9B (LM Studio, 132k window) — free
- **As implementer** (tickets 02–16a, plus 16b's first two runs): got early tickets done but slowly. 3–5 compactions per packet were common, with many 1–2 hour runs. It repeatedly ticked boxes without the work, misreported test counts, wrote tests that couldn't fail, and once blamed its own flaky design on "environmental pollution". It never used the mandatory Opus handoff.
- **As reviewer** (16b onward): often thorough, with break tests, pixel sampling on 12d and abort probes on 16c/13b. But it was sometimes confidently wrong: it cleared 16b's read bugs and called 16c's wrong error line "exact".

### Qwen3.5 9B FP8 (vLLM on coder2, 262k window) — free
- **As implementer** (16b, 16c): the weakest. It reasoned only 30–75 tokens per agent step and dropped items from lists after failed edits. It wrote final reports from the brief instead of checking (claimed "all 13 fixed" with no new tests added). Both packets needed Opus rework. See `qwen-false-done-diagnosis.md`.
- **As reviewer**: poor. It mostly restated the implementer's claims, reviewed the wrong code once, and once ran git in the main checkout and reversed an earlier fix.

### Qwen-long 3.8 27B Q4_K_M (llama.cpp, 64k window) — free
- **As implementer** (12c, 12d and their reworks): a big step up in honesty and care. Its claims matched its pasted evidence. It proved tests fail by breaking the code, and found a two-safeguard subtlety on its own. 12c and 12d merged with no Opus rework.
- **Limit**: the 64k window. There were 3–11 compactions per run at ~48k, and one run died when a compaction summary overflowed the 16k output limit.
- **As reviewer**: good. It used break tests and probes. It missed 12d's grey bar because it read the stylesheet instead of rendering it.

### saluki 27B IQ2_XS (llama.cpp, 180k window) — free
- **As implementer** (13b, 17a, 07a, 11b, 17b, 24a, 24b): strong. 17b needed two Opus reworks (write-timeout semantics, warning timing and log ordering — design questions); 24a and 24b needed only small fixes. Earlier: 07a was a clean semver rework (19 tests, 0 compactions); Haiku found one pre-existing pre-release-ordering bug it exposed. 11b looked complete but had 4 must-fixes that only a real click and a second run would show (rows disabled by a wrong Qt flag, boxes never unlocked after a run, stale overrides): it tested through `setCheckState`, not the UI. Earlier: 13b was clean, with 0 compactions and two small orchestrator fixes. 17a was a hard threading packet: complete, 17 tests and 1 compaction at a 152k peak, with sensible design choices explained. Reviewers then found real lock races (overflow state read outside the lock, timeout path, exact-fit overflow), which is the hard part. Its ceiling on concurrency is still open.
- **Config note**: its chat template rejects `reasoning_effort: high`. Run it at the default (`xhigh`).
- **As reviewer**: good. It reproduced 17a's overflow race with a deterministic probe, plus close/write races, and in round 2 re-ran those probes against the fix (29,936/29,936 bytes accounted; 50/50 clean closes).
- **On 2×128k slots** (from 2026-10-25): 25.2 tok/s alone, 22.4 tok/s each with two streams (+78% aggregate); 11b peaked at 107.5k of 131k with 0 compactions.

### Qwen3.8 Flash Next Q2 (Strata, llama.cpp on coder2, 128k window, high reasoning) — free
- **Setup** (2026-10-26): replaced saluki. ~77 tok/s on a short benchmark (≈3× saluki), 16/16 clean tool calls, API key required, 4k output limit.
- **As implementer** (24b rework): finished the rework from a saved WIP — all 4 items, each with a test shown failing first; it caught a bug in the orchestrator-saved WIP (wrong stem level); 0 compactions; 446 passed ×3. Honest, evidence-backed reply.
- **As implementer** (18a, 18b): both complete with mutation-proven tests (18a: shared stream base + SERIAL; 18b: UDP datagram queue), ~35–45 min each. But it **misreports its own step counts**: 18a claimed "first compaction, well under 150 steps" (actual 161 steps, 2 compactions); 18b claimed "~60 steps" (actual 158, 1 compaction), so it never triggers the Opus-handoff rule. 18a also left a box unticked while marking the packet resolved. Trust collect.py, not its self-report.
- **On the tuned 262k config** (16d, 2026-10-26: n_ctx 262,144, compaction buffer 32,768): 21 min, 87 steps, 0 compactions at a 106k peak; ~43 output tok/s, the same as before the tune. Clean, small Lua module; tests shown failing first; its self-report (~90 steps, 0 compactions) was accurate this time.
- **31b** (2026-10-27, 262k config): found a real root cause by instrumenting (pyserial's `socket://` open resets the input buffer) and fixed the tests correctly, but **ended its turn with a background verification run still going and the packet unticked**; 60 min, 130 steps, 0 compactions at a 170k peak, which is beyond the old 131k window, so the bigger context paid off. As reviewer it was excellent: probes, before/after controls, 30/30 and 10/10 runs, and a precise product note.
- **19, 06c, 07b, 07c, 07d** (2026-10-27, 262k config): 19 (VISA) needed an Opus rework for pyvisa-py I/O semantics (overrunning reads, write with no timeout), the second time network I/O design went beyond it. The catalog/record packets were fast and clean: 07b 16 min/64 steps, 07c ~19 min/41 steps, 07d ~13 min/~50 steps, all 0 compactions, each finished with only small light-review fixes. Self-reports were accurate from 16d on, and it now volunteers weak spots unprompted (07c: 3 tests pin pre-existing behaviour; 07d: its ORDER BY test can't fail on SQLite).
- **As reviewer** (06c-07d): consistently the deeper of the two reviewers. It found must-fixes Haiku missed in 07b (an assertion that couldn't fail) and 07c (broken installed dependency reported as "unresolved"; the Run screen never shows broken definitions at all, a ticket-05 gap now packet 05b), and answered brief questions from the code (07d: only SQLite is accepted, so the ORDER BY question is moot). Reviews take 12-55 min against Haiku's ~2-4 min.
- **15a, 15b, 17c, 15e** (2026-10-27/28): 15a 23 min/133 steps (said ~50) and 17c 30 min/149 steps were clean, with only test-gap must-fixes. 15e (action ordering) was correct apart from one bug inherited from 14b/14c. 15b (sub-schedule expansion, the largest packet yet: 31 min/151 steps, 0 compactions, 174k peak) got the structure right but missed per-instance Lua isolation and anchored include resolution wrongly (the second partly from my brief), which needed an Opus rework. Opus's rework then left a `debug`-library escape that Haiku's round 2 found. The step-count handoff was dropped (2026-10-27); Strata's self-counts stay 1.2-2.7× low.
- **Opus as implementer** (15c, a one-off at user request, effort medium): clean design; 8 mutations each failed tests; one must-fix (RecursionError at about 1000 include levels). It paused once at the rate limit and resumed.
- **Concurrency** (2026-10-26): two agents on its single slot ran ~12.2 s/turn each vs 6.0 s solo (≈49%): the slot time-shares with no aggregate gain, so it runs one session at a time.
- **As reviewer** (24b round 2): good — verified each fix with probes and mutation tests, and found a real coverage gap (filter-vs-typed-extension precedence untested; flipping it still passed all tests).

### Claude Opus — paid
- **Orchestrator**: by far the largest Claude cost, mostly cache reads. It catches most false claims and makes small fixes directly.
- **Rework** (16b, 16c): excellent. It fixed in one pass what 2–4 local runs couldn't, proved its tests fail first, and found real bugs the reviewers had only suspected.

### Claude Haiku — paid
- **Reviewer**: the most valuable per token. It found the real must-fixes in nearly every round: 16b's read bugs, 16c's wrong error line, 12d's grey bar (via a screenshot), and 13b/17a's sleep and lock issues. It produced occasional false alarms.
- **Screenshots**: rendering the UI caught what code-reading reviewers missed.

## Runtime and iteration speed (summary)

- **Per step**, the Qwen 9B on vLLM was fastest (~6 s/step) but did the least per step. Ornith ran ~21 s/step as implementer and ~74 s/step as reviewer, because reviews ran full suites and break tests while the same LM Studio box served other work. Qwen-long and saluki both run ~30 s/step on coder2's llama.cpp. saluki's raw request latency is about twice Qwen-long's (~13 s vs ~6 s per short reply), but it needs far fewer compactions (0.4 vs 5.6 per 100 steps), so whole runs take similar time.
- **Per packet (start → merge, review-flow era)**: 16b 8.4 h (four local runs, Opus rework, three review rounds); 12c 4.1 h and 12d 2.8 h (Qwen-long, one local rework each, two rounds); 16c 1.5 h (Opus rework); **13b 1.0 h (saluki, one round, two orchestrator fixes)**. Earlier Ornith packets merged in 0.1–2.3 h, but without independent review, and many needed orchestrator fixes not visible in the timing.
- **Claude subagents are fast**: a Haiku review takes ~2 min and an Opus rework ~7 min. They cost money, though, while the local runs take 15 min to over an hour.

## Recommendation (current)

- Implementer: Strata (from 2026-10-26; saluki before), with Opus rework for concurrency/design issues or after two local failures.
- Reviewers: Haiku + Strata on every packet (user, 2026-10-27: "slower but is finding things"). Ornith was dropped 2026-10-25 (it rarely found anything Haiku missed, was slowest, and was often confidently wrong).
- Watch: Strata's self-reported step counts were unreliable in 18a/18b (about 2× low), better from 06d on; it once ended a turn with a background run still going (31b).
- Cost: trim orchestrator verbosity and repeated checks, since that's where most Claude tokens go.

## Runtime and throughput by model and role

Wall time is session start to finish. *Sec / step* is wall time per agent step (one model reply plus its tool calls). *Output tok / s* is output tokens over wall time, so it includes tool and test time. Local models are free; 💲 marks paid Claude models. Claude rows have no fixed window, so peaks are raw tokens.

| Model | Role | Runs | Packets | Total time | Median run | Sec / step | Output tok / s | Compactions / 100 steps | Max peak context |
|---|---|---|---|---|---|---|---|---|---|
| Claude Haiku 💲 | haiku-reviewer | 21 | 12 | 50 min | 2 min | 10 | 150 | 0.0 | 102,221 |
| Claude Haiku 💲 | side-job | 29 | 0 | 1.9 h | 3 min | 11 | 109 | 0.0 | 156,762 |
| Claude Opus 💲 | opus-helper | 1 | 1 | 4 min | 4 min | 11 | 63 | 0.0 | 64,758 |
| Claude Opus 💲 | opus-implementer | 7 | 5 | 1.5 h | 8 min | 22 | 46 | 0.0 | 178,993 |
| Ornith 1.5 9B (LM Studio) | explorer | 1 | 1 | 2 min | 2 min | 15 | 31 | 0.0 | 50,451 |
| Ornith 1.5 9B (LM Studio) | implementer | 35 | 32 | 42.9 h | 49 min | 21 | 11 | 1.2 | 113,250 |
| Ornith 1.5 9B (LM Studio) | merger | 3 | 3 | 4 min | 1 min | 5 | 27 | 0.0 | 36,187 |
| Ornith 1.5 9B (LM Studio) | reviewer | 10 | 6 | 7.4 h | 29 min | 74 | 3 | 0.6 | 112,086 |
| Qwen-long 3.8 27B Q4 (llama.cpp) | implementer | 4 | 2 | 3.5 h | 40 min | 33 | 22 | 5.6 | 49,765 |
| Qwen-long 3.8 27B Q4 (llama.cpp) | reviewer | 4 | 2 | 51 min | 12 min | 33 | 23 | 4.3 | 48,206 |
| Qwen3.5 9B FP8 (vLLM) | implementer | 5 | 2 | 1.0 h | 14 min | 6 | 49 | 0.0 | 135,075 |
| Qwen3.5 9B FP8 (vLLM) | reviewer | 7 | 2 | 27 min | 2 min | 5 | 44 | 0.0 | 109,577 |
| Qwen3.8 Flash Next Q2 (Strata) | explorer | 2 | 2 | 59 min | 30 min | 14 | 45 | 0.0 | 165,567 |
| Qwen3.8 Flash Next Q2 (Strata) | implementer | 24 | 22 | 11.2 h | 26 min | 15 | 38 | 0.2 | 205,131 |
| Qwen3.8 Flash Next Q2 (Strata) | reviewer | 25 | 23 | 13.9 h | 21 min | 20 | 42 | 0.3 | 211,278 |
| saluki 27B IQ2_XS (llama.cpp) | implementer | 9 | 7 | 14.9 h | 87 min | 58 | 13 | 0.8 | 152,355 |
| saluki 27B IQ2_XS (llama.cpp) | reviewer | 11 | 7 | 5.9 h | 30 min | 38 | 17 | 0.0 | 101,215 |

## Iteration speed by packet

*Start → merge* runs from the first implementer session to the merge commit on `integration/ate-v1`, including reviews, rework and orchestrator time. Review rounds are counted by Haiku reviews (one per round). Earlier tickets ran before the review flow existed, so their cycle times aren't comparable.

| Packet | Implementer model(s) | Impl runs | Rework by | Review rounds | Start → merge | Merged |
|---|---|---|---|---|---|---|
| 03 | Ornith 1.5 9B | 1 | — | 0 | 2.1 h | yes |
| 08 | Ornith 1.5 9B | 1 | — | 0 | 2.3 h | yes |
| 09 | Ornith 1.5 9B | 3 | — | 0 | 5.0 h | yes |
| 10 | Ornith 1.5 9B | 1 | — | 0 | 4.3 h | yes |
| 13a | Ornith 1.5 9B | 1 | — | 0 | 0.8 h | yes |
| 14a | Ornith 1.5 9B | 1 | — | 0 | 0.3 h | yes |
| 14b | Ornith 1.5 9B | 1 | — | 0 | 0.1 h | yes |
| 12a | Ornith 1.5 9B | 1 | — | 0 | 0.2 h | yes |
| 12b | Ornith 1.5 9B | 1 | — | 0 | 0.4 h | yes |
| 13c | Ornith 1.5 9B | 1 | — | 0 | 1.0 h | yes |
| 14c | Ornith 1.5 9B | 1 | — | 0 | 0.5 h | yes |
| 06a | Ornith 1.5 9B | 1 | — | 0 | 0.3 h | yes |
| 11a | Ornith 1.5 9B | 1 | — | 0 | 0.2 h | yes |
| 06b | Ornith 1.5 9B | 1 | — | 0 | 0.3 h | yes |
| 21a | Ornith 1.5 9B | 1 | — | 0 | 0.5 h | yes |
| 22a | Ornith 1.5 9B | 1 | — | 0 | 1.4 h | yes |
| 22b | Ornith 1.5 9B | 1 | — | 0 | 1.8 h | yes |
| 23a | Ornith 1.5 9B | 1 | — | 0 | 0.8 h | yes |
| 23b | Ornith 1.5 9B | 1 | — | 0 | 1.6 h | yes |
| 23c | Ornith 1.5 9B | 1 | — | 0 | 0.2 h | yes |
| 23d | Ornith 1.5 9B | 1 | — | 0 | 0.5 h | yes |
| 26a | Ornith 1.5 9B | 1 | — | 0 | 0.1 h | yes |
| 26b | Ornith 1.5 9B | 1 | — | 0 | 0.7 h | yes |
| 16a | Ornith 1.5 9B | 1 | — | 0 | 2.0 h | yes |
| 16b | Ornith 1.5 9B, Qwen3.5 9B FP8 | 4 | Opus, 2× local | 3 | 8.4 h | yes |
| 16c | Qwen3.5 9B FP8 | 1 | Opus | 1 | 1.5 h | yes |
| 12c | Qwen-long 3.8 27B Q4 | 1 | 1× local | 2 | 4.1 h | yes |
| 12d | Qwen-long 3.8 27B Q4 | 1 | 1× local | 2 | 2.8 h | yes |
| 13b | saluki 27B IQ2_XS | 1 | — | 1 | 1.0 h | yes |
| 17a | saluki 27B IQ2_XS | 1 | Opus | 2 | 5.7 h | yes |
| 07a | saluki 27B IQ2_XS | 1 | — | 1 | 3.0 h | yes |
| 11b | saluki 27B IQ2_XS | 1 | 1× local | 2 | 4.8 h | yes |
| 24a | saluki 27B IQ2_XS | 1 | — | 1 | 4.8 h | yes |
| 17b | saluki 27B IQ2_XS | 1 | Opus | 3 | 8.7 h | yes |
| 24b | Qwen3.8 Flash Next Q2, saluki 27B IQ2_XS | 1 | 2× local | 2 | 2.8 h | yes |
| 31a | Qwen3.8 Flash Next Q2 | 1 | — | 1 | 4.5 h | yes |
| 18a | Qwen3.8 Flash Next Q2 | 1 | — | 0 | 3.3 h | yes |
| 18b | Qwen3.8 Flash Next Q2 | 1 | 1× local | 0 | 2.0 h | yes |
| 06d | Qwen3.8 Flash Next Q2 | 1 | — | 0 | 2.5 h | yes |
| 16d | Qwen3.8 Flash Next Q2 | 1 | — | 0 | 1.8 h | yes |
| 31b | Qwen3.8 Flash Next Q2 | 1 | — | 0 | 2.3 h | yes |
| 19 | Qwen3.8 Flash Next Q2 | 1 | — | 0 | 2.5 h | yes |
| 06c | Qwen3.8 Flash Next Q2 | 1 | — | 0 | 1.4 h | yes |
| 07b | Qwen3.8 Flash Next Q2 | 1 | — | 0 | 0.5 h | yes |
| 07c | Qwen3.8 Flash Next Q2 | 1 | — | 0 | 0.7 h | yes |
| 07d | Qwen3.8 Flash Next Q2 | 1 | — | 0 | 0.6 h | yes |
| 15a | Qwen3.8 Flash Next Q2 | 1 | — | 0 | 0.8 h | yes |
| 15b | Qwen3.8 Flash Next Q2 | 1 | Opus | 0 | 1.4 h | yes |
| 17c | Qwen3.8 Flash Next Q2 | 1 | — | 0 | 1.5 h | yes |
| 15e | Qwen3.8 Flash Next Q2 | 1 | — | 0 | 0.9 h | yes |
| 15d | Qwen3.8 Flash Next Q2 | 2 | — | 0 | 1.6 h | yes |
| 20 | Qwen3.8 Flash Next Q2 | 1 | — | 0 | 0.9 h | yes |
| 21b | Qwen3.8 Flash Next Q2 | 1 | — | 0 | 1.2 h | yes |
| 21c | Qwen3.8 Flash Next Q2 | 1 | — | 0 | 1.0 h | yes |
| 05b | Qwen3.8 Flash Next Q2 | 1 | — | 0 | 17.6 h | yes |
| 10a | Qwen3.8 Flash Next Q2 | 1 | — | 0 | 9.7 h | yes |
| 01 | Ornith 1.5 9B | 1 | — | 0 | — | yes (no merge commit) |
| 02 | Ornith 1.5 9B | 1 | — | 0 | — | yes (no merge commit) |
| 04 | Ornith 1.5 9B | 1 | — | 0 | — | yes (no merge commit) |
| 05 | Ornith 1.5 9B | 1 | — | 0 | — | yes (no merge commit) |
| 06 | Ornith 1.5 9B | 1 | — | 0 | — | yes (no merge commit) |
| 11 | Ornith 1.5 9B | 1 | — | 0 | — | yes (no merge commit) |
| 26 | Ornith 1.5 9B | 1 | — | 0 | — | yes (no merge commit) |

## Paid usage (Claude)

| Model | Role | Input tokens | of which cache reads | Output tokens |
|---|---|---|---|---|
| Claude Opus | orchestrator | 479.4M | 467.8M | 1140k |
| Claude Haiku | side-job | 36.4M | 34.1M | 748k |
| Claude Opus | opus-implementer | 22.7M | 22.0M | 250k |
| Claude Haiku | haiku-reviewer | 15.8M | 14.3M | 450k |
| Claude Opus | opus-helper | 1.2M | 1.2M | 16k |
