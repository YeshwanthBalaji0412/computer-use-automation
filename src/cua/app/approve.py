"""Human review of a recorded capability.

`risk_class` and `status` are deliberately orthogonal. Risk is a property of the *action*
- opening an account is irreversible no matter who recorded it. Status is how much we
trust *this recording* of that action. Unattended execution of a write needs both to line
up, and only a person can supply the second one.

So this is the smallest possible thing that is still real: a named human, a timestamp, and
a state transition written back into the artifact. It is what turns a `blocked` replay
into a `success` one, and it is the reason `blocked` is a distinct result variant rather
than a kind of failure - the run did not go wrong, it was correctly refused pending this.

Deliberately *not* here: an approval workflow, a queue, roles, or a second signature. Those
are the right shape for production and the wrong shape for a take-home. See `## Cuts`.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from cua.schema.capability import ApprovalStatus, Capability, RiskClass


def approve_capability(
    *,
    capability_id: str,
    reviewer: str,
    capabilities_dir: Path,
    undo: bool = False,
) -> str:
    """Flip a capability between `draft` and `approved`, recording who did it.

    Writes back to the same versioned file the artifact was loaded from. Approval is
    pinned to a version by construction: recording the flow again produces a new version,
    which starts at `draft` like everything else. That is the property you want - you
    cannot silently swap the steps under an approval that has already been granted.
    """
    path = _resolve(capability_id, capabilities_dir)
    capability = Capability.model_validate_json(path.read_text(encoding="utf-8"))

    if undo:
        capability.status = ApprovalStatus.DRAFT
        capability.provenance.reviewed_by = None
        capability.provenance.reviewed_at = None
        verb = "returned to draft"
    else:
        if capability.status is ApprovalStatus.APPROVED:
            return (
                f"{capability.qualified_name} is already approved "
                f"(by {capability.provenance.reviewed_by or 'unknown'})."
            )
        capability.status = ApprovalStatus.APPROVED
        capability.provenance.reviewed_by = reviewer
        capability.provenance.reviewed_at = datetime.now(UTC)
        verb = "approved"

    path.write_text(capability.model_dump_json(indent=2), encoding="utf-8")

    lines = [f"{capability.qualified_name} {verb} by {reviewer}.", _evidence_line(capability)]
    if not undo and capability.risk_class is not RiskClass.READ_ONLY:
        lines.append(
            f"  This capability is {capability.risk_class}. Replay will now run its "
            f"risky steps, and still requires --approve on each run."
        )
    return "\n".join(line for line in lines if line)


def _evidence_line(capability: Capability) -> str:
    """What the reviewer is deciding on. An approval with no replay history behind it is
    a signature on an untested thing, and the command should say so out loud."""
    stability = capability.stability
    if not stability.replays:
        return "  No replay history yet - approving on inspection alone."
    return (
        f"  Evidence: {stability.successes}/{stability.replays} replays succeeded"
        f" ({stability.success_rate:.0%})."
    )


def _resolve(capability_id: str, capabilities_dir: Path) -> Path:
    if "@" in capability_id:
        path = capabilities_dir / f"{capability_id}.json"
        if not path.exists():
            raise FileNotFoundError(f"no capability at {path}")
        return path
    matches = sorted(capabilities_dir.glob(f"{capability_id}@*.json"))
    if not matches:
        raise FileNotFoundError(f"no capability matching {capability_id!r} in {capabilities_dir}")
    return matches[-1]
