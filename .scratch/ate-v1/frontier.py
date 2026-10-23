#!/usr/bin/env python3
"""Ticket progress for ATE v1: done vs remaining work items, and the unblocked frontier.

Work items are unsplit tickets (`NN-*.md`) and packets (`NNx-*.md`). A ticket with `Status: split`
is an umbrella: it counts as resolved once every packet listed in its `Split into:` line is.
"""
import re
from pathlib import Path

D = Path(__file__).parent / "issues"
st, bl, kids = {}, {}, {}
for f in sorted(D.glob("*.md")):
    i = re.match(r"(\d\d[a-z]?)-", f.name).group(1)
    s = f.read_text()
    st[i] = re.search(r"\*\*Status:\*\* (\S+)", s).group(1)
    bl[i] = re.findall(r"\b(\d\d[a-z]?)\b(?= \()", re.search(r"\*\*Blocked by:\*\* (.*)", s).group(1))
    m = re.search(r"\*\*Split into:\*\* ([\d a-z,]+)", s)
    if m:
        kids[i] = re.findall(r"\d\d[a-z]", m.group(1))


def done(i):
    return all(done(k) for k in kids[i]) if i in kids else st[i] == "resolved"


items = [i for i in st if i not in kids]
finished = [i for i in items if done(i)]
frontier = [i for i in items if not done(i) and all(done(b) for b in bl[i])]
print(f"{len(finished)} done, {len(items) - len(finished)} remaining (of {len(items)} work items); "
      f"{len(frontier)} unblocked: {', '.join(frontier)}")
