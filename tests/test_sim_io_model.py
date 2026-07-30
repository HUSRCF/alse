from __future__ import annotations

from fractions import Fraction
import unittest

from burstserve.sim.io_model import (
    ContinuationObject,
    ImmutableObject,
    TransitionInfeasibleError,
    calculate_transition,
    cold_transfer_time_ns,
    pcie_payload_rate_bytes_per_second,
    serialized_cold_swap_time_ns,
)
from burstserve.sim.model import (
    RawResourceUsage,
    RequestSpec,
    RequestState,
    ResidencyState,
    SchedulerState,
    TenantLedger,
    TenantPolicy,
    TenantState,
    WorkloadSignature,
)


def _terminal_request(
    request_id: str,
    *,
    status: str = "completed",
) -> RequestState:
    signature = WorkloadSignature(
        model="toy",
        revision="r1",
        height=1,
        width=1,
        frame_count=1,
        batch_size=1,
        dtype="bf16",
        cfg_mode="none",
        scheduler="euler",
        total_steps=1,
        attention_backend="none",
        streaming_mode="resident",
        profile_id="test",
    )
    return RequestState(
        spec=RequestSpec(
            request_id=request_id,
            tenant_id=f"tenant-{request_id}",
            signature=signature,
            arrival_ns=0,
            deadline_ns=None,
            kind="test",
        ),
        completed_steps=1 if status == "completed" else 0,
        status=status,
    )


def _lifecycle_state(*requests: RequestState) -> SchedulerState:
    tenant_ids = sorted({request.spec.tenant_id for request in requests})
    backlogged = {
        request.spec.tenant_id for request in requests if request.is_backlogged
    }
    return SchedulerState(
        tenants=tuple(
            TenantState(
                TenantPolicy(tenant_id),
                TenantLedger(tenant_id),
                active=tenant_id in backlogged,
            )
            for tenant_id in tenant_ids
        ),
        requests=tuple(requests),
    )


class ResidencyTransitionTest(unittest.TestCase):
    def test_resident_switch_has_zero_weight_and_state_traffic(self) -> None:
        residency = ResidencyState(
            device_immutable_ids=("weights",),
            device_continuation_ids=("video-state",),
            host_continuation_ids=("video-state",),
            dirty_continuation_ids=("video-state",),
        )
        transition = calculate_transition(
            residency,
            residency,
            immutable_objects=(ImmutableObject("weights", 20_000_000_000),),
            continuation_objects=(
                ContinuationObject("video-state", 512_000_000, "video"),
            ),
        )
        self.assertEqual(transition.kind, "resident")
        self.assertEqual(transition.total_bytes, 0)
        self.assertEqual(transition.resource_vector, RawResourceUsage())

    def test_state_only_switch_moves_real_dirty_and_host_state(self) -> None:
        before = ResidencyState(
            device_immutable_ids=("shared-weights",),
            device_continuation_ids=("video-state",),
            host_continuation_ids=("urgent-state", "video-state"),
            dirty_continuation_ids=("video-state",),
        )
        after = ResidencyState(
            device_immutable_ids=("shared-weights",),
            device_continuation_ids=("urgent-state",),
            host_continuation_ids=("urgent-state", "video-state"),
        )
        transition = calculate_transition(
            before,
            after,
            immutable_objects=(
                ImmutableObject("shared-weights", 20_000_000_000),
            ),
            continuation_objects=(
                ContinuationObject("video-state", 600_000_000, "video"),
                ContinuationObject("urgent-state", 100_000_000, "urgent"),
            ),
        )
        self.assertEqual(transition.kind, "state_only")
        self.assertEqual(transition.immutable_h2d_bytes, 0)
        self.assertEqual(transition.immutable_d2h_bytes, 0)
        self.assertEqual(transition.continuation_h2d_bytes, 100_000_000)
        self.assertEqual(transition.continuation_d2h_bytes, 600_000_000)
        self.assertEqual(
            transition.resource_vector,
            RawResourceUsage(
                pcie_h2d_bytes=100_000_000,
                pcie_d2h_bytes=600_000_000,
            ),
        )

    def test_cold_switch_loads_only_missing_immutable_bytes(self) -> None:
        before = ResidencyState(device_immutable_ids=("old-weights",))
        after = ResidencyState(device_immutable_ids=("new-weights",))
        transition = calculate_transition(
            before,
            after,
            immutable_objects=(
                ImmutableObject("old-weights", 20_000_000_000),
                ImmutableObject("new-weights", 7_000_000_000),
            ),
            continuation_objects=(),
        )
        self.assertEqual(transition.kind, "cold")
        self.assertEqual(transition.immutable_h2d_bytes, 7_000_000_000)
        self.assertEqual(transition.immutable_d2h_bytes, 0)
        self.assertEqual(transition.total_d2h_bytes, 0)

    def test_shared_weights_are_not_reloaded_and_eviction_is_zero_transfer(self) -> None:
        objects = (
            ImmutableObject("shared", 3_000),
            ImmutableObject("old-only", 5_000),
            ImmutableObject("new-only", 7_000),
        )
        transition = calculate_transition(
            ResidencyState(device_immutable_ids=("shared", "old-only")),
            ResidencyState(device_immutable_ids=("shared", "new-only")),
            immutable_objects=objects,
            continuation_objects=(),
        )
        self.assertEqual(transition.kind, "cold")
        self.assertEqual(transition.immutable_h2d_bytes, 7_000)
        self.assertEqual(transition.immutable_d2h_bytes, 0)

        eviction = calculate_transition(
            ResidencyState(device_immutable_ids=("shared",)),
            ResidencyState(),
            immutable_objects=objects,
            continuation_objects=(),
        )
        self.assertEqual(eviction.kind, "zero_transfer")
        self.assertEqual(eviction.total_bytes, 0)

    def test_missing_immutable_shadow_never_fabricates_d2h(self) -> None:
        with self.assertRaisesRegex(
            TransitionInfeasibleError,
            "no host reload source",
        ):
            calculate_transition(
                ResidencyState(),
                ResidencyState(device_immutable_ids=("weights",)),
                immutable_objects=(
                    ImmutableObject(
                        "weights",
                        20_000_000_000,
                        host_shadow_available=False,
                    ),
                ),
                continuation_objects=(),
            )
        with self.assertRaisesRegex(
            TransitionInfeasibleError,
            "cannot be safely discarded",
        ):
            calculate_transition(
                ResidencyState(device_immutable_ids=("weights",)),
                ResidencyState(),
                immutable_objects=(
                    ImmutableObject(
                        "weights",
                        20_000_000_000,
                        host_shadow_available=False,
                    ),
                ),
                continuation_objects=(),
            )

    def test_dirty_continuation_cannot_be_silently_lost(self) -> None:
        with self.assertRaisesRegex(
            TransitionInfeasibleError,
            "explicit authorization",
        ):
            calculate_transition(
                ResidencyState(
                    device_continuation_ids=("state",),
                    dirty_continuation_ids=("state",),
                ),
                ResidencyState(),
                immutable_objects=(),
                continuation_objects=(
                    ContinuationObject("state", 1_000, "request"),
                ),
            )

    def test_unknown_catalog_references_are_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "unknown immutable"):
            calculate_transition(
                ResidencyState(),
                ResidencyState(device_immutable_ids=("unknown",)),
                immutable_objects=(),
                continuation_objects=(),
            )
        with self.assertRaisesRegex(TransitionInfeasibleError, "no source"):
            calculate_transition(
                ResidencyState(),
                ResidencyState(host_continuation_ids=("known-but-absent",)),
                immutable_objects=(),
                continuation_objects=(
                    ContinuationObject(
                        "known-but-absent",
                        1_000,
                        "request",
                    ),
                ),
            )

    def test_last_clean_copy_requires_explicit_terminal_lifecycle_discard(self) -> None:
        before = ResidencyState(
            device_continuation_ids=("state",),
            host_continuation_ids=("state",),
        )
        after = ResidencyState()
        objects = (ContinuationObject("state", 1_000, "request"),)
        with self.assertRaisesRegex(
            TransitionInfeasibleError,
            "explicit authorization",
        ):
            calculate_transition(
                before,
                after,
                immutable_objects=(),
                continuation_objects=objects,
            )

        completed = _terminal_request("request")
        discarded = calculate_transition(
            before,
            after,
            immutable_objects=(),
            continuation_objects=objects,
            discard_continuation_ids=("state",),
            lifecycle_state=_lifecycle_state(completed),
        )
        self.assertEqual(discarded.kind, "zero_transfer")
        self.assertEqual(discarded.total_bytes, 0)

    def test_dirty_terminal_discard_needs_no_pointless_d2h(self) -> None:
        before = ResidencyState(
            device_continuation_ids=("state",),
            dirty_continuation_ids=("state",),
        )
        discarded = calculate_transition(
            before,
            ResidencyState(),
            immutable_objects=(),
            continuation_objects=(
                ContinuationObject("state", 4_000, "request"),
            ),
            discard_continuation_ids=("state",),
            lifecycle_state=_lifecycle_state(
                _terminal_request("request", status="rejected")
            ),
        )
        self.assertEqual(discarded.kind, "zero_transfer")
        self.assertEqual(discarded.continuation_d2h_bytes, 0)

    def test_discard_authorization_is_exact_and_rejects_nonterminal_owner(self) -> None:
        before = ResidencyState(host_continuation_ids=("state",))
        objects = (ContinuationObject("state", 1_000, "request"),)
        running = RequestState(
            spec=_terminal_request("request").spec,
            status="running",
        )
        with self.assertRaisesRegex(ValueError, "completed/rejected"):
            calculate_transition(
                before,
                ResidencyState(),
                immutable_objects=(),
                continuation_objects=objects,
                discard_continuation_ids=("state",),
                lifecycle_state=_lifecycle_state(running),
            )
        with self.assertRaisesRegex(
            TransitionInfeasibleError,
            "unused",
        ):
            calculate_transition(
                before,
                before,
                immutable_objects=(),
                continuation_objects=objects,
                discard_continuation_ids=("state",),
                lifecycle_state=_lifecycle_state(
                    _terminal_request("request")
                ),
            )


class PcieColdFixtureTest(unittest.TestCase):
    def test_gen4_x16_seventy_percent_rate_is_exact(self) -> None:
        rate = pcie_payload_rate_bytes_per_second(
            generation=4,
            lanes=16,
            efficiency_numerator=7,
            efficiency_denominator=10,
        )
        self.assertIsInstance(rate, Fraction)
        self.assertEqual(rate, Fraction(286_720_000_000, 13))

    def test_twenty_gb_decimal_and_binary_are_distinguished(self) -> None:
        decimal_ns = cold_transfer_time_ns(20_000_000_000)
        binary_ns = cold_transfer_time_ns(20 * 1024**3)
        self.assertEqual(decimal_ns, 906_808_036)
        self.assertEqual(binary_ns, 973_677_715)
        decimal_seconds = decimal_ns / 1_000_000_000
        binary_seconds = binary_ns / 1_000_000_000
        self.assertAlmostEqual(decimal_seconds, 0.907, places=3)
        self.assertAlmostEqual(binary_seconds, 0.974, places=3)
        self.assertGreater(binary_seconds, decimal_seconds)

    def test_serialized_twenty_gb_each_way_is_cold_fixture_only(self) -> None:
        serialized_ns = serialized_cold_swap_time_ns(
            d2h_bytes=20_000_000_000,
            h2d_bytes=20_000_000_000,
        )
        self.assertEqual(serialized_ns, 1_813_616_072)
        serialized_seconds = serialized_ns / 1_000_000_000
        self.assertAlmostEqual(serialized_seconds, 1.814, places=3)

    def test_efficiency_and_sizes_require_exact_bounded_integers(self) -> None:
        with self.assertRaises(ValueError):
            pcie_payload_rate_bytes_per_second(
                generation=4,
                lanes=16,
                efficiency_numerator=11,
                efficiency_denominator=10,
            )
        with self.assertRaises(TypeError):
            cold_transfer_time_ns(20.0)  # type: ignore[arg-type]


if __name__ == "__main__":
    unittest.main()
