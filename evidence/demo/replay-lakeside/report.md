# Replay: member.savings-balance@1.0.0 (success)

- run: `rep_9f5c4cfd1889` (replay)
- events: 45

| # | actor | event | step | detail |
|---|-------|-------|------|--------|
| 1 | automation | run_started |  | tenant_overlay=lakeside, steps_added=1 |
| 2 | automation | run_started |  | capability=member.savings-balance@1.0.0, tenant=lakeside |
| 3 | automation | action |  | navigate to the capability entry point |
| 4 | automation | step_started | s1 | Enter user ID to sign in. |
| 5 | automation | policy_decision | s1 | decision=allow, rule=default |
| 6 | automation | locator_resolved |  | target=the textbox 'User ID' in the 'Operator Sign In' section, outcome=resolved |
| 7 | automation | action | s1 | Enter user ID to sign in. |
| 8 | automation | step_finished | s1 | duration_ms=42 |
| 9 | automation | step_started | s2 | Enter password to sign in. |
| 10 | automation | policy_decision | s2 | decision=allow, rule=default |
| 11 | automation | locator_resolved |  | target=the textbox 'Password' in the 'Operator Sign In' section, outcome=resolved |
| 12 | automation | action | s2 | Enter password to sign in. |
| 13 | automation | step_finished | s2 | duration_ms=64 |
| 14 | automation | step_started | s3 | Submit credentials to sign in. |
| 15 | automation | policy_decision | s3 | decision=allow, rule=default |
| 16 | automation | locator_resolved |  | target=the button 'Sign In' in the 'Operator Sign In' section, outcome=resolved |
| 17 | automation | action | s3 | Submit credentials to sign in. |
| 18 | automation | step_finished | s3 | duration_ms=5668 |
| 19 | automation | step_started | s4 | Enter member ID to search for member details. |
| 20 | automation | policy_decision | s4 | decision=allow, rule=default |
| 21 | automation | locator_resolved |  | target=the textbox 'Member ID' in the 'Member Search' section (frame contentFrame), outcom |
| 22 | automation | locator_degraded | s4 | winning_tier=3, tiers_tried=[1, 2, 3, 3, 5, 6] |
| 23 | automation | action | s4 | Enter member ID to search for member details. |
| 24 | automation | step_finished | s4 | duration_ms=5069 |
| 25 | automation | step_started | lakeside-branch | Lakeside requires a branch before it will run a member search |
| 26 | automation | policy_decision | lakeside-branch | decision=allow, rule=default |
| 27 | automation | locator_resolved |  | target=the Branch dropdown in the Member Search form, outcome=resolved |
| 28 | automation | action | lakeside-branch | Lakeside requires a branch before it will run a member search |
| 29 | automation | step_finished | lakeside-branch | duration_ms=5515 |
| 30 | automation | step_started | s5 | Search for the member with ID 100042. |
| 31 | automation | policy_decision | s5 | decision=allow, rule=default |
| 32 | automation | locator_resolved |  | target=the button 'Search' in the 'Member Search' section (frame contentFrame), outcome=re |
| 33 | automation | action | s5 | Search for the member with ID 100042. |
| 34 | automation | step_finished | s5 | duration_ms=5622 |
| 35 | automation | step_started | s6 | View details for member 100042. |
| 36 | automation | policy_decision | s6 | decision=allow, rule=default |
| 37 | automation | locator_resolved |  | target=the link 'View' in the row where Member ID = 100042, Name = J. RIVERA (frame conten |
| 38 | automation | action | s6 | View details for member 100042. |
| 39 | automation | step_finished | s6 | duration_ms=5627 |
| 40 | automation | step_started | s7 | Retrieve the current savings balance for member 100042. |
| 41 | automation | policy_decision | s7 | decision=allow, rule=default |
| 42 | automation | locator_resolved |  | target=the Balance cell in the row where Account = Savings (frame contentFrame), outcome=r |
| 43 | automation | action | s7 | extracted=savingsBalance |
| 44 | automation | step_finished | s7 | duration_ms=32 |
| 45 | automation | run_finished |  | status=success, duration_ms=28187 |
