"""Saved capabilities, as a catalogue an AI agent can call.

This is the far end of the through-line: *the model discovers, the artifact becomes a
capability, deterministic replay is how the agent invokes it.* The registry is what makes
"a capability an agent can call" literal rather than aspirational - it turns every saved
artifact into a tool definition, generated from the same Pydantic declaration that types
the code and validates the artifact on load.

What a calling agent needs, and gets here:

* **what it does** - the capability's own description, written during discovery
* **what to pass** - JSON Schema from the declared inputs, with patterns and examples
* **what comes back** - the declared outputs and their types
* **what can go wrong** - the declared business outcomes, listed in the description so
  the agent knows `MEMBER_NOT_FOUND` is a possible answer *before* it ever invokes

That last point is the one most tool catalogues miss. A tool whose failure modes are
undocumented forces the caller to treat every non-success as an outage.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from cua.schema.capability import (
    ApprovalStatus,
    Capability,
    OutcomeSeverity,
    RiskClass,
)


class CapabilityRegistry:
    """Loads artifacts from disk and presents them as callable tools."""

    def __init__(self, directory: Path) -> None:
        self._directory = directory

    def load_all(self) -> list[Capability]:
        out: list[Capability] = []
        for path in sorted(self._directory.glob("*.json")):
            try:
                out.append(Capability.model_validate_json(path.read_text(encoding="utf-8")))
            except Exception as exc:  # a bad artifact is a finding, not a crash
                print(f"  ! skipping {path.name}: {exc}")
        return out

    def get(self, capability_id: str) -> Capability | None:
        return next((c for c in self.load_all() if c.id == capability_id), None)

    def tool_definitions(self, *, approved_only: bool = False) -> list[dict[str, Any]]:
        """Every capability as a tool an agent could be handed.

        `approved_only` is the switch a production caller would set: an unattended agent
        should see only capabilities a human has reviewed. It defaults to False here so
        the catalogue is inspectable during development, which is when you most need to
        look at a draft.
        """
        return [
            tool_definition(capability)
            for capability in self.load_all()
            if not approved_only or capability.status is ApprovalStatus.APPROVED
        ]


def tool_definition(capability: Capability) -> dict[str, Any]:
    """One capability -> one tool definition, in the neutral shape `cua.discovery.llm`
    uses. Whichever provider is in play translates it exactly like any other tool."""
    return {
        "name": capability.id.replace(".", "_").replace("-", "_"),
        "description": describe_for_agent(capability),
        "arguments_schema": capability.input_json_schema(),
    }


def describe_for_agent(capability: Capability) -> str:
    """The docstring an LLM reads before deciding whether to call this.

    Deliberately includes the failure modes and the risk class. An agent choosing between
    capabilities needs to know that one of them opens an account.
    """
    lines = [capability.description.strip()]

    if capability.outputs:
        returns = ", ".join(f"{o.name} ({o.type})" for o in capability.outputs)
        lines.append(f"Returns: {returns}.")

    # Split by severity, because the caller does two different things with them. An
    # answer is handled; a declared failure is retried or reported. Listing a 500 as "an
    # answer rather than an error" would be worse than not declaring it at all.
    answers = [
        o.code
        for o in capability.known_outcomes
        if o.terminal and o.severity is not OutcomeSeverity.ERROR
    ]
    failures = [
        o.code
        for o in capability.known_outcomes
        if o.terminal and o.severity is OutcomeSeverity.ERROR
    ]
    if answers:
        lines.append(
            f"May instead return one of these expected outcomes, which are answers "
            f"rather than errors: {', '.join(answers)}."
        )
    if failures:
        lines.append(
            f"May fail with: {', '.join(failures)}. These are declared so you can "
            f"recognise them, but the request did not succeed."
        )

    if capability.risk_class is not RiskClass.READ_ONLY:
        lines.append(
            f"Risk: {capability.risk_class}. Requires an approved capability and an "
            f"explicit caller approval before it will run."
        )
    if capability.status is not ApprovalStatus.APPROVED:
        lines.append(f"Status: {capability.status} - not yet approved for unattended use.")
    return " ".join(lines)


def render(capabilities: list[Capability]) -> str:
    """The catalogue, for a human."""
    if not capabilities:
        return "No capabilities found. Run `cua discover` to record one."

    lines = [f"{len(capabilities)} capability(ies)", ""]
    for capability in capabilities:
        lines.append(f"  {capability.qualified_name}")
        lines.append(f"    {capability.display_name}")
        lines.append(
            f"    status={capability.status}  risk={capability.risk_class}  "
            f"idempotent={capability.idempotent}  steps={len(capability.steps)}"
        )
        for param in capability.inputs:
            bits: list[str] = [str(param.type)]
            if param.pattern:
                bits.append(param.pattern)
            if param.example:
                bits.append(f"e.g. {param.example}")
            lines.append(f"    in   {param.name}: {', '.join(bits)}")
        for output in capability.outputs:
            lines.append(f"    out  {output.name}: {output.type} [{output.sensitivity}]")
        codes = [o.code for o in capability.known_outcomes if o.terminal]
        if codes:
            lines.append(f"    outcomes  {', '.join(codes)}")
        lines.append("")
    return "\n".join(lines)


def render_detail(capability: Capability) -> str:
    """Exactly what an agent would be handed, so a reviewer can check it."""
    return json.dumps(tool_definition(capability), indent=2)
