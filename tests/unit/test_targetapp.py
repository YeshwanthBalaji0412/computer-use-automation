"""Tests for the target app itself.

The target app is test scaffolding, but it has to be *correct* scaffolding: every
assertion the replay engine's error taxonomy makes later depends on this app producing
exactly the state it claims to. In particular `test_control_ids_churn_between_renders`
is load-bearing for the whole project - if ids were stable, recording a selector would
work and the semantic locator ladder would be unjustified.
"""

from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient
from targetapp.app import app

pytestmark = pytest.mark.unit


def client() -> TestClient:
    return TestClient(app, follow_redirects=False)


def signed_in(slug: str = "meridian") -> TestClient:
    c = client()
    r = c.post(f"/tenants/{slug}/login", data={"username": "operator", "password": "x"})
    assert r.status_code == 302
    return c


# ---------------------------------------------------------------- the premise


def test_control_ids_churn_between_renders() -> None:
    """The core justification for semantic locators: ids are not stable.

    Two renders of the same screen must produce different control ids. Any automation
    that recorded one would break on the second run.
    """
    c = signed_in()
    pattern = re.compile(r"ctl00_ContentPlaceHolder1_frmSearch_ctl(\d{2})_btnSearch")

    seen = set()
    for _ in range(8):
        body = c.get("/tenants/meridian/frame/content?screen=search").text
        found = pattern.findall(body)
        assert found, "expected a churning ASP.NET-style control id on the search screen"
        seen.add(found[0])

    assert len(seen) > 1, f"ids did not churn across renders: {seen}"


def test_no_test_ids_anywhere() -> None:
    """Legacy enterprise apps essentially never have test ids. Neither does this one."""
    c = signed_in()
    for url in (
        "/tenants/meridian/frame/content?screen=search",
        "/tenants/meridian/frame/content?screen=detail&memberId=100042",
    ):
        body = c.get(url).text
        assert "data-testid" not in body
        assert "data-test" not in body


def test_home_is_a_real_frameset_with_nested_content() -> None:
    c = signed_in()
    body = c.get("/tenants/meridian/home").text
    assert "<frameset" in body
    assert 'name="navFrame"' in body
    assert 'name="contentFrame"' in body
    assert "<body" not in body.lower().split("<noframes>")[0]


# ---------------------------------------------------------------- flow A


def test_search_happy_path_reaches_results_then_detail() -> None:
    c = signed_in()
    r = c.post("/tenants/meridian/frame/search", data={"memberId": "100042"})
    assert r.status_code == 200
    assert "Search Results" in r.text
    assert "J. RIVERA" in r.text
    assert ">View<" in r.text

    d = c.get("/tenants/meridian/frame/content?screen=detail&memberId=100042")
    assert d.status_code == 200
    assert "Member Detail" in d.text
    assert "4,182.55" in d.text


def test_login_is_required_for_content() -> None:
    r = client().get("/tenants/meridian/frame/content?screen=search")
    assert "Session Ended" in r.text or "Sign In" in r.text


# ---------------------------------------------------------------- business outcomes


def test_not_found_is_http_200_not_an_error() -> None:
    """'No such member' is a legitimate answer, so the app must not signal it as a fault."""
    c = signed_in()
    r = c.post("/tenants/meridian/frame/search", data={"memberId": "999999"})
    assert r.status_code == 200
    assert "No member records found" in r.text


def test_permission_denied_is_distinct_from_not_found() -> None:
    c = signed_in()
    r = c.post("/tenants/meridian/frame/search", data={"memberId": "100777"})
    assert r.status_code == 200
    assert "not authorized" in r.text.lower()
    assert "No member records found" not in r.text


@pytest.mark.parametrize("bad", ["ABC", "12", "1000422", "10a042"])
def test_validation_error_for_malformed_id(bad: str) -> None:
    c = signed_in()
    r = c.post("/tenants/meridian/frame/search", data={"memberId": bad})
    assert r.status_code == 200
    assert "Invalid Member ID" in r.text


# ---------------------------------------------------------------- injected faults


def test_server_error_is_a_hard_failure_with_a_5xx() -> None:
    c = signed_in()
    r = c.get(
        "/tenants/meridian/frame/content?screen=search",
        headers={"X-CUA-Fault": "500"},
    )
    assert r.status_code == 500
    assert "Server Error" in r.text


def test_known_interstitial_appears_once_then_lets_a_retry_through() -> None:
    c = signed_in()
    first = c.post(
        "/tenants/meridian/frame/search",
        data={"memberId": "100042"},
        headers={"X-CUA-Fault": "interstitial"},
    )
    assert 'role="dialog"' in first.text
    assert "Scheduled Maintenance" in first.text

    second = c.post(
        "/tenants/meridian/frame/search",
        data={"memberId": "100042"},
        headers={"X-CUA-Fault": "interstitial"},
    )
    assert "Scheduled Maintenance" not in second.text


def test_unknown_dialog_is_a_different_modal_from_the_known_one() -> None:
    """Replay must be able to tell a declared interstitial from an undeclared one."""
    c = signed_in()
    r = c.get(
        "/tenants/meridian/frame/content?screen=detail&memberId=100042",
        headers={"X-CUA-Fault": "unknown-dialog"},
    )
    assert 'role="dialog"' in r.text
    assert "Regulation CC Hold Notice" in r.text
    assert "Scheduled Maintenance" not in r.text


def test_session_expiry_bounces_the_frame_not_the_top_level_url() -> None:
    """The nasty one: the address bar never changes, so detection must be visual."""
    c = signed_in()
    r = c.get(
        "/tenants/meridian/frame/content?screen=detail&memberId=100042",
        headers={"X-CUA-Fault": "expire"},
    )
    assert r.status_code == 200
    assert "Session Ended" in r.text
    assert "timed out" in r.text.lower()


# ---------------------------------------------------------------- flow B (write)


def test_subaccount_flow_reaches_a_confirmation_number() -> None:
    c = signed_in()
    review = c.post(
        "/tenants/meridian/frame/subaccount/review",
        data={"memberId": "100043", "nickname": "Rainy Day", "acctType": "SAVINGS"},
    )
    assert "Review Sub-Account Request" in review.text
    assert "Confirm and Open Account" in review.text

    done = c.post(
        "/tenants/meridian/frame/subaccount/confirm",
        data={"memberId": "100043", "nickname": "Rainy Day", "acctType": "SAVINGS"},
    )
    assert "Sub-Account Opened" in done.text
    assert re.search(r"SA-\d{6}", done.text)


def test_duplicate_subaccount_is_a_business_outcome_not_a_second_account() -> None:
    """Pre-flight check: makes a non-idempotent capability safe to attempt twice."""
    c = signed_in()
    r = c.post(
        "/tenants/meridian/frame/subaccount/review",
        data={"memberId": "100042", "nickname": "vacation fund", "acctType": "SAVINGS"},
    )
    assert "already exists" in r.text
    assert "No new account was created" in r.text


def test_write_timeout_is_ambiguous_and_must_not_look_like_a_clean_failure() -> None:
    c = signed_in()
    r = c.post(
        "/tenants/meridian/frame/subaccount/confirm",
        data={"memberId": "100108", "nickname": "AMBIG", "acctType": "SAVINGS"},
        headers={"X-CUA-Fault": "write-timeout"},
    )
    assert r.status_code == 500


# ---------------------------------------------------------------- tenant variance


def test_tenants_differ_in_the_ways_that_break_naive_locators() -> None:
    m = signed_in("meridian").get("/tenants/meridian/frame/content?screen=search").text
    lk = signed_in("lakeside").get("/tenants/lakeside/frame/content?screen=search").text

    assert 'value="Search"' in m and "Member ID" in m
    assert 'value="Find Member"' in lk and "Member Number" in lk

    # Lakeside adds a required step, not just a rename.
    assert "Branch" not in m.split("Servicing Console")[0] or "ddlBranch" not in m
    assert "ddlBranch" in lk


def test_lakeside_requires_branch_selection() -> None:
    c = signed_in("lakeside")
    r = c.post("/tenants/lakeside/frame/search", data={"memberId": "100042"})
    assert "Branch selection is required" in r.text

    ok = c.post("/tenants/lakeside/frame/search", data={"memberId": "100042", "branch": "MAIN"})
    assert "Search Results" in ok.text


def test_lakeside_renders_the_detail_screen_with_different_markup() -> None:
    c = signed_in("lakeside")
    r = c.get("/tenants/lakeside/frame/content?screen=detail&memberId=100042")
    assert "<dl" in r.text
    assert "4,182.55" in r.text

    m = signed_in("meridian").get("/tenants/meridian/frame/content?screen=detail&memberId=100042")
    assert "<dl" not in m.text
    assert "4,182.55" in m.text
