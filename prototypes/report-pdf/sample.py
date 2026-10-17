"""PROTOTYPE, throwaway. Builds a fake Test Report context and renders the HTML.

python sample.py [--steps N] [--logs M] [--furniture css|xhtml2pdf|none] [-o out.html]

Everything here stands in for what the real report would read from the Test Run record.
"""
import argparse
import base64
import random
import struct
import zlib
from pathlib import Path

from jinja2 import Environment, FileSystemLoader

HERE = Path(__file__).parent
RESULTS = ["PASS", "FAIL", "ERROR", "SKIPPED", "NOTRUN", "FORCED_NOTRUN", "NOREPORT"]


def logo_data_uri():
    # tiny hand-made PNG: a blue badge with a white bar, 120x40
    w, h = 120, 40
    rows = []
    for y in range(h):
        row = bytearray([0])
        for x in range(w):
            inside = 4 <= x < w - 4 and 4 <= y < h - 4
            bar = 14 <= y < 26 and 16 <= x < w - 16
            row += bytes((255, 255, 255, 255) if bar else (31, 78, 140, 255) if inside else (0, 0, 0, 0))
        rows.append(bytes(row))
    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d))
    png = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0))
    png += chunk(b"IDAT", zlib.compress(b"".join(rows))) + chunk(b"IEND", b"")
    return "data:image/png;base64," + base64.b64encode(png).decode()


STEP_NAMES = [
    ("Input current at no load", "A", "0.050 .. 0.120", lambda r: f"{r.uniform(0.06, 0.11):.3f}"),
    ("Output voltage, rail 1", "V", "47.50 .. 48.50", lambda r: f"{r.uniform(47.7, 48.3):.2f}"),
    ("Ripple at full load", "mVpp", "< 120", lambda r: f"{r.uniform(40, 110):.1f}"),
    ("Fan tachometer", "rpm", "2800 .. 3600", lambda r: f"{r.randint(2900, 3500)}"),
    ("Firmware version", "", "2.14.0", lambda r: "2.14.0"),
    ("Insulation resistance, L-PE", "MΩ", "> 100", lambda r: f"{r.uniform(150, 900):.0f}"),
    ("Efficiency at 50 % load", "%", ">= 92.0", lambda r: f"{r.uniform(92.2, 94.5):.1f}"),
    ("Over-temperature trip", "°C", "85 .. 95", lambda r: f"{r.uniform(86, 94):.1f}"),
]


def build(n_steps=40, n_logs=200, seed=1):
    r = random.Random(seed)
    rows = []  # flattened expanded tree, in tree order
    counts = {k: 0 for k in RESULTS}
    failures = []
    log_groups = [{"owner": "Run", "lines": []}, {"owner": "Pre-test Action: PSU-48V Final ATP", "lines": []}]

    def ts(i):
        s = 3 + i * 2
        return f"14:{(s // 60) % 60:02d}:{s % 60:02d}.{r.randint(0, 999):03d}"

    def add_step(num, depth, i, force=None):
        name, unit, exp, gen = STEP_NAMES[i % len(STEP_NAMES)]
        res = force or r.choices(RESULTS, weights=[92, 2, 0.5, 1, 0.5, 0.2, 0.3])[0]
        actual = "" if res in ("NOTRUN", "FORCED_NOTRUN", "SKIPPED", "NOREPORT") else gen(r) + (f" {unit}" if unit else "")
        msg = {
            "PASS": "within limits",
            "FAIL": "outside limits",
            "ERROR": "rail1.lua:42: attempt to compare nil with number",
            "SKIPPED": "skipped: option not fitted",
            "NOTRUN": "",
            "FORCED_NOTRUN": "forced not run by schedule",
            "NOREPORT": "step returned without calling ATE.report",
        }[res]
        if res == "FAIL":
            actual = "51.20 V" if unit == "V" else actual
        row = dict(kind="step", depth=depth, number=num, name=name, message=msg,
                   expected=exp + (f" {unit}" if unit else ""),
                   actual=actual, result=res, attempts=[], inputs=[], override=None)
        if i % 17 == 5:
            row["inputs"].append("Input 1 (LED colour): Green")
        if i % 23 == 7:
            row["override"] = "disabled by Operator"
            row["result"] = res = "NOTRUN"
            row["actual"], row["message"] = "", ""
        if i % 19 == 11 and res == "PASS":
            # retried: two earlier attempts
            row["attempts"] = [
                dict(n=1, message="outside limits → RETRY", expected=row["expected"], actual="2710 rpm", result="FAIL"),
                dict(n=2, message="outside limits → RETRY", expected=row["expected"], actual="2765 rpm", result="FAIL"),
            ]
        counts[res] += 1
        if res in ("FAIL", "ERROR", "NOREPORT"):
            failures.append(row)
        for k in range(max(1, n_logs // max(1, n_steps))):
            level = r.choices(["INFO", "WARNING", "ERROR"], weights=[90, 8, 2])[0]
            row.setdefault("log", []).append(
                dict(t=ts(len(rows)), level=level, source=f"step {num}",
                     text=r.choice(["SCPI> MEAS:VOLT? CH1", "load set to 12.5 A", "settling 250 ms",
                                    "reading stable after 3 samples", "relay K4 closed", "DMM range 100 V"])))
        rows.append(row)
        return row

    i = 0
    num = 10
    while i < n_steps:
        if i and i % 25 == 0:
            # an include header with a nested Sub-schedule
            depth = 1
            hdr = dict(kind="header", depth=0, number=f"{num}", name=f"Burn-in channel {i // 25} (burnin.tslua)", summary=None)
            rows.append(hdr)
            sub_counts = {}
            for j in range(min(6, n_steps - i)):
                s = add_step(f"BI{i // 25}.{j + 1}", depth, i)
                sub_counts[s["result"]] = sub_counts.get(s["result"], 0) + 1
                i += 1
            hdr["summary"] = f"{sum(sub_counts.values())} steps: " + ", ".join(f"{v} {k}" for k, v in sub_counts.items())
            num += 10
            continue
        force = "FAIL" if i == 7 else "ERROR" if i == 13 else None
        add_step(str(num), 0, i, force)
        num += 10
        i += 1

    run_lines = [dict(t="14:03:22.101", level="INFO", source="run", text="Test Run started by J. Citizen on BENCH-03"),
                 dict(t="14:03:22.140", level="WARNING", source="run", text="checksum mismatch: lib/scpi.lua (expected 9f2c…, computed 41ab…)")]
    log_groups[0]["lines"] = run_lines
    log_groups[1]["lines"] = [dict(t="14:03:22.300", level="INFO", source="pre-test", text="power analyser zeroed")]
    for row in rows:
        if row["kind"] == "step" and row.get("log"):
            log_groups.append({"owner": f"Step {row['number']} · {row['name']}", "lines": row["log"]})

    total_retried = sum(1 for x in rows if x["kind"] == "step" and x["attempts"])
    overrides = sum(1 for x in rows if x["kind"] == "step" and x["override"])
    return dict(
        org="Southern Power Systems Pty Ltd",
        logo=logo_data_uri(),
        paper="A4",
        doc=dict(title="PSU-48V Final Acceptance Test Procedure", id="ATP-0117", rev="C"),
        run=dict(uuid="0192f3a4-7b1c-7e2d-9a10-5c3e8f0b2d41", uut_type="PSU-48V", serial="SN240117-0042",
                 project="Contract 7712 / Lot 3", station="BENCH-03", start="2026-10-17 14:03:22 +10:30",
                 end="2026-10-17 14:41:09 +10:30", duration="37 min 47 s", recorded_by="1.0.3",
                 generated_by="1.1.0", generated_at="2026-10-18 09:12:40 +10:30", stop_on_fail="No",
                 result="FAIL", end_reason="COMPLETED", operator="J. Citizen", interrupted=False,
                 mismatch=True),
        schedule=dict(name="PSU-48V Final ATP", description="Final acceptance of the 48 V 1.2 kW rectifier module after burn-in.",
                      file="psu48/final_atp.tslua", release="MN-2041 · PSU-48V Test Media · 2.3.1"),
        counts=counts, retried=total_retried, overrides=overrides, failures=failures,
        rows=rows,
        equipment=[
            dict(name="Digital multimeter", serial="MY5402113", connection="VISA USB0::0x2A8D::0x1301::MY5402113::INSTR", cal="2027-03-01", notes=""),
            dict(name="Electronic load", serial="LD-88213", connection="TCP 10.0.4.21:5025", cal="2026-12-15", notes="channel 2 only"),
            dict(name="Power analyser", serial="PA3000-0193", connection="SERIAL COM4 115200 8N1", cal="2027-01-20", notes=None),
            dict(name="Hipot tester", serial="", connection="", cal="", notes="manual, reading entered by Operator"),
        ],
        releases=[dict(media="MN-2041", name="PSU-48V Test Media", version="2.3.1", sha="3b7e0c51d2a4f9e1b8c6a7d5e4f3029187a6b5c4d3e2f1a0b9c8d7e6f5a4b3c2"),
                  dict(media="MN-1100", name="Common Instrument Library", version="1.8.0", sha="a0f1e2d3c4b5a6978877665544332211ffeeddccbbaa99887766554433221100")],
        files=[dict(path="psu48/final_atp.tslua", media="MN-2041", exp="5d41402abc4b2a76b9719d911017c592ae2f6c1b77e5b2b7d1a0c9e8f7a6b5c4", got=None, stored="No"),
               dict(path="psu48/burnin.tslua", media="MN-2041", exp="7c211433f02071597741e6ff5a8ea34789abad0f2c1d3e4b5a69788796a5b4c3", got=None, stored="No"),
               dict(path="lib/scpi.lua", media="MN-1100", exp="9f2c5e8a1b3d4f6a7c8e9d0b1a2c3e4f5a6b7c8d9e0f1a2b3c4d5e6f7a8b9c0d", got="41ab09f8e7d6c5b4a3928170f6e5d4c3b2a1908f7e6d5c4b3a29180f7e6d5c4b", stored="Yes"),
               dict(path="ate.util", media="", exp="", got="e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855", stored="No")],
        instances=[dict(name="Burn-in channel", file="psu48/burnin.tslua", prefix="BI1."),
                   dict(name="Burn-in channel", file="psu48/burnin.tslua", prefix="BI2.")],
        log_groups=log_groups,
        results=RESULTS,
    )


def render(ctx, furniture):
    env = Environment(loader=FileSystemLoader(HERE), autoescape=True, trim_blocks=True, lstrip_blocks=True)
    return env.get_template("report.html.j2").render(furniture=furniture, font_dir="fonts", **ctx)  # relative to cwd: Windows drive paths confuse xhtml2pdf


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--steps", type=int, default=40)
    ap.add_argument("--logs", type=int, default=200)
    ap.add_argument("--furniture", default="css", choices=["css", "xhtml2pdf", "none"])
    ap.add_argument("-o", default="out/report.html")
    a = ap.parse_args()
    Path(a.o).parent.mkdir(exist_ok=True)
    Path(a.o).write_text(render(build(a.steps, a.logs), a.furniture), encoding="utf-8")
    print(a.o)
