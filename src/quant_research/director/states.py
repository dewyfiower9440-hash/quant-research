"""Research Director state machine (design v2.1 §10). Pure: the transition table and its check.

Differences from the §10 diagram, by design of the Phase 3 scope:
- a cycle ends at DONE (REPLAN -> DONE, or OBSERVE -> DONE after a cooldown) and the next cycle starts at
  OBSERVE, so every cycle re-reads the memory summary (the diagram's REPLAN -> HYPOTHESIZE edge);
- HYPOTHESIZE / PLAN -> REPLAN when the LLM answer is refused by the schema twice or the model refuses
  (§4 A: "1회 재요청 후 폐기·기록");
- VALIDATE is a recorded stub until the validator exists (Phase 5); it never reads the holdout.
"""

from __future__ import annotations

from enum import StrEnum

from quant_research.errors import AppError


class State(StrEnum):
    OBSERVE = "OBSERVE"
    HYPOTHESIZE = "HYPOTHESIZE"
    PLAN = "PLAN"
    ACQUIRE_DATA = "ACQUIRE_DATA"
    GENERATE = "GENERATE"
    SEARCH = "SEARCH"
    BACKTEST = "BACKTEST"
    VALIDATE = "VALIDATE"
    ANALYZE = "ANALYZE"
    LEARN = "LEARN"
    REPLAN = "REPLAN"
    PAUSED = "PAUSED"
    DONE = "DONE"


S = State
TRANSITIONS: dict[State, frozenset[State]] = {
    S.OBSERVE: frozenset({S.HYPOTHESIZE, S.DONE}),
    S.HYPOTHESIZE: frozenset({S.PLAN, S.OBSERVE, S.REPLAN}),
    S.PLAN: frozenset({S.ACQUIRE_DATA, S.GENERATE, S.REPLAN}),
    S.ACQUIRE_DATA: frozenset({S.GENERATE, S.REPLAN}),
    S.GENERATE: frozenset({S.SEARCH}),
    S.SEARCH: frozenset({S.BACKTEST, S.ANALYZE}),
    S.BACKTEST: frozenset({S.VALIDATE, S.ANALYZE}),
    S.VALIDATE: frozenset({S.ANALYZE}),
    S.ANALYZE: frozenset({S.LEARN}),
    S.LEARN: frozenset({S.REPLAN}),
    S.REPLAN: frozenset({S.DONE}),
    S.PAUSED: frozenset({S.OBSERVE}),
    S.DONE: frozenset(),
}
ACTIVE = frozenset(s for s in State if s not in (S.PAUSED, S.DONE))


class IllegalTransition(AppError):
    """A transition that the §10 state machine does not allow."""


def check_transition(src: State, dst: State) -> None:
    if dst == S.PAUSED and src in ACTIVE:
        return  # budget / health / quality guards may pause any active state
    if dst not in TRANSITIONS[src]:
        raise IllegalTransition(f"{src} -> {dst} is not a §10 transition")
