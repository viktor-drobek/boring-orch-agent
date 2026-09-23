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
- the adapter has reported a terminal stop reason (`cancelled`, `stopped`,
  `terminated`, `killed`, `timeout` or `exit`).

A notification, a signal, or one evidence source by itself is not confirmation.
Incomplete evidence is `Unknown`; the reservation stays held so a later worker
cannot overlap an execution whose effects are uncertain. A separate operator
resolution is required for an unknown outcome.

The latency target for sending the cooperative request is separate from the
bounded termination grace. The contract never converts a timeout in the local
client into a claim that a remote agent stopped.
