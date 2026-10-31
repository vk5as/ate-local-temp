#!/usr/bin/env python3
"""Collect context-usage stats for ATE v1 agents into usage.jsonl (one record per session, upserted).

Sources: opencode's SQLite (per-step `tokens.input` = prompt size sent that step, i.e. context
occupancy; `compaction` rows = auto/manual compactions) and Claude Code transcripts for the
orchestrator. LM Studio's loaded context length is read live so % of window is comparable.

Usage: collect.py            # rescan every known ATE session
       collect.py --report   # also print a markdown table
"""
import glob, json, os, re, sqlite3, sys, urllib.request
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "usage.jsonl")
OC_DB = os.path.expanduser("~/.local/share/opencode/opencode.db")
CC_DIR = os.path.expanduser("~/.claude/projects/-home-dev-projects-ate-ornith")
LMS = "http://192.168.8.94:1234/api/v0/models"
VLLM = "http://192.168.8.186:8000/v1/models"  # coder2
LLAMA = "http://192.168.8.186:8081/v1/models"  # coder2 llama.cpp (Qwen-long)
SALUKI = "http://192.168.8.186:8080/v1/models"  # coder2 :8080 (saluki-long, then Strata qwen3.8-flash-next)


def lms_ctx():
    ctx = {}
    try:
        with urllib.request.urlopen(LMS, timeout=5) as r:
            ctx.update({m["id"]: m.get("loaded_context_length") for m in json.load(r)["data"] if m.get("state") == "loaded"})
    except Exception:
        pass
    try:
        with urllib.request.urlopen(VLLM, timeout=5) as r:
            ctx.update({m["id"]: m.get("max_model_len") for m in json.load(r)["data"]})
    except Exception:
        pass
    for url in (LLAMA, SALUKI):
        try:
            # the Strata server on :8080 needs its key; take it from opencode.json
            headers = {}
            try:
                prov = json.load(open(os.path.expanduser("~/.config/opencode/opencode.json")))["provider"]
                key = prov.get("strata", {}).get("options", {}).get("apiKey")
                if key and url == SALUKI:
                    headers["Authorization"] = f"Bearer {key}"
            except Exception:
                pass
            with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=5) as r:
                ctx.update({m["id"]: (m.get("meta") or {}).get("n_ctx") for m in json.load(r)["data"]})
        except Exception:
            pass
    return ctx


def classify(title):
    t = title or ""
    m = re.search(r"(?:ticket|packet) (\d+[a-z]?)", t, re.I)
    if re.match(r"\s*merge", t, re.I):
        role = "merger"
    elif re.match(r"\s*review", t, re.I):
        role = "reviewer"
    elif re.match(r"\s*(implement (ticket|packet)|fix)", t, re.I):
        role = "implementer"
    elif m:
        role = "explorer"
    else:
        role = "orchestrator"
    return (m.group(1).zfill(3 if m.group(1)[-1].isalpha() else 2) if m else None), role


def oc_records(ctx):
    db = sqlite3.connect(f"file:{OC_DB}?mode=ro", uri=True)
    sessions = db.execute(
        "select id,parent_id,title,directory,model,time_created,time_updated,time_idle "
        "from session_v2 where directory like '%ate-ornith%' order by time_created").fetchall()
    for sid, parent, title, directory, model, t0, t1, tidle in sessions:
        model_id = json.loads(model)["id"] if model else None
        steps, comps, last_in, outcome = [], [], 0, None
        tot = {"input": 0, "output": 0, "reasoning": 0}
        for typ, data in db.execute(
                "select type,data from session_message where session_id=? order by seq", (sid,)):
            j = json.loads(data)
            if typ == "assistant" and j.get("tokens"):
                tk = j["tokens"]
                # context occupancy = uncached input + cached prompt (llama.cpp and vLLM report the
                # cached part separately; LM Studio reports none)
                cache = tk.get("cache") if isinstance(tk.get("cache"), dict) else {}
                occ = tk["input"] + cache.get("read", 0) + cache.get("write", 0)
                steps.append(occ)
                last_in = occ
                for k in tot:
                    tot[k] += tk.get(k, 0)
            elif typ == "compaction":
                comps.append({"reason": j.get("reason"), "status": j.get("status"),
                              "context_before": last_in, "step": len(steps),
                              "summary_chars": len(j.get("summary") or "")})
            elif typ == "idle":
                outcome = j.get("outcome")
        window = ctx.get(model_id)
        peak = max(steps, default=0)
        ticket, role = classify(title)
        yield {
            "source": "opencode", "session": sid, "parent": parent, "title": title,
            "ticket": ticket, "role": role, "model": model_id, "directory": directory,
            "context_window": window, "steps": len(steps), "peak_context": peak,
            "peak_pct": round(100 * peak / window, 1) if window else None,
            "final_context": last_in,
            "compactions": len(comps), "compaction_detail": comps,
            "tokens_total": tot, "outcome": outcome or ("running" if not tidle else None),
            "wall_minutes": round(((t1 or t0) - t0) / 60000, 1),
            "started": datetime.fromtimestamp(t0 / 1000, timezone.utc).isoformat().replace("+00:00", "Z"),
        }


def _cc_session(path):
    """Context/compaction/token stats for one Claude Code transcript (context = input + cache read +
    cache write per assistant turn). Also returns the first user prompt, used to label helpers."""
    steps, comps, seen = [], [], set()
    tok = {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0}
    first = last = prompt = None
    for line in open(path):
        try:
            j = json.loads(line)
        except ValueError:
            continue
        ts = j.get("timestamp")
        first = first or ts
        last = ts or last
        if prompt is None and j.get("type") == "user":
            c = (j.get("message") or {}).get("content")
            prompt = c if isinstance(c, str) else json.dumps(c)[:4000]
        if j.get("type") == "system" and j.get("subtype") == "compact_boundary" or j.get("isCompactSummary"):
            comps.append({"reason": (j.get("compactMetadata") or {}).get("trigger", "auto"),
                          "context_before": steps[-1] if steps else 0, "step": len(steps)})
        msg = j.get("message") or {}
        u = msg.get("usage")
        if j.get("type") == "assistant" and u and msg.get("id") not in seen:
            seen.add(msg.get("id"))
            steps.append(u.get("input_tokens", 0) + u.get("cache_read_input_tokens", 0)
                         + u.get("cache_creation_input_tokens", 0))
            tok["input"] += steps[-1]
            tok["output"] += u.get("output_tokens", 0)
            tok["cache_read"] += u.get("cache_read_input_tokens", 0)
            tok["cache_write"] += u.get("cache_creation_input_tokens", 0)
    return steps, comps, tok, first, last, prompt or ""


def _cc_record(path, steps, comps, tok, first, last, **fields):
    rec = {
        "source": "claude-code", "session": os.path.basename(path)[:-6], "model": "claude-opus-5-5",
        "context_window": None, "steps": len(steps), "peak_context": max(steps), "peak_pct": None,
        "final_context": steps[-1], "compactions": len(comps), "compaction_detail": comps,
        "tokens_total": tok, "started": first, "updated": last,
    }
    rec.update(fields)
    if rec.get("role") in ("haiku-reviewer", "side-job"):
        rec["model"] = "claude-haiku"  # Agent-tool subagents run with model: haiku
    try:
        t0 = datetime.fromisoformat(first.replace("Z", "+00:00"))
        t1 = datetime.fromisoformat(last.replace("Z", "+00:00"))
        rec["wall_minutes"] = round((t1 - t0).total_seconds() / 60, 1)
    except Exception:
        pass
    return rec


def cc_records():
    """The Claude Code orchestrator, plus Opus helpers that implementers start in their worktrees
    (``claude -p`` there writes transcripts under a project dir named after the worktree path)."""
    for path in glob.glob(os.path.join(CC_DIR, "*.jsonl")):
        steps, comps, tok, first, last, _ = _cc_session(path)
        if steps:
            yield _cc_record(path, steps, comps, tok, first, last, title="Orchestrator (Claude Code)",
                             ticket=None, role="orchestrator", outcome="running",
                             note="Took over from the opencode orchestrator via a T3 context handoff (summary of runs 1-12).")
    # Agent-tool subagents of the orchestrator (Haiku reviewers, screenshot jobs)
    for path in glob.glob(os.path.join(CC_DIR, "*", "subagents", "agent-*.jsonl")):
        steps, comps, tok, first, last, prompt = _cc_session(path)
        if not steps:
            continue
        m = re.search(r"\bpacket[ -](\d\d[a-z]?)\b", prompt, re.I)
        review = re.search(r"Haiku review", prompt, re.I)
        yield _cc_record(path, steps, comps, tok, first, last,
                         title=("Haiku review of packet " + m.group(1)) if review and m else "Claude subagent",
                         ticket=m.group(1) if review and m else None,
                         role="haiku-reviewer" if review else "side-job", outcome="done")
    helper_dirs = glob.glob(os.path.expanduser("~/.claude/projects/*-t3-worktrees-ate-ornith-*"))
    for path in (p for d in helper_dirs for p in glob.glob(os.path.join(d, "*.jsonl"))):
        steps, comps, tok, first, last, prompt = _cc_session(path)
        if not steps:
            continue
        if "Rules for you" in prompt:  # an Opus helper started by a worker
            title, role = "Opus helper", "opus-helper"
        elif "ATE v1 spec" in prompt and len(steps) >= 5:  # an Opus rework implementer (T3's 1-2 step branch-naming calls also quote the brief)
            title, role = "Opus rework", "opus-implementer"
        elif "Your reviewer name is" in prompt and len(steps) >= 5:  # an Opus review-only packet (33)
            title, role = "Opus review", "opus-reviewer"
        else:  # T3 also runs Claude here (branch naming etc.)
            continue
        m = (re.search(r"handoff-(\d\d[a-z]?)\b", prompt) or re.search(r"\bpacket[ -](\d\d[a-z])\b", prompt, re.I)
             or re.search(r"issues/(\d\d[a-z]?)-", prompt))
        yield _cc_record(path, steps, comps, tok, first, last, title=title,
                         ticket=m.group(1) if m else None, role=role, outcome="done",
                         directory=os.path.basename(os.path.dirname(path)))


def main():
    ctx = lms_ctx()
    existing = {}
    if os.path.exists(OUT):
        for line in open(OUT):
            r = json.loads(line)
            existing[r["session"]] = r
    for r in list(oc_records(ctx)) + list(cc_records()):
        old = existing.get(r["session"], {})
        # keep the window seen while it ran if LM Studio has since unloaded the model
        r["context_window"] = r["context_window"] or old.get("context_window")
        if r["context_window"] and r["peak_context"]:
            r["peak_pct"] = round(100 * r["peak_context"] / r["context_window"], 1)
        if "notes" in old:
            r["notes"] = old["notes"]
        existing[r["session"]] = r
    with open(OUT, "w") as f:
        for r in sorted(existing.values(), key=lambda r: (r["ticket"] or "zz", r["session"])):
            f.write(json.dumps(r) + "\n")
    if "--report" in sys.argv:
        print("| ticket | role | title | steps | peak ctx | peak % | compactions (ctx before) | in/out tok | outcome |")
        print("|---|---|---|---|---|---|---|---|---|")
        for r in sorted(existing.values(), key=lambda r: (r["ticket"] or "zz", r["role"])):
            c = ", ".join(str(d["context_before"]) for d in r["compaction_detail"]) or "-"
            t = r["tokens_total"]
            print(f"| {r['ticket'] or '-'} | {r['role']} | {r['title']} | {r['steps']} | {r['peak_context']} | "
                  f"{r['peak_pct']} | {r['compactions']} ({c}) | {t['input']}/{t['output']} | {r['outcome']} |")


if __name__ == "__main__":
    main()
