# Discovery: member.open-subaccount@1.0.0

- run: `dis_1580514a23f8` (discovery)
- events: 50

| # | actor | event | step | detail |
|---|-------|-------|------|--------|
| 1 | automation | run_started |  | goal=Open a new SAVINGS sub-account nicknamed HOLIDAY FUND for member 100042, and read bac |
| 2 | model | model_turn |  | step=1, text=Looking at the sign-in screen. |
| 3 | automation | observed |  | url=http://localhost:4000/tenants/meridian/, elements=6 |
| 4 | model | model_turn |  | step=2, text= |
| 5 | automation | policy_decision |  | tool=fill, decision=allow |
| 6 | automation | action |  | sign in as the servicing operator |
| 7 | model | model_turn |  | step=3, text= |
| 8 | automation | policy_decision |  | tool=fill, decision=allow |
| 9 | automation | action |  | supply the operator password |
| 10 | model | model_turn |  | step=4, text= |
| 11 | automation | policy_decision |  | tool=click, decision=allow |
| 12 | automation | action |  | sign in to the console |
| 13 | model | model_turn |  | step=5, text= |
| 14 | automation | policy_decision |  | tool=fill, decision=allow |
| 15 | automation | action |  | enter the member number to look up |
| 16 | model | model_turn |  | step=6, text= |
| 17 | automation | policy_decision |  | tool=click, decision=allow |
| 18 | automation | action |  | submit the member search |
| 19 | model | model_turn |  | step=7, text= |
| 20 | automation | policy_decision |  | tool=click, decision=allow |
| 21 | automation | action |  | open the matching member's detail screen |
| 22 | model | model_turn |  | step=8, text= |
| 23 | automation | policy_decision |  | tool=click, decision=allow |
| 24 | automation | action |  | start the new sub-account form for this member |
| 25 | model | model_turn |  | step=9, text= |
| 26 | automation | policy_decision |  | tool=fill, decision=allow |
| 27 | automation | action |  | name the new sub-account |
| 28 | model | model_turn |  | step=10, text= |
| 29 | automation | policy_decision |  | tool=select, decision=allow |
| 30 | automation | action |  | open it as a savings sub-account |
| 31 | model | model_turn |  | step=11, text= |
| 32 | automation | policy_decision |  | tool=click, decision=escalate |
| 33 | automation | escalation_raised |  | reason=risky_step_needs_approval, detail=control name 'Continue' matches \b(save|update|ap |
| 34 | system | control_transferred |  | to=human, epoch=1 |
| 35 | system | control_transferred |  | to=automation, epoch=2 |
| 36 | automation | policy_decision |  | tool=click, decision=allow_after_authorisation |
| 37 | automation | action |  | review the new sub-account |
| 38 | model | model_turn |  | step=12, text= |
| 39 | automation | policy_decision |  | tool=click, decision=escalate |
| 40 | automation | escalation_raised |  | reason=risky_step_needs_approval, detail=control name 'Confirm and Open Account' matches \ |
| 41 | system | control_transferred |  | to=human, epoch=3 |
| 42 | system | control_transferred |  | to=automation, epoch=4 |
| 43 | automation | policy_decision |  | tool=click, decision=allow_after_authorisation |
| 44 | automation | action |  | commit the new sub-account |
| 45 | model | model_turn |  | step=13, text= |
| 46 | automation | action |  | capture the confirmation number the caller needs as a receipt |
| 47 | model | model_turn |  | step=14, text= |
| 48 | automation | assertion |  | describe=the sub-account confirmation screen, elements=1 |
| 49 | model | model_turn |  | step=15, text= |
| 50 | automation | run_finished |  | stop_reason=finished, actions=12 |
