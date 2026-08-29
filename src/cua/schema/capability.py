"""The capability artifact.

A capability is **a contract, not a macro**. A calling AI agent must be able to answer
four questions from the artifact alone, without reading any code:

    what does this do · what do I pass · what do I get back · what can go wrong

The last one is the part most designs get wrong. The brief names it explicitly: *"'No
such member' is a legitimate answer the caller needs, not a crash. Conflating the two is
the most common design mistake here."* So expected business outcomes are **declared data**
in `known_outcomes`, with their own detectors - not exceptions caught somewhere in an
executor. They appear in the capability's published contract, which means an agent knows
`MEMBER_NOT_FOUND` is a possible answer *before* it ever invokes the thing.

Design decisions worth defending
--------------------------------
1.  `Step.intent` on every step. A reviewer approving automation that touches member
    money reads prose, not JSON.
2.  `Step.target` is a ranked `Locator`, not a selector. Robustness is a property of the
    artifact, so it is inspectable and diffable in review.
3.  `pre_assert` / `post_assert` per step, not just at the end. That is what turns "run
    failed" into "step s3 expected the results table, observed the login page".
4.  Sensitivity is declared on every input and output, so redaction is driven by the
    schema rather than by a regex hoping to catch everything.
5.  Secrets are referenced by name (`secret_ref`), never inlined. It is structurally
    impossible to commit a password inside an artifact.
6.  `risk_class` and `status` are orthogonal. Risk is about the action; status is about
    trust. `irreversible_write` + `draft` never runs unattended.
7.  `policy` here can only ever *narrow* the global policy, never widen it - so a
    tampered artifact cannot grant itself permissions.
8.  `idempotent` is load-bearing, not decoration: it gates retry. A non-idempotent step
    that times out mid-write is *ambiguous*, and retrying could double-post.
9.  URLs are canonicalised to patterns (`{{base_url}}`, `/member/:id`) so an artifact
    describes a *product*, not one tenant's deployment of it.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

from cua.schema.locator import Locator

SCHEMA_VERSION = "1.0.0"


# ---------------------------------------------------------------- typed I/O


class Sensitivity(StrEnum):
    """Drives redaction automatically. Declared once here; enforced everywhere."""

    PUBLIC = "public"
    #: Non-public but not regulated: a branch code, an account nickname.
    INTERNAL = "internal"
    #: Regulated financial data or PII. Masked in logs, artifacts, screenshots, prompts.
    PII = "pii"
    #: Credentials and tokens. Never stored, never shown, never sent to a model.
    SECRET = "secret"


class ParamType(StrEnum):
    STRING = "string"
    INTEGER = "integer"
    NUMBER = "number"
    BOOLEAN = "boolean"
    CURRENCY = "currency"
    DATE = "date"
    ENUM = "enum"


class InputParam(BaseModel):
    """One argument the calling agent supplies per invocation."""

    name: str
    type: ParamType = ParamType.STRING
    required: bool = True
    description: str = ""
    pattern: str | None = Field(
        default=None,
        description="Regex the value must match. Validated before the "
        "browser is even launched, so a malformed id fails fast and cheaply.",
    )
    enum: list[str] | None = None
    default: Any = None
    sensitivity: Sensitivity = Sensitivity.INTERNAL
    example: str | None = Field(
        default=None,
        description="Shown to a calling agent. Must never be real data.",
    )


class OutputField(BaseModel):
    """One value the capability returns, and how to read it off the screen."""

    name: str
    type: ParamType = ParamType.STRING
    required: bool = True
    description: str = ""
    sensitivity: Sensitivity = Sensitivity.INTERNAL
    locator: Locator | None = Field(
        default=None, description="Where to read it. None for values derived from inputs."
    )
    #: How to turn screen text into a typed value. `currency` normalises "$4,182.55" and
    #: "4182.55 USD" to the same number, so the caller gets a value, not a rendering.
    parse: Literal["text", "currency", "integer", "number", "date"] = "text"
    unit: str | None = None


# ---------------------------------------------------------------- assertions


class AssertionKind(StrEnum):
    ELEMENT_PRESENT = "element_present"
    #: "the declared output `name` can be read here". Resolved through the *effective*
    #: output locator at evaluation time, so a tenant overlay that changes where a value
    #: lives is picked up without the success condition itself being overridden.
    OUTPUT_PRESENT = "output_present"
    ELEMENT_ABSENT = "element_absent"
    TEXT_PRESENT = "text_present"
    TEXT_ABSENT = "text_absent"
    URL_MATCHES = "url_matches"
    ALL_OF = "all_of"
    ANY_OF = "any_of"


class Assertion(BaseModel):
    """A checkpoint.

    From the brief's glossary: *"a condition you assert to confirm you actually reached
    the state you expected, rather than assuming the click worked."*
    """

    kind: AssertionKind
    describe: str = ""
    locator: Locator | None = None
    pattern: str | None = Field(default=None, description="Regex for TEXT_* and URL_MATCHES.")
    of: list[Assertion] = Field(default_factory=list, description="For ALL_OF / ANY_OF.")
    output: str | None = Field(default=None, description="Output name, for OUTPUT_PRESENT.")

    @model_validator(mode="after")
    def _check_shape(self) -> Assertion:
        if self.kind in (AssertionKind.ALL_OF, AssertionKind.ANY_OF):
            if not self.of:
                raise ValueError(f"{self.kind} requires a non-empty `of`")
        elif self.kind in (AssertionKind.TEXT_PRESENT, AssertionKind.TEXT_ABSENT):
            if not self.pattern:
                raise ValueError(f"{self.kind} requires `pattern`")
        elif self.kind is AssertionKind.URL_MATCHES:
            if not self.pattern:
                raise ValueError("url_matches requires `pattern`")
        elif self.kind is AssertionKind.OUTPUT_PRESENT:
            if not self.output:
                raise ValueError("output_present requires `output`")
        elif self.locator is None:
            raise ValueError(f"{self.kind} requires a `locator`")
        return self


# ---------------------------------------------------------------- outcomes & recovery


class OutcomeSeverity(StrEnum):
    INFO = "info"
    WARN = "warn"


class KnownOutcome(BaseModel):
    """An expected business result. **Not an error.**

    Declared in the artifact so it is part of the published contract: a calling agent
    reads the capability and learns that `MEMBER_NOT_FOUND` is a thing it must handle,
    rather than discovering it as an exception at 3am.
    """

    code: str = Field(description="Stable, machine-readable, e.g. MEMBER_NOT_FOUND.")
    message: str = Field(description="Human-readable explanation for the caller.")
    detect: Assertion = Field(description="How replay recognises this state on screen.")
    severity: OutcomeSeverity = OutcomeSeverity.INFO
    terminal: bool = Field(default=True, description="Stop the run and return this to the caller.")
    returns: dict[str, Any] = Field(
        default_factory=dict,
        description="Extra structured payload, e.g. {'found': false}.",
    )


class RecoveryActionKind(StrEnum):
    DISMISS = "dismiss"
    WAIT_AND_RETRY = "wait_and_retry"
    RUN_CAPABILITY = "run_capability"
    RELOAD = "reload"


class RecoveryAction(BaseModel):
    kind: RecoveryActionKind
    locator: Locator | None = None
    capability: str | None = Field(
        default=None, description="For RUN_CAPABILITY, e.g. 'auth.login@1'."
    )


class Recovery(BaseModel):
    """A transient condition the capability knows how to get past on its own.

    Every recovery is bounded. An unbounded retry loop is how "deterministic" quietly
    degrades into "eventually consistent", and in a write flow it is how you double-post.
    """

    id: str
    describe: str = ""
    detect: Assertion
    action: RecoveryAction
    max_attempts: int = Field(default=1, ge=1, le=5)
    backoff_ms: list[int] = Field(default_factory=list)
    restart_from_step: str | None = Field(
        default=None,
        description="Step id to resume from, e.g. after re-authenticating.",
    )


# ---------------------------------------------------------------- steps


class ActionKind(StrEnum):
    NAVIGATE = "navigate"
    CLICK = "click"
    FILL = "fill"
    SELECT = "select"
    PRESS = "press"
    EXTRACT = "extract"
    ASSERT = "assert"
    WAIT = "wait"


class RiskClass(StrEnum):
    """What this action does to the world, which decides how conservatively to treat it."""

    READ_ONLY = "read_only"
    #: Changes state, but undoable: a saved search, a preference.
    REVERSIBLE_WRITE = "reversible_write"
    #: Opens an account, moves money, sends a notice. Fail closed.
    IRREVERSIBLE_WRITE = "irreversible_write"


class StepValue(BaseModel):
    """Where a step's value comes from. Exactly one field is set.

    `secret_ref` is the reason this is a model rather than a plain string: credentials
    are resolved from the environment at execution time and referenced by name here, so
    an artifact physically cannot contain one.
    """

    literal: str | None = None
    param: str | None = Field(default=None, description="Name of an InputParam.")
    secret_ref: str | None = Field(default=None, description="Name in the secret store.")

    @model_validator(mode="after")
    def _exactly_one(self) -> StepValue:
        provided = [f for f in (self.literal, self.param, self.secret_ref) if f is not None]
        if len(provided) != 1:
            raise ValueError("StepValue needs exactly one of literal/param/secret_ref")
        return self

    def describe(self) -> str:
        if self.param:
            return f"<param:{self.param}>"
        if self.secret_ref:
            return f"<secret:{self.secret_ref}>"
        return repr(self.literal)


class WaitKind(StrEnum):
    ELEMENT_PRESENT = "element_present"
    ELEMENT_ABSENT = "element_absent"
    NETWORK_IDLE = "network_idle"
    TEXT_PRESENT = "text_present"


class WaitCondition(BaseModel):
    kind: WaitKind
    locator: Locator | None = None
    pattern: str | None = None
    timeout_ms: int = 10_000


class OnFailure(StrEnum):
    #: Hand the observation to the state classifier and let it decide. The default,
    #: because the classifier is where the business/recoverable/hard split lives.
    CLASSIFY = "classify"
    RETRY = "retry"
    SKIP = "skip"
    ESCALATE = "escalate"
    FAIL = "fail"


class Step(BaseModel):
    id: str
    intent: str = Field(
        description="Why this step exists, in plain words. Written for the human who "
        "approves the capability, and reused as the label in failure reports."
    )
    action: ActionKind
    target: Locator | None = None
    control_name: str = Field(
        default="",
        description="Accessible name of the control this step acts on, as recorded. "
        "Kept separately from the locator because the winning strategy may not be a "
        "name-based one - a grid link targeted by row has no name in its locator, but a "
        "reviewer still needs to see that the step clicks 'View', and the risk "
        "classifier still needs to judge it by what it says.",
    )
    value: StepValue | None = None
    url: str | None = Field(default=None, description="For NAVIGATE; a canonical pattern.")
    output: str | None = Field(default=None, description="For EXTRACT: OutputField name.")

    risk: RiskClass = RiskClass.READ_ONLY
    wait_for: list[WaitCondition] = Field(default_factory=list)
    pre_assert: list[Assertion] = Field(
        default_factory=list, description="Guard: are we on the screen we think we are?"
    )
    post_assert: list[Assertion] = Field(
        default_factory=list, description="Checkpoint: did the action actually work?"
    )
    on_failure: OnFailure = OnFailure.CLASSIFY
    timeout_ms: int = 15_000
    retries: int = Field(default=1, ge=0, le=5)

    @model_validator(mode="after")
    def _check_shape(self) -> Step:
        needs_target = {
            ActionKind.CLICK,
            ActionKind.FILL,
            ActionKind.SELECT,
            ActionKind.PRESS,
            ActionKind.EXTRACT,
        }
        if self.action in needs_target and self.target is None:
            raise ValueError(f"step {self.id}: {self.action} requires a target locator")
        if self.action is ActionKind.NAVIGATE and not self.url:
            raise ValueError(f"step {self.id}: navigate requires a url")
        if self.action in (ActionKind.FILL, ActionKind.SELECT) and self.value is None:
            raise ValueError(f"step {self.id}: {self.action} requires a value")
        if self.action is ActionKind.EXTRACT and not self.output:
            raise ValueError(f"step {self.id}: extract requires an output name")
        return self


# ---------------------------------------------------------------- the capability


class ApprovalStatus(StrEnum):
    """Orthogonal to risk. Risk is what the action does; status is how much we trust
    this recording of it. Unattended execution of a write requires both to line up."""

    DRAFT = "draft"
    APPROVED = "approved"
    DEPRECATED = "deprecated"


class ProductRef(BaseModel):
    """The artifact targets a *vendor product*, not a tenant.

    Roughly 100 tenants x 20 apps is 2,000 instances but only ~20 products. The unit of
    authoring therefore has to be the product, with tenants layered on as overlays.
    """

    vendor: str
    app: str
    version_range: str = Field(default="*", description="Semver range, e.g. '>=8.2 <9'.")


class TargetRef(BaseModel):
    surface: Literal["web", "legacy-web", "desktop", "terminal"] = "web"
    product: ProductRef
    entry_url_pattern: str = Field(
        description="Canonicalised, e.g. '{{base_url}}/frame/content?screen=search'."
    )
    requires_auth: bool = True
    auth_capability: str | None = Field(
        default=None, description="Capability used to (re-)authenticate, e.g. 'auth.login@1'."
    )


class CapabilityPolicy(BaseModel):
    """Per-capability limits. Intersected with the global policy at run time - a
    capability can only narrow what it is allowed to do, never widen it."""

    allowed_origins: list[str] = Field(default_factory=list)
    allowed_actions: list[ActionKind] = Field(default_factory=list)
    max_steps: int = 40
    max_duration_ms: int = 120_000


class Provenance(BaseModel):
    discovered_by: Literal["llm", "human", "import"] = "llm"
    model: str | None = None
    prompt_version: str | None = None
    discovery_run_id: str | None = None
    recorded_at: datetime | None = None
    recorded_against_tenant: str | None = None
    surface_fingerprint: str | None = Field(
        default=None,
        description="Skeleton hash of the ENTRY screen at record time. Compared against "
        "the entry screen on every replay - the same screen, or the comparison is "
        "meaningless. A mismatch marks the run drift-suspected without failing it.",
    )
    reviewed_by: str | None = None
    reviewed_at: datetime | None = None


class Stability(BaseModel):
    """Evidence for the approval decision. Makes 'safe to run unattended' measurable
    rather than a matter of opinion."""

    replays: int = 0
    successes: int = 0
    last_verified_at: datetime | None = None
    locator_tier_histogram: dict[str, int] = Field(default_factory=dict)

    @property
    def success_rate(self) -> float:
        return self.successes / self.replays if self.replays else 0.0


class Capability(BaseModel):
    """A reusable, reviewable, parameterised unit of work an AI agent can invoke."""

    schema_version: str = SCHEMA_VERSION
    id: str = Field(description="Namespaced, e.g. 'member.savings-balance'.")
    version: str = Field(
        description="Semver. patch = locator repair, minor = additive input/output, "
        "major = contract change."
    )
    display_name: str
    description: str = Field(
        description="What this does, in the words a calling agent will see in its tool "
        "catalog. This is the capability's docstring for an LLM."
    )

    target: TargetRef
    status: ApprovalStatus = ApprovalStatus.DRAFT
    risk_class: RiskClass = RiskClass.READ_ONLY
    idempotent: bool = Field(
        default=True,
        description="Gates retry. A non-idempotent step that times out mid-write is "
        "ambiguous - the write may or may not have landed - so replay must escalate "
        "rather than retry and risk double-posting.",
    )

    inputs: list[InputParam] = Field(default_factory=list)
    outputs: list[OutputField] = Field(default_factory=list)

    preconditions: list[Assertion] = Field(default_factory=list)
    preflight: list[Step] = Field(
        default_factory=list,
        description="Optional duplicate check run before a non-idempotent write, so "
        "invoking the capability twice returns DUPLICATE_RECORD instead of doing it twice.",
    )
    steps: list[Step] = Field(default_factory=list)
    success_condition: Assertion | None = None

    known_outcomes: list[KnownOutcome] = Field(default_factory=list)
    recoveries: list[Recovery] = Field(default_factory=list)

    policy: CapabilityPolicy = Field(default_factory=CapabilityPolicy)
    provenance: Provenance = Field(default_factory=Provenance)
    stability: Stability = Field(default_factory=Stability)

    @property
    def qualified_name(self) -> str:
        return f"{self.id}@{self.version}"

    @model_validator(mode="after")
    def _check_references(self) -> Capability:
        """Catch dangling references at load time rather than mid-run in a browser."""
        param_names = {p.name for p in self.inputs}
        output_names = {o.name for o in self.outputs}
        step_ids = {s.id for s in self.steps}

        for step in list(self.steps) + list(self.preflight):
            if step.value and step.value.param and step.value.param not in param_names:
                raise ValueError(
                    f"step {step.id} references unknown input param {step.value.param!r}"
                )
            if step.action is ActionKind.EXTRACT and step.output not in output_names:
                raise ValueError(f"step {step.id} extracts unknown output {step.output!r}")
        for recovery in self.recoveries:
            target = recovery.restart_from_step
            if target and target not in step_ids:
                raise ValueError(f"recovery {recovery.id} restarts from unknown step {target!r}")

        codes = [o.code for o in self.known_outcomes]
        if len(codes) != len(set(codes)):
            raise ValueError("known_outcome codes must be unique")

        if len({s.id for s in self.steps}) != len(self.steps):
            raise ValueError("step ids must be unique")
        return self

    def effective_locator(self, step: Step) -> Locator | None:
        """The locator a step will really use.

        For an EXTRACT step this is the *output's* locator, not the step's own. The two
        can differ once a tenant overlay is applied - an institution that renders a
        balance in a definition list instead of a table overrides the output - and any
        component that consults `step.target` directly will disagree with the executor
        about where the value is. Having one method say so is what stopped the
        conformance sweep reporting a false failure for a step that replays fine.
        """
        if step.action is ActionKind.EXTRACT and step.output:
            field = next((o for o in self.outputs if o.name == step.output), None)
            if field is not None and field.locator is not None:
                return field.locator
        return step.target

    def input_json_schema(self) -> dict[str, Any]:
        """The agent-facing argument contract.

        Emitted from the same declaration that types the code and validates artifacts at
        load - one source of truth, four consumers. This is what gets handed to an LLM as
        a tool's `input_schema`.
        """
        properties: dict[str, Any] = {}
        required: list[str] = []
        for param in self.inputs:
            prop: dict[str, Any] = {"description": param.description}
            if param.type in (ParamType.INTEGER,):
                prop["type"] = "integer"
            elif param.type in (ParamType.NUMBER, ParamType.CURRENCY):
                prop["type"] = "number"
            elif param.type is ParamType.BOOLEAN:
                prop["type"] = "boolean"
            else:
                prop["type"] = "string"
            if param.pattern:
                prop["pattern"] = param.pattern
            if param.enum:
                prop["enum"] = param.enum
            if param.example is not None:
                prop["examples"] = [param.example]
            properties[param.name] = prop
            if param.required:
                required.append(param.name)

        return {
            "type": "object",
            "properties": properties,
            "required": required,
            "additionalProperties": False,
        }
