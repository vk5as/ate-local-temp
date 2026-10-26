"""Build model-evaluation.md: per-model quality, runtime and iteration speed for the ATE v1 build.

The numbers come from usage.jsonl (run collect.py first) and the merge commits on integration/ate-v1.
The written assessments come from model-notes.md, which the orchestrator edits by hand.

    python3 .scratch/ate-v1/context-usage/collect.py
    python3 .scratch/ate-v1/context-usage/model_eval.py
"""
import collections, json, os, re, statistics, subprocess
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, "..", "..", ".."))
USAGE = os.path.join(HERE, "usage.jsonl")
NOTES = os.path.join(HERE, "model-notes.md")
OUT = os.path.join(HERE, "model-evaluation.md")

LABEL = {
    "ornith-1.5-9b": "Ornith 1.5 9B (LM Studio)",
    "Qwen3.5-9B-FP8-dynamic": "Qwen3.5 9B FP8 (vLLM)",
    "qwen3.8-27b": "Qwen-long 3.8 27B Q4 (llama.cpp)",
    "saluki-27b": "saluki 27B IQ2_XS (llama.cpp)",
    "qwen3.8-flash-next-q2_0": "Qwen3.8 Flash Next Q2 (Strata)",
    "claude-opus-5-5": "Claude Opus",
    "claude-haiku": "Claude Haiku",
}
PAID = {"claude-opus-5-5", "claude-haiku"}


def _ts(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00")) if s else None


def merges():
    """packet/ticket id -> merge time, from the integration branch's merge commits."""
    out = subprocess.run(["git", "-C", REPO, "log", "--format=%cI %s", "integration/ate-v1"],
                         capture_output=True, text=True).stdout
    found = {}
    for line in out.splitlines():
        m = re.match(r"(\S+) Merge (?:packet|ticket) (\d+[a-z]?)\b", line)
        if m:
            key = m.group(2).zfill(3 if m.group(2)[-1].isalpha() else 2)
            found.setdefault(key, datetime.fromisoformat(m.group(1)))
    return found


def main():
    rows = [json.loads(l) for l in open(USAGE)]
    work = [r for r in rows if r.get("role") != "orchestrator" and r.get("model")]

    # --- per model and role ---------------------------------------------------------------------
    agg = collections.defaultdict(lambda: collections.defaultdict(list))
    for r in work:
        a = agg[(r["model"], r.get("role"))]
        a["mins"].append(r.get("wall_minutes") or 0)
        a["steps"].append(r.get("steps") or 0)
        a["comp"].append(r.get("compactions") or 0)
        a["peak"].append(r.get("peak_context") or 0)
        t = r.get("tokens_total") or {}
        a["out"].append(t.get("output", 0) or 0)
        a["tickets"].append(r.get("ticket"))

    lines = []
    lines.append("| Model | Role | Runs | Packets | Total time | Median run | Sec / step | Output tok / s | Compactions / 100 steps | Max peak context |")
    lines.append("|---|---|---|---|---|---|---|---|---|---|")
    for (model, role), a in sorted(agg.items(), key=lambda kv: (LABEL.get(kv[0][0], kv[0][0]), str(kv[0][1]))):
        mins, steps = sum(a["mins"]), sum(a["steps"])
        sec_step = (mins * 60 / steps) if steps and mins else None
        tok_s = (sum(a["out"]) / (mins * 60)) if mins else None
        lines.append("| {} | {} | {} | {} | {} | {} | {} | {} | {} | {:,} |".format(
            LABEL.get(model, model) + (" 💲" if model in PAID else ""), role, len(a["mins"]),
            len({t for t in a["tickets"] if t}),
            f"{mins/60:.1f} h" if mins >= 60 else f"{mins:.0f} min",
            f"{statistics.median(a['mins']):.0f} min",
            f"{sec_step:.0f}" if sec_step else "—",
            f"{tok_s:.0f}" if tok_s else "—",
            f"{100*sum(a['comp'])/steps:.1f}" if steps else "—",
            max(a["peak"]) if a["peak"] else 0))
    per_model = "\n".join(lines)

    # --- iteration speed: first implementer start -> merge, per packet ---------------------------
    merged = merges()
    by_ticket = collections.defaultdict(list)
    for r in work:
        if r.get("ticket"):
            by_ticket[r["ticket"]].append(r)
    it = ["| Packet | Implementer model(s) | Impl runs | Rework by | Review rounds | Start → merge | Merged |",
          "|---|---|---|---|---|---|---|"]
    for ticket in sorted(by_ticket, key=lambda t: merged.get(t, datetime.max.replace(tzinfo=timezone.utc))):
        rs = by_ticket[ticket]
        impl = [r for r in rs if r.get("role") == "implementer"]
        if not impl:
            continue
        starts = [_ts(r.get("started")) for r in impl if r.get("started")]
        start = min(starts) if starts else None
        end = merged.get(ticket)
        cycle = (end - start).total_seconds() / 3600 if start and end else None
        models = sorted({LABEL.get(r["model"], r["model"]).split(" (")[0] for r in impl})
        fixes = [r for r in impl if (r.get("title") or "").lower().startswith("fix")]
        opus = [r for r in rs if r.get("role") == "opus-implementer"]
        rework = ", ".join(filter(None, ["Opus" if opus else "", f"{len(fixes)}× local" if fixes else ""])) or "—"
        rounds = len([r for r in rs if r.get("role") == "haiku-reviewer"])
        if end:
            status = "yes"
        else:
            issue = [f for f in os.listdir(os.path.join(REPO, ".scratch", "ate-v1", "issues"))
                     if re.match(rf"{re.escape(ticket.lstrip('0') if ticket[:1] == '0' and len(ticket) == 2 else ticket)}-|{re.escape(ticket)}-", f)]
            text = open(os.path.join(REPO, ".scratch", "ate-v1", "issues", issue[0])).read() if issue else ""
            status = ("yes (no merge commit)" if re.search(r"\*\*Status:\*\* (resolved|split)", text)
                      else "in progress")
        it.append(f"| {ticket} | {', '.join(models)} | {len(impl) - len(fixes)} | {rework} | {rounds} | "
                  f"{f'{cycle:.1f} h' if cycle is not None else '—'} | {status} |")
    iteration = "\n".join(it)

    # --- cost (paid models only) -----------------------------------------------------------------
    cost = collections.defaultdict(lambda: [0, 0, 0])
    for r in rows:
        if r.get("model") in PAID:
            t = r.get("tokens_total") or {}
            c = cost[(r["model"], r.get("role"))]
            c[0] += t.get("input", 0) or 0
            c[1] += t.get("cache_read", 0) or 0
            c[2] += t.get("output", 0) or 0
    cl = ["| Model | Role | Input tokens | of which cache reads | Output tokens |", "|---|---|---|---|---|"]
    for (m, role), (i, cr, o) in sorted(cost.items(), key=lambda kv: -kv[1][0]):
        cl.append(f"| {LABEL.get(m, m)} | {role} | {i/1e6:.1f}M | {cr/1e6:.1f}M | {o/1e3:.0f}k |")

    notes = open(NOTES).read() if os.path.exists(NOTES) else ""
    done = len(merged)
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    with open(OUT, "w") as f:
        f.write(f"# Model evaluation — ATE v1 build\n\nGenerated {now} by `model_eval.py`, after {done} merged packets/tickets. "
                "Regenerated every 5 merged packets.\n\n")
        f.write(notes.strip() + "\n\n")
        f.write("## Runtime and throughput by model and role\n\n"
                "Wall time is session start to finish. *Sec / step* is wall time per agent step (one model reply plus its tool calls). "
                "*Output tok / s* is output tokens over wall time, so it includes tool and test time. "
                "Local models are free; 💲 marks paid Claude models. Claude rows have no fixed window, so peaks are raw tokens.\n\n")
        f.write(per_model + "\n\n")
        f.write("## Iteration speed by packet\n\n"
                "*Start → merge* runs from the first implementer session to the merge commit on `integration/ate-v1`, "
                "including reviews, rework and orchestrator time. Review rounds are counted by Haiku reviews (one per round). "
                "Earlier tickets ran before the review flow existed, so their cycle times aren't comparable.\n\n")
        f.write(iteration + "\n\n")
        f.write("## Paid usage (Claude)\n\n")
        f.write("\n".join(cl) + "\n")
    print(f"wrote {OUT} ({done} merges)")


if __name__ == "__main__":
    main()
