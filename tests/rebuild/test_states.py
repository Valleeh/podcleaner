"""S2.1 -- only legal transitions, exhaustively, with the row left untouched.

The contract asks for ALL state pairs to be tested. That is 6 x 6 = 36 ordered
pairs; 8 are legal and 28 must raise. Every pair below is generated from the
transition table itself, so adding a state to the machine automatically adds
test cases rather than silently going untested.
"""

from __future__ import annotations

import itertools

import pytest

from podcleaner.core.db import Database, EpisodeNotFound
from podcleaner.core.states import (
    ALL_STATES,
    LEGAL_TRANSITIONS,
    NEXT_STATE,
    TERMINAL_STATES,
    WORKING_STATES,
    IllegalTransition,
    State,
    UnknownState,
    illegal_pairs,
    is_legal,
    legal_pairs,
    next_state,
    validate_transition,
)

ALL_PAIRS = list(itertools.product(ALL_STATES, ALL_STATES))
LEGAL = set(legal_pairs())
ILLEGAL = set(illegal_pairs())


# --------------------------------------------------------------- table shape


def test_the_matrix_is_exhaustive_and_partitioned():
    assert len(ALL_PAIRS) == 36
    assert len(LEGAL) == 8, sorted((a.value, b.value) for a, b in LEGAL)
    assert len(ILLEGAL) == 28
    assert LEGAL | ILLEGAL == set(ALL_PAIRS)
    assert LEGAL & ILLEGAL == set()


def test_happy_path_is_the_declared_pipeline():
    assert [s.value for s in WORKING_STATES] == [
        "discovered", "fetched", "analyzed", "cut",
    ]
    chain = [State.DISCOVERED]
    while chain[-1] in NEXT_STATE:
        chain.append(NEXT_STATE[chain[-1]])
    assert [s.value for s in chain] == [
        "discovered", "fetched", "analyzed", "cut", "published",
    ]


def test_terminal_states_have_no_exits():
    assert TERMINAL_STATES == {State.PUBLISHED, State.FAILED}
    for s in TERMINAL_STATES:
        assert LEGAL_TRANSITIONS[s] == frozenset()
        with pytest.raises(IllegalTransition):
            next_state(s)


def test_every_non_terminal_state_can_fail():
    for s in ALL_STATES:
        if s in TERMINAL_STATES:
            assert not is_legal(s, State.FAILED)
        else:
            assert is_legal(s, State.FAILED)


# ------------------------------------------------------ pure-function matrix


@pytest.mark.parametrize(
    "src,dst", ALL_PAIRS, ids=[f"{a.value}->{b.value}" for a, b in ALL_PAIRS]
)
def test_validate_transition_matches_the_table(src, dst):
    if (src, dst) in LEGAL:
        assert validate_transition(src, dst) == (src, dst)
        assert is_legal(src, dst) is True
    else:
        assert is_legal(src, dst) is False
        with pytest.raises(IllegalTransition) as exc:
            validate_transition(src, dst)
        assert src.value in str(exc.value) and dst.value in str(exc.value)


def test_self_transitions_are_all_illegal():
    for s in ALL_STATES:
        assert (s, s) in ILLEGAL


# ------------------------------------------------------------ negative control


@pytest.mark.parametrize(
    "bogus", ["", "DISCOVERED", "done", "publish", None, 3, object(), ["discovered"]]
)
def test_unknown_states_are_rejected_not_coerced(bogus):
    assert is_legal(bogus, State.FETCHED) is False
    assert is_legal(State.DISCOVERED, bogus) is False
    with pytest.raises(UnknownState):
        validate_transition(State.DISCOVERED, bogus)
    with pytest.raises(UnknownState):
        validate_transition(bogus, State.FETCHED)


def test_unknown_state_is_a_kind_of_illegal_transition():
    assert issubclass(UnknownState, IllegalTransition)


# -------------------------------------------------- persisted matrix (S2.1)


def _snapshot(db: Database, episode_id: int) -> dict:
    row = dict(db.get_episode(episode_id))
    row["_transitions"] = [dict(t) for t in db.transitions(episode_id)]
    return row


@pytest.mark.parametrize(
    "src,dst", ALL_PAIRS, ids=[f"{a.value}->{b.value}" for a, b in ALL_PAIRS]
)
def test_persisted_transition_matrix(db, src, dst):
    """Every illegal pair raises AND leaves the row byte-for-byte unmodified."""
    episode_id = db.add_episode(f"guid-{src.value}-{dst.value}")
    db.force_state(episode_id, src)
    before = _snapshot(db, episode_id)
    assert before["state"] == src.value

    if (src, dst) in LEGAL:
        assert db.transition(episode_id, dst) is dst
        after = _snapshot(db, episode_id)
        assert after["state"] == dst.value
        assert len(after["_transitions"]) == len(before["_transitions"]) + 1
        assert after["_transitions"][-1]["from_state"] == src.value
        assert after["_transitions"][-1]["to_state"] == dst.value
    else:
        with pytest.raises(IllegalTransition):
            db.transition(episode_id, dst)
        after = _snapshot(db, episode_id)
        assert after == before, (
            f"illegal {src.value}->{dst.value} modified the row: "
            f"{ {k: (before[k], after[k]) for k in before if before[k] != after[k]} }"
        )


def test_illegal_transition_writes_no_audit_row(db):
    episode_id = db.add_episode("guid-audit")
    with pytest.raises(IllegalTransition):
        db.transition(episode_id, State.PUBLISHED)  # discovered -> published
    assert db.transitions() == []
    assert db.get_episode(episode_id)["state"] == "discovered"


def test_full_happy_path_persists(db):
    episode_id = db.add_episode("guid-happy")
    for src in WORKING_STATES:
        db.transition(episode_id, NEXT_STATE[src])
    row = db.get_episode(episode_id)
    assert row["state"] == "published"
    assert [(t["from_state"], t["to_state"]) for t in db.transitions(episode_id)] == [
        ("discovered", "fetched"),
        ("fetched", "analyzed"),
        ("analyzed", "cut"),
        ("cut", "published"),
    ]
    # ...and published is a dead end, even for the persisted path.
    with pytest.raises(IllegalTransition):
        db.transition(episode_id, State.FAILED)


def test_transition_of_missing_episode_raises(db):
    with pytest.raises(EpisodeNotFound):
        db.transition(9999, State.FETCHED)
