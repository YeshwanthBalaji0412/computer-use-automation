# Discovery: member.savings-balance@1.0.0

- run: `dis_99ab9c84e90f` (discovery)
- events: 27

| # | actor | event | step | detail |
|---|-------|-------|------|--------|
| 1 | automation | run_started |  | goal=replay a recorded discovery transcript, target=http://localhost:4000/tenants/meridian |
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
| 23 | automation | action |  | read the member's current savings balance |
| 24 | model | model_turn |  | step=9, text= |
| 25 | automation | assertion |  | describe=the member detail screen, elements=0 |
| 26 | model | model_turn |  | step=10, text= |
| 27 | automation | run_finished |  | stop_reason=finished, actions=7 |
