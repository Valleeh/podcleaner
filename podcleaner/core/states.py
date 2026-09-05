"""The episode state machine.

Legal transitions are declared as *data* (:data:`LEGAL_TRANSITIONS`) rather than
being scattered through ``if`` statements, so that the full transition matrix can
be enumerated and tested exhaustively (verification contract S2.1).

The happy path is::

    discovered -> fetched -> analyzed -> cut -> published

Any non-terminal state may also move to ``failed``. ``published`` and ``failed``
are terminal: nothing leaves them, including self-transitions.
"""

from __future__ import annotations

from enum import Enum
from typing import Dict, FrozenSet, Iterable

__all__ = [
    "State",
    "IllegalTransition",
    "LEGAL_TRANSITIONS",
    "NEXT_STATE",
    "TERMINAL_STATES",
    "WORKING_STATES",
    "ALL_STATES",
    "is_legal",
    "validate_transition",
    "next_state",
    "coerce",
]


class State(str, Enum):
    """Episode lifecycle states.

    Subclasses ``str`` so values round-trip through SQLite unchanged and
    comparisons against plain strings work.
    """

    DISCOVERED = "discovered"
    FETCHED = "fetched"
    ANALYZED = "analyzed"
    CUT = "cut"
    PUBLISHED = "published"
    FAILED = "failed"

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.value


class IllegalTransition(Exception):
    """Raised when a transition is not in :data:`LEGAL_TRANSITIONS`.

    Raising this must leave persistent state untouched.
    """

    def __init__(self, from_state: object, to_state: object, reason: str = ""):
        self.from_state = from_state
        self.to_state = to_state
        detail = f": {reason}" if reason else ""
        super().__init__(
            f"illegal transition {from_state!r} -> {to_state!r}{detail}"
        )


class UnknownState(IllegalTransition):
    """Raised when a state name is not part of the machine at all."""


#: The only forward progression through the pipeline.
_PIPELINE = (
    State.DISCOVERED,
    State.FETCHED,
    State.ANALYZED,
    State.CUT,
    State.PUBLISHED,
)

#: States that never leave.
TERMINAL_STATES: FrozenSet[State] = frozenset({State.PUBLISHED, State.FAILED})

#: States a worker can pick up and do work in. Each one has exactly one
#: successor on the happy path.
WORKING_STATES: tuple[State, ...] = (
    State.DISCOVERED,
    State.FETCHED,
    State.ANALYZED,
    State.CUT,
)

ALL_STATES: tuple[State, ...] = tuple(State)

#: ``from -> successor on the happy path``.
NEXT_STATE: Dict[State, State] = {
    _PIPELINE[i]: _PIPELINE[i + 1] for i in range(len(_PIPELINE) - 1)
}

#: The transition matrix, as data. ``LEGAL_TRANSITIONS[a]`` is the complete set
#: of states reachable from ``a`` in one step. Everything not listed raises.
LEGAL_TRANSITIONS: Dict[State, FrozenSet[State]] = {
    State.DISCOVERED: frozenset({State.FETCHED, State.FAILED}),
    State.FETCHED: frozenset({State.ANALYZED, State.FAILED}),
    State.ANALYZED: frozenset({State.CUT, State.FAILED}),
    State.CUT: frozenset({State.PUBLISHED, State.FAILED}),
    State.PUBLISHED: frozenset(),
    State.FAILED: frozenset(),
}

# Structural invariants, checked at import time so a bad edit to the table above
# is a hard error rather than a silently weaker state machine.
assert set(LEGAL_TRANSITIONS) == set(State), "transition table must cover every state"
for _s, _targets in LEGAL_TRANSITIONS.items():
    assert _s not in _targets, f"self-transition declared for {_s}"
    assert _targets <= set(State), f"unknown target in {_s} row"
for _s in TERMINAL_STATES:
    assert not LEGAL_TRANSITIONS[_s], f"terminal state {_s} has outgoing edges"


def coerce(value: object) -> State:
    """Turn ``value`` into a :class:`State`, raising :class:`UnknownState`.

    Accepts a :class:`State`, or any string equal to one of its values.
    """
    if isinstance(value, State):
        return value
    if isinstance(value, str):
        try:
            return State(value)
        except ValueError:
            pass
    raise UnknownState(value, value, f"{value!r} is not a known state")


def is_legal(from_state: object, to_state: object) -> bool:
    """``True`` iff ``from_state -> to_state`` is a declared edge.

    Unknown state names are simply not legal (this never raises), so callers
    can use it as a predicate. Use :func:`validate_transition` when you want
    the failure to be loud.
    """
    try:
        src = coerce(from_state)
        dst = coerce(to_state)
    except UnknownState:
        return False
    return dst in LEGAL_TRANSITIONS[src]


def validate_transition(from_state: object, to_state: object) -> tuple[State, State]:
    """Return ``(src, dst)`` or raise :class:`IllegalTransition`.

    This is the single chokepoint every persistence path funnels through.
    """
    src = coerce(from_state)
    dst = coerce(to_state)
    if dst not in LEGAL_TRANSITIONS[src]:
        if src in TERMINAL_STATES:
            reason = f"{src.value} is terminal"
        elif src is dst:
            reason = "self-transitions are not allowed"
        else:
            legal = ", ".join(sorted(s.value for s in LEGAL_TRANSITIONS[src])) or "(none)"
            reason = f"legal targets from {src.value} are: {legal}"
        raise IllegalTransition(src.value, dst.value, reason)
    return src, dst


def next_state(from_state: object) -> State:
    """The happy-path successor of ``from_state``.

    Raises :class:`IllegalTransition` for terminal states, which have none.
    """
    src = coerce(from_state)
    try:
        return NEXT_STATE[src]
    except KeyError:
        raise IllegalTransition(
            src.value, None, f"{src.value} has no successor (terminal)"
        ) from None


def illegal_pairs() -> Iterable[tuple[State, State]]:
    """Every ordered pair of states that is *not* a legal transition.

    Used by the S2.1 exhaustive test; exposed here so the test and the
    implementation cannot disagree about what "all pairs" means.
    """
    for src in State:
        for dst in State:
            if dst not in LEGAL_TRANSITIONS[src]:
                yield src, dst


def legal_pairs() -> Iterable[tuple[State, State]]:
    """Every ordered pair of states that *is* a legal transition."""
    for src in State:
        for dst in sorted(LEGAL_TRANSITIONS[src], key=lambda s: s.value):
            yield src, dst
