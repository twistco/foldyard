"""The FIFO helpers (tests/fifo.py) the hang tests rely on: a hang test that can't see an error,
or leaves a call blocked past its teardown, proves nothing and leaks a git."""

from __future__ import annotations

import pytest


def test_what_the_call_raises_reaches_the_test(fifos):
    def boom():
        raise ValueError("inside the call")

    with pytest.raises(ValueError, match="inside the call"):
        fifos.returns_within(5, boom)


def test_a_reader_that_opens_the_fifo_again_is_released_too(fifos, tmp_path):
    # Git opens its config more than once in one run: one release isn't enough.
    fifo = fifos.plant(tmp_path / "planted")

    def reads_twice() -> int:
        return sum(len(fifo.read_bytes()) for _ in range(3))

    returned, _ = fifos.returns_within(0.2, reads_twice)
    assert not returned  # the control: it does block
    fifos.release()  # raises if the call is still blocked
