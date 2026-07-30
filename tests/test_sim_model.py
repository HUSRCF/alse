from __future__ import annotations

from dataclasses import FrozenInstanceError
import json
import unittest
from unittest.mock import patch

from burstserve.sim.model import (
    Action,
    CanonicalEnvelope,
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
    SIM_SCHEMA_VERSION,
    SchedulerState,
    TenantLedger,
    TenantPolicy,
    TenantResourceUsage,
    TenantState,
    TraceEvent,
    UnsupportedSchemaVersionError,
    WorkloadSignature,
    canonical_json,
    decode_versioned,
    encode_versioned,
)


def _signature(**overrides: object) -> WorkloadSignature:
    values: dict[str, object] = {
        "model": "toy-dit",
        "revision": "r1",
        "height": 512,
        "width": 512,
        "frame_count": 1,
        "batch_size": 1,
        "dtype": "bf16",
        "cfg_mode": "batched",
        "scheduler": "euler",
        "total_steps": 4,
        "attention_backend": "sdpa",
        "streaming_mode": "resident",
        "profile_id": "test-profile-v1",
    }
    values.update(overrides)
    return WorkloadSignature(**values)  # type: ignore[arg-type]


def _request(
    request_id: str = "request-1",
    tenant_id: str = "tenant-a",
) -> RequestState:
    return RequestState(
        spec=RequestSpec(
            request_id=request_id,
            tenant_id=tenant_id,
            signature=_signature(),
            arrival_ns=10,
            deadline_ns=100,
            kind="urgent",
        ),
        status="running",
    )


class ExactRatioTest(unittest.TestCase):
    def test_normalizes_and_computes_without_floating_point(self) -> None:
        self.assertEqual(ExactRatio(20, 30), ExactRatio(2, 3))
        self.assertEqual(
            ExactRatio(1, 3) + ExactRatio(1, 6),
            ExactRatio(1, 2),
        )
        self.assertEqual(
            ExactRatio(3, 4) * ExactRatio(2, 9),
            ExactRatio(1, 6),
        )
        self.assertEqual(
            ExactRatio(3, 4) / ExactRatio(2, 3),
            ExactRatio(9, 8),
        )
        self.assertLess(ExactRatio(1, 7), ExactRatio(1, 6))
        self.assertEqual(
            canonical_json(ExactRatio(2, 4)),
            '{"denominator":2,"numerator":1}',
        )
        self.assertEqual(
            canonical_json({"ratio": ExactRatio(2, 4)}),
            '{"ratio":{"denominator":2,"numerator":1}}',
        )
        with self.assertRaises(TypeError):
            canonical_json({"float": 0.5})

    def test_rejects_bool_zero_denominator_and_zero_division(self) -> None:
        with self.assertRaises(TypeError):
            ExactRatio(True, 1)
        with self.assertRaises(ValueError):
            ExactRatio(1, 0)
        with self.assertRaises(ZeroDivisionError):
            ExactRatio(1, 2) / ExactRatio()


class WorkloadAndRequestModelTest(unittest.TestCase):
    def test_signature_and_request_ids_are_content_stable(self) -> None:
        first = _signature()
        second = _signature()
        changed = _signature(width=768)
        self.assertEqual(first.stable_id, second.stable_id)
        self.assertNotEqual(first.stable_id, changed.stable_id)
        self.assertTrue(first.stable_id.startswith("wls2-"))
        self.assertEqual(first.to_key()["schema_version"], SIM_SCHEMA_VERSION)
        stable_before = first.stable_id
        with patch("burstserve.sim.model.SIM_SCHEMA_VERSION", 99):
            self.assertEqual(first.stable_id, stable_before)
            self.assertEqual(first.to_key()["schema_version"], 2)

        request = _request().spec
        duplicate = _request().spec
        self.assertEqual(request.stable_id, duplicate.stable_id)
        self.assertTrue(request.stable_id.startswith("req2-"))

    def test_integer_and_timing_validation_rejects_ambiguous_values(self) -> None:
        with self.assertRaises(TypeError):
            _signature(total_steps=True)
        with self.assertRaises(ValueError):
            _signature(height=0)
        with self.assertRaises(ValueError):
            _signature(model=" ")
        with self.assertRaises(ValueError):
            RequestSpec(
                request_id="request-1",
                tenant_id="tenant-a",
                signature=_signature(),
                arrival_ns=10,
                deadline_ns=10,
                kind="urgent",
            )

    def test_request_completion_status_is_exact(self) -> None:
        spec = RequestSpec(
            request_id="request-1",
            tenant_id="tenant-a",
            signature=_signature(total_steps=2),
            arrival_ns=0,
            deadline_ns=None,
            kind="video",
        )
        state = RequestState(spec=spec, completed_steps=1, status="running")
        self.assertEqual(state.remaining_steps, 1)
        with self.assertRaises(ValueError):
            RequestState(spec=spec, completed_steps=2, status="running")
        complete = RequestState(spec=spec, completed_steps=2, status="completed")
        self.assertEqual(complete.remaining_steps, 0)


class AllocationAndResidencyModelTest(unittest.TestCase):
    def test_residency_and_action_ids_normalize_set_like_order(self) -> None:
        first_residency = ResidencyState(
            device_immutable_ids=("weights-b", "weights-a"),
            device_continuation_ids=("state-b", "state-a"),
            host_continuation_ids=("state-a",),
            dirty_continuation_ids=("state-b",),
        )
        second_residency = ResidencyState(
            device_immutable_ids=("weights-a", "weights-b"),
            device_continuation_ids=("state-a", "state-b"),
            host_continuation_ids=("state-a",),
            dirty_continuation_ids=("state-b",),
        )
        self.assertEqual(first_residency, second_residency)
        self.assertEqual(first_residency.stable_id, second_residency.stable_id)

        allocation_a = RequestAllocation("a", 1, 32, 128, 8)
        allocation_b = RequestAllocation("b", 2, 3, 4, 4)
        first_action = Action(
            allocations=(allocation_b, allocation_a),
            target_residency=first_residency,
        )
        second_action = Action(
            allocations=(allocation_a, allocation_b),
            target_residency=second_residency,
        )
        self.assertEqual(allocation_a.quota, ExactRatio(1, 4))
        self.assertEqual(first_action.action_id, second_action.action_id)
        self.assertTrue(first_action.is_corun)

    def test_invalid_residency_and_allocations_are_rejected(self) -> None:
        with self.assertRaises(ValueError):
            ResidencyState(
                device_continuation_ids=("device-only",),
                dirty_continuation_ids=(),
            )
        with self.assertRaises(ValueError):
            ResidencyState(device_immutable_ids=("duplicate", "duplicate"))
        with self.assertRaises(ValueError):
            RequestAllocation("request", 1, 5, 4, 1)
        with self.assertRaises(ValueError):
            Action(
                allocations=(
                    RequestAllocation("a", 1, 3, 4, 1),
                    RequestAllocation("b", 1, 3, 4, 1),
                ),
                target_residency=ResidencyState(),
            )


class SchedulerAndResourceModelTest(unittest.TestCase):
    def test_scheduler_normalizes_and_rejects_duplicate_tenants_requests(self) -> None:
        tenant_a = TenantState(
            TenantPolicy("a"),
            TenantLedger("a"),
            active=True,
        )
        tenant_b = TenantState(
            TenantPolicy("b", 2, 1),
            TenantLedger("b", fair_service_coordinate=ExactRatio(7, 3)),
            active=True,
        )
        request_a = _request("r-a", "a")
        request_b = _request("r-b", "b")
        state = SchedulerState(
            GlobalFairState(ExactRatio(5, 2)),
            current_time_ns=10,
            tenants=(tenant_b, tenant_a),
            requests=(request_b, request_a),
        )
        self.assertEqual([item.tenant_id for item in state.tenants], ["a", "b"])
        self.assertEqual(
            [item.spec.request_id for item in state.requests],
            ["r-a", "r-b"],
        )
        self.assertTrue(state.stable_id.startswith("sch2-"))
        with self.assertRaises(ValueError):
            SchedulerState(tenants=(tenant_a, tenant_a))
        with self.assertRaises(ValueError):
            SchedulerState(tenants=(tenant_a,), requests=(request_b,))
        with self.assertRaisesRegex(ValueError, "active set"):
            SchedulerState(
                tenants=(
                    TenantState(TenantPolicy("idle"), TenantLedger("idle"), True),
                ),
            )

    def test_resource_types_are_separate_exact_and_non_negative(self) -> None:
        raw = RawResourceUsage(
            sm_ns=128_000,
            hbm_bytes=20,
            pcie_h2d_bytes=30,
        )
        self.assertEqual(
            (raw + RawResourceUsage(pcie_d2h_bytes=4)).to_key(),
            {
                "sm_ns": 128_000,
                "hbm_bytes": 20,
                "pcie_h2d_bytes": 30,
                "pcie_d2h_bytes": 4,
            },
        )
        normalized = ResourceTimeVector(
            compute_ns=ExactRatio(1, 2),
            hbm_ns=ExactRatio(2),
        )
        self.assertEqual(normalized.dominant_ns, ExactRatio(2))
        self.assertEqual(
            normalized.scale(ExactRatio(1, 2)).compute_ns,
            ExactRatio(1, 4),
        )
        with self.assertRaises(ValueError):
            ResourceTimeVector(compute_ns=ExactRatio(-1))
        with self.assertRaises(ValueError):
            ResourceCapacities(0, 1, 1, 1)

    def test_dataclasses_are_frozen(self) -> None:
        ledger = TenantLedger(
            tenant_id="tenant-a",
            canonical_service_ns=100,
            fair_service_coordinate=ExactRatio(80),
        )
        with self.assertRaises(FrozenInstanceError):
            ledger.canonical_service_ns = 0  # type: ignore[misc]

    def test_resource_debt_epoch_is_part_of_v2_ledger_state(self) -> None:
        ledger = TenantLedger("tenant-a")
        self.assertIn("resource_debt_updated_ns", ledger.to_key())
        self.assertIsNone(ledger.to_key()["resource_debt_updated_ns"])
        with self.assertRaisesRegex(ValueError, "update epoch"):
            TenantLedger(
                "tenant-a",
                resource_debt=ResourceTimeVector(
                    compute_ns=ExactRatio(1),
                ),
                resource_decay_policy_id="policy",
            )

        state = SchedulerState(
            tenants=(
                TenantState(
                    TenantPolicy("tenant-a"),
                    ledger,
                ),
            ),
        )
        encoded = encode_versioned(state)
        self.assertEqual(
            encoded,
            (
                '{"kind":"scheduler_state","payload":{"current_time_ns":0,'
                '"global_fair":'
                '{"virtual_time":{"denominator":1,"numerator":0}},'
                '"requests":[],"schema_version":2,"tenants":[{"active":false,'
                '"ledger":{"canonical_service_ns":0,'
                '"fair_service_coordinate":{"denominator":1,"numerator":0},'
                '"last_active_ns":null,"resource_debt":{"compute_ns":'
                '{"denominator":1,"numerator":0},"hbm_ns":{"denominator":1,'
                '"numerator":0},"pcie_d2h_ns":{"denominator":1,"numerator":0},'
                '"pcie_h2d_ns":{"denominator":1,"numerator":0}},'
                '"resource_debt_updated_ns":null,'
                '"resource_decay_policy_id":null,'
                '"resource_decay_remainder_ns":0,"tenant_id":"tenant-a"},'
                '"policy":{"tenant_id":"tenant-a","weight_denominator":1,'
                '"weight_numerator":1}}]},"schema":"burstserve.sim",'
                '"schema_version":2}'
            ),
        )
        decoded = decode_versioned(encoded)
        self.assertEqual(decoded.encode(), encoded)
        self.assertIsInstance(decoded, CanonicalEnvelope)
        self.assertNotIsInstance(decoded, SchedulerState)
        missing_epoch = json.loads(encoded)
        del missing_epoch["payload"]["tenants"][0]["ledger"][
            "resource_debt_updated_ns"
        ]
        with self.assertRaisesRegex(ValueError, "TenantLedger"):
            decode_versioned(canonical_json(missing_epoch))

        invalid_current = json.loads(encoded)
        invalid_current["payload"]["current_time_ns"] = -1
        with self.assertRaises(ValueError):
            decode_versioned(canonical_json(invalid_current))

        invalid_ratio = json.loads(encoded)
        invalid_ratio["payload"]["tenants"][0]["ledger"][
            "fair_service_coordinate"
        ]["denominator"] = 0
        with self.assertRaises(ValueError):
            decode_versioned(canonical_json(invalid_ratio))

        noncanonical_ratio = json.loads(encoded)
        coordinate = noncanonical_ratio["payload"]["tenants"][0]["ledger"][
            "fair_service_coordinate"
        ]
        coordinate["numerator"] = 2
        coordinate["denominator"] = 4
        with self.assertRaisesRegex(ValueError, "not normalized"):
            decode_versioned(canonical_json(noncanonical_ratio))

        invalid_remainder = json.loads(encoded)
        invalid_remainder["payload"]["tenants"][0]["ledger"][
            "resource_decay_remainder_ns"
        ] = -1
        with self.assertRaises(ValueError):
            decode_versioned(canonical_json(invalid_remainder))

        unbound_debt = json.loads(encoded)
        unbound_debt["payload"]["tenants"][0]["ledger"]["resource_debt"][
            "compute_ns"
        ]["numerator"] = 1
        with self.assertRaisesRegex(ValueError, "policy and update epoch"):
            decode_versioned(canonical_json(unbound_debt))

        future_epoch = json.loads(encoded)
        future_ledger = future_epoch["payload"]["tenants"][0]["ledger"]
        future_ledger["resource_decay_policy_id"] = "policy"
        future_ledger["resource_debt_updated_ns"] = 1
        with self.assertRaisesRegex(ValueError, "exceeds current_time"):
            decode_versioned(canonical_json(future_epoch))

        invalid_policy = json.loads(encoded)
        invalid_policy["payload"]["tenants"][0]["policy"][
            "weight_numerator"
        ] = 0
        with self.assertRaises(ValueError):
            decode_versioned(canonical_json(invalid_policy))


class ResultAndTraceModelTest(unittest.TestCase):
    def test_quantum_result_has_sorted_per_tenant_raw_attribution(self) -> None:
        result = QuantumResult(
            action_id="action",
            started_ns=100,
            finished_ns=160,
            completed_steps=(("b", 2), ("a", 1)),
            total_resource_usage=RawResourceUsage(sm_ns=40, hbm_bytes=4),
            resource_usage_by_tenant=(
                TenantResourceUsage("tenant-b", RawResourceUsage(sm_ns=30)),
                TenantResourceUsage(
                    "tenant-a",
                    RawResourceUsage(sm_ns=10, hbm_bytes=4),
                ),
            ),
        )
        self.assertEqual(result.completed_steps, (("a", 1), ("b", 2)))
        self.assertEqual(
            [item.tenant_id for item in result.resource_usage_by_tenant],
            ["tenant-a", "tenant-b"],
        )
        self.assertEqual(
            result.resource_usage,
            RawResourceUsage(sm_ns=40, hbm_bytes=4),
        )
        self.assertEqual(result.elapsed_ns, 60)
        self.assertTrue(result.stable_id.startswith("qrs2-"))
        with self.assertRaises(ValueError):
            QuantumResult(
                action_id="action",
                started_ns=0,
                finished_ns=1,
                completed_steps=(),
                total_resource_usage=RawResourceUsage(),
                resource_usage_by_tenant=(
                    TenantResourceUsage("a", RawResourceUsage()),
                    TenantResourceUsage("a", RawResourceUsage()),
                ),
            )
        with self.assertRaisesRegex(ValueError, "per-tenant"):
            QuantumResult(
                action_id="action",
                started_ns=0,
                finished_ns=1,
                completed_steps=(("a", 1),),
                total_resource_usage=RawResourceUsage(),
            )

    def test_trace_event_normalizes_payload_and_has_versioned_id(self) -> None:
        first = TraceEvent(
            sequence=3,
            timestamp_ns=90,
            kind="arrival",
            subject_id="request-a",
            payload=(("z", 2), ("a", "value")),
        )
        second = TraceEvent(
            sequence=3,
            timestamp_ns=90,
            kind="arrival",
            subject_id="request-a",
            payload=(("a", "value"), ("z", 2)),
        )
        self.assertEqual(first, second)
        self.assertEqual(first.stable_id, second.stable_id)
        self.assertTrue(first.stable_id.startswith("evt2-"))
        with self.assertRaises(ValueError):
            TraceEvent(0, 0, "x", "y", (("a", 1), ("a", 2)))

    def test_v2_envelope_roundtrip_and_unknown_version_fail_closed(self) -> None:
        event = TraceEvent(
            sequence=1,
            timestamp_ns=20,
            kind="arrival",
            subject_id="request-a",
            payload=(("seed", 7),),
        )
        encoded = encode_versioned(event)
        self.assertEqual(
            encoded,
            (
                '{"kind":"trace_event","payload":{"kind":"arrival",'
                '"payload":[["seed",7]],"schema_version":2,"sequence":1,'
                '"subject_id":"request-a","timestamp_ns":20},'
                '"schema":"burstserve.sim","schema_version":2}'
            ),
        )
        with patch("burstserve.sim.model.SIM_SCHEMA_VERSION", 99):
            decoded = decode_versioned(encoded)
        self.assertIsInstance(decoded, CanonicalEnvelope)
        self.assertEqual(decoded.schema_version, 2)
        self.assertEqual(decoded.kind, "trace_event")
        self.assertEqual(decoded.encode(), encoded)
        self.assertIn('"schema_version":2', encoded)

        unknown_payload = json.loads(encoded)
        unknown_payload["schema_version"] = 3
        unknown_payload["payload"]["schema_version"] = 3
        unknown = canonical_json(unknown_payload)
        with self.assertRaises(UnsupportedSchemaVersionError):
            decode_versioned(unknown)
        malformed_payload = json.loads(encoded)
        del malformed_payload["payload"]["subject_id"]
        with self.assertRaisesRegex(ValueError, "payload fields"):
            decode_versioned(canonical_json(malformed_payload))
        with self.assertRaisesRegex(ValueError, "canonical"):
            decode_versioned(encoded + "\n")


class VersionedRecordSemanticValidationTest(unittest.TestCase):
    def _objects(self) -> dict[str, object]:
        signature = _signature()
        spec_a = RequestSpec(
            request_id="request-a",
            tenant_id="tenant-a",
            signature=signature,
            arrival_ns=10,
            deadline_ns=100,
            kind="urgent",
        )
        spec_b = RequestSpec(
            request_id="request-b",
            tenant_id="tenant-b",
            signature=signature,
            arrival_ns=10,
            deadline_ns=None,
            kind="video",
        )
        request_a = RequestState(spec_a, status="running")
        request_b = RequestState(spec_b, status="runnable")
        residency = ResidencyState(
            device_immutable_ids=("weights-b", "weights-a"),
            device_continuation_ids=("state-b", "state-a"),
            host_continuation_ids=("state-b", "state-a"),
            dirty_continuation_ids=("state-b",),
        )
        action = Action(
            allocations=(
                RequestAllocation("request-b", 1, 1, 2, 4),
                RequestAllocation("request-a", 2, 1, 2, 8),
            ),
            target_residency=residency,
        )
        usage_a = RawResourceUsage(sm_ns=4, hbm_bytes=1)
        usage_b = RawResourceUsage(sm_ns=6, hbm_bytes=2)
        quantum = QuantumResult(
            action_id=action.action_id,
            started_ns=10,
            finished_ns=20,
            completed_steps=(("request-b", 1), ("request-a", 1)),
            total_resource_usage=usage_a + usage_b,
            resource_usage_by_tenant=(
                TenantResourceUsage("tenant-b", usage_b),
                TenantResourceUsage("tenant-a", usage_a),
            ),
        )
        scheduler = SchedulerState(
            current_time_ns=10,
            tenants=(
                TenantState(
                    TenantPolicy("tenant-b"),
                    TenantLedger("tenant-b"),
                    active=True,
                ),
                TenantState(
                    TenantPolicy("tenant-a"),
                    TenantLedger("tenant-a"),
                    active=True,
                ),
            ),
            requests=(request_b, request_a),
        )
        trace = TraceEvent(
            sequence=1,
            timestamp_ns=10,
            kind="arrival",
            subject_id="request-a",
            payload=(("z", 2), ("a", "value")),
        )
        return {
            "action": action,
            "quantum_result": quantum,
            "request_spec": spec_a,
            "request_state": request_a,
            "residency_state": residency,
            "scheduler_state": scheduler,
            "trace_event": trace,
            "workload_signature": signature,
        }

    def test_all_eight_kinds_have_legal_record_level_roundtrip(self) -> None:
        objects = self._objects()
        self.assertEqual(len(objects), 8)
        for kind, obj in objects.items():
            with self.subTest(kind=kind):
                encoded = encode_versioned(obj)
                decoded = decode_versioned(encoded)
                self.assertEqual(decoded.kind, kind)
                self.assertEqual(decoded.encode(), encoded)

    def test_all_eight_kinds_reject_invalid_self_contained_semantics(self) -> None:
        objects = self._objects()

        def rejected(kind: str, mutate: object) -> None:
            record = json.loads(encode_versioned(objects[kind]))
            mutate(record["payload"])  # type: ignore[operator]
            with self.assertRaises((TypeError, ValueError)):
                decode_versioned(canonical_json(record))

        mutations = {
            "workload_signature": lambda payload: payload.__setitem__(
                "width",
                0,
            ),
            "request_spec": lambda payload: payload.__setitem__(
                "deadline_ns",
                payload["arrival_ns"],
            ),
            "request_state": lambda payload: payload.__setitem__(
                "completed_steps",
                -1,
            ),
            "residency_state": lambda payload: payload[
                "device_continuation_ids"
            ].append("state-device-only"),
            "action": lambda payload: payload["allocations"][0].__setitem__(
                "quota_numerator",
                3,
            ),
            "quantum_result": lambda payload: payload[
                "total_resource_usage"
            ].__setitem__("sm_ns", payload["total_resource_usage"]["sm_ns"] + 1),
            "trace_event": lambda payload: payload.__setitem__("sequence", -1),
            "scheduler_state": lambda payload: payload.__setitem__(
                "current_time_ns",
                -1,
            ),
        }
        for kind, mutation in mutations.items():
            with self.subTest(kind=kind):
                rejected(kind, mutation)

    def test_set_like_wire_lists_reject_noncanonical_order(self) -> None:
        objects = self._objects()

        def reverse_and_reject(
            kind: str,
            select: object,
        ) -> None:
            record = json.loads(encode_versioned(objects[kind]))
            values = select(record["payload"])  # type: ignore[operator]
            self.assertGreaterEqual(len(values), 2)
            values.reverse()
            with self.assertRaisesRegex(ValueError, "sorted|ordering"):
                decode_versioned(canonical_json(record))

        cases = (
            (
                "residency_state",
                lambda payload: payload["device_immutable_ids"],
            ),
            ("action", lambda payload: payload["allocations"]),
            ("quantum_result", lambda payload: payload["completed_steps"]),
            (
                "quantum_result",
                lambda payload: payload["resource_usage_by_tenant"],
            ),
            ("trace_event", lambda payload: payload["payload"]),
            ("scheduler_state", lambda payload: payload["tenants"]),
            ("scheduler_state", lambda payload: payload["requests"]),
        )
        for index, (kind, selector) in enumerate(cases):
            with self.subTest(kind=kind, case=index):
                reverse_and_reject(kind, selector)

        scheduler_payload = json.loads(
            encode_versioned(objects["scheduler_state"])
        )["payload"]
        self.assertEqual(
            [
                record["policy"]["tenant_id"]
                for record in scheduler_payload["tenants"]
            ],
            sorted(
                record["policy"]["tenant_id"]
                for record in scheduler_payload["tenants"]
            ),
        )
        self.assertEqual(
            [
                record["request_spec_id"]
                for record in scheduler_payload["requests"]
            ],
            sorted(
                record["request_spec_id"]
                for record in scheduler_payload["requests"]
            ),
        )


if __name__ == "__main__":
    unittest.main()
