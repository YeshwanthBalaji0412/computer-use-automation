# Replay: member.savings-balance@1.0.0 (success)

- run: `rep_1525697c7567` (replay)
- events: 38

| # | actor | event | step | detail |
|---|-------|-------|------|--------|
| 1 | automation | run_started |  | capability=member.savings-balance@1.0.0, tenant=meridian |
| 2 | automation | action |  | navigate to the capability entry point |
| 3 | automation | step_started | s1 | Enter user ID to sign in. |
| 4 | automation | policy_decision | s1 | decision=allow, rule=default |
| 5 | automation | locator_resolved |  | target=the textbox 'User ID' in the 'Operator Sign In' section, outcome=resolved |
| 6 | automation | action | s1 | Enter user ID to sign in. |
| 7 | automation | step_finished | s1 | duration_ms=51 |
| 8 | automation | step_started | s2 | Enter password to sign in. |
| 9 | automation | policy_decision | s2 | decision=allow, rule=default |
| 10 | automation | locator_resolved |  | target=the textbox 'Password' in the 'Operator Sign In' section, outcome=resolved |
| 11 | automation | action | s2 | Enter password to sign in. |
| 12 | automation | step_finished | s2 | duration_ms=62 |
| 13 | automation | step_started | s3 | Submit credentials to sign in. |
| 14 | automation | policy_decision | s3 | decision=allow, rule=default |
| 15 | automation | locator_resolved |  | target=the button 'Sign In' in the 'Operator Sign In' section, outcome=resolved |
| 16 | automation | action | s3 | Submit credentials to sign in. |
| 17 | automation | step_finished | s3 | duration_ms=5668 |
| 18 | automation | step_started | s4 | Enter member ID to search for member details. |
| 19 | automation | policy_decision | s4 | decision=allow, rule=default |
| 20 | automation | locator_resolved |  | target=the textbox 'Member ID' in the 'Member Search' section (frame contentFrame), outcom |
| 21 | automation | action | s4 | Enter member ID to search for member details. |
| 22 | automation | step_finished | s4 | duration_ms=5051 |
| 23 | automation | step_started | s5 | Search for the member with ID 100042. |
| 24 | automation | policy_decision | s5 | decision=allow, rule=default |
| 25 | automation | locator_resolved |  | target=the button 'Search' in the 'Member Search' section (frame contentFrame), outcome=re |
| 26 | automation | action | s5 | Search for the member with ID 100042. |
| 27 | automation | step_finished | s5 | duration_ms=5621 |
| 28 | automation | step_started | s6 | View details for member 100042. |
| 29 | automation | policy_decision | s6 | decision=allow, rule=default |
| 30 | automation | locator_resolved |  | target=the link 'View' in the row where Member ID = 100042, Name = J. RIVERA (frame conten |
| 31 | automation | action | s6 | View details for member 100042. |
| 32 | automation | step_finished | s6 | duration_ms=5626 |
| 33 | automation | step_started | s7 | Retrieve the current savings balance for member 100042. |
| 34 | automation | policy_decision | s7 | decision=allow, rule=default |
| 35 | automation | locator_resolved |  | target=the Balance cell in the row where Account = Savings (frame contentFrame), outcome=r |
| 36 | automation | action | s7 | extracted=savingsBalance |
| 37 | automation | step_finished | s7 | duration_ms=34 |
| 38 | automation | run_finished |  | status=success, duration_ms=22684 |
