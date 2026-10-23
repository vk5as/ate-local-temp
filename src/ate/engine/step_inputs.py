"""Step Input declarations — parsing and validation for ``ATE.input(n)``.

Ticket 10 checkbox a. A Test Schedule declares the inputs a step asks the Operator for via
``:setInputs({ [1] = {...}, ... })``; each entry is validated against the Test Schedule format
(see ``.scratch/ate-v1-wayfinding/assets/test-schedule-format.md``) and normalised to a plain
Python structure the UI and :class:`~ate.engine.operator_input.OperatorInput` can consume.

This module never imports lupa or widgets: it validates already-materialised declarations, so a
schedule's raw Lua tables are converted to plain Python before reaching it (see ``lua_host.py``).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence


# The Step Input types the format allows. ENUM is the only type that carries a widget; PROMPT and
# POLL are the two non-blocking prompt styles.
_INPUT_TYPES = ("ENUM", "INTEGER", "FLOAT", "STRING", "PROMPT", "POLL")

# The widgets an ENUM input can use (RADIOBUT pick one, CHECKBOX pick many, PUSHBUT buttons).
_ENUM_WIDGETS = ("RADIOBUT", "CHECKBOX", "PUSHBUT")

# Layout orientation for every input type.
_ORIENTS = ("VERT", "HORIZ")


class InvalidStepInput(Exception):
    """A Step Input declaration violates the Test Schedule format (ticket 10 checkbox a).

    Raised while validating or normalising declarations — bad types, widgets, ranges, patterns and
    so on are all load errors. The message names the input's index and the offending field."""


def _is_number(value: Any) -> bool:
    """True for real ints/floats but not booleans (a Lua boolean is never a range bound)."""
    return isinstance(value, (int, float)) and not isinstance(value, bool)


@dataclass(frozen=True)
class StepInput:
    """A validated declaration for one ``ATE.input(n)``.

    Only the fields relevant to ``type`` are populated; the rest stay at their defaults so a single
    consumer can read every field without branching on type."""

    index: int  # n in ATE.input(n); 1-based, in declaration order
    desc: str
    type: str
    widget: str = ""  # RADIOBUT / CHECKBOX / PUSHBUT for ENUM only
    enum: tuple[str, ...] = ()  # option labels for ENUM only
    range: dict[str, Any] = field(default_factory=dict)  # {} or {min, max} for INTEGER/FLOAT only
    pattern: str | None = None  # regex the STRING value must match
    prompt: str | tuple[str, ...] = ""  # PROMPT/POLL text (a string or array of strings)
    tooltip: str | None = None
    orient: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Return this declaration in its original schedule-format shape for storage.

        The normalized StepInput dataclass renames fields (e.g. ``widget`` instead of the ENUM input
        widget name); a UI rendering the step's inputs needs the on-disk representation, so persist
        that rather than the internal one. Tuples become lists so the result is JSON-serializable."""
        out: dict[str, Any] = {
            "desc": self.desc,
            "type": self.type,
            "range": self.range,
            "pattern": self.pattern,
            "prompt": list(self.prompt) if isinstance(self.prompt, tuple) else self.prompt,
            "tooltip": self.tooltip,
            "orient": self.orient,
        }
        if self.type == "ENUM":
            out["input"] = self.widget  # RADIOBUT / CHECKBOX / PUSHBUT
            out["enum"] = list(self.enum)
        return out


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise InvalidStepInput(message)


def validate_inputs(declarations: Mapping[int, Mapping[str, Any]]) -> list[StepInput]:
    """Validate and normalise a step's raw input declarations.

    ``declarations`` maps each 1-based index n to the declaration dict for ``ATE.input(n)`` (the
    values are already materialised from Lua tables). Returns the validated declarations in index
    order; raises :class:`InvalidStepInput` on any violation."""
    ordered: list[StepInput] = []
    for index, raw in sorted(declarations.items()):
        ordered.append(_validate_one(index, raw))
    return ordered


def _validate_one(index: int, raw: Mapping[str, Any]) -> StepInput:
    """Validate one declaration and build its :class:`StepInput`."""
    # Required fields first.
    desc = raw.get("desc")
    if not isinstance(desc, str) or desc == "":
        raise InvalidStepInput(f"input {index}: missing required field 'desc'")

    decl_type = raw.get("type")
    if decl_type not in _INPUT_TYPES:
        raise InvalidStepInput(
            f"input {index}: type must be one of {_INPUT_TYPES}, got {decl_type!r}"
        )

    # Known fields used on a type they don't apply to are rejected — this catches mistakes like an
    # ``enum`` on an INTEGER or a ``range`` on a STRING. Truly unknown keys are ignored so a stray
    # extra field never breaks a valid schedule.
    if decl_type != "ENUM" and ("input" in raw or "enum" in raw):
        raise InvalidStepInput(f"input {index}: 'input'/'enum' only apply to ENUM")
    if decl_type not in ("INTEGER", "FLOAT") and "range" in raw:
        raise InvalidStepInput(f"input {index}: 'range' only applies to INTEGER/FLOAT")
    if decl_type != "STRING" and "pattern" in raw:
        raise InvalidStepInput(f"input {index}: 'pattern' only applies to STRING")
    if decl_type not in ("PROMPT", "POLL") and "prompt" in raw:
        raise InvalidStepInput(f"input {index}: 'prompt' only applies to PROMPT/POLL")

    if decl_type == "ENUM":
        widget = raw.get("input")
        _require(widget in _ENUM_WIDGETS, f"input {index}: ENUM input must be one of {_ENUM_WIDGETS}")
        enum = raw.get("enum")
        labels = _as_string_list(enum, index) if isinstance(enum, (list, tuple)) else ()
        _require(len(labels) > 0, f"input {index}: ENUM needs at least one option label")
        orient = _orient(raw.get("orient"), index)
        tooltip = _tooltip(raw.get("tooltip"))
        return StepInput(
            index=index,
            desc=desc,
            type="ENUM",
            widget=widget,
            enum=tuple(labels),
            orient=orient,
            tooltip=tooltip,
        )

    if decl_type in ("INTEGER", "FLOAT"):
        _range = raw.get("range")
        bounds = _as_bounds(_range) if isinstance(_range, dict) else {}
        return StepInput(index=index, desc=desc, type=decl_type, range=bounds)

    if decl_type == "STRING":
        pattern = raw.get("pattern")
        if pattern is not None:
            _require(isinstance(pattern, str), f"input {index}: STRING pattern must be a string")
        orient = _orient(raw.get("orient"), index)
        tooltip = _tooltip(raw.get("tooltip"))
        return StepInput(
            index=index, desc=desc, type="STRING", pattern=pattern, orient=orient, tooltip=tooltip
        )

    # PROMPT and POLL: prompt text is required (a string or an array of strings).
    prompt = raw.get("prompt")
    if isinstance(prompt, str):
        prompt_values: tuple[str, ...] = (prompt,)
    elif isinstance(prompt, (list, tuple)):
        prompt_values = tuple(p for p in prompt if isinstance(p, str))
    else:
        raise InvalidStepInput(f"input {index}: PROMPT/POLL needs 'prompt' text")
    orient = _orient(raw.get("orient"), index)
    tooltip = _tooltip(raw.get("tooltip"))
    return StepInput(
        index=index, desc=desc, type=decl_type, prompt=prompt_values, orient=orient, tooltip=tooltip
    )


def _as_string_list(value: Any, index: int) -> list[str]:
    """Return ``value`` as a list of strings, raising if any element isn't one."""
    labels = [item for item in value]
    for label in labels:
        if not isinstance(label, str):
            raise InvalidStepInput(f"input {index}: enum labels must be strings")
    return labels


def _as_bounds(value: dict[str, Any]) -> dict[str, Any]:
    """Normalise an INTEGER/FLOAT range to ``{}`` or ``{min, max}``.

    The format is strict about shape: an empty table means unbounded, and a two-key table must carry
    both bounds. A single bound (e.g. only ``max``) is rejected."""
    if len(value) == 0:
        return {}
    if set(value.keys()) == {"min", "max"}:
        bounds = {"min": value["min"], "max": value["max"]}
        _require(_is_number(bounds["min"]), f"input {value.get('min', '?')}: range.min must be a number")
        _require(_is_number(bounds["max"]), f"input {value.get('max', '?')}: range.max must be a number")
        return bounds
    raise InvalidStepInput(
        f"input {value.get('min', value.get('max', '?'))}: range must be {{}} or "
        "{{min, max}}, got keys " + ", ".join(sorted(value.keys()))
    )


def _orient(value: Any, index: int) -> str | None:
    """Return the orientation when given one; ``None`` means it was not declared."""
    if value is None:
        return None
    if value in _ORIENTS:
        return value
    raise InvalidStepInput(f"input {index}: orient must be one of {_ORIENTS}, got {value!r}")


def _tooltip(value: Any) -> str | None:
    """Return the tooltip when given a string; ``None`` means it was not declared."""
    if value is None:
        return None
    if isinstance(value, str):
        return value
    raise InvalidStepInput("tooltip must be a string")
