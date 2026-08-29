# Replay: member.savings-balance@1.0.0 (business_outcome)

- run: `rep_a3c5911d4555` (replay)
- events: 18

| # | actor | event | step | detail |
|---|-------|-------|------|--------|
| 1 | automation | run_started |  | capability=member.savings-balance@1.0.0, tenant=meridian |
| 2 | automation | action |  | navigate to the capability entry point |
| 3 | automation | step_started | s1 | Enter user ID to sign in. |
| 4 | automation | policy_decision | s1 | decision=allow, rule=default |
| 5 | automation | locator_resolved |  | target=the textbox 'User ID' in the 'Operator Sign In' section, outcome=resolved |
| 6 | automation | action | s1 | Enter user ID to sign in. |
| 7 | automation | step_finished | s1 | duration_ms=48 |
| 8 | automation | step_started | s2 | Enter password to sign in. |
| 9 | automation | policy_decision | s2 | decision=allow, rule=default |
| 10 | automation | locator_resolved |  | target=the textbox 'Password' in the 'Operator Sign In' section, outcome=resolved |
| 11 | automation | action | s2 | Enter password to sign in. |
| 12 | automation | step_finished | s2 | duration_ms=64 |
| 13 | automation | step_started | s3 | Submit credentials to sign in. |
| 14 | automation | policy_decision | s3 | decision=allow, rule=default |
| 15 | automation | locator_resolved |  | target=the button 'Sign In' in the 'Operator Sign In' section, outcome=resolved |
| 16 | automation | action | s3 | Submit credentials to sign in. |
| 17 | automation | business_outcome | s3 | code=APP_ERROR |
| 18 | automation | run_finished |  | status=business_outcome, duration_ms=6310 |
