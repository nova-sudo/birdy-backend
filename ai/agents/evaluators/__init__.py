"""
ai/agents/evaluators
--------------------
One module per rule kind. Every module exports a pure function:

    evaluate(rule, client, ad_days, ads, adsets, as_of, *, near_miss=False) -> list[Finding]

No I/O and no clock reads: the runner and dry-run replay supply `as_of`.
"""

from ai.agents.evaluators import flag_declining, flag_no_spend, pause_over_target, revive_winner

REGISTRY = {
    "pause_over_target": pause_over_target.evaluate,
    "revive_winner": revive_winner.evaluate,
    "flag_no_spend": flag_no_spend.evaluate,
    "flag_declining": flag_declining.evaluate,
}
