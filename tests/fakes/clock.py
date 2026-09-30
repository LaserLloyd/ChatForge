"""Fake clock for testing."""

import asyncio


class FakeClock:
    """A controllable clock for testing async code."""

    def __init__(self, start_time: float = 0.0):
        self._now = float(start_time)

    @property
    def now(self) -> float:
        """Get the current time."""
        return self._now

    def __call__(self) -> float:
        """Callable interface returning current time."""
        return self._now

    def advance(self, seconds: float) -> None:
        """Advance the clock by the given number of seconds."""
        self._now += seconds

    async def sleep(self, seconds: float) -> None:
        """Async sleep that advances the fake clock."""
        self.advance(seconds)
        await asyncio.sleep(0)
