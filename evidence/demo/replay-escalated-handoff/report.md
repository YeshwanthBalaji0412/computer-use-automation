# Replay: member.savings-balance@1.0.0 (success)

- run: `rep_87fd7bc8fc4c` (replay)
- events: 38

| # | actor | event | step | detail |
|---|-------|-------|------|--------|
| 1 | automation | run_started |  | capability=member.savings-balance@1.0.0, tenant=meridian |
| 2 | automation | action |  | navigate to the capability entry point |
| 3 | automation | step_started | s1 | sign in as the servicing operator |
| 4 | automation | policy_decision | s1 | decision=allow, rule=default |
| 5 | automation | locator_resolved |  | target=the textbox 'User ID' in the 'Operator Sign In' section, outcome=resolved |
| 6 | automation | action | s1 | sign in as the servicing operator |
| 7 | automation | step_finished | s1 | duration_ms=14 |
| 8 | automation | step_started | s2 | supply the operator password |
| 9 | automation | policy_decision | s2 | decision=allow, rule=default |
| 10 | automation | locator_resolved |  | target=the textbox 'Password' in the 'Operator Sign In' section, outcome=resolved |
| 11 | automation | action | s2 | supply the operator password |
| 12 | automation | step_finished | s2 | duration_ms=28 |
| 13 | automation | step_started | s3 | sign in to the console |
| 14 | automation | policy_decision | s3 | decision=allow, rule=default |
| 15 | automation | locator_resolved |  | target=the button 'Sign In' in the 'Operator Sign In' section, outcome=resolved |
| 16 | automation | action | s3 | sign in to the console |
| 17 | automation | step_finished | s3 | duration_ms=5612 |
| 18 | automation | step_started | s4 | enter the member number to look up |
| 19 | automation | policy_decision | s4 | decision=allow, rule=default |
| 20 | automation | locator_resolved |  | target=the textbox 'Member ID' in the 'Member Search' section (frame contentFrame), outcom |
| 21 | automation | action | s4 | enter the member number to look up |
| 22 | automation | step_finished | s4 | duration_ms=5032 |
| 23 | automation | step_started | s5 | submit the member search |
| 24 | automation | policy_decision | s5 | decision=allow, rule=default |
| 25 | automation | locator_resolved |  | target=the button 'Search' in the 'Member Search' section (frame contentFrame), outcome=re |
| 26 | automation | action | s5 | submit the member search |
| 27 | automation | step_finished | s5 | duration_ms=5605 |
| 28 | automation | step_started | s6 | open the matching member's detail screen |
| 29 | automation | policy_decision | s6 | decision=allow, rule=default |
| 30 | automation | locator_resolved |  | target=the link 'View' in the row where Member ID = 100042, Name = J. RIVERA (frame conten |
| 31 | automation | action | s6 | open the matching member's detail screen |
| 32 | automation | escalation_raised | s6 | reason=unknown_dialog, detail=a dialog titled 'Regulation CC Hold Notice' appeared and is  |
| 33 | system | control_transferred | s6 | to=human, epoch=1 |
| 34 | human | human_action | s6 | detail=click button 'Acknowledge' |
| 35 | system | control_transferred |  | to=automation, epoch=2 |
| 36 | automation | locator_resolved |  | target=the Balance cell in the row where Account = Savings (frame contentFrame), outcome=r |
| 37 | automation | action | s7 | extracted=savingsBalance |
| 38 | automation | run_finished |  | status=success, duration_ms=32731 |
