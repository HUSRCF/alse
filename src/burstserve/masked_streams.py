"""One stream per quota, kept alive, with the mask read back.

Week 9-10 binds each executor to a CUDA stream carrying an SM mask. Two
things about that are settled by evidence rather than preference, and
both were expensive to learn:

**Streams are pooled, never destroyed mid-run.** Destroying a stream
between quotas hung the next measurement outright -- torch's
ExternalStream and its caching allocator still refer to it, and a
measurement process sat at 97% GPU for 2.5 hours producing nothing. It
did not look like a hang; it looked like a slow measurement. A handful
of live streams costs nothing next to that.

**The installed mask is read back, every time.** ``hipExtStreamCreate-
WithCUMask`` can decline to install what was asked for, and a stream
that quietly carries the full die produces a co-run with an unusually
*low* externality -- a number that looks like good news. The acceptance
clause is that the measured SM set matches the manifest exactly, so the
readback is the evidence for it rather than a sanity check.

The HIP calls are injected. The pool's logic -- reuse, readback,
refusal -- is testable without a GPU, and the failure modes it guards
against are the ones a GPU would let through silently.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Protocol


class MaskRefused(RuntimeError):
    """The runtime installed a different mask than the one requested.

    Fatal rather than a warning: continuing would measure a partition
    that does not exist, and would do so in the direction that flatters
    the result.
    """


class StreamFactory(Protocol):
    """Creates a masked stream and reports what was actually installed."""

    def __call__(self, mask: int) -> tuple[object, int]:
        ...


@dataclass(frozen=True)
class MaskedStream:
    """A stream, its requested mask, and the mask the runtime installed."""

    units: int
    requested_mask: int
    installed_mask: int
    handle: object

    @property
    def popcount(self) -> int:
        return bin(self.installed_mask).count("1")


class MaskedStreamPool:
    """Streams by quota, created once and reused.

    Keyed by the mask rather than the unit count, because two requests
    asking for 16 units on opposite halves of the die need different
    streams and the same width. A pool keyed on width would hand them the
    same stream and silently un-partition them.
    """

    def __init__(self, factory: StreamFactory, *, maskable_units: int = 32):
        self.factory = factory
        self.maskable_units = maskable_units
        self._streams: dict[int, MaskedStream] = {}
        self.creations = 0

    def contiguous_mask(self, units: int, *, offset: int = 0) -> int:
        if not 1 <= units <= self.maskable_units:
            raise ValueError(f"quota {units} outside "
                             f"1..{self.maskable_units}")
        if offset + units > self.maskable_units:
            raise ValueError(f"{units} units at offset {offset} runs past "
                             f"the die")
        return ((1 << units) - 1) << offset

    def acquire(self, mask: int) -> MaskedStream:
        existing = self._streams.get(mask)
        if existing is not None:
            return existing
        handle, installed = self.factory(mask)
        if installed != mask:
            raise MaskRefused(
                f"asked for {hex(mask)}, runtime installed "
                f"{hex(installed)}; a stream that does not carry its mask "
                f"is not a partition"
            )
        stream = MaskedStream(units=bin(mask).count("1"), requested_mask=mask,
                              installed_mask=installed, handle=handle)
        self._streams[mask] = stream
        self.creations += 1
        return stream

    def for_quota(self, units: int, *, offset: int = 0) -> MaskedStream:
        return self.acquire(self.contiguous_mask(units, offset=offset))

    def disjoint_pair(self, units_a: int,
                      units_b: int) -> tuple[MaskedStream, MaskedStream]:
        """Two streams that share no unit.

        Disjointness is what makes a co-run a partition rather than an
        oversubscription, so it is constructed here rather than left to
        the caller to arrange correctly.
        """
        if units_a + units_b > self.maskable_units:
            raise ValueError(f"{units_a}+{units_b} exceeds "
                             f"{self.maskable_units} units")
        left = self.for_quota(units_a, offset=0)
        right = self.for_quota(units_b, offset=units_a)
        if left.installed_mask & right.installed_mask:
            raise MaskRefused("the two installed masks overlap")
        return left, right

    def attestation(self) -> list[dict]:
        """What was installed, for the manifest comparison.

        The acceptance clause is that the measured SM set matches the
        manifest exactly, so this reports masks rather than widths: two
        different 16-unit masks are the same width and not the same set.
        """
        return [
            {
                "units": s.units,
                "requested_mask": hex(s.requested_mask),
                "installed_mask": hex(s.installed_mask),
                "popcount": s.popcount,
                "matches_request": s.installed_mask == s.requested_mask,
            }
            for s in sorted(self._streams.values(),
                            key=lambda s: s.requested_mask)
        ]

    @property
    def live(self) -> int:
        return len(self._streams)
