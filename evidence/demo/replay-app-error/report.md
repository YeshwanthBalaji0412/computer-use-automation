# Replay: member.savings-balance@1.0.0 (business_outcome)

- run: `rep_4e086d484253` (replay)
- events: 18

| # | actor | event | step | detail |
|---|-------|-------|------|--------|
| 1 | automation | run_started |  | capability=member.savings-balance@1.0.0, tenant=meridian |
| 2 | automation | action |  | navigate to the capability entry point |
| 3 | automation | step_started | s1 | sign in as the servicing operator |
| 4 | automation | policy_decision | s1 | decision=allow, rule=default |
| 5 | automation | locator_resolved |  | target=the textbox 'User ID' in the 'Operator Sign In' section, outcome=resolved |
| 6 | automation | action | s1 | sign in as the servicing operator |
| 7 | automation | step_finished | s1 | duration_ms=15 |
| 8 | automation | step_started | s2 | supply the operator password |
| 9 | automation | policy_decision | s2 | decision=allow, rule=default |
| 10 | automation | locator_resolved |  | target=the textbox 'Password' in the 'Operator Sign In' section, outcome=resolved |
| 11 | automation | action | s2 | supply the operator password |
| 12 | automation | step_finished | s2 | duration_ms=29 |
| 13 | automation | step_started | s3 | sign in to the console |
| 14 | automation | policy_decision | s3 | decision=allow, rule=default |
| 15 | automation | locator_resolved |  | target=the button 'Sign In' in the 'Operator Sign In' section, outcome=resolved |
| 16 | automation | action | s3 | sign in to the console |
| 17 | automation | business_outcome | s3 | code=APP_ERROR |
| 18 | automation | run_finished |  | status=business_outcome, duration_ms=6230 |
