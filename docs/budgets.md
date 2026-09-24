# ACP budget contract

Each ACP adapter has a capability matrix (`boring_agent.acp.ADAPTER_CAPABILITIES`,
keyed by the agent executable). It is a property of the adapter: a task document
that tries to declare its own matrix is refused. A requested hard budget the
adapter cannot enforce is refused, unless the task explicitly opts into the weaker
contract (`weaker_contract_opt_in`, `allow_weaker_contract` or
`budget_contract: weaker`). The refusal names every unenforceable budget.

| Budget | Required capability | Contract when supported |
| --- | --- | --- |
| deadline / wall clock | timer-driven supervisor | enforced locally |
| process-tree termination | process-group identity and signals | enforced after escalation; cessation still needs adapter evidence |
| per-call limit | adapter-level call deadline | otherwise unenforceable |
| internal step count | agent/protocol step visibility | opaque ACP turns cannot enforce it |
| token total | cumulative usage reporting | approximate accounting; not a hard local limit |

Token totals reported after opaque turns are cumulative. The accounting stores the
latest cumulative maximum and never sums the same prefix twice. Both the ACP names
(`input_tokens`, `output_tokens`, `total_tokens`) and the OpenAI names
(`prompt_tokens`, `completion_tokens`) are understood. A report without counts is
unknown, not zero: `total_tokens` stays `null` and `known` stays false until a real
count arrives. A decreasing cumulative report is marked inconsistent and cannot
refund a budget. The legacy HTTP provider path (`providers.py`) still reports one
summed token count; separate input and output counts there are future work.

A weaker opt-in changes the claim, not the observed data: unenforceable fields
remain listed in the launch record and the result must not describe them as
hard limits. Native Coddy jobs are not routed through this legacy ACP policy or
through the SQLite manager/worker path.
