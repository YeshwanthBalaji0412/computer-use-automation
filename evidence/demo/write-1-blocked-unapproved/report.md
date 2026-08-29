# Replay: member.open-subaccount@1.0.0 (blocked)

- run: `rep_367b6374dcd9` (replay)
- events: 50

| # | actor | event | step | detail |
|---|-------|-------|------|--------|
| 1 | automation | run_started |  | capability=member.open-subaccount@1.0.0, tenant=meridian |
| 2 | automation | action |  | navigate to the capability entry point |
| 3 | automation | step_started | s1 | sign in as the servicing operator |
| 4 | automation | policy_decision | s1 | decision=allow, rule=default |
| 5 | automation | locator_resolved |  | target=the textbox 'User ID' in the 'Operator Sign In' section, outcome=resolved |
| 6 | automation | action | s1 | sign in as the servicing operator |
| 7 | automation | step_finished | s1 | duration_ms=37 |
| 8 | automation | step_started | s2 | supply the operator password |
| 9 | automation | policy_decision | s2 | decision=allow, rule=default |
| 10 | automation | locator_resolved |  | target=the textbox 'Password' in the 'Operator Sign In' section, outcome=resolved |
| 11 | automation | action | s2 | supply the operator password |
| 12 | automation | step_finished | s2 | duration_ms=64 |
| 13 | automation | step_started | s3 | sign in to the console |
| 14 | automation | policy_decision | s3 | decision=allow, rule=default |
| 15 | automation | locator_resolved |  | target=the button 'Sign In' in the 'Operator Sign In' section, outcome=resolved |
| 16 | automation | action | s3 | sign in to the console |
| 17 | automation | step_finished | s3 | duration_ms=5669 |
| 18 | automation | step_started | s4 | enter the member number to look up |
| 19 | automation | policy_decision | s4 | decision=allow, rule=default |
| 20 | automation | locator_resolved |  | target=the textbox 'Member ID' in the 'Member Search' section (frame contentFrame), outcom |
| 21 | automation | action | s4 | enter the member number to look up |
| 22 | automation | step_finished | s4 | duration_ms=5069 |
| 23 | automation | step_started | s5 | submit the member search |
| 24 | automation | policy_decision | s5 | decision=allow, rule=default |
| 25 | automation | locator_resolved |  | target=the button 'Search' in the 'Member Search' section (frame contentFrame), outcome=re |
| 26 | automation | action | s5 | submit the member search |
| 27 | automation | step_finished | s5 | duration_ms=5639 |
| 28 | automation | step_started | s6 | open the matching member's detail screen |
| 29 | automation | policy_decision | s6 | decision=allow, rule=default |
| 30 | automation | locator_resolved |  | target=the link 'View' in the row where Member ID = 100042, Name = J. RIVERA (frame conten |
| 31 | automation | action | s6 | open the matching member's detail screen |
| 32 | automation | step_finished | s6 | duration_ms=5643 |
| 33 | automation | step_started | s7 | start the new sub-account form for this member |
| 34 | automation | policy_decision | s7 | decision=allow, rule=default |
| 35 | automation | locator_resolved |  | target=the link 'Open Sub-Account' in the 'Member Detail' section (frame contentFrame), ou |
| 36 | automation | action | s7 | start the new sub-account form for this member |
| 37 | automation | step_finished | s7 | duration_ms=5615 |
| 38 | automation | step_started | s8 | name the new sub-account |
| 39 | automation | policy_decision | s8 | decision=allow, rule=default |
| 40 | automation | locator_resolved |  | target=the textbox 'Account Nickname' in the 'Open Sub-Account' section (frame contentFram |
| 41 | automation | action | s8 | name the new sub-account |
| 42 | automation | step_finished | s8 | duration_ms=5066 |
| 43 | automation | step_started | s9 | open it as a savings sub-account |
| 44 | automation | policy_decision | s9 | decision=allow, rule=default |
| 45 | automation | locator_resolved |  | target=the combobox 'Account Type' in the 'Open Sub-Account' section (frame contentFrame), |
| 46 | automation | action | s9 | open it as a savings sub-account |
| 47 | automation | step_finished | s9 | duration_ms=5484 |
| 48 | automation | step_started | s10 | review the new sub-account |
| 49 | automation | policy_decision | s10 | decision=block, rule=risk_gates.reversible_write |
| 50 | automation | run_finished |  | status=blocked, duration_ms=38855 |
