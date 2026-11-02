"""ATE's HTML Test Report (ticket 21).

A self-contained, inline-CSS HTML document rendered from a bundled Jinja2 template.
Packet 21a builds the file, its naming and the Cover section; neighbouring packets add
the Steps / Equipment / Configuration / Run log sections and the PDF render path.
"""

from .html import render_report, write_report

__all__ = ["render_report", "write_report"]
