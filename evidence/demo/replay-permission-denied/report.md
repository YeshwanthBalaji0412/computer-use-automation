# Replay: member.savings-balance@1.0.0 (business_outcome)

- run: `rep_fd9e9069e5a4` (replay)
- events: 28

| # | actor | event | step | detail |
|---|-------|-------|------|--------|
| 1 | automation | run_started |  | capability=member.savings-balance@1.0.0, tenant=meridian |
| 2 | automation | action |  | navigate to the capability entry point |
| 3 | automation | step_started | s1 | Enter user ID to sign in. |
| 4 | automation | policy_decision | s1 | decision=allow, rule=default |
| 5 | automation | locator_resolved |  | target=the textbox 'User ID' in the 'Operator Sign In' section, outcome=resolved |
| 6 | automation | action | s1 | Enter user ID to sign in. |
| 7 | automation | step_finished | s1 | duration_ms=16 |
| 8 | automation | step_started | s2 | Enter password to sign in. |
| 9 | automation | policy_decision | s2 | decision=allow, rule=default |
| 10 | automation | locator_resolved |  | target=the textbox 'Password' in the 'Operator Sign In' section, outcome=resolved |
| 11 | automation | action | s2 | Enter password to sign in. |
| 12 | automation | step_finished | s2 | duration_ms=27 |
| 13 | automation | step_started | s3 | Submit credentials to sign in. |
| 14 | automation | policy_decision | s3 | decision=allow, rule=default |
| 15 | automation | locator_resolved |  | target=the button 'Sign In' in the 'Operator Sign In' section, outcome=resolved |
| 16 | automation | action | s3 | Submit credentials to sign in. |
| 17 | automation | step_finished | s3 | duration_ms=5594 |
| 18 | automation | step_started | s4 | Enter member ID to search for member details. |
| 19 | automation | policy_decision | s4 | decision=allow, rule=default |
| 20 | automation | locator_resolved |  | target=the textbox 'Member ID' in the 'Member Search' section (frame contentFrame), outcom |
| 21 | automation | action | s4 | Enter member ID to search for member details. |
| 22 | automation | step_finished | s4 | duration_ms=5033 |
| 23 | automation | step_started | s5 | Search for the member with ID 100042. |
| 24 | automation | policy_decision | s5 | decision=allow, rule=default |
| 25 | automation | locator_resolved |  | target=the button 'Search' in the 'Member Search' section (frame contentFrame), outcome=re |
| 26 | automation | action | s5 | Search for the member with ID 100042. |
| 27 | automation | business_outcome | s5 | code=PERMISSION_DENIED |
| 28 | automation | run_finished |  | status=business_outcome, duration_ms=16824 |
