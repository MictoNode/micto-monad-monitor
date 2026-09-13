"""Tests for the re-entry note appended to LEFT ACTIVE SET alerts"""

import time
from typing import Optional

from monad_monitor.huginn import ValidatorSetState
from monad_monitor.main import entering_reentry_note


def make_validator_set(
    epoch: Optional[int] = 1241,
    entering_ids: Optional[set] = None,
) -> ValidatorSetState:
    if entering_ids is None:
        entering_ids = {224}
    return ValidatorSetState(
        network="testnet",
        epoch=epoch,
        in_delay_period=False,
        counts={"entering": len(entering_ids)},
        leaving_ids=set(),
        leaving=[],
        fetched_at=time.time(),
        entering_ids=entering_ids,
        entering=[{"validator_id": i, "name": f"v{i}", "stake": 9000000} for i in sorted(entering_ids)],
    )


def test_note_added_when_validator_is_entering():
    note = entering_reentry_note(224, make_validator_set())

    assert "entering list for the next epoch" in note
    assert "epoch 1241" in note
    assert "next epoch boundary" in note


def test_no_note_when_validator_not_entering():
    assert entering_reentry_note(187, make_validator_set()) == ""


def test_no_note_when_validator_set_unavailable():
    assert entering_reentry_note(224, None) == ""


def test_no_note_when_validator_id_unresolved():
    assert entering_reentry_note(None, make_validator_set()) == ""
