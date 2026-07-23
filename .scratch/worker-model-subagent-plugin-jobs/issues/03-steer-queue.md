# Steer queue: durable mid-run message injection

## Source context

- PRD: /home/klg2138/deepseek_plugin/.scratch/worker-model-subagent-plugin-jobs/PRD.md
- ADRs: docs/adr/0008-steer-queue.md
- Context: /home/klg2138/deepseek_plugin/CONTEXT.md

## What to build

Mid-run steering end-to-end: a `steer <job> <message>` subcommand appends an ordered durable entry to the Job's steer queue; the Runtime loop drains the queue at each loop-iteration boundary, injecting each steer as a user message into the Thread (recorded in transcript and log) before the next API call. Steering a finished Job refuses with guidance to resume; steers survive the queue-writer's death (they are files, not IPC).

## Acceptance criteria

- [ ] A steer written between scripted turns appears as a user message in the very next API request the fake provider receives
- [ ] Multiple steers inject in write order, exactly once each, surviving a simulated worker restart between write and drain
- [ ] Steers are visible in the Thread transcript and the progress log with timestamps
- [ ] Steering a finished Job refuses and points to resume; steering an unknown Job errors clearly

## Blocked by

- jobs/01-durable-dispatch
