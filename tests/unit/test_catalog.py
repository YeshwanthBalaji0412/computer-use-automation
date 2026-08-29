"""The capability catalogue, and the human review gate in front of it.

The brief's through-line ends at *"a capability an AI agent can call."* These tests pin
down what "callable" has to mean for that sentence to be worth anything:

* the calling agent is told what to pass, in a schema generated from the same declaration
  that types the code - so the published contract cannot drift from the implementation;
* it is told the **business outcomes** before it invokes, because a tool whose expected
  answers are undocumented forces the caller to treat every non-success as an outage;
* it is told when a capability is risky or unreviewed, so an unattended caller can refuse.

The approval tests cover the other half: `draft -> approved` is a real transition written
back into the artifact by a named person, and it is pinned to a version.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from cua.app.approve import approve_capability
from cua.catalog.registry import (
    CapabilityRegistry,
    describe_for_agent,
    render,
    tool_definition,
)
from cua.schema.capability import (
    ActionKind,
    ApprovalStatus,
    Assertion,
    AssertionKind,
    Capability,
    InputParam,
    KnownOutcome,
    OutputField,
    ProductRef,
    RiskClass,
    Sensitivity,
    Step,
    StepValue,
    TargetRef,
)
from cua.schema.locator import Locator, LocatorStrategy, Tier

pytestmark = pytest.mark.unit


def locator(name: str, role: str = "button") -> Locator:
    return Locator(
        describe=f"the {name} control",
        strategies=[LocatorStrategy(tier=Tier.ROLE_NAME_EXACT, role=role, name=name)],
    )


def capability(**overrides: object) -> Capability:
    defaults: dict[str, object] = {
        "id": "member.savings-balance",
        "version": "1.0.0",
        "display_name": "Read savings balance",
        "description": "Look up a member by ID and read their current savings balance.",
        "target": TargetRef(
            product=ProductRef(vendor="corelink", app="servicing-console"),
            entry_url_pattern="{{base_url}}/",
        ),
        "inputs": [InputParam(name="memberId", pattern=r"^\d{6}$", example="100042")],
        "outputs": [
            OutputField(
                name="savingsBalance",
                type="currency",
                sensitivity=Sensitivity.PII,
                locator=locator("Savings", role="cell"),
            )
        ],
        "known_outcomes": [
            KnownOutcome(
                code="MEMBER_NOT_FOUND",
                message="No member exists with that identifier.",
                detect=Assertion(kind=AssertionKind.TEXT_PRESENT, pattern="No records found"),
            )
        ],
        "steps": [
            Step(
                id="s1",
                intent="type the member id",
                action=ActionKind.FILL,
                target=locator("Member ID", role="textbox"),
                value=StepValue(param="memberId"),
            )
        ],
        "success": Assertion(kind=AssertionKind.OUTPUT_PRESENT, output="savingsBalance"),
    }
    return Capability.model_validate(defaults | overrides)


def write(directory: Path, cap: Capability) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{cap.id}@{cap.version}.json"
    path.write_text(cap.model_dump_json(indent=2), encoding="utf-8")
    return path


# --------------------------------------------------------------------------- catalogue


def test_tool_definition_is_a_valid_callable_contract() -> None:
    """Name, description, arguments. The shape any provider's tool API expects."""
    definition = tool_definition(capability())

    assert definition["name"] == "member_savings_balance"
    schema = definition["arguments_schema"]
    assert schema["required"] == ["memberId"]
    assert schema["properties"]["memberId"]["pattern"] == r"^\d{6}$"
    # An agent that has to guess the format of a member id will guess wrong.
    assert schema["properties"]["memberId"]["examples"] == ["100042"]
    # Closed, so a hallucinated argument is rejected rather than silently dropped.
    assert schema["additionalProperties"] is False
    # A tool definition that cannot be serialised cannot be sent.
    json.dumps(definition)


def test_business_outcomes_are_advertised_before_invocation() -> None:
    """The point of declaring outcomes in the schema.

    If the caller only learns `MEMBER_NOT_FOUND` exists by receiving one, it has to treat
    it as an outage. Putting it in the tool description is what makes it an *answer*.
    """
    description = describe_for_agent(capability())

    assert "MEMBER_NOT_FOUND" in description
    assert "rather than errors" in description
    assert "savingsBalance" in description


def test_risky_and_unapproved_capabilities_say_so() -> None:
    risky = capability(
        id="member.open-savings",
        risk_class=RiskClass.IRREVERSIBLE_WRITE,
        status=ApprovalStatus.DRAFT,
    )
    description = describe_for_agent(risky)

    assert "irreversible_write" in description
    assert "not yet approved" in description

    # ...and an approved read-only capability carries neither warning, so the warnings
    # stay meaningful rather than becoming boilerplate the caller learns to ignore.
    plain = describe_for_agent(capability(status=ApprovalStatus.APPROVED))
    assert "irreversible" not in plain
    assert "not yet approved" not in plain


def test_registry_can_hide_unapproved_capabilities(tmp_path: Path) -> None:
    """The switch a production caller sets: an unattended agent sees only reviewed work."""
    write(tmp_path, capability(status=ApprovalStatus.APPROVED))
    write(tmp_path, capability(id="member.open-savings", status=ApprovalStatus.DRAFT))
    registry = CapabilityRegistry(tmp_path)

    assert len(registry.tool_definitions()) == 2
    approved = registry.tool_definitions(approved_only=True)
    assert [d["name"] for d in approved] == ["member_savings_balance"]


def test_a_corrupt_artifact_does_not_take_down_the_catalogue(tmp_path: Path) -> None:
    """One bad file is a finding about that file, not an outage for every other
    capability. A catalogue that refuses to load at all is worse than one that is short."""
    write(tmp_path, capability())
    (tmp_path / "broken@1.0.0.json").write_text('{"id": "nope"}', encoding="utf-8")

    assert [c.id for c in CapabilityRegistry(tmp_path).load_all()] == ["member.savings-balance"]


def test_render_is_useful_when_there_is_nothing_to_show(tmp_path: Path) -> None:
    assert "cua discover" in render(CapabilityRegistry(tmp_path).load_all())


# --------------------------------------------------------------------------- approval


def test_approval_records_who_and_when(tmp_path: Path) -> None:
    write(tmp_path, capability())

    message = approve_capability(
        capability_id="member.savings-balance", reviewer="ops@cu", capabilities_dir=tmp_path
    )

    stored = CapabilityRegistry(tmp_path).get("member.savings-balance")
    assert stored is not None
    assert stored.status is ApprovalStatus.APPROVED
    assert stored.provenance.reviewed_by == "ops@cu"
    assert stored.provenance.reviewed_at is not None
    assert "approved by ops@cu" in message


def test_approval_says_when_there_is_no_evidence_behind_it(tmp_path: Path) -> None:
    """An approval with no replay history is a signature on an untested thing. The
    command should say that out loud rather than let it pass silently."""
    write(tmp_path, capability())

    message = approve_capability(
        capability_id="member.savings-balance", reviewer="ops@cu", capabilities_dir=tmp_path
    )
    assert "No replay history" in message


def test_approval_is_pinned_to_a_version(tmp_path: Path) -> None:
    """The property that makes approval mean anything: you cannot swap the steps under an
    approval that has already been granted. Re-recording produces a new version, and a new
    version starts at draft like everything else."""
    write(tmp_path, capability())
    approve_capability(
        capability_id="member.savings-balance", reviewer="ops@cu", capabilities_dir=tmp_path
    )
    write(tmp_path, capability(version="1.1.0"))

    registry = CapabilityRegistry(tmp_path)
    by_version = {c.version: c.status for c in registry.load_all()}
    assert by_version == {"1.0.0": ApprovalStatus.APPROVED, "1.1.0": ApprovalStatus.DRAFT}


def test_approval_can_be_withdrawn(tmp_path: Path) -> None:
    """Revocation has to be as easy as granting, or nobody revokes."""
    write(tmp_path, capability())
    approve_capability(
        capability_id="member.savings-balance", reviewer="ops@cu", capabilities_dir=tmp_path
    )
    approve_capability(
        capability_id="member.savings-balance",
        reviewer="ops@cu",
        capabilities_dir=tmp_path,
        undo=True,
    )

    stored = CapabilityRegistry(tmp_path).get("member.savings-balance")
    assert stored is not None
    assert stored.status is ApprovalStatus.DRAFT
    assert stored.provenance.reviewed_by is None


def test_approving_an_unknown_capability_fails_loudly(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        approve_capability(
            capability_id="member.nonexistent", reviewer="ops@cu", capabilities_dir=tmp_path
        )
