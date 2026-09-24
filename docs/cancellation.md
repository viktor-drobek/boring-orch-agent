# ACP cancellation contract

Cancellation has two phases and two independent evidence sources.

1. The supervisor sends the ACP cooperative cancellation notification. This is a
   request, not a terminal observation; the attempt remains active and retains
   its reservation.
2. A timer-driven supervisor sends `SIGTERM` to the ACP process group after the
   cooperative grace period. If the group still exists after a second bounded
   grace period, it sends `SIGKILL` to that same group.

The runtime reports `Cancelled` only when **both** conditions are true:

- the process group is gone; and
- the adapter has reported a terminal stop reason: one of ACP's own
  (`cancelled`, `end_turn`, `max_tokens`, `max_turn_requests`, `refusal`) or a
  process-level outcome the supervisor observed (`stopped`, `terminated`,
  `killed`, `timeout`, `exit`).

A notification, a signal, or one evidence source by itself is not confirmation.
Incomplete evidence is `Unknown`; the reservation stays held so a later worker
cannot overlap an execution whose effects are uncertain. A separate operator
resolution is required for an unknown outcome.

The latency target for sending the cooperative request is separate from the
bounded termination grace. The contract never converts a timeout in the local
client into a claim that a remote agent stopped.

`boring_agent.acp.CancellationSupervisor` implements the decision rule; a caller
must drive it with a timer (poll it on a schedule), reap the child so a zombie
does not keep the group alive, and supply the real process-group id. A single
late poll escalates through `SIGTERM` and `SIGKILL` in one call.
