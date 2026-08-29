"""The tools the model may use.

Defined as Pydantic models so the JSON Schema the model sees is generated from the same
declaration that validates the arguments coming back - the one-schema-many-consumers
pattern the whole project is organised around, applied here to the model's own contract.

Two deliberate choices in this vocabulary:

**`why` is required on every acting tool.** It costs the model a few tokens and it buys
two things: the brief's "a structured log of what the agent did *and why*", and the
`intent` field on every recorded step, which is what a human reviewer reads when
approving a capability. A step that says "clicked e11" is unreviewable.

**The model declares parameters and outputs explicitly** (`is_parameter`, `extract`).
The compiler verifies those claims against what actually happened rather than trusting
them, but having the model state its intent turns an inference problem into a
verification problem, which is a much easier one.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from cua.discovery.llm import ToolSpec


class ObserveArgs(BaseModel):
    """Look at the current screen."""

    include_screenshot: bool = Field(
        default=False,
        description="Request a screenshot as well as the accessibility tree. Use only "
        "when the tree is ambiguous; screenshots are expensive.",
    )


class NavigateArgs(BaseModel):
    url: str = Field(description="Absolute URL. Must be inside the permitted origins.")
    why: str = Field(description="Why this navigation is needed, in one short sentence.")


class ClickArgs(BaseModel):
    ref: str = Field(description="Element ref from the most recent observation, e.g. 'e14'.")
    why: str = Field(description="What this click is meant to accomplish.")


class FillArgs(BaseModel):
    ref: str
    text: str = Field(description="Text to type.")
    why: str
    is_parameter: bool = Field(
        default=False,
        description="True if this value should become an input parameter of the "
        "capability rather than a fixed constant - e.g. a member id supplied per call.",
    )
    parameter_name: str = Field(
        default="",
        description="camelCase name for the parameter, e.g. 'memberId'. Required when "
        "is_parameter is true.",
    )


class SelectArgs(BaseModel):
    ref: str
    value: str = Field(description="Option value or visible label.")
    why: str


class PressArgs(BaseModel):
    ref: str
    key: str = Field(description="Key name, e.g. 'Enter'.")
    why: str


class ExtractArgs(BaseModel):
    ref: str
    output_name: str = Field(description="camelCase name, e.g. 'savingsBalance'.")
    output_type: str = Field(
        default="text",
        description="One of: text, currency, integer, number, date.",
    )
    why: str
    sensitive: bool = Field(
        default=False,
        description="True if this value is regulated financial data or PII. Sensitive "
        "outputs are masked in logs, screenshots and artifacts.",
    )


class AssertStateArgs(BaseModel):
    """Declare the checkpoint that identifies the screen you are on."""

    describe: str = Field(description="What state this is, e.g. 'the member detail screen'.")
    refs: list[str] = Field(
        default_factory=list,
        description="Elements whose presence proves you are on this screen.",
    )


class NoteOutcomeArgs(BaseModel):
    """Record an exceptional-but-legitimate screen you encountered.

    Seeds the capability's `known_outcomes`, which is how "no such member" becomes a
    typed result the calling agent can handle rather than an error at run time.
    """

    code: str = Field(description="SCREAMING_SNAKE, e.g. MEMBER_NOT_FOUND.")
    describe: str = Field(description="What this state means for the caller.")
    text_pattern: str = Field(
        default="",
        description="Distinctive text that identifies this state, for the detector.",
    )


class RequestHumanArgs(BaseModel):
    reason: str = Field(description="Why you cannot safely continue.")
    tried: str = Field(default="", description="What you already attempted.")


class FinishArgs(BaseModel):
    summary: str = Field(description="What you accomplished.")
    capability_id: str = Field(
        description="Namespaced id for the capability, e.g. 'member.savings-balance'."
    )
    description: str = Field(
        description="One or two sentences describing this capability for the catalogue "
        "another AI agent will read before invoking it."
    )


#: name -> (args model, description shown to the model)
TOOL_SPECS: dict[str, tuple[type[BaseModel], str]] = {
    "observe": (
        ObserveArgs,
        "Look at the current screen. Returns the accessibility tree: roles, accessible "
        "names, and table row context. Call this after anything that changes the page - "
        "element refs are only valid for the observation that produced them.",
    ),
    "navigate": (NavigateArgs, "Go to a URL."),
    "click": (ClickArgs, "Click an element."),
    "fill": (FillArgs, "Type text into a field, replacing anything already there."),
    "select": (SelectArgs, "Choose an option in a dropdown."),
    "press": (PressArgs, "Press a key while an element is focused."),
    "extract": (
        ExtractArgs,
        "Record a value from the screen as an output of this capability.",
    ),
    "assert_state": (
        AssertStateArgs,
        "Declare a checkpoint proving you reached an expected screen. Call this when you "
        "arrive somewhere important, especially the final screen.",
    ),
    "note_outcome": (
        NoteOutcomeArgs,
        "Record a legitimate-but-exceptional result you hit, such as 'no records found' "
        "or 'not authorized'. These become typed outcomes the capability can return.",
    ),
    "request_human": (
        RequestHumanArgs,
        "Stop and ask a human operator for help. Use this when you are stuck, when a "
        "screen is ambiguous, or when an action looks irreversible and you are unsure.",
    ),
    "finish": (FinishArgs, "The goal is complete. Ends the run."),
}

#: Tools that change the world and therefore need policy, lease and recording checks.
ACTING_TOOLS = frozenset({"navigate", "click", "fill", "select", "press"})


def tool_definitions() -> list[ToolSpec]:
    """The tool list, generated from the Pydantic models above.

    Provider-neutral: plain JSON Schema, translated to a vendor's key names inside the
    client. The first version emitted one provider's shape directly from here, which put
    a wire format on the wrong side of the LLM boundary.
    """
    return [
        ToolSpec(
            name=name,
            description=description,
            arguments_schema=_schema_for(model),
        )
        for name, (model, description) in TOOL_SPECS.items()
    ]


def _schema_for(model: type[BaseModel]) -> dict[str, Any]:
    schema = model.model_json_schema()
    schema.pop("title", None)
    schema["additionalProperties"] = False
    schema.setdefault("required", [])
    return schema


def parse_args(name: str, raw: dict[str, Any]) -> BaseModel:
    model, _ = TOOL_SPECS[name]
    return model.model_validate(raw)
