"""SIG4: meshsrv/repeat_block.py - the same traceroute target at most once per window."""
from meshsrv.repeat_block import RepeatBlock


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def test_a_new_key_is_free():
    assert RepeatBlock(60, Clock()).retry_after("!aabbccdd") == 0


def test_a_marked_key_waits_the_rest_of_the_window():
    clock = Clock()
    block = RepeatBlock(60, clock)
    block.mark("!aabbccdd")
    assert block.retry_after("!aabbccdd") == 60
    clock.now += 20.5
    assert block.retry_after("!aabbccdd") == 40, "rounded up to whole seconds"
    assert block.retry_after("!11223344") == 0, "other nodes are not blocked"
    clock.now += 40
    assert block.retry_after("!aabbccdd") == 0


def test_marking_again_restarts_the_window():
    clock = Clock()
    block = RepeatBlock(60, clock)
    block.mark("a")
    clock.now += 61
    block.mark("a")
    assert block.retry_after("a") == 60


def test_old_keys_are_dropped_when_the_table_grows():
    clock = Clock()
    block = RepeatBlock(60, clock, max_keys=3)
    for key in "abc":
        block.mark(key)
    clock.now += 100
    block.mark("d")
    assert set(block._started) == {"d"}
