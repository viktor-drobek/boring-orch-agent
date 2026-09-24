# ACP budget contract

ACP adapters expose a capability matrix before launch. The manager refuses a
requested hard budget when the adapter cannot enforce it, unless the task
explicitly opts into the weaker contract (`weaker_contract_opt_in`,
`allow_weaker_contract` or `budget_contract: weaker`). The refusal names every
unenforceable budget.

| Budget | Required capability | Contract when supported |
| --- | --- | --- |
| deadline / wall clock | timer-driven supervisor | enforced locally |
| process-tree termination | process-group identity and signals | enforced after escalation; cessation still needs adapter evidence |
| per-call limit | adapter-level call deadline | otherwise unenforceable |
| internal step count | agent/protocol step visibility | opaque ACP turns cannot enforce it |
| token total | cumulative usage reporting | approximate accounting; not a hard local limit |

Token totals reported after opaque turns are cumulative. The runtime stores the
latest cumulative maximum and never sums the same prefix twice. Input and output
counts are retained separately when available. A missing report is unknown, not
zero; a decreasing cumulative report is marked inconsistent and cannot refund a
budget.

A weaker opt-in changes the claim, not the observed data: unenforceable fields
remain listed in the launch record and the result must not describe them as
hard limits. Native Coddy jobs are not routed through this legacy ACP policy or
through the SQLite manager/worker path.
