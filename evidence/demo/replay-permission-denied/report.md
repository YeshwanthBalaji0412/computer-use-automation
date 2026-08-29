# Replay: member.savings-balance@1.0.0 (business_outcome)

- run: `rep_daab1ccc81aa` (replay)
- events: 28

| # | actor | event | step | detail |
|---|-------|-------|------|--------|
| 1 | automation | run_started |  | capability=member.savings-balance@1.0.0, tenant=meridian |
| 2 | automation | action |  | navigate to the capability entry point |
| 3 | automation | step_started | s1 | sign in as the servicing operator |
| 4 | automation | policy_decision | s1 | decision=allow, rule=default |
| 5 | automation | locator_resolved |  | target=the textbox 'User ID' in the 'Operator Sign In' section, outcome=resolved |
| 6 | automation | action | s1 | sign in as the servicing operator |
| 7 | automation | step_finished | s1 | duration_ms=16 |
| 8 | automation | step_started | s2 | supply the operator password |
| 9 | automation | policy_decision | s2 | decision=allow, rule=default |
| 10 | automation | locator_resolved |  | target=the textbox 'Password' in the 'Operator Sign In' section, outcome=resolved |
| 11 | automation | action | s2 | supply the operator password |
| 12 | automation | step_finished | s2 | duration_ms=28 |
| 13 | automation | step_started | s3 | sign in to the console |
| 14 | automation | policy_decision | s3 | decision=allow, rule=default |
| 15 | automation | locator_resolved |  | target=the button 'Sign In' in the 'Operator Sign In' section, outcome=resolved |
| 16 | automation | action | s3 | sign in to the console |
| 17 | automation | step_finished | s3 | duration_ms=5615 |
| 18 | automation | step_started | s4 | enter the member number to look up |
| 19 | automation | policy_decision | s4 | decision=allow, rule=default |
| 20 | automation | locator_resolved |  | target=the textbox 'Member ID' in the 'Member Search' section (frame contentFrame), outcom |
| 21 | automation | action | s4 | enter the member number to look up |
| 22 | automation | step_finished | s4 | duration_ms=5035 |
| 23 | automation | step_started | s5 | submit the member search |
| 24 | automation | policy_decision | s5 | decision=allow, rule=default |
| 25 | automation | locator_resolved |  | target=the button 'Search' in the 'Member Search' section (frame contentFrame), outcome=re |
| 26 | automation | action | s5 | submit the member search |
| 27 | automation | business_outcome | s5 | code=PERMISSION_DENIED |
| 28 | automation | run_finished |  | status=business_outcome, duration_ms=16835 |
