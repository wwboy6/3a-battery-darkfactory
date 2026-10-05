# Dark Factory

## Factory Members

### Productive Member

- @architect - designs the system
- @dev - writes the backend code
- @tester - writes the backend test cases
- @dev_fe - writes the frontend code
- @tester_fe - writes the frontend test cases
- @int_tester - writes the integration test cases

### Status Checker

- @checker - check if backend test cases would pass
- @checker_fe - check if frontend test cases would pass
- @int_checker - check if integration test cases would pass

## Workflow
- Architect make technical decisions and notify all productive workers
- For backend, @dev and @tester would tell @checker when they are done
- @checker would run the test case if both @dev and @testers are done. It will tell @tester for any failed test, or tell @int_checker if all pass.
- @tester would check if failed test is bug in test case or implementation, and tell @dev for implementation bug. They would then tell @check to re-run the task after fix.
- Frontend team would do the same for same manner as backend
- @int_check would run integration tests when all other members are done. It will tell @int_tester for any failed test, or tell @architect if everything is passed. @int_tester would check the test case or tell the corresponding team to fix bugs.

## Design Choices

### No redundant work

No agent would work for the meaning that is already present in the scope. Architect would not repeat any content in the spec - it just states which decision has been made. No agent would do anything similar with another agent.

### Human language communication

There is no protocal for their communication. They may talk too much sometimes but their conversation is understandable for human, and they would cover any exceptions by thinking like a human.

Sometimes @dev may find something ambiguous in spec and ask @architect to review it. It is not stated in mandates but it just works.

### One design as a source of truth

Architect make every technical decision at the very beginning of each stage, and it manages exception whenever other agents meet exception. This avoids any messy conversation in the chat room.

It also act as a team leader to manage the flow of the development, without explicitly specifying him to do so.

### Parallel work

All productive members would run at the same time. They are told not to run the test case themselves to avoid running it before their work completes, and to avoid duplicating test runs.

### Cost reduction

Status checkers are set as low in reasoning effort, to run the test cases when both @dev and @tester are done working, and report when the test run finishes. It would not talk in the chat room except reporting, and write no file. This avoids unnecessary conversation, reduces unnecessary thinking effort to read test results, and it makes the workflow clear.

We are using DeepSeek as the LLM service provider so we don't have many choices of LLM model. For other service providers, a cheaper model should do the work for status checkers.

Only @architect is set for the most expansive setup. Productive members run fast and smoothly with flash model.

### Spec file

@architect would save spec as file for each stage, as an immediate reference for other agents to work, and as a reference in the future. Band chat room is not a good place to save info for the long future as agents may forget room messages after chat history is compact by harness.

The message like test result would be posted in the room instead, which would not be referenced later.

## Failure Handling

For the previous test runs in old setups, we meet many failures and the mandates was updated to cater them:
- BAND app has a bug about posting “thinking” events, that would post an event for each “word” in their thought, which would hang up the app. The thinking event was then disabled.
- The agents may argue about the correctness of spec. In this latest version architect is the only one who explains it.
- Sometimes the team locks up as some agent didn’t notify the next member after their job was done. Some instruction was added to the mandate to avoid it.

## Cost

USD $2.48 in total
