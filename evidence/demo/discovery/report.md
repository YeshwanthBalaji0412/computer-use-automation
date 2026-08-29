# Discovery: member.savings-balance@1.0.0

- run: `dis_362562475c83` (discovery)
- events: 28

| # | actor | event | step | detail |
|---|-------|-------|------|--------|
| 1 | automation | run_started |  | goal=replay a recorded discovery transcript, target=http://localhost:4000/tenants/meridian |
| 2 | model | model_turn |  | step=1, text= |
| 3 | automation | policy_decision |  | tool=navigate, decision=allow |
| 4 | automation | action |  | To start at the entry point for the meridian tenant. |
| 5 | model | model_turn |  | step=2, text= |
| 6 | automation | policy_decision |  | tool=fill, decision=allow |
| 7 | automation | action |  | Enter user ID to sign in. |
| 8 | model | model_turn |  | step=3, text= |
| 9 | automation | policy_decision |  | tool=fill, decision=allow |
| 10 | automation | action |  | Enter password to sign in. |
| 11 | model | model_turn |  | step=4, text= |
| 12 | automation | policy_decision |  | tool=click, decision=allow |
| 13 | automation | action |  | Submit credentials to sign in. |
| 14 | model | model_turn |  | step=5, text= |
| 15 | automation | policy_decision |  | tool=fill, decision=allow |
| 16 | automation | action |  | Enter member ID to search for member details. |
| 17 | model | model_turn |  | step=6, text= |
| 18 | automation | policy_decision |  | tool=click, decision=allow |
| 19 | automation | action |  | Search for the member with ID 100042. |
| 20 | model | model_turn |  | step=7, text= |
| 21 | automation | policy_decision |  | tool=click, decision=allow |
| 22 | automation | action |  | View details for member 100042. |
| 23 | model | model_turn |  | step=8, text= |
| 24 | automation | action |  | Retrieve the current savings balance for member 100042. |
| 25 | model | model_turn |  | step=9, text= |
| 26 | automation | assertion |  | describe=the member detail screen for member 100042, elements=5 |
| 27 | model | model_turn |  | step=10, text= |
| 28 | automation | run_finished |  | stop_reason=finished, actions=8 |
