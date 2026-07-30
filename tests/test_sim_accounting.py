from __future__ import annotations

from dataclasses import replace
from decimal import Decimal, getcontext
import unittest

from burstserve.sim.accounting import (
    DebtDecayPolicy,
    DualLedgerQuantumAccountingResult,
    RESOURCE_DEBT_DECAY_SCHEMA_VERSION,
    account_completed_steps,
    account_dual_ledger_quantum,
    account_quantum,
    account_quantum_completed_steps,
    account_quantum_resource_usage,
    compare_virtual_service,
    least_served_tenant,
    normalize_resource_usage,
    register_request,
    register_tenant,
    reject_request,
    resource_entitlement,
    service_lag,
    update_resource_debt,
    validate_quantum_result,
    verify_dual_ledger_quantum_accounting,
    verify_quantum_canonical_accounting,
    verify_quantum_resource_accounting,
)
from burstserve.sim.model import (
    Action,
    ExactRatio,
    GlobalFairState,
    QuantumResult,
    RawResourceUsage,
    RequestAllocation,
    RequestSpec,
    RequestState,
    ResidencyState,
    ResourceCapacities,
    ResourceTimeVector,
    SchedulerState,
    TenantLedger,
    TenantPolicy,
    TenantResourceUsage,
    TenantState,
    WorkloadSignature,
)
from burstserve.sim.protocols import ProfileProvider


def _signature(total_steps: int = 4, model: str = "toy-dit") -> WorkloadSignature:
    return WorkloadSignature(
        model=model,
        revision="r1",
        height=512,
        width=512,
        frame_count=1,
        batch_size=1,
        dtype="bf16",
        cfg_mode="batched",
        scheduler="euler",
        total_steps=total_steps,
        attention_backend="sdpa",
        streaming_mode="resident",
        profile_id="test-profile-v1",
    )


def _request(
    request_id: str = "request-a",
    tenant_id: str = "tenant-a",
    total_steps: int = 4,
    *,
    model: str = "toy-dit",
    arrival_ns: int = 0,
    last_progress_ns: int | None = None,
) -> RequestState:
    return RequestState(
        spec=RequestSpec(
            request_id=request_id,
            tenant_id=tenant_id,
            signature=_signature(total_steps, model),
            arrival_ns=arrival_ns,
            deadline_ns=None,
            kind="video",
        ),
        status="running",
        last_progress_ns=last_progress_ns,
    )


class _ToyProfile:
    def __init__(self, step_ns: tuple[int, ...]) -> None:
        self.step_ns = step_ns

    def canonical_step_ns(
        self,
        signature: WorkloadSignature,
        step_index: int,
    ) -> int:
        del signature
        return self.step_ns[step_index]

    def execution_ns(
        self,
        requests: tuple[RequestState, ...],
        action: Action,
    ) -> int:
        del requests, action
        return 1

    def remaining_p99_ns(
        self,
        request: RequestState,
        action: Action,
    ) -> int:
        del request, action
        return 1

    def transition_p99_ns(
        self,
        before: ResidencyState,
        after: ResidencyState,
    ) -> int:
        del before, after
        return 1

    def externality_ns(
        self,
        action: Action,
        active_requests: tuple[RequestState, ...],
    ) -> int:
        del action, active_requests
        return 1

    def memory_peak_bytes(self, action: Action) -> int:
        del action
        return 1


class _BadCanonicalProfile(_ToyProfile):
    def canonical_step_ns(
        self,
        signature: WorkloadSignature,
        step_index: int,
    ) -> int:
        del signature, step_index
        return 1.5  # type: ignore[return-value]


def _state_with_requests(
    *requests: RequestState,
    policies: tuple[TenantPolicy, ...] | None = None,
    virtual_time: ExactRatio = ExactRatio(),
    maximum_credit_ns: int = 80,
) -> SchedulerState:
    if policies is None:
        tenant_ids = sorted({request.spec.tenant_id for request in requests})
        policies = tuple(TenantPolicy(tenant_id) for tenant_id in tenant_ids)
    state = SchedulerState(global_fair=GlobalFairState(virtual_time))
    for policy in policies:
        state = register_tenant(state, policy)
    for request in requests:
        state = register_request(
            state,
            request,
            maximum_credit_ns=maximum_credit_ns,
        )
    return state


def _quantum_result(
    action: Action,
    completed_steps: tuple[tuple[str, int], ...],
    usage_by_tenant: tuple[TenantResourceUsage, ...],
    *,
    elapsed_ns: int = 100,
    started_ns: int = 0,
    success: bool = True,
    error: str | None = None,
) -> QuantumResult:
    total = sum(
        (item.usage for item in usage_by_tenant),
        RawResourceUsage(),
    )
    return QuantumResult(
        action_id=action.action_id,
        started_ns=started_ns,
        finished_ns=started_ns + elapsed_ns,
        completed_steps=completed_steps,
        total_resource_usage=total,
        resource_usage_by_tenant=usage_by_tenant,
        success=success,
        error=error,
    )


class BacklogFairLifecycleTest(unittest.TestCase):
    def test_new_arrival_starts_zero_lag_without_fake_service(self) -> None:
        request = _request()
        state = _state_with_requests(
            request,
            policies=(TenantPolicy("tenant-a", 3, 2),),
            virtual_time=ExactRatio(1_000),
        )
        tenant = state.tenant("tenant-a")
        self.assertTrue(tenant.active)
        self.assertEqual(tenant.ledger.canonical_service_ns, 0)
        self.assertEqual(
            tenant.ledger.fair_service_coordinate,
            ExactRatio(1_500),
        )
        self.assertEqual(service_lag(state, "tenant-a"), ExactRatio())

    def test_finish_long_idle_rearrival_is_capped_at_one_quantum(self) -> None:
        profile = _ToyProfile(tuple(10 for _ in range(100)))
        state = _state_with_requests(
            _request("a-1", "a", 1),
            _request("b-1", "b", 100),
        )
        finished_a = account_completed_steps(
            state,
            "a-1",
            completed_steps=1,
            completion_time_ns=10,
            profile=profile,
        )
        state = finished_a.state_after
        self.assertFalse(state.tenant("a").active)
        self.assertTrue(state.tenant("b").active)

        long_b = account_completed_steps(
            state,
            "b-1",
            completed_steps=50,
            completion_time_ns=20,
            profile=profile,
        )
        state = long_b.state_after
        rearrival = _request("a-2", "a", 1, arrival_ns=20)
        state = register_request(
            state,
            rearrival,
            maximum_credit_ns=80,
        )
        self.assertTrue(state.tenant("a").active)
        self.assertEqual(service_lag(state, "a"), ExactRatio(80))

    def test_registration_reuses_ledger_and_rejects_weight_drift(self) -> None:
        state = _state_with_requests(_request())
        same = register_tenant(state, TenantPolicy("tenant-a"))
        self.assertIs(same, state)
        with self.assertRaisesRegex(ValueError, "change weight"):
            register_tenant(state, TenantPolicy("tenant-a", 2, 1))

    def test_rejecting_last_request_atomically_removes_backlog(self) -> None:
        state = _state_with_requests(_request())
        state = reject_request(state, "request-a", at_ns=0)
        self.assertTrue(state.request("request-a").is_terminal)
        self.assertFalse(state.tenant("tenant-a").active)

    def test_request_splitting_has_identical_tenant_service_outcome(self) -> None:
        profile = _ToyProfile((10, 10))
        unsplit = _state_with_requests(_request("whole", "a", 2))
        unsplit_after = account_completed_steps(
            unsplit,
            "whole",
            completed_steps=2,
            completion_time_ns=10,
            profile=profile,
        ).state_after

        split = _state_with_requests(
            _request("part-1", "a", 1),
            _request("part-2", "a", 1),
        )
        split_after = account_quantum_completed_steps(
            split,
            (("part-1", 1), ("part-2", 1)),
            completion_time_ns=10,
            profile=profile,
        ).state_after
        self.assertEqual(
            unsplit_after.tenant("a").ledger.canonical_service_ns,
            split_after.tenant("a").ledger.canonical_service_ns,
        )
        self.assertEqual(
            unsplit_after.tenant("a").ledger.fair_service_coordinate,
            split_after.tenant("a").ledger.fair_service_coordinate,
        )
        self.assertEqual(
            unsplit_after.global_fair,
            split_after.global_fair,
        )

    def test_multiweight_atomic_quantum_preserves_total_lag(self) -> None:
        profile = _ToyProfile(tuple(10 for _ in range(4)))
        state = _state_with_requests(
            _request("a", "a", 4),
            _request("b", "b", 4),
            policies=(TenantPolicy("a"), TenantPolicy("b", 2, 1)),
        )
        result = account_quantum_completed_steps(
            state,
            (("a", 3), ("b", 3)),
            completion_time_ns=30,
            profile=profile,
        )
        lag_a = service_lag(result.state_after, "a")
        lag_b = service_lag(result.state_after, "b")
        self.assertEqual(lag_a, ExactRatio(-10))
        self.assertEqual(lag_b, ExactRatio(10))
        self.assertEqual(lag_a + lag_b, ExactRatio())
        self.assertEqual(
            result.start_active_tenant_ids,
            ("a", "b"),
        )
        self.assertEqual(result.source, "standalone")
        self.assertIsNone(result.action_id)
        self.assertIsNone(result.quantum_result_id)
        verify_quantum_canonical_accounting(
            result,
            state,
            (("a", 3), ("b", 3)),
            completion_time_ns=30,
            profile=profile,
        )


class SchedulerWatermarkAndAllocationStateTest(unittest.TestCase):
    def test_participant_and_nonparticipant_history_block_stale_start(self) -> None:
        for participant_progress, nonparticipant_progress in (
            (30, 10),
            (10, 30),
        ):
            with self.subTest(
                participant_progress=participant_progress,
                nonparticipant_progress=nonparticipant_progress,
            ):
                state = SchedulerState(
                    current_time_ns=30,
                    tenants=(
                        TenantState(
                            TenantPolicy("a"),
                            TenantLedger("a"),
                            active=True,
                        ),
                        TenantState(
                            TenantPolicy("b"),
                            TenantLedger("b"),
                            active=True,
                        ),
                    ),
                    requests=(
                        _request(
                            "ra",
                            "a",
                            last_progress_ns=participant_progress,
                        ),
                        _request(
                            "rb",
                            "b",
                            last_progress_ns=nonparticipant_progress,
                        ),
                    ),
                )
                action = Action(
                    allocations=(
                        RequestAllocation("ra", 1, 1, 1, 1),
                    ),
                    target_residency=ResidencyState(),
                )
                stale = _quantum_result(
                    action,
                    (),
                    (TenantResourceUsage("a", RawResourceUsage()),),
                    started_ns=29,
                    elapsed_ns=1,
                )
                with self.assertRaisesRegex(ValueError, "current_time_ns"):
                    validate_quantum_result(state, action, stale)

    def test_sleeping_tenant_history_is_in_global_watermark(self) -> None:
        state = SchedulerState(
            current_time_ns=50,
            tenants=(
                TenantState(
                    TenantPolicy("active"),
                    TenantLedger("active"),
                    active=True,
                ),
                TenantState(
                    TenantPolicy("sleeping"),
                    TenantLedger("sleeping", last_active_ns=50),
                ),
            ),
            requests=(_request("ra", "active"),),
        )
        action = Action(
            allocations=(RequestAllocation("ra", 1, 1, 1, 1),),
            target_residency=ResidencyState(),
        )
        stale = _quantum_result(
            action,
            (),
            (TenantResourceUsage("active", RawResourceUsage()),),
            started_ns=49,
            elapsed_ns=1,
        )
        with self.assertRaisesRegex(ValueError, "current_time_ns"):
            validate_quantum_result(state, action, stale)

    def test_canonical_then_dual_cannot_replay_an_older_quantum(self) -> None:
        profile = _ToyProfile((10, 10))
        state = _state_with_requests(_request("ra", "a", 2))
        canonical = account_completed_steps(
            state,
            "ra",
            completed_steps=1,
            completion_time_ns=10,
            profile=profile,
        )
        self.assertEqual(canonical.state_after.current_time_ns, 10)
        action = Action(
            allocations=(RequestAllocation("ra", 1, 1, 1, 1),),
            target_residency=ResidencyState(),
        )
        stale = _quantum_result(
            action,
            (),
            (TenantResourceUsage("a", RawResourceUsage()),),
            started_ns=9,
            elapsed_ns=2,
        )
        capacities = ResourceCapacities(128, 1, 1, 1)
        with self.assertRaisesRegex(ValueError, "current_time_ns"):
            account_dual_ledger_quantum(
                canonical.state_after,
                action,
                stale,
                capacities,
                profile=profile,
            )
        with self.assertRaisesRegex(ValueError, "current_time_ns"):
            account_quantum_completed_steps(
                canonical.state_after,
                (),
                completion_time_ns=9,
                profile=profile,
            )

    def test_late_arrival_and_rejection_time_regression_fail(self) -> None:
        state = SchedulerState(current_time_ns=20)
        state = register_tenant(state, TenantPolicy("a"))
        with self.assertRaisesRegex(ValueError, "arrival.*regress"):
            register_request(
                state,
                _request("late", "a", arrival_ns=19),
                maximum_credit_ns=80,
            )
        arrived = register_request(
            state,
            _request("on-time", "a", arrival_ns=25),
            maximum_credit_ns=80,
        )
        self.assertEqual(arrived.current_time_ns, 25)
        with self.assertRaisesRegex(ValueError, "rejection time"):
            reject_request(arrived, "on-time", at_ns=24)
        rejected = reject_request(arrived, "on-time", at_ns=30)
        self.assertEqual(rejected.current_time_ns, 30)

    def test_compute_allocations_reject_queued_and_suspended_requests(self) -> None:
        for status in ("queued", "suspended"):
            with self.subTest(status=status):
                request = replace(_request("ra", "a"), status=status)
                state = _state_with_requests(request)
                compute = Action(
                    allocations=(
                        RequestAllocation("ra", 1, 1, 1, 1),
                    ),
                    target_residency=ResidencyState(),
                )
                compute_result = _quantum_result(
                    compute,
                    (),
                    (TenantResourceUsage("a", RawResourceUsage()),),
                    elapsed_ns=1,
                )
                with self.assertRaisesRegex(
                    ValueError,
                    "runnable/running",
                ):
                    validate_quantum_result(state, compute, compute_result)

                residency_only = Action(
                    allocations=(),
                    target_residency=ResidencyState(),
                )
                residency_result = _quantum_result(
                    residency_only,
                    (),
                    (),
                    elapsed_ns=1,
                )
                self.assertEqual(
                    validate_quantum_result(
                        state,
                        residency_only,
                        residency_result,
                    ),
                    (),
                )


class CanonicalAndQuantumValidationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.profile = _ToyProfile((40, 40, 40, 40))
        self.state = _state_with_requests(
            _request("ra", "ta"),
            _request("rb", "tb"),
        )
        self.solo = Action(
            allocations=(RequestAllocation("ra", 1, 1, 4, 8),),
            target_residency=ResidencyState(),
        )
        self.corun = Action(
            allocations=(
                RequestAllocation("ra", 1, 3, 4, 4),
                RequestAllocation("rb", 1, 1, 4, 4),
            ),
            target_residency=ResidencyState(),
        )

    def test_real_quota_corunner_walltime_invariance(self) -> None:
        solo_result = _quantum_result(
            self.solo,
            (("ra", 1),),
            (
                TenantResourceUsage(
                    "ta",
                    RawResourceUsage(sm_ns=128 * 400),
                ),
            ),
            elapsed_ns=400,
        )
        corun_result = _quantum_result(
            self.corun,
            (("ra", 1),),
            (
                TenantResourceUsage("ta", RawResourceUsage(sm_ns=3_000)),
                TenantResourceUsage("tb", RawResourceUsage()),
            ),
            elapsed_ns=70,
        )
        validate_quantum_result(self.state, self.solo, solo_result)
        validate_quantum_result(self.state, self.corun, corun_result)
        solo_charge = account_quantum_completed_steps(
            self.state,
            solo_result.completed_steps,
            completion_time_ns=400,
            profile=self.profile,
        )
        corun_charge = account_quantum_completed_steps(
            self.state,
            corun_result.completed_steps,
            completion_time_ns=70,
            profile=self.profile,
        )
        self.assertEqual(
            solo_charge.total_canonical_charge_ns,
            corun_charge.total_canonical_charge_ns,
        )
        self.assertEqual(solo_charge.total_canonical_charge_ns, 40)

    def test_state_action_binding_participant_completeness_and_no_extras(self) -> None:
        valid = _quantum_result(
            self.corun,
            (("ra", 1), ("rb", 1)),
            (
                TenantResourceUsage("ta", RawResourceUsage(sm_ns=20)),
                TenantResourceUsage("tb", RawResourceUsage(sm_ns=30)),
            ),
        )
        self.assertEqual(
            validate_quantum_result(self.state, self.corun, valid),
            ("ta", "tb"),
        )
        missing = _quantum_result(
            self.corun,
            (("ra", 1), ("rb", 1)),
            (TenantResourceUsage("ta", RawResourceUsage(sm_ns=50)),),
        )
        with self.assertRaisesRegex(ValueError, "missing"):
            validate_quantum_result(self.state, self.corun, missing)
        extra = QuantumResult(
            action_id=self.solo.action_id,
            started_ns=0,
            finished_ns=1,
            completed_steps=(("ra", 1),),
            total_resource_usage=RawResourceUsage(),
            resource_usage_by_tenant=(
                TenantResourceUsage("ta", RawResourceUsage()),
                TenantResourceUsage("extra", RawResourceUsage()),
            ),
        )
        with self.assertRaisesRegex(ValueError, "extra"):
            validate_quantum_result(self.state, self.solo, extra)
        with self.assertRaisesRegex(ValueError, "action_id"):
            validate_quantum_result(self.state, self.solo, valid)

    def test_completion_must_be_allocated_and_within_quantum(self) -> None:
        outside = _quantum_result(
            self.solo,
            (("rb", 1),),
            (TenantResourceUsage("ta", RawResourceUsage()),),
        )
        with self.assertRaisesRegex(ValueError, "absent"):
            validate_quantum_result(self.state, self.solo, outside)
        too_many = QuantumResult(
            action_id=self.solo.action_id,
            started_ns=0,
            finished_ns=1,
            completed_steps=(("ra", 2),),
            total_resource_usage=RawResourceUsage(),
            resource_usage_by_tenant=(
                TenantResourceUsage("ta", RawResourceUsage()),
            ),
        )
        with self.assertRaisesRegex(ValueError, "allocated quantum"):
            validate_quantum_result(self.state, self.solo, too_many)

    def test_independent_total_must_strictly_conserve_every_component(self) -> None:
        with self.assertRaisesRegex(ValueError, "conserve"):
            QuantumResult(
                action_id=self.solo.action_id,
                started_ns=0,
                finished_ns=1,
                completed_steps=(("ra", 1),),
                total_resource_usage=RawResourceUsage(sm_ns=11),
                resource_usage_by_tenant=(
                    TenantResourceUsage("ta", RawResourceUsage(sm_ns=10)),
                ),
            )

    def test_sm_time_is_upper_bounded_by_device_and_tenant_quota(self) -> None:
        capacities = ResourceCapacities(
            total_sms=128,
            hbm_bytes_per_second=1,
            pcie_h2d_bytes_per_second=1,
            pcie_d2h_bytes_per_second=1,
        )
        within_quota = _quantum_result(
            self.solo,
            (),
            (
                TenantResourceUsage(
                    "ta",
                    RawResourceUsage(sm_ns=3_200),
                ),
            ),
            elapsed_ns=100,
        )
        validate_quantum_result(
            self.state,
            self.solo,
            within_quota,
            capacities=capacities,
        )
        over_quota = _quantum_result(
            self.solo,
            (),
            (
                TenantResourceUsage(
                    "ta",
                    RawResourceUsage(sm_ns=3_201),
                ),
            ),
            elapsed_ns=100,
        )
        with self.assertRaisesRegex(ValueError, "quota-time upper bound"):
            validate_quantum_result(
                self.state,
                self.solo,
                over_quota,
                capacities=capacities,
            )
        over_device = _quantum_result(
            self.solo,
            (),
            (
                TenantResourceUsage(
                    "ta",
                    RawResourceUsage(sm_ns=12_801),
                ),
            ),
            elapsed_ns=100,
        )
        with self.assertRaisesRegex(ValueError, "total_sms"):
            validate_quantum_result(
                self.state,
                self.solo,
                over_device,
                capacities=capacities,
            )

    def test_quantum_timing_and_failed_completion_are_fail_closed(self) -> None:
        arrived = _state_with_requests(
            _request("late", "tenant", arrival_ns=10),
        )
        action = Action(
            allocations=(RequestAllocation("late", 1, 1, 1, 1),),
            target_residency=ResidencyState(),
        )
        before_arrival = _quantum_result(
            action,
            (),
            (TenantResourceUsage("tenant", RawResourceUsage()),),
            started_ns=9,
        )
        with self.assertRaisesRegex(ValueError, "current_time_ns"):
            validate_quantum_result(arrived, action, before_arrival)

        progressed = SchedulerState(
            current_time_ns=20,
            tenants=(
                TenantState(
                    TenantPolicy("tenant"),
                    TenantLedger("tenant"),
                    active=True,
                ),
            ),
            requests=(
                _request(
                    "progressed",
                    "tenant",
                    arrival_ns=10,
                    last_progress_ns=20,
                ),
            ),
        )
        progressed_action = Action(
            allocations=(RequestAllocation("progressed", 1, 1, 1, 1),),
            target_residency=ResidencyState(),
        )
        stale_start = _quantum_result(
            progressed_action,
            (),
            (TenantResourceUsage("tenant", RawResourceUsage()),),
            started_ns=19,
        )
        with self.assertRaisesRegex(ValueError, "current_time_ns"):
            validate_quantum_result(
                progressed,
                progressed_action,
                stale_start,
            )
        zero_time = _quantum_result(
            action,
            (),
            (TenantResourceUsage("tenant", RawResourceUsage()),),
            elapsed_ns=0,
            started_ns=10,
        )
        with self.assertRaisesRegex(ValueError, "positive elapsed"):
            validate_quantum_result(arrived, action, zero_time)
        failed_with_progress = _quantum_result(
            action,
            (("late", 1),),
            (TenantResourceUsage("tenant", RawResourceUsage()),),
            started_ns=10,
            success=False,
            error="executor failure",
        )
        with self.assertRaisesRegex(ValueError, "failed quantum"):
            validate_quantum_result(arrived, action, failed_with_progress)

    def test_bad_profile_and_terminal_overrun_fail(self) -> None:
        with self.assertRaises(TypeError):
            account_completed_steps(
                self.state,
                "ra",
                completed_steps=1,
                completion_time_ns=1,
                profile=_BadCanonicalProfile((1,)),
            )
        with self.assertRaises(ValueError):
            account_completed_steps(
                self.state,
                "ra",
                completed_steps=5,
                completion_time_ns=1,
                profile=self.profile,
            )

    def test_weighted_virtual_comparison_is_exact(self) -> None:
        left = TenantState(
            TenantPolicy("left", 2, 1),
            TenantLedger("left", fair_service_coordinate=ExactRatio(3)),
        )
        right = TenantState(
            TenantPolicy("right"),
            TenantLedger("right", fair_service_coordinate=ExactRatio(2)),
        )
        self.assertEqual(
            compare_virtual_service(
                left.ledger,
                left.policy,
                right.ledger,
                right.policy,
            ),
            -1,
        )
        self.assertEqual(
            least_served_tenant((right, left)).tenant_id,
            "left",
        )


class ResourceNormalizationDebtAndAtomicQuantumTest(unittest.TestCase):
    def setUp(self) -> None:
        self.capacities = ResourceCapacities(
            total_sms=128,
            hbm_bytes_per_second=1_000_000_000,
            pcie_h2d_bytes_per_second=500_000_000,
            pcie_d2h_bytes_per_second=250_000_000,
        )

    def test_raw_usage_and_entitlement_share_exact_common_units(self) -> None:
        normalized = normalize_resource_usage(
            RawResourceUsage(
                sm_ns=6_400,
                hbm_bytes=50,
                pcie_h2d_bytes=25,
                pcie_d2h_bytes=25,
            ),
            self.capacities,
        )
        self.assertEqual(normalized.compute_ns, ExactRatio(50))
        self.assertEqual(normalized.hbm_ns, ExactRatio(50))
        self.assertEqual(normalized.pcie_h2d_ns, ExactRatio(50))
        self.assertEqual(normalized.pcie_d2h_ns, ExactRatio(100))
        self.assertEqual(normalized.dominant_ns, ExactRatio(100))
        one = TenantPolicy("one")
        three = TenantPolicy("three", 3, 1)
        self.assertEqual(
            resource_entitlement(100, one, (one, three)).compute_ns,
            ExactRatio(25),
        )

    def test_atomic_quantum_updates_all_start_active_tenants_once(self) -> None:
        policy = DebtDecayPolicy(
            tick_ns=1,
            factor_numerator=1,
            factor_denominator=1,
            debt_scale_denominator=1,
        )
        state = _state_with_requests(
            _request("ra", "a"),
            _request("rb", "b"),
        )
        tenants: list[TenantState] = []
        for tenant in state.tenants:
            debt = (
                ResourceTimeVector(compute_ns=ExactRatio(100))
                if tenant.tenant_id == "b"
                else ResourceTimeVector()
            )
            tenants.append(
                replace(
                    tenant,
                    ledger=replace(
                        tenant.ledger,
                        resource_debt=debt,
                        resource_decay_policy_id=policy.policy_id,
                        resource_debt_updated_ns=0,
                    ),
                )
            )
        state = SchedulerState(
            global_fair=state.global_fair,
            tenants=tuple(tenants),
            requests=state.requests,
        )
        action = Action(
            allocations=(RequestAllocation("ra", 1, 1, 1, 1),),
            target_residency=ResidencyState(),
        )
        result = _quantum_result(
            action,
            (("ra", 1),),
            (
                TenantResourceUsage(
                    "a",
                    RawResourceUsage(sm_ns=12_800),
                ),
            ),
            elapsed_ns=100,
        )
        accounted = account_quantum_resource_usage(
            state,
            action,
            result,
            self.capacities,
            decay_policy=policy,
        )
        self.assertEqual(accounted.start_active_tenant_ids, ("a", "b"))
        self.assertEqual(
            [item.tenant_id for item in accounted.tenant_updates],
            ["a", "b"],
        )
        # A: 100 usage - 50 entitlement. B: 100 debt - 50 entitlement.
        self.assertEqual(
            accounted.state_after.tenant("a").ledger.resource_debt.compute_ns,
            ExactRatio(50),
        )
        self.assertEqual(
            accounted.state_after.tenant("b").ledger.resource_debt.compute_ns,
            ExactRatio(50),
        )

    def test_dual_ledger_is_one_resource_then_lifecycle_transition(self) -> None:
        policy = DebtDecayPolicy(
            tick_ns=10,
            factor_numerator=1,
            factor_denominator=1,
            debt_scale_denominator=1,
        )
        state = _state_with_requests(
            _request("ra", "a", 1),
            _request("rb", "b", 2),
        )
        action = Action(
            allocations=(RequestAllocation("ra", 1, 1, 1, 1),),
            target_residency=ResidencyState(),
        )
        result = _quantum_result(
            action,
            (("ra", 1),),
            (
                TenantResourceUsage(
                    "a",
                    RawResourceUsage(hbm_bytes=30),
                ),
            ),
            elapsed_ns=10,
        )
        accounted = account_dual_ledger_quantum(
            state,
            action,
            result,
            self.capacities,
            profile=_ToyProfile((40, 40)),
            decay_policy=policy,
        )
        self.assertEqual(accounted.canonical_accounting.source, "quantum")
        self.assertEqual(
            accounted.canonical_accounting.quantum_result_id,
            result.stable_id,
        )
        verify_quantum_resource_accounting(
            accounted.resource_accounting,
            state,
            action,
            result,
            self.capacities,
            decay_policy=policy,
        )
        verify_quantum_canonical_accounting(
            accounted.canonical_accounting,
            accounted.resource_accounting.state_after,
            result.completed_steps,
            completion_time_ns=result.finished_ns,
            profile=_ToyProfile((40, 40)),
            action=action,
            result=result,
            quantum_start_state=state,
        )
        verify_dual_ledger_quantum_accounting(
            accounted,
            state,
            action,
            result,
            self.capacities,
            profile=_ToyProfile((40, 40)),
            decay_policy=policy,
        )
        self.assertEqual(accounted.start_active_tenant_ids, ("a", "b"))
        self.assertEqual(
            accounted.resource_accounting.start_active_tenant_ids,
            accounted.canonical_accounting.start_active_tenant_ids,
        )
        self.assertTrue(
            accounted.resource_accounting.state_after.tenant("a").active
        )
        self.assertFalse(accounted.state_after.tenant("a").active)
        self.assertTrue(accounted.state_after.tenant("b").active)
        ledger = accounted.state_after.tenant("a").ledger
        self.assertEqual(ledger.resource_debt.hbm_ns, ExactRatio(25))
        self.assertEqual(ledger.resource_debt_updated_ns, 10)
        self.assertEqual(ledger.canonical_service_ns, 40)
        self.assertEqual(
            account_quantum(
                state,
                action,
                result,
                self.capacities,
                profile=_ToyProfile((40, 40)),
                decay_policy=policy,
            ).state_after,
            accounted.state_after,
        )

    def test_dual_verifier_rejects_ids_policy_profile_and_intermediate_tamper(
        self,
    ) -> None:
        state = _state_with_requests(
            _request("ra", "a", 2),
            _request("rb", "b", 2),
        )
        action = Action(
            allocations=(RequestAllocation("ra", 1, 1, 1, 1),),
            target_residency=ResidencyState(),
        )
        result = _quantum_result(
            action,
            (("ra", 1),),
            (
                TenantResourceUsage(
                    "a",
                    RawResourceUsage(hbm_bytes=30),
                ),
            ),
            elapsed_ns=10,
        )
        profile = _ToyProfile((40, 40))
        policy = DebtDecayPolicy(
            tick_ns=10,
            factor_numerator=1,
            factor_denominator=1,
            debt_scale_denominator=1,
        )
        evidence = account_dual_ledger_quantum(
            state,
            action,
            result,
            self.capacities,
            profile=profile,
            decay_policy=policy,
        )

        fake_resource = replace(
            evidence.resource_accounting,
            quantum_result_id="fake-result",
        )
        fake_canonical = replace(
            evidence.canonical_accounting,
            quantum_result_id="fake-result",
        )
        fake_ids = DualLedgerQuantumAccountingResult(
            state_before=state,
            state_after=evidence.state_after,
            action_id=action.action_id,
            quantum_result_id="fake-result",
            resource_accounting=fake_resource,
            canonical_accounting=fake_canonical,
        )
        with self.assertRaisesRegex(ValueError, "quantum_result_id"):
            verify_dual_ledger_quantum_accounting(
                fake_ids,
                state,
                action,
                result,
                self.capacities,
                profile=profile,
                decay_policy=policy,
            )

        alternate_profile = _ToyProfile((41, 41))
        profile_tamper = account_dual_ledger_quantum(
            state,
            action,
            result,
            self.capacities,
            profile=alternate_profile,
            decay_policy=policy,
        )
        with self.assertRaisesRegex(ValueError, "canonical.*recompute"):
            verify_dual_ledger_quantum_accounting(
                profile_tamper,
                state,
                action,
                result,
                self.capacities,
                profile=profile,
                decay_policy=policy,
            )

        changed_capacities = replace(
            self.capacities,
            hbm_bytes_per_second=500_000_000,
        )
        intermediate_tamper = account_dual_ledger_quantum(
            state,
            action,
            result,
            changed_capacities,
            profile=profile,
            decay_policy=policy,
        )
        with self.assertRaisesRegex(ValueError, "resource.*recompute"):
            verify_dual_ledger_quantum_accounting(
                intermediate_tamper,
                state,
                action,
                result,
                self.capacities,
                profile=profile,
                decay_policy=policy,
            )

        changed_policy = replace(policy, tick_ns=20)
        policy_tamper = account_dual_ledger_quantum(
            state,
            action,
            result,
            self.capacities,
            profile=profile,
            decay_policy=changed_policy,
        )
        with self.assertRaisesRegex(ValueError, "resource.*recompute"):
            verify_dual_ledger_quantum_accounting(
                policy_tamper,
                state,
                action,
                result,
                self.capacities,
                profile=profile,
                decay_policy=policy,
            )

        fake_action = Action(
            allocations=(RequestAllocation("ra", 1, 1, 1, 2),),
            target_residency=ResidencyState(),
        )
        with self.assertRaisesRegex(ValueError, "action_id"):
            verify_dual_ledger_quantum_accounting(
                evidence,
                state,
                fake_action,
                result,
                self.capacities,
                profile=profile,
                decay_policy=policy,
            )
        fake_result = replace(result, finished_ns=11)
        with self.assertRaisesRegex(ValueError, "quantum_result_id"):
            verify_dual_ledger_quantum_accounting(
                evidence,
                state,
                action,
                fake_result,
                self.capacities,
                profile=profile,
                decay_policy=policy,
            )

    def test_canonical_verifier_independently_enforces_action_result_semantics(
        self,
    ) -> None:
        state = _state_with_requests(_request("ra", "a", 4))
        capacities = self.capacities
        profile = _ToyProfile((10, 10, 10, 10))

        valid_action = Action(
            allocations=(RequestAllocation("ra", 2, 1, 1, 1),),
            target_residency=ResidencyState(),
        )
        valid_result = _quantum_result(
            valid_action,
            (("ra", 2),),
            (
                TenantResourceUsage(
                    "a",
                    RawResourceUsage(hbm_bytes=10),
                ),
            ),
            elapsed_ns=10,
        )
        valid = account_dual_ledger_quantum(
            state,
            valid_action,
            valid_result,
            capacities,
            profile=profile,
        )

        short_action = Action(
            allocations=(RequestAllocation("ra", 1, 1, 1, 1),),
            target_residency=ResidencyState(),
        )
        overrun = _quantum_result(
            short_action,
            (("ra", 2),),
            (
                TenantResourceUsage(
                    "a",
                    RawResourceUsage(hbm_bytes=10),
                ),
            ),
            elapsed_ns=10,
        )
        with self.assertRaisesRegex(ValueError, "allocated quantum"):
            verify_quantum_canonical_accounting(
                valid.canonical_accounting,
                valid.resource_accounting.state_after,
                overrun.completed_steps,
                completion_time_ns=overrun.finished_ns,
                profile=profile,
                action=short_action,
                result=overrun,
                quantum_start_state=state,
            )

        failure = _quantum_result(
            short_action,
            (("ra", 1),),
            (
                TenantResourceUsage(
                    "a",
                    RawResourceUsage(hbm_bytes=10),
                ),
            ),
            elapsed_ns=10,
            success=False,
            error="executor failed after resource use",
        )
        with self.assertRaisesRegex(ValueError, "failed quantum"):
            verify_quantum_canonical_accounting(
                valid.canonical_accounting,
                valid.resource_accounting.state_after,
                failure.completed_steps,
                completion_time_ns=failure.finished_ns,
                profile=profile,
                action=short_action,
                result=failure,
                quantum_start_state=state,
            )

    def test_canonical_verifier_binds_resource_only_intermediate_projection(
        self,
    ) -> None:
        state = _state_with_requests(
            _request("ra", "a", 2),
            policies=(TenantPolicy("a"), TenantPolicy("sleep")),
        )
        action = Action(
            allocations=(RequestAllocation("ra", 1, 1, 1, 1),),
            target_residency=ResidencyState(),
        )
        result = _quantum_result(
            action,
            (("ra", 1),),
            (
                TenantResourceUsage(
                    "a",
                    RawResourceUsage(hbm_bytes=30),
                ),
            ),
            elapsed_ns=10,
        )
        profile = _ToyProfile((40, 40))
        valid = account_dual_ledger_quantum(
            state,
            action,
            result,
            self.capacities,
            profile=profile,
        )
        intermediate = valid.resource_accounting.state_after

        def verify(intermediate_state: SchedulerState) -> None:
            verify_quantum_canonical_accounting(
                valid.canonical_accounting,
                intermediate_state,
                result.completed_steps,
                completion_time_ns=result.finished_ns,
                profile=profile,
                action=action,
                result=result,
                quantum_start_state=state,
            )

        # The real resource phase is a legal canonical intermediate even
        # though the active tenant's resource ledger has changed.
        verify(intermediate)
        with self.assertRaisesRegex(
            ValueError,
            "requires quantum_start_state",
        ):
            verify_quantum_canonical_accounting(
                valid.canonical_accounting,
                intermediate,
                result.completed_steps,
                completion_time_ns=result.finished_ns,
                profile=profile,
                action=action,
                result=result,
            )

        def with_request(request: RequestState) -> SchedulerState:
            return replace(intermediate, requests=(request,))

        original_request = intermediate.request("ra")
        request_tampers = {
            "arrival": replace(
                original_request,
                spec=replace(original_request.spec, arrival_ns=1),
            ),
            "deadline": replace(
                original_request,
                spec=replace(original_request.spec, deadline_ns=100),
            ),
            "signature": replace(
                original_request,
                spec=replace(
                    original_request.spec,
                    signature=replace(
                        original_request.spec.signature,
                        width=768,
                    ),
                ),
            ),
            "status": replace(original_request, status="suspended"),
            "progress_time": replace(
                original_request,
                last_progress_ns=1,
            ),
        }
        for name, request in request_tampers.items():
            with self.subTest(tamper=name):
                with self.assertRaisesRegex(ValueError, "request state"):
                    verify(with_request(request))

        def with_tenant(tenant: TenantState) -> SchedulerState:
            return replace(
                intermediate,
                tenants=tuple(
                    tenant
                    if existing.tenant_id == tenant.tenant_id
                    else existing
                    for existing in intermediate.tenants
                ),
            )

        active_tenant = intermediate.tenant("a")
        with self.assertRaisesRegex(ValueError, "tenant policy"):
            verify(
                with_tenant(
                    replace(
                        active_tenant,
                        policy=TenantPolicy("a", 2, 1),
                    )
                )
            )

        with self.assertRaisesRegex(ValueError, "global fairness"):
            verify(
                replace(
                    intermediate,
                    global_fair=GlobalFairState(ExactRatio(1)),
                )
            )

        canonical_ledger_tampers = {
            "canonical_service_ns": replace(
                active_tenant.ledger,
                canonical_service_ns=(
                    active_tenant.ledger.canonical_service_ns + 1
                ),
            ),
            "fair_service_coordinate": replace(
                active_tenant.ledger,
                fair_service_coordinate=(
                    active_tenant.ledger.fair_service_coordinate
                    + ExactRatio(1)
                ),
            ),
            "last_active_ns": replace(
                active_tenant.ledger,
                last_active_ns=1,
            ),
        }
        for field_name, ledger in canonical_ledger_tampers.items():
            with self.subTest(tamper=field_name):
                with self.assertRaisesRegex(
                    ValueError,
                    f"canonical ledger field {field_name}",
                ):
                    verify(with_tenant(replace(active_tenant, ledger=ledger)))

        sleeping_tenant = intermediate.tenant("sleep")
        sleeping_debt = replace(
            sleeping_tenant.ledger,
            resource_debt=ResourceTimeVector(
                compute_ns=ExactRatio(1),
            ),
            resource_decay_policy_id="tampered-policy",
            resource_debt_updated_ns=result.finished_ns,
        )
        with self.assertRaisesRegex(
            ValueError,
            "inactive tenant resource field",
        ):
            verify(
                with_tenant(
                    replace(sleeping_tenant, ledger=sleeping_debt)
                )
            )

        with self.assertRaisesRegex(ValueError, "tenant count, IDs, or order"):
            verify(
                replace(
                    intermediate,
                    tenants=(active_tenant,),
                )
            )

        with self.assertRaisesRegex(ValueError, "current_time_ns"):
            verify(
                replace(
                    intermediate,
                    current_time_ns=result.finished_ns + 1,
                )
            )
        with self.assertRaisesRegex(ValueError, "current_time_ns"):
            verify_quantum_canonical_accounting(
                valid.canonical_accounting,
                intermediate,
                result.completed_steps,
                completion_time_ns=result.finished_ns,
                profile=profile,
                action=action,
                result=result,
                quantum_start_state=replace(
                    state,
                    current_time_ns=result.started_ns + 1,
                ),
            )

        decay_policy = DebtDecayPolicy()
        bound_active = state.tenant("a")
        bound_state = replace(
            state,
            tenants=tuple(
                replace(
                    tenant,
                    ledger=replace(
                        tenant.ledger,
                        resource_decay_policy_id=decay_policy.policy_id,
                        resource_debt_updated_ns=0,
                    ),
                )
                if tenant.tenant_id == bound_active.tenant_id
                else tenant
                for tenant in state.tenants
            ),
        )
        bound_valid = account_dual_ledger_quantum(
            bound_state,
            action,
            result,
            self.capacities,
            profile=profile,
            decay_policy=decay_policy,
        )
        bound_intermediate = bound_valid.resource_accounting.state_after
        bound_intermediate_active = bound_intermediate.tenant("a")
        drifted_intermediate = replace(
            bound_intermediate,
            tenants=tuple(
                replace(
                    tenant,
                    ledger=replace(
                        tenant.ledger,
                        resource_decay_policy_id="tampered-policy",
                    ),
                )
                if tenant.tenant_id == bound_intermediate_active.tenant_id
                else tenant
                for tenant in bound_intermediate.tenants
            ),
        )
        with self.assertRaisesRegex(ValueError, "policy drift"):
            verify_quantum_canonical_accounting(
                bound_valid.canonical_accounting,
                drifted_intermediate,
                result.completed_steps,
                completion_time_ns=result.finished_ns,
                profile=profile,
                action=action,
                result=result,
                quantum_start_state=bound_state,
            )

    def test_evidence_constructors_reject_missing_active_and_tampered_deltas(
        self,
    ) -> None:
        state = _state_with_requests(
            _request("ra", "a", 2),
            _request("rb", "b", 2),
        )
        action = Action(
            allocations=(RequestAllocation("ra", 1, 1, 1, 1),),
            target_residency=ResidencyState(),
        )
        result = _quantum_result(
            action,
            (("ra", 1),),
            (TenantResourceUsage("a", RawResourceUsage(hbm_bytes=30)),),
            elapsed_ns=10,
        )
        evidence = account_dual_ledger_quantum(
            state,
            action,
            result,
            self.capacities,
            profile=_ToyProfile((40, 40)),
        )
        with self.assertRaisesRegex(ValueError, "exactly match state_before"):
            replace(
                evidence.resource_accounting,
                start_active_tenant_ids=("a",),
                tenant_updates=(
                    evidence.resource_accounting.tenant_updates[0],
                ),
            )

        update_a = evidence.resource_accounting.tenant_updates[0]
        bad_raw = replace(
            update_a,
            raw_usage=RawResourceUsage(hbm_bytes=31),
        )
        with self.assertRaisesRegex(ValueError, "normalized usage"):
            replace(
                evidence.resource_accounting,
                tenant_updates=(
                    bad_raw,
                    evidence.resource_accounting.tenant_updates[1],
                ),
            )

        charge = evidence.canonical_accounting.charges[0]
        bad_charge = replace(
            charge,
            canonical_charge_ns=charge.canonical_charge_ns + 1,
        )
        with self.assertRaisesRegex(ValueError, "state delta"):
            replace(
                evidence.canonical_accounting,
                charges=(bad_charge,),
                total_canonical_charge_ns=(
                    evidence.canonical_accounting.total_canonical_charge_ns + 1
                ),
            )

    def test_entitlement_is_equal_component_bounded_and_weight_conserving(
        self,
    ) -> None:
        zero = ResourceTimeVector()
        with self.assertRaisesRegex(ValueError, "equal share"):
            update_resource_debt(
                zero,
                zero,
                ResourceTimeVector(compute_ns=ExactRatio(1)),
                decay_elapsed_ns=0,
                entitlement_elapsed_ns=1,
            )
        over = ResourceTimeVector(
            compute_ns=ExactRatio(2),
            hbm_ns=ExactRatio(2),
            pcie_h2d_ns=ExactRatio(2),
            pcie_d2h_ns=ExactRatio(2),
        )
        with self.assertRaisesRegex(ValueError, "exceeds"):
            update_resource_debt(
                zero,
                zero,
                over,
                decay_elapsed_ns=0,
                entitlement_elapsed_ns=1,
            )

        state = _state_with_requests(
            _request("ra", "a"),
            _request("rb", "b"),
            policies=(TenantPolicy("a"), TenantPolicy("b", 3, 1)),
        )
        action = Action(
            allocations=(RequestAllocation("ra", 1, 1, 1, 1),),
            target_residency=ResidencyState(),
        )
        result = _quantum_result(
            action,
            (),
            (TenantResourceUsage("a", RawResourceUsage()),),
            elapsed_ns=100,
        )
        resource = account_quantum_resource_usage(
            state,
            action,
            result,
            self.capacities,
        )
        expected = {"a": ExactRatio(25), "b": ExactRatio(75)}
        for tenant_update in resource.tenant_updates:
            shares = tuple(
                getattr(tenant_update.update.entitlement, component)
                for component in ResourceTimeVector.COMPONENTS
            )
            self.assertTrue(all(share == shares[0] for share in shares))
            self.assertEqual(shares[0], expected[tenant_update.tenant_id])
        for component in ResourceTimeVector.COMPONENTS:
            self.assertEqual(
                sum(
                    (
                        getattr(update.update.entitlement, component)
                        for update in resource.tenant_updates
                    ),
                    ExactRatio(),
                ),
                ExactRatio(100),
            )

    def test_failed_and_zero_progress_quantums_keep_resource_evidence(self) -> None:
        state = _state_with_requests(_request("ra", "a"))
        action = Action(
            allocations=(RequestAllocation("ra", 1, 1, 1, 1),),
            target_residency=ResidencyState(),
        )
        for success, error in (
            (True, None),
            (False, "executor failure"),
        ):
            with self.subTest(success=success):
                result = _quantum_result(
                    action,
                    (),
                    (
                        TenantResourceUsage(
                            "a",
                            RawResourceUsage(hbm_bytes=10),
                        ),
                    ),
                    elapsed_ns=4,
                    success=success,
                    error=error,
                )
                accounted = account_dual_ledger_quantum(
                    state,
                    action,
                    result,
                    self.capacities,
                    profile=_ToyProfile((40, 40, 40, 40)),
                )
                self.assertEqual(
                    accounted.canonical_accounting.charges,
                    (),
                )
                self.assertEqual(
                    accounted.canonical_accounting.total_canonical_charge_ns,
                    0,
                )
                self.assertTrue(accounted.state_after.tenant("a").active)
                self.assertEqual(
                    accounted.state_after.tenant(
                        "a"
                    ).ledger.resource_debt.hbm_ns,
                    ExactRatio(6),
                )

    def test_sleep_ages_old_debt_without_idle_entitlement(self) -> None:
        policy = DebtDecayPolicy(
            tick_ns=10,
            factor_numerator=2,
            factor_denominator=4,
            debt_scale_denominator=1,
        )
        state = _state_with_requests(_request("first", "a", 1))
        first_action = Action(
            allocations=(RequestAllocation("first", 1, 1, 1, 1),),
            target_residency=ResidencyState(),
        )
        first_result = _quantum_result(
            first_action,
            (("first", 1),),
            (
                TenantResourceUsage(
                    "a",
                    RawResourceUsage(hbm_bytes=104),
                ),
            ),
            elapsed_ns=4,
        )
        first = account_dual_ledger_quantum(
            state,
            first_action,
            first_result,
            self.capacities,
            profile=_ToyProfile((10,)),
            decay_policy=policy,
        )
        sleeping = first.state_after
        self.assertFalse(sleeping.tenant("a").active)
        self.assertEqual(
            sleeping.tenant("a").ledger.resource_debt.hbm_ns,
            ExactRatio(100),
        )
        self.assertEqual(
            sleeping.tenant("a").ledger.resource_debt_updated_ns,
            4,
        )

        awake = register_request(
            sleeping,
            _request("second", "a", 2, arrival_ns=20),
            maximum_credit_ns=80,
        )
        second_action = Action(
            allocations=(RequestAllocation("second", 1, 1, 1, 1),),
            target_residency=ResidencyState(),
        )
        second_result = _quantum_result(
            second_action,
            (),
            (TenantResourceUsage("a", RawResourceUsage()),),
            started_ns=20,
            elapsed_ns=4,
        )
        second = account_dual_ledger_quantum(
            awake,
            second_action,
            second_result,
            self.capacities,
            profile=_ToyProfile((10, 10)),
            decay_policy=policy,
        )
        update = second.resource_accounting.tenant_updates[0].update
        self.assertEqual(update.decay_elapsed_ns, 20)
        self.assertEqual(update.entitlement_elapsed_ns, 4)
        self.assertEqual(
            second.state_after.tenant("a").ledger.resource_debt.hbm_ns,
            ExactRatio(21),
        )
        self.assertEqual(
            second.state_after.tenant("a").ledger.resource_debt_updated_ns,
            24,
        )

        zero = ResourceTimeVector()
        current_entitlement = ResourceTimeVector(
            compute_ns=ExactRatio(4),
            hbm_ns=ExactRatio(4),
            pcie_h2d_ns=ExactRatio(4),
            pcie_d2h_ns=ExactRatio(4),
        )
        sleep_segment = update_resource_debt(
            ResourceTimeVector(hbm_ns=ExactRatio(100)),
            zero,
            zero,
            decay_elapsed_ns=16,
            entitlement_elapsed_ns=0,
            decay_policy=policy,
        )
        quantum_segment = update_resource_debt(
            sleep_segment.after,
            zero,
            current_entitlement,
            decay_elapsed_ns=4,
            entitlement_elapsed_ns=4,
            carried_remainder_ns=sleep_segment.new_remainder_ns,
            decay_policy=policy,
        )
        self.assertEqual(update.after, quantum_segment.after)

        overlapping = _quantum_result(
            second_action,
            (),
            (TenantResourceUsage("a", RawResourceUsage()),),
            started_ns=23,
            elapsed_ns=2,
        )
        with self.assertRaisesRegex(ValueError, "current_time_ns"):
            account_dual_ledger_quantum(
                second.state_after,
                second_action,
                overlapping,
                self.capacities,
                profile=_ToyProfile((10, 10)),
                decay_policy=policy,
            )

    def test_remainder_makes_segmented_and_combined_ticks_consistent(self) -> None:
        policy = DebtDecayPolicy(
            tick_ns=10,
            factor_numerator=2,
            factor_denominator=4,
            debt_scale_denominator=4,
        )
        before = ResourceTimeVector(compute_ns=ExactRatio(100))
        zero = ResourceTimeVector()
        first = update_resource_debt(
            before,
            zero,
            zero,
            decay_elapsed_ns=6,
            entitlement_elapsed_ns=0,
            decay_policy=policy,
        )
        second = update_resource_debt(
            first.after,
            zero,
            zero,
            decay_elapsed_ns=4,
            entitlement_elapsed_ns=0,
            carried_remainder_ns=first.new_remainder_ns,
            decay_policy=policy,
        )
        combined = update_resource_debt(
            before,
            zero,
            zero,
            decay_elapsed_ns=10,
            entitlement_elapsed_ns=0,
            decay_policy=policy,
        )
        self.assertEqual(first.new_remainder_ns, 6)
        self.assertEqual(second.new_remainder_ns, 0)
        self.assertEqual(second.after, combined.after)
        self.assertEqual(second.after.compute_ns, ExactRatio(50))

    def test_quantum_accounting_persists_remainder_in_tenant_ledger(self) -> None:
        policy = DebtDecayPolicy(
            tick_ns=10,
            factor_numerator=2,
            factor_denominator=4,
            debt_scale_denominator=4,
        )
        state = _state_with_requests(_request("ra", "a"))
        tenant = state.tenant("a")
        state = SchedulerState(
            global_fair=state.global_fair,
            tenants=(
                replace(
                    tenant,
                    ledger=replace(
                        tenant.ledger,
                        resource_debt=ResourceTimeVector(
                            compute_ns=ExactRatio(100)
                        ),
                        resource_decay_policy_id=policy.policy_id,
                        resource_debt_updated_ns=0,
                    ),
                ),
            ),
            requests=state.requests,
        )
        action = Action(
            allocations=(RequestAllocation("ra", 1, 1, 1, 1),),
            target_residency=ResidencyState(),
        )

        def result_for(started: int, elapsed: int) -> QuantumResult:
            # Full-device SM usage exactly equals the tenant's entitlement,
            # isolating decay/remainder behavior in the compute component.
            return _quantum_result(
                action,
                (),
                (
                    TenantResourceUsage(
                        "a",
                        RawResourceUsage(sm_ns=128 * elapsed),
                    ),
                ),
                elapsed_ns=elapsed,
                started_ns=started,
            )

        first = account_quantum_resource_usage(
            state,
            action,
            result_for(0, 6),
            self.capacities,
            decay_policy=policy,
        )
        self.assertEqual(
            first.state_after.tenant("a").ledger.resource_decay_remainder_ns,
            6,
        )
        second = account_quantum_resource_usage(
            first.state_after,
            action,
            result_for(6, 4),
            self.capacities,
            decay_policy=policy,
        )
        ledger = second.state_after.tenant("a").ledger
        self.assertEqual(ledger.resource_decay_remainder_ns, 0)
        self.assertEqual(ledger.resource_debt.compute_ns, ExactRatio(50))

    def test_many_submillisecond_intervals_preserve_total_ticks(self) -> None:
        policy = DebtDecayPolicy()
        remainder = 0
        total_ticks = 0
        for _ in range(60_000):
            _, ticks, remainder = policy.factor_for_elapsed(
                999_999,
                carried_remainder_ns=remainder,
            )
            total_ticks += ticks
        combined_ticks, combined_remainder = divmod(
            60_000 * 999_999,
            policy.tick_ns,
        )
        self.assertEqual(total_ticks, combined_ticks)
        self.assertEqual(remainder, combined_remainder)
        self.assertEqual((total_ticks, remainder), (59_999, 940_000))

    def test_decay_is_monotone_conservative_and_policy_identity_is_complete(self) -> None:
        policy = DebtDecayPolicy()
        factors = [
            policy.factor_for_elapsed(value)[0]
            for value in (0, 1_000_000, 10_000_000, 60_000_000_000)
        ]
        self.assertEqual(factors, sorted(factors, reverse=True))
        getcontext().prec = 60
        observed = (
            Decimal(factors[-1].numerator)
            / Decimal(factors[-1].denominator)
        )
        ideal = (-Decimal(1)).exp()
        self.assertGreaterEqual(observed, ideal)
        self.assertLess((observed - ideal) / ideal, Decimal("1e-14"))
        self.assertEqual(
            policy.schema_version,
            RESOURCE_DEBT_DECAY_SCHEMA_VERSION,
        )
        changed = replace(policy, tick_ns=2_000_000)
        self.assertNotEqual(policy.policy_id, changed.policy_id)
        self.assertNotEqual(policy.to_key(), changed.to_key())

    def test_policy_drift_fails_closed_and_quantization_is_conservative(self) -> None:
        policy = DebtDecayPolicy(debt_scale_denominator=8)
        value = ExactRatio(1, 3)
        quantized = policy.quantize_up(value)
        self.assertGreaterEqual(quantized, value)
        self.assertLess(quantized - value, ExactRatio(1, 8))

        state = _state_with_requests(_request("ra", "a"))
        tenant = state.tenant("a")
        bound = replace(
            tenant,
            ledger=replace(
                tenant.ledger,
                resource_decay_policy_id=policy.policy_id,
                resource_debt_updated_ns=0,
            ),
        )
        state = SchedulerState(
            global_fair=state.global_fair,
            tenants=(bound,),
            requests=state.requests,
        )
        action = Action(
            allocations=(RequestAllocation("ra", 1, 1, 1, 1),),
            target_residency=ResidencyState(),
        )
        result = _quantum_result(
            action,
            (),
            (TenantResourceUsage("a", RawResourceUsage()),),
        )
        with self.assertRaisesRegex(ValueError, "policy drift"):
            account_quantum_resource_usage(
                state,
                action,
                result,
                self.capacities,
                decay_policy=replace(policy, tick_ns=2_000_000),
            )


if __name__ == "__main__":
    unittest.main()
