"""Fake member records.

Every value here is invented. No real names, no real account numbers, no real PII —
the brief is explicit that we never use real credentials or real data, and the whole
point of owning the target app is that we never have to.

Specific IDs are reserved to trigger *business outcomes* — legitimate answers the
caller needs, not failures. These need no fault-injection machinery because they are
simply how the app behaves:

    999999  -> no such member
    100777  -> exists, but the operator's role cannot view it
    non-6-digit -> server-side validation rejects the input
"""

from __future__ import annotations

from typing import TypedDict


class Member(TypedDict):
    member_id: str
    name: str
    savings_balance: str
    checking_balance: str
    status: str
    branch: str
    joined: str


MEMBERS: dict[str, Member] = {
    "100042": {
        "member_id": "100042",
        "name": "J. RIVERA",
        "savings_balance": "4,182.55",
        "checking_balance": "1,203.11",
        "status": "ACTIVE",
        "branch": "MAIN",
        "joined": "03/14/2016",
    },
    "100043": {
        "member_id": "100043",
        "name": "M. CHEN",
        "savings_balance": "912.10",
        "checking_balance": "88.42",
        "status": "DORMANT",
        "branch": "NORTHGATE",
        "joined": "11/02/2019",
    },
    "100108": {
        "member_id": "100108",
        "name": "A. OKONKWO",
        "savings_balance": "15,340.00",
        "checking_balance": "2,776.90",
        "status": "ACTIVE",
        "branch": "MAIN",
        "joined": "07/21/2011",
    },
}

#: Exists in the core, but this operator's role is not entitled to it.
RESTRICTED_MEMBER_IDS = {"100777"}

#: Sub-accounts already open, keyed by member id. Drives the DUPLICATE_RECORD
#: business outcome so that opening a sub-account is safe to attempt twice.
EXISTING_SUBACCOUNTS: dict[str, set[str]] = {
    "100042": {"VACATION FUND"},
}

BRANCHES = ["MAIN", "NORTHGATE", "WESTSIDE"]
SUBACCOUNT_TYPES = ["SAVINGS", "MONEY MARKET", "CERTIFICATE"]


def lookup(member_id: str) -> Member | None:
    return MEMBERS.get(member_id)


def is_restricted(member_id: str) -> bool:
    return member_id in RESTRICTED_MEMBER_IDS


def is_valid_member_id(raw: str) -> bool:
    """Six digits. Anything else is a validation error, not a 'not found'."""
    return len(raw) == 6 and raw.isdigit()
