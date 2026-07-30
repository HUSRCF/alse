from __future__ import annotations

from dataclasses import replace
from hashlib import sha256
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

import burstserve.sim.trace as trace_module
from burstserve.sim import (
    ARRIVAL_GENERATOR_ALGORITHM_VERSION,
    MANUAL_TRACE_ALGORITHM_VERSION,
    REQUEST_ARRIVAL,
    REQUEST_CANCEL,
    REQUEST_COMPLETE,
    REQUEST_DEADLINE,
    TENANT_ARRIVAL,
    TRACE_SCHEMA,
    TRACE_SCHEMA_VERSION,
    ArrivalGeneratorConfig,
    ReplayFrame,
    RequestSpec,
    RequestState,
    SchedulerState,
    TenantLedger,
    TenantPolicy,
    TraceDocument,
    TraceEvent,
    TraceHeader,
    TraceReplayResult,
    UnsupportedTraceSchemaVersionError,
    WorkloadSignature,
    canonical_json,
    decode_trace_jsonl,
    encode_trace_jsonl,
    event_order_key,
    generate_arrival_trace,
    make_request_arrival,
    make_request_cancel,
    make_request_complete,
    make_request_deadline,
    make_tenant_arrival,
    replay_trace,
)


REPOSITORY = Path(__file__).resolve().parents[1]


def _signature(
    model: str = "toy-dit",
    **overrides: object,
) -> WorkloadSignature:
    values: dict[str, object] = {
        "model": model,
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


def _config(**overrides: object) -> ArrivalGeneratorConfig:
    values: dict[str, object] = {
        "seed": 0x0123456789ABCDEF,
        "start_ns": 1_000,
        "request_count": 6,
        "minimum_interarrival_ns": 10,
        "interarrival_jitter_ns": 30,
        "deadline_offset_ns": 100,
        "request_kind": "urgent",
        "request_id_prefix": "generated",
    }
    values.update(overrides)
    return ArrivalGeneratorConfig(**values)  # type: ignore[arg-type]


def _generated_document() -> TraceDocument:
    return generate_arrival_trace(
        _config(),
        tenants=(
            TenantPolicy("tenant-b", 2, 1),
            TenantPolicy("tenant-a", 1, 1),
        ),
        signatures=(
            _signature("video", total_steps=8),
            _signature("image", width=768),
        ),
        maximum_sleeper_credit_ns=17,
    )


def _manual_document() -> TraceDocument:
    signature = _signature(total_steps=3)
    request_a = RequestSpec(
        request_id="request-a",
        tenant_id="tenant-a",
        signature=signature,
        arrival_ns=10,
        deadline_ns=100,
        kind="urgent",
    )
    request_b = RequestSpec(
        request_id="request-b",
        tenant_id="tenant-b",
        signature=signature,
        arrival_ns=10,
        deadline_ns=None,
        kind="video",
    )
    events = (
        make_tenant_arrival(
            sequence=0,
            timestamp_ns=0,
            policy=TenantPolicy("tenant-a"),
        ),
        make_tenant_arrival(
            sequence=1,
            timestamp_ns=0,
            policy=TenantPolicy("tenant-b", 2, 1),
        ),
        make_request_arrival(sequence=2, spec=request_a),
        make_request_arrival(sequence=3, spec=request_b),
        make_request_cancel(
            sequence=4,
            timestamp_ns=20,
            request_id="request-b",
        ),
        make_request_complete(
            sequence=5,
            timestamp_ns=90,
            request_id="request-a",
        ),
        make_request_deadline(
            sequence=6,
            timestamp_ns=100,
            request_id="request-a",
        ),
    )
    return TraceDocument(
        header=TraceHeader(maximum_sleeper_credit_ns=7),
        signatures=(signature,),
        events=events,
    )


def _records(document: TraceDocument) -> list[dict[str, object]]:
    return [
        json.loads(line)
        for line in encode_trace_jsonl(document).decode("utf-8").splitlines()
    ]


def _encode_records(records: list[dict[str, object]]) -> bytes:
    return (
        "".join(canonical_json(record) + "\n" for record in records)
    ).encode("utf-8")


def _replace_event(
    document: TraceDocument,
    index: int,
    event: TraceEvent,
) -> TraceDocument:
    events = list(document.events)
    events[index] = event
    return TraceDocument(
        header=document.header,
        signatures=document.signatures,
        events=tuple(events),
    )


class ArrivalGeneratorTest(unittest.TestCase):
    def test_same_seed_and_reordered_inputs_produce_identical_bytes(self) -> None:
        config = _config()
        tenants = (
            TenantPolicy("tenant-b", 2, 1),
            TenantPolicy("tenant-a"),
        )
        signatures = (
            _signature("video", total_steps=8),
            _signature("image", width=768),
        )
        first = generate_arrival_trace(
            config,
            tenants=tenants,
            signatures=signatures,
            maximum_sleeper_credit_ns=17,
        )
        second = generate_arrival_trace(
            config,
            tenants=tuple(reversed(tenants)),
            signatures=tuple(reversed(signatures)),
            maximum_sleeper_credit_ns=17,
        )
        first_bytes = encode_trace_jsonl(first)
        self.assertEqual(first_bytes, encode_trace_jsonl(second))
        self.assertEqual(decode_trace_jsonl(first_bytes), first)
        self.assertEqual(first.header.generator_algorithm, (
            ARRIVAL_GENERATOR_ALGORITHM_VERSION
        ))
        self.assertEqual(first.header.generator_seed, config.seed)
        self.assertEqual(
            sha256(first_bytes).hexdigest(),
            "29c5398813d373cf40ff7b914f1c278958302a705f30a35cbfca53f8cbbd6a61",
        )

    def test_different_seed_changes_arrivals_and_trace_identity(self) -> None:
        first = _generated_document()
        second = generate_arrival_trace(
            _config(seed=_config().seed + 1),
            tenants=(
                TenantPolicy("tenant-b", 2, 1),
                TenantPolicy("tenant-a"),
            ),
            signatures=(
                _signature("video", total_steps=8),
                _signature("image", width=768),
            ),
            maximum_sleeper_credit_ns=17,
        )
        self.assertNotEqual(first.events, second.events)
        self.assertNotEqual(first.stable_id, second.stable_id)

    def test_algorithm_metadata_is_complete_and_cross_bound(self) -> None:
        document = _generated_document()
        parameters = dict(document.header.generator_parameters)
        self.assertEqual(
            set(parameters),
            {
                "deadline_offset_ns",
                "interarrival_jitter_ns",
                "minimum_interarrival_ns",
                "request_count",
                "request_id_prefix",
                "request_kind",
                "start_ns",
            },
        )
        tampered_header = replace(
            document.header,
            generator_seed=document.header.generator_seed + 1,
        )
        with self.assertRaisesRegex(ValueError, "do not match seed"):
            TraceDocument(
                header=tampered_header,
                signatures=document.signatures,
                events=document.events,
            )
        changed_parameters = tuple(
            (
                key,
                value + 1 if key == "deadline_offset_ns" else value,
            )
            for key, value in document.header.generator_parameters
        )
        with self.assertRaisesRegex(ValueError, "do not match seed"):
            TraceDocument(
                header=replace(
                    document.header,
                    generator_parameters=changed_parameters,
                ),
                signatures=document.signatures,
                events=document.events,
            )

    def test_generator_validates_integer_contract_and_inputs(self) -> None:
        with self.assertRaises(TypeError):
            _config(seed=True)
        with self.assertRaises(ValueError):
            _config(seed=1 << 64)
        with self.assertRaises(ValueError):
            _config(minimum_interarrival_ns=0)
        with self.assertRaises(ValueError):
            _config(deadline_offset_ns=0)
        with self.assertRaises(ValueError):
            _config(request_count=499_999)
        with self.assertRaises(ValueError):
            _config(request_kind=" ")
        with self.assertRaises(ValueError):
            generate_arrival_trace(
                _config(request_count=1),
                tenants=(),
                signatures=(_signature(),),
            )
        with self.assertRaises(ValueError):
            generate_arrival_trace(
                _config(request_count=1),
                tenants=(TenantPolicy("tenant-a"),),
                signatures=(),
            )
        with self.assertRaisesRegex(ValueError, "byte-size|record-count"):
            generate_arrival_trace(
                _config(request_count=499_998),
                tenants=(
                    TenantPolicy("tenant-a"),
                    TenantPolicy("tenant-b"),
                ),
                signatures=(
                    _signature("signature-a"),
                    _signature("signature-b"),
                ),
            )
        with self.assertRaises(ValueError):
            generate_arrival_trace(
                _config(request_count=0),
                tenants=(
                    TenantPolicy("tenant-a"),
                    TenantPolicy("tenant-a"),
                ),
                signatures=(),
            )
        with self.assertRaisesRegex(ValueError, "at most"):
            generate_arrival_trace(
                _config(request_count=0),
                tenants=(TenantPolicy("tenant-a", 1 << 64, 1),),
                signatures=(),
            )
        with self.assertRaisesRegex(ValueError, "at most"):
            generate_arrival_trace(
                _config(request_count=0),
                tenants=(),
                signatures=(_signature(width=1 << 64),),
            )

    def test_impossible_wire_size_is_rejected_before_stream_entry(
        self,
    ) -> None:
        request_count = (
            trace_module._MAX_TRACE_BYTES
            // trace_module._MIN_GENERATED_REQUEST_WIRE_BYTES
            + 1
        )
        config = _config(request_count=request_count)
        with (
            patch.object(
                trace_module,
                "_canonical_inputs",
                side_effect=AssertionError("canonical input iteration entered"),
            ) as canonical_inputs,
            patch.object(
                trace_module,
                "_iter_generated_events",
                side_effect=AssertionError("event iterator entered"),
            ) as event_iterator,
            patch.object(
                trace_module,
                "_canonical_record_chunk",
                side_effect=AssertionError("record serializer entered"),
            ) as serializer,
        ):
            with self.assertRaisesRegex(ValueError, "byte-size"):
                generate_arrival_trace(
                    config,
                    tenants=(TenantPolicy("t"),),
                    signatures=(_signature("m"),),
                )
        canonical_inputs.assert_not_called()
        event_iterator.assert_not_called()
        serializer.assert_not_called()

    def test_minimum_wire_preflight_keeps_exact_boundary_possible(
        self,
    ) -> None:
        class ExactPreflightReached(Exception):
            pass

        request_count = 3
        exact_lower_bound = (
            request_count
            * trace_module._MIN_GENERATED_REQUEST_WIRE_BYTES
        )
        config = _config(request_count=request_count)
        with (
            patch.object(
                trace_module,
                "_MAX_TRACE_BYTES",
                exact_lower_bound,
            ),
            patch.object(
                trace_module,
                "_canonical_inputs",
                side_effect=ExactPreflightReached,
            ) as canonical_inputs,
        ):
            with self.assertRaises(ExactPreflightReached):
                generate_arrival_trace(
                    config,
                    tenants=(TenantPolicy("t"),),
                    signatures=(_signature("m"),),
                )
        canonical_inputs.assert_called_once()

        with (
            patch.object(
                trace_module,
                "_MAX_TRACE_BYTES",
                exact_lower_bound - 1,
            ),
            patch.object(
                trace_module,
                "_canonical_inputs",
                side_effect=AssertionError("lower-bound rejection was late"),
            ) as canonical_inputs,
        ):
            with self.assertRaisesRegex(ValueError, "byte-size"):
                generate_arrival_trace(
                    config,
                    tenants=(TenantPolicy("t"),),
                    signatures=(_signature("m"),),
                )
        canonical_inputs.assert_not_called()

    def test_golden_bytes_are_hash_seed_and_process_stable(self) -> None:
        script = """
from hashlib import sha256
from burstserve.sim import (
    ArrivalGeneratorConfig, TenantPolicy, WorkloadSignature,
    encode_trace_jsonl, generate_arrival_trace,
)
def signature(model, width, steps):
    return WorkloadSignature(
        model=model, revision="r1", height=512, width=width, frame_count=1,
        batch_size=1, dtype="bf16", cfg_mode="batched", scheduler="euler",
        total_steps=steps, attention_backend="sdpa",
        streaming_mode="resident", profile_id="test-profile-v1",
    )
config = ArrivalGeneratorConfig(
    seed=0x0123456789ABCDEF, start_ns=1000, request_count=6,
    minimum_interarrival_ns=10, interarrival_jitter_ns=30,
    deadline_offset_ns=100, request_kind="urgent",
    request_id_prefix="generated",
)
document = generate_arrival_trace(
    config,
    tenants=(TenantPolicy("tenant-b", 2, 1), TenantPolicy("tenant-a")),
    signatures=(signature("video", 512, 8), signature("image", 768, 4)),
    maximum_sleeper_credit_ns=17,
)
print(sha256(encode_trace_jsonl(document)).hexdigest())
"""
        outputs: list[str] = []
        for hash_seed in ("0", "1", "123"):
            environment = dict(os.environ)
            environment["PYTHONHASHSEED"] = hash_seed
            python_path = str(REPOSITORY / "src")
            if environment.get("PYTHONPATH"):
                python_path += os.pathsep + environment["PYTHONPATH"]
            environment["PYTHONPATH"] = python_path
            completed = subprocess.run(
                [sys.executable, "-c", script],
                cwd=REPOSITORY,
                env=environment,
                check=True,
                capture_output=True,
                text=True,
            )
            outputs.append(completed.stdout.strip())
        self.assertEqual(outputs, [outputs[0]] * len(outputs))
        self.assertEqual(
            outputs[0],
            "29c5398813d373cf40ff7b914f1c278958302a705f30a35cbfca53f8cbbd6a61",
        )

    def test_byte_budget_preflight_precedes_full_event_materialization(
        self,
    ) -> None:
        exact_size = len(encode_trace_jsonl(_generated_document()))
        with patch(
            "burstserve.sim.trace._MAX_TRACE_BYTES",
            exact_size,
        ):
            at_limit = _generated_document()
            self.assertEqual(len(encode_trace_jsonl(at_limit)), exact_size)

        with (
            patch(
                "burstserve.sim.trace._MAX_TRACE_BYTES",
                exact_size - 1,
            ),
            patch(
                "burstserve.sim.trace._generate_events",
                side_effect=AssertionError(
                    "full event generation ran before byte preflight"
                ),
            ) as full_generation,
        ):
            with self.assertRaisesRegex(ValueError, "byte-size"):
                _generated_document()
            full_generation.assert_not_called()

    def test_timestamp_and_deadline_overflow_fail_before_materialization(
        self,
    ) -> None:
        tenants = (TenantPolicy("tenant-a"),)
        signatures = (_signature(),)
        overflowing_configs = (
            _config(
                seed=0,
                start_ns=(1 << 64) - 1,
                request_count=1,
                minimum_interarrival_ns=1,
                interarrival_jitter_ns=0,
                deadline_offset_ns=1,
            ),
            _config(
                seed=0,
                start_ns=(1 << 64) - 2,
                request_count=1,
                minimum_interarrival_ns=1,
                interarrival_jitter_ns=0,
                deadline_offset_ns=2,
            ),
        )
        for config in overflowing_configs:
            with (
                self.subTest(config=config),
                patch(
                    "burstserve.sim.trace._generate_events",
                    side_effect=AssertionError(
                        "overflow reached full materialization"
                    ),
                ) as full_generation,
            ):
                with self.assertRaisesRegex(ValueError, "exceeds uint64"):
                    generate_arrival_trace(
                        config,
                        tenants=tenants,
                        signatures=signatures,
                    )
                full_generation.assert_not_called()


class TraceCodecTest(unittest.TestCase):
    def test_manual_round_trip_is_byte_identical(self) -> None:
        document = _manual_document()
        encoded = encode_trace_jsonl(document)
        decoded = decode_trace_jsonl(encoded)
        self.assertEqual(decoded, document)
        self.assertEqual(encode_trace_jsonl(decoded), encoded)
        self.assertTrue(document.stable_id.startswith("trc1-"))
        header = json.loads(encoded.splitlines()[0])
        self.assertEqual(header["schema"], TRACE_SCHEMA)
        self.assertEqual(header["schema_version"], TRACE_SCHEMA_VERSION)
        self.assertEqual(
            header["generator_algorithm"],
            MANUAL_TRACE_ALGORITHM_VERSION,
        )

    def test_rejects_invalid_utf8_bom_line_endings_and_blank_lines(self) -> None:
        encoded = encode_trace_jsonl(_manual_document())
        with self.assertRaises(TypeError):
            decode_trace_jsonl(encoded.decode("utf-8"))  # type: ignore[arg-type]
        for bad in (
            b"",
            b"\xff\n",
            b"\xef\xbb\xbf" + encoded,
            encoded.rstrip(b"\n"),
            encoded.replace(b"\n", b"\r\n"),
            encoded.split(b"\n", 1)[0] + b"\n\n"
            + encoded.split(b"\n", 1)[1],
        ):
            with self.subTest(bad=bad[:20]):
                with self.assertRaises((TypeError, ValueError)):
                    decode_trace_jsonl(bad)

    def test_wire_prescan_rejects_oversized_inputs_before_json_parser(
        self,
    ) -> None:
        oversized_record_count = b"{}\n" * (
            trace_module._MAX_TRACE_RECORDS + 1
        )
        oversized_line = (
            b"x" * (trace_module._MAX_TRACE_LINE_BYTES + 1) + b"\n"
        )
        for encoded, message in (
            (oversized_record_count, "record-count"),
            (oversized_line, "line-size"),
        ):
            with (
                self.subTest(message=message),
                patch.object(
                    trace_module,
                    "_strict_json_loads",
                    side_effect=AssertionError("strict JSON parser entered"),
                ) as strict_parser,
            ):
                with self.assertRaisesRegex(ValueError, message):
                    decode_trace_jsonl(encoded)
                strict_parser.assert_not_called()

    def test_wire_limits_are_inclusive_at_the_exact_boundary(self) -> None:
        class StrictParserReached(Exception):
            pass

        self.assertEqual(trace_module._MAX_TRACE_BYTES, 64 * 1024 * 1024)
        self.assertEqual(
            trace_module._MAX_TRACE_LINE_BYTES,
            2 * 1024 * 1024,
        )
        self.assertEqual(trace_module._MAX_TRACE_RECORDS, 1_000_000)

        exact_line = b"x" * trace_module._MAX_TRACE_LINE_BYTES + b"\n"
        exact_records = b"{}\n" * trace_module._MAX_TRACE_RECORDS
        for encoded in (exact_line, exact_records):
            with (
                self.subTest(encoded_size=len(encoded)),
                patch.object(
                    trace_module,
                    "_strict_json_loads",
                    side_effect=StrictParserReached,
                ) as strict_parser,
            ):
                with self.assertRaises(StrictParserReached):
                    decode_trace_jsonl(encoded)
                strict_parser.assert_called_once()

        with (
            patch.object(trace_module, "_MAX_TRACE_BYTES", 3),
            patch.object(
                trace_module,
                "_strict_json_loads",
                side_effect=StrictParserReached,
            ) as strict_parser,
        ):
            with self.assertRaises(StrictParserReached):
                decode_trace_jsonl(b"{}\n")
            strict_parser.assert_called_once()

        with (
            patch.object(trace_module, "_MAX_TRACE_BYTES", 3),
            patch.object(
                trace_module,
                "_strict_json_loads",
                side_effect=AssertionError("strict JSON parser entered"),
            ) as strict_parser,
        ):
            with self.assertRaisesRegex(ValueError, "byte-size"):
                decode_trace_jsonl(b"{} \n")
            strict_parser.assert_not_called()

    def test_wire_prescan_depth_guard_is_stable_and_string_aware(self) -> None:
        class StrictParserReached(Exception):
            pass

        exact_depth = (
            b"[" * trace_module._MAX_JSON_NESTING
            + b"]" * trace_module._MAX_JSON_NESTING
            + b"\n"
        )
        too_deep = (
            b"[" * (trace_module._MAX_JSON_NESTING + 1)
            + b"]" * (trace_module._MAX_JSON_NESTING + 1)
            + b"\n"
        )
        brackets_in_string = (
            b'{"escaped":"[[[\\\\\\\"{{{]]]}}}"}\n'
        )
        with patch.object(
            trace_module,
            "_strict_json_loads",
            side_effect=StrictParserReached,
        ) as strict_parser:
            with self.assertRaises(StrictParserReached):
                decode_trace_jsonl(exact_depth)
            strict_parser.assert_called_once()

        with patch.object(
            trace_module,
            "_strict_json_loads",
            side_effect=AssertionError("strict JSON parser entered"),
        ) as strict_parser:
            with self.assertRaisesRegex(ValueError, "nesting"):
                decode_trace_jsonl(too_deep)
            strict_parser.assert_not_called()

        with patch.object(
            trace_module,
            "_strict_json_loads",
            side_effect=StrictParserReached,
        ) as strict_parser:
            with self.assertRaises(StrictParserReached):
                decode_trace_jsonl(brackets_in_string)
            strict_parser.assert_called_once()

    def test_recursion_errors_are_normalized_at_decoder_entry(self) -> None:
        encoded = encode_trace_jsonl(_manual_document())
        with patch.object(
            trace_module,
            "_strict_json_loads",
            side_effect=RecursionError("implementation-specific message"),
        ):
            with self.assertRaisesRegex(ValueError, "nesting"):
                decode_trace_jsonl(encoded)

    def test_decode_and_stable_id_do_not_call_full_document_encoder(self) -> None:
        document = _manual_document()
        encoded = encode_trace_jsonl(document)
        expected_id = document.stable_id
        with patch.object(
            trace_module,
            "encode_trace_jsonl",
            side_effect=AssertionError("full encoder entered"),
        ) as encoder:
            decoded = decode_trace_jsonl(encoded)
            self.assertEqual(decoded, document)
            self.assertEqual(decoded.stable_id, expected_id)
        encoder.assert_not_called()

    def test_rejects_noncanonical_json_forms(self) -> None:
        encoded = encode_trace_jsonl(_manual_document())
        first, remainder = encoded.split(b"\n", 1)
        decoded_header = json.loads(first)
        unsorted = json.dumps(
            decoded_header,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=False,
        ).encode("utf-8") + b"\n" + remainder
        if unsorted == encoded:
            unsorted = b" " + encoded
        variants = (
            b" " + encoded,
            first + b" \n" + remainder,
            unsorted,
            encoded.replace(b"tenant-a", b"tenant-\\u0061", 1),
        )
        for variant in variants:
            with self.subTest(variant=variant[:40]):
                with self.assertRaisesRegex(ValueError, "canonical"):
                    decode_trace_jsonl(variant)

    def test_rejects_duplicate_keys_float_nan_and_surrogate(self) -> None:
        encoded = encode_trace_jsonl(_manual_document())
        first, remainder = encoded.split(b"\n", 1)
        duplicate = first.replace(
            b'{"generator_algorithm":',
            b'{"schema_version":1,"generator_algorithm":',
            1,
        )
        self.assertIn(b'"schema_version":1', first)
        bad_values = (
            duplicate + b"\n" + remainder,
            first.replace(
                b'"maximum_sleeper_credit_ns":7',
                b'"maximum_sleeper_credit_ns":1.0',
            )
            + b"\n"
            + remainder,
            first.replace(
                b'"maximum_sleeper_credit_ns":7',
                b'"maximum_sleeper_credit_ns":NaN',
            )
            + b"\n"
            + remainder,
            encoded.replace(b"tenant-a", b"tenant-\\ud800", 1),
        )
        for bad in bad_values:
            with self.subTest(bad=bad[:50]):
                with self.assertRaises(ValueError):
                    decode_trace_jsonl(bad)

    def test_rejects_unknown_fields_at_every_record_level(self) -> None:
        base = _records(_manual_document())
        mutations: list[list[dict[str, object]]] = []

        header_unknown = json.loads(json.dumps(base))
        header_unknown[0]["unknown"] = 1
        mutations.append(header_unknown)

        signature_unknown = json.loads(json.dumps(base))
        signature_unknown[1]["unknown"] = 1
        mutations.append(signature_unknown)

        signature_payload_unknown = json.loads(json.dumps(base))
        signature_payload_unknown[1]["signature"]["unknown"] = 1
        mutations.append(signature_payload_unknown)

        event_unknown = json.loads(json.dumps(base))
        event_unknown[2]["unknown"] = 1
        mutations.append(event_unknown)

        event_payload_unknown = json.loads(json.dumps(base))
        event_payload_unknown[2]["event"]["unknown"] = 1
        mutations.append(event_payload_unknown)

        for records in mutations:
            with self.subTest(records=records):
                with self.assertRaises(ValueError):
                    decode_trace_jsonl(_encode_records(records))

    def test_rejects_unsupported_schema_algorithm_and_model_versions(self) -> None:
        base = _records(_manual_document())
        changes = (
            ("schema_version", 2),
            ("schema", "other.trace"),
            ("model_schema_version", 999),
            ("generator_algorithm", "python-random-latest"),
        )
        for key, value in changes:
            records = json.loads(json.dumps(base))
            records[0][key] = value
            with self.subTest(key=key):
                with self.assertRaises(
                    (UnsupportedTraceSchemaVersionError, ValueError)
                ):
                    decode_trace_jsonl(_encode_records(records))

        for record_index in (1, 2):
            records = json.loads(json.dumps(base))
            records[record_index]["schema_version"] = True
            with self.subTest(record_index=record_index):
                with self.assertRaises(UnsupportedTraceSchemaVersionError):
                    decode_trace_jsonl(_encode_records(records))

    def test_small_wire_header_cannot_request_unbounded_regeneration(self) -> None:
        for claimed_count, message in (
            (10**12, "at most"),
            (499_998, "observed request_arrival"),
        ):
            records = _records(_generated_document())
            parameters = records[0]["generator_parameters"]
            for parameter in parameters:
                if parameter[0] == "request_count":
                    parameter[1] = claimed_count
                    break
            else:
                self.fail("generated header omitted request_count")
            encoded = _encode_records(records)
            self.assertLess(len(encoded), 64 * 1024)
            with self.subTest(claimed_count=claimed_count):
                with self.assertRaisesRegex(ValueError, message):
                    decode_trace_jsonl(encoded)

    def test_rejects_bad_signature_identity_and_record_order(self) -> None:
        document = _generated_document()
        base = _records(document)
        bad_identity = json.loads(json.dumps(base))
        bad_identity[1]["signature_id"] = (
            "wls2-" + "0" * 64
        )
        with self.assertRaisesRegex(ValueError, "signature_id"):
            decode_trace_jsonl(_encode_records(bad_identity))

        signature_count = len(document.signatures)
        reversed_signatures = json.loads(json.dumps(base))
        signature_slice = reversed_signatures[1 : 1 + signature_count]
        reversed_signatures[1 : 1 + signature_count] = reversed(
            signature_slice
        )
        with self.assertRaisesRegex(ValueError, "sorted"):
            decode_trace_jsonl(_encode_records(reversed_signatures))

        signature_after_event = json.loads(json.dumps(base))
        moved = signature_after_event.pop(1)
        signature_after_event.append(moved)
        with self.assertRaisesRegex(ValueError, "precede"):
            decode_trace_jsonl(_encode_records(signature_after_event))

    def test_rejects_noncanonical_embedded_lists_and_sequences(self) -> None:
        base = _records(_manual_document())

        unsorted_payload = json.loads(json.dumps(base))
        tenant_event = unsorted_payload[2]["event"]
        tenant_event["payload"] = list(reversed(tenant_event["payload"]))
        with self.assertRaisesRegex(ValueError, "canonical"):
            decode_trace_jsonl(_encode_records(unsorted_payload))

        sequence_gap = json.loads(json.dumps(base))
        sequence_gap[3]["event"]["sequence"] = 99
        with self.assertRaisesRegex(ValueError, "contiguous"):
            decode_trace_jsonl(_encode_records(sequence_gap))

        reordered_events = json.loads(json.dumps(base))
        reordered_events[2], reordered_events[3] = (
            reordered_events[3],
            reordered_events[2],
        )
        with self.assertRaisesRegex(ValueError, "canonical|contiguous"):
            decode_trace_jsonl(_encode_records(reordered_events))

    def test_integer_decoding_is_independent_of_process_digit_limit(self) -> None:
        document = _manual_document()
        valid = encode_trace_jsonl(document)
        first, remainder = valid.split(b"\n", 1)
        oversized = first.replace(
            b'"maximum_sleeper_credit_ns":7',
            b'"maximum_sleeper_credit_ns":' + b"9" * 5_000,
        ) + b"\n" + remainder
        negative = first.replace(
            b'"maximum_sleeper_credit_ns":7',
            b'"maximum_sleeper_credit_ns":-1',
        ) + b"\n" + remainder
        self.assertNotEqual(valid, oversized)

        original_limit = sys.get_int_max_str_digits()
        bounded_limit = original_limit if original_limit else 4_300
        outcomes: list[tuple[str, str]] = []
        valid_ids: list[str] = []
        try:
            for digit_limit in (bounded_limit, 0):
                sys.set_int_max_str_digits(digit_limit)
                valid_ids.append(decode_trace_jsonl(valid).stable_id)
                try:
                    decode_trace_jsonl(oversized)
                except ValueError as error:
                    outcomes.append((type(error).__name__, str(error)))
                else:
                    self.fail("5000-digit wire integer was accepted")
        finally:
            sys.set_int_max_str_digits(original_limit)
        self.assertEqual(valid_ids, [document.stable_id, document.stable_id])
        self.assertEqual(outcomes[0], outcomes[1])
        self.assertIn("magnitude exceeds uint64", outcomes[0][1])
        with self.assertRaisesRegex(ValueError, "at least 0"):
            decode_trace_jsonl(negative)

    def test_uint64_wire_boundary_matches_programmatic_boundary(self) -> None:
        uint64_max = (1 << 64) - 1
        document = TraceDocument(
            header=TraceHeader(
                maximum_sleeper_credit_ns=uint64_max,
            ),
            events=(
                make_tenant_arrival(
                    sequence=0,
                    timestamp_ns=uint64_max,
                    policy=TenantPolicy("tenant-a", uint64_max, 1),
                ),
            ),
        )
        encoded = encode_trace_jsonl(document)
        self.assertEqual(decode_trace_jsonl(encoded), document)
        signature_document = TraceDocument(
            signatures=(_signature(width=uint64_max),),
        )
        self.assertEqual(
            decode_trace_jsonl(encode_trace_jsonl(signature_document)),
            signature_document,
        )

        with self.assertRaisesRegex(ValueError, "at most"):
            TraceHeader(maximum_sleeper_credit_ns=1 << 64)
        with self.assertRaisesRegex(ValueError, "at most"):
            TraceDocument(signatures=(_signature(width=1 << 64),))
        very_large_integer = 10**4_999
        with self.assertRaisesRegex(ValueError, "at most"):
            TraceHeader(
                maximum_sleeper_credit_ns=very_large_integer,
            )
        with self.assertRaisesRegex(ValueError, "at most"):
            TraceDocument(
                signatures=(_signature(width=very_large_integer),),
            )
        oversized_event = TraceEvent(
            sequence=0,
            timestamp_ns=1 << 64,
            kind=TENANT_ARRIVAL,
            subject_id="tenant-a",
            payload=(
                ("weight_denominator", 1),
                ("weight_numerator", 1),
            ),
        )
        with self.assertRaisesRegex(ValueError, "at most"):
            TraceDocument(events=(oversized_event,))

        first, remainder = encoded.split(b"\n", 1)
        oversized_wire = first.replace(
            str(uint64_max).encode("ascii"),
            str(1 << 64).encode("ascii"),
            1,
        ) + b"\n" + remainder
        with self.assertRaisesRegex(ValueError, "magnitude exceeds uint64"):
            decode_trace_jsonl(oversized_wire)


class TraceSemanticValidationTest(unittest.TestCase):
    def test_all_lifecycle_kinds_have_a_frozen_total_order(self) -> None:
        document = _manual_document()
        self.assertEqual(
            [event.kind for event in document.events],
            [
                TENANT_ARRIVAL,
                TENANT_ARRIVAL,
                REQUEST_ARRIVAL,
                REQUEST_ARRIVAL,
                REQUEST_CANCEL,
                REQUEST_COMPLETE,
                REQUEST_DEADLINE,
            ],
        )
        self.assertEqual(
            list(document.events),
            sorted(document.events, key=event_order_key),
        )
        self.assertEqual(
            [event.timestamp_ns for event in document.events],
            sorted(event.timestamp_ns for event in document.events),
        )

    def test_rejects_unknown_tenant_signature_and_request_references(self) -> None:
        signature = _signature()
        unknown_tenant_spec = RequestSpec(
            "request-a",
            "missing",
            signature,
            10,
            None,
            "urgent",
        )
        with self.assertRaisesRegex(ValueError, "tenant.*not arrived"):
            TraceDocument(
                signatures=(signature,),
                events=(
                    make_request_arrival(
                        sequence=0,
                        spec=unknown_tenant_spec,
                    ),
                ),
            )

        request_event = TraceEvent(
            sequence=1,
            timestamp_ns=10,
            kind=REQUEST_ARRIVAL,
            subject_id="request-a",
            payload=(
                ("deadline_ns", None),
                ("request_kind", "urgent"),
                ("signature_id", "wls2-" + "0" * 64),
                ("tenant_id", "tenant-a"),
            ),
        )
        with self.assertRaisesRegex(ValueError, "unknown signature"):
            TraceDocument(
                signatures=(signature,),
                events=(
                    make_tenant_arrival(
                        sequence=0,
                        timestamp_ns=0,
                        policy=TenantPolicy("tenant-a"),
                    ),
                    request_event,
                ),
            )

        with self.assertRaisesRegex(ValueError, "has not arrived"):
            TraceDocument(
                events=(
                    make_request_cancel(
                        sequence=0,
                        timestamp_ns=1,
                        request_id="missing",
                    ),
                ),
            )

    def test_rejects_duplicate_tenant_request_and_terminal_events(self) -> None:
        tenant = make_tenant_arrival(
            sequence=0,
            timestamp_ns=0,
            policy=TenantPolicy("tenant-a"),
        )
        with self.assertRaisesRegex(ValueError, "tenant.*more than once"):
            TraceDocument(
                events=(
                    tenant,
                    replace(tenant, sequence=1, timestamp_ns=1),
                ),
            )

        signature = _signature()
        spec = RequestSpec(
            "request-a",
            "tenant-a",
            signature,
            10,
            None,
            "video",
        )
        request = make_request_arrival(sequence=1, spec=spec)
        with self.assertRaisesRegex(ValueError, "request.*more than once"):
            TraceDocument(
                signatures=(signature,),
                events=(
                    tenant,
                    request,
                    replace(request, sequence=2, timestamp_ns=11),
                ),
            )

        with self.assertRaisesRegex(ValueError, "terminal event"):
            TraceDocument(
                signatures=(signature,),
                events=(
                    tenant,
                    request,
                    make_request_complete(
                        sequence=2,
                        timestamp_ns=20,
                        request_id="request-a",
                    ),
                    make_request_cancel(
                        sequence=3,
                        timestamp_ns=21,
                        request_id="request-a",
                    ),
                ),
            )

    def test_deadline_records_are_exact_and_closed(self) -> None:
        document = _manual_document()
        without_deadline = TraceDocument
        with self.assertRaisesRegex(ValueError, "closure mismatch"):
            without_deadline(
                header=document.header,
                signatures=document.signatures,
                events=document.events[:-1],
            )

        wrong_deadline = replace(
            document.events[-1],
            timestamp_ns=101,
        )
        with self.assertRaisesRegex(ValueError, "timestamp"):
            _replace_event(document, len(document.events) - 1, wrong_deadline)

        no_deadline_event = make_request_deadline(
            sequence=5,
            timestamp_ns=30,
            request_id="request-b",
        )
        events = list(document.events)
        events.insert(5, no_deadline_event)
        events = [
            replace(event, sequence=index)
            for index, event in enumerate(sorted(events, key=event_order_key))
        ]
        with self.assertRaisesRegex(ValueError, "without a deadline"):
            TraceDocument(
                header=document.header,
                signatures=document.signatures,
                events=tuple(events),
            )

        duplicate = list(document.events)
        duplicate.append(
            make_request_deadline(
                sequence=0,
                timestamp_ns=100,
                request_id="request-a",
            )
        )
        duplicate = [
            replace(event, sequence=index)
            for index, event in enumerate(
                sorted(duplicate, key=event_order_key)
            )
        ]
        with self.assertRaisesRegex(ValueError, "more than one deadline"):
            TraceDocument(
                header=document.header,
                signatures=document.signatures,
                events=tuple(duplicate),
            )

    def test_rejects_payload_shape_weight_normalization_and_unknown_kind(self) -> None:
        document = _manual_document()
        tenant = document.events[0]
        with self.assertRaisesRegex(ValueError, "payload fields"):
            _replace_event(
                document,
                0,
                replace(
                    tenant,
                    payload=tenant.payload + (("extra", 1),),
                ),
            )

        non_normal_weight = TraceEvent(
            sequence=0,
            timestamp_ns=0,
            kind=TENANT_ARRIVAL,
            subject_id="tenant-a",
            payload=(
                ("weight_denominator", 2),
                ("weight_numerator", 2),
            ),
        )
        with self.assertRaisesRegex(ValueError, "normalized"):
            TraceDocument(events=(non_normal_weight,))

        cancel = document.events[4]
        with self.assertRaisesRegex(ValueError, "payload must be empty"):
            _replace_event(
                document,
                4,
                replace(cancel, payload=(("why", "test"),)),
            )

        unknown = TraceEvent(0, 0, "action_selected", "request-a")
        with self.assertRaisesRegex(ValueError, "unsupported lifecycle"):
            TraceDocument(events=(unknown,))

    def test_rejects_time_or_tie_order_regression(self) -> None:
        document = _manual_document()
        swapped = list(document.events)
        swapped[0], swapped[1] = swapped[1], swapped[0]
        swapped = [
            replace(event, sequence=index)
            for index, event in enumerate(swapped)
        ]
        with self.assertRaisesRegex(ValueError, "canonical"):
            TraceDocument(
                header=document.header,
                signatures=document.signatures,
                events=tuple(swapped),
            )
        regressed = list(document.events)
        regressed[4] = replace(regressed[4], timestamp_ns=5)
        with self.assertRaisesRegex(ValueError, "canonical"):
            TraceDocument(
                header=document.header,
                signatures=document.signatures,
                events=tuple(regressed),
            )


class TraceReplayTest(unittest.TestCase):
    def test_replay_updates_only_abstract_lifecycle_state(self) -> None:
        document = _manual_document()
        replay = replay_trace(document)
        self.assertEqual(len(replay.frames), len(document.events))
        self.assertEqual(replay.trace_id, document.stable_id)
        self.assertEqual(replay.final_state.current_time_ns, 100)
        self.assertEqual(
            replay.final_state.request("request-a").status,
            "completed",
        )
        self.assertEqual(
            replay.final_state.request("request-a").completed_steps,
            3,
        )
        self.assertEqual(
            replay.final_state.request("request-a").last_progress_ns,
            90,
        )
        self.assertEqual(
            replay.final_state.request("request-b").status,
            "rejected",
        )
        cancel_frame = next(
            frame
            for frame in replay.frames
            if frame.event.subject_id == "request-b"
            and frame.event.kind == REQUEST_CANCEL
        )
        self.assertEqual(cancel_frame.event.kind, REQUEST_CANCEL)
        self.assertIsNotNone(cancel_frame.request_state_after)
        self.assertEqual(
            cancel_frame.request_state_after.status,
            "rejected",
        )
        self.assertFalse(replay.final_state.tenant("tenant-a").active)
        self.assertFalse(replay.final_state.tenant("tenant-b").active)
        for tenant in replay.final_state.tenants:
            self.assertEqual(tenant.ledger.canonical_service_ns, 0)
            self.assertEqual(tenant.ledger.resource_debt.dominant_ns, 0)

    def test_deadline_event_advances_time_without_fabricating_completion(self) -> None:
        document = _generated_document()
        replay = replay_trace(document)
        deadline_frames = [
            frame
            for frame in replay.frames
            if frame.event.kind == REQUEST_DEADLINE
        ]
        self.assertEqual(
            len(deadline_frames),
            _config().request_count,
        )
        for frame in deadline_frames:
            self.assertIsNone(frame.request_state_after)
            self.assertIsNone(frame.tenant_state_after)
            request = replay.final_state.request(frame.event.subject_id)
            self.assertEqual(request.status, "queued")
            self.assertEqual(request.completed_steps, 0)

    def test_replay_retains_only_linear_incremental_state_at_scale(self) -> None:
        def run(request_count: int):
            document = generate_arrival_trace(
                _config(
                    request_count=request_count,
                    request_id_prefix="scale",
                ),
                tenants=(TenantPolicy("tenant-a"),),
                signatures=(_signature("scale-model"),),
            )
            return replay_trace(document)

        small = run(1_000)
        large = run(3_000)

        def retained_deltas(replay: TraceReplayResult) -> int:
            return sum(
                int(frame.request_state_after is not None)
                + int(frame.tenant_state_after is not None)
                for frame in replay.frames
            )

        self.assertEqual(len(small.frames), 2 * 1_000 + 1)
        self.assertEqual(len(large.frames), 2 * 3_000 + 1)
        self.assertEqual(retained_deltas(small), 1_000 + 2)
        self.assertEqual(retained_deltas(large), 3_000 + 2)
        self.assertLess(retained_deltas(large), 3 * retained_deltas(small))
        self.assertTrue(all(not hasattr(frame, "state") for frame in large.frames))
        self.assertEqual(len(large.final_state.requests), 3_000)
        self.assertTrue(
            all(request.status == "queued" for request in large.final_state.requests)
        )

    def test_replay_constructs_scheduler_state_once_at_the_end(self) -> None:
        original_scheduler_state = trace_module.SchedulerState
        construction_count = [0]

        class CountingSchedulerStateMeta(type):
            def __call__(cls, *args, **kwargs):
                construction_count[0] += 1
                return original_scheduler_state(*args, **kwargs)

            def __instancecheck__(cls, instance):
                return isinstance(instance, original_scheduler_state)

        class CountingSchedulerState(metaclass=CountingSchedulerStateMeta):
            pass

        with patch.object(
            trace_module,
            "SchedulerState",
            CountingSchedulerState,
        ):
            replay = replay_trace(_manual_document())

        self.assertEqual(construction_count, [1])
        self.assertIsInstance(replay.final_state, original_scheduler_state)

    def test_tenant_reactivation_uses_header_credit_bound(self) -> None:
        signature = _signature(total_steps=1)
        first = RequestSpec(
            "request-a",
            "tenant-a",
            signature,
            1,
            None,
            "video",
        )
        second = RequestSpec(
            "request-b",
            "tenant-a",
            signature,
            10,
            None,
            "video",
        )
        events = (
            make_tenant_arrival(
                sequence=0,
                timestamp_ns=0,
                policy=TenantPolicy("tenant-a"),
            ),
            make_request_arrival(sequence=1, spec=first),
            make_request_complete(
                sequence=2,
                timestamp_ns=2,
                request_id="request-a",
            ),
            make_request_arrival(sequence=3, spec=second),
        )
        document = TraceDocument(
            header=TraceHeader(maximum_sleeper_credit_ns=5),
            signatures=(signature,),
            events=events,
        )
        replay = replay_trace(document)
        self.assertTrue(replay.final_state.tenant("tenant-a").active)
        self.assertEqual(
            replay.final_state.request("request-b").status,
            "queued",
        )

    def test_replay_frame_rejects_forged_lifecycle_deltas(self) -> None:
        document = _manual_document()
        replay = replay_trace(document)
        arrival = next(
            frame.event
            for frame in replay.frames
            if frame.event.kind == REQUEST_ARRIVAL
        )
        cancel = next(
            frame.event
            for frame in replay.frames
            if frame.event.kind == REQUEST_CANCEL
        )
        queued = next(
            frame.request_state_after
            for frame in replay.frames
            if (
                frame.event.kind == REQUEST_ARRIVAL
                and frame.event.subject_id == cancel.subject_id
            )
        )
        self.assertIsNotNone(queued)

        with self.assertRaisesRegex(ValueError, "must carry"):
            ReplayFrame(event=arrival)
        with self.assertRaisesRegex(ValueError, "rejected"):
            ReplayFrame(
                event=cancel,
                request_state_after=queued,
            )
        with self.assertRaisesRegex(ValueError, "progress"):
            ReplayFrame(
                event=cancel,
                request_state_after=replace(
                    queued,
                    completed_steps=1,
                    status="rejected",
                    last_progress_ns=cancel.timestamp_ns + 1,
                ),
            )
        with self.assertRaisesRegex(ValueError, "cannot carry"):
            ReplayFrame(
                event=next(
                    frame.event
                    for frame in replay.frames
                    if frame.event.kind == REQUEST_DEADLINE
                ),
                request_state_after=queued,
            )

        tenant_arrival_frame = next(
            frame
            for frame in replay.frames
            if frame.event.kind == TENANT_ARRIVAL
        )
        tenant_after = tenant_arrival_frame.tenant_state_after
        self.assertIsNotNone(tenant_after)
        with self.assertRaisesRegex(ValueError, "pristine"):
            ReplayFrame(
                event=tenant_arrival_frame.event,
                tenant_state_after=replace(
                    tenant_after,
                    ledger=TenantLedger(
                        tenant_id=tenant_after.tenant_id,
                        canonical_service_ns=999,
                        last_active_ns=(
                            tenant_arrival_frame.event.timestamp_ns + 1
                        ),
                    ),
                ),
            )

        arrival_frame = next(
            frame
            for frame in replay.frames
            if (
                frame.event.kind == REQUEST_ARRIVAL
                and frame.tenant_state_after is not None
            )
        )
        self.assertIsNotNone(arrival_frame.request_state_after)
        self.assertIsNotNone(arrival_frame.tenant_state_after)
        with self.assertRaisesRegex(ValueError, "last_active_ns"):
            ReplayFrame(
                event=arrival_frame.event,
                request_state_after=arrival_frame.request_state_after,
                tenant_state_after=replace(
                    arrival_frame.tenant_state_after,
                    ledger=replace(
                        arrival_frame.tenant_state_after.ledger,
                        last_active_ns=arrival_frame.event.timestamp_ns + 1,
                    ),
                ),
            )

    def test_replay_result_binds_document_frames_and_final_state(self) -> None:
        document = _manual_document()
        replay = replay_trace(document)
        empty_at_final_time = SchedulerState(
            current_time_ns=replay.final_state.current_time_ns,
        )

        with self.assertRaisesRegex(ValueError, "cover every"):
            TraceReplayResult(
                document=document,
                frames=replay.frames[:-1],
                final_state=replay.final_state,
            )
        with self.assertRaisesRegex(ValueError, "final_state"):
            TraceReplayResult(
                document=document,
                frames=replay.frames,
                final_state=empty_at_final_time,
            )

        other_document = _generated_document()
        with self.assertRaisesRegex(ValueError, "cover every|bound document"):
            TraceReplayResult(
                document=other_document,
                frames=replay.frames,
                final_state=replay.final_state,
            )

        self.assertEqual(replay.trace_id, document.stable_id)
        with self.assertRaises(TypeError):
            TraceReplayResult(  # type: ignore[call-arg]
                trace_id="trc1-" + "0" * 64,
                document=document,
                frames=replay.frames,
                final_state=replay.final_state,
            )

    def test_replay_rejects_non_document_inputs(self) -> None:
        with self.assertRaises(TypeError):
            replay_trace(b"not a document")  # type: ignore[arg-type]
        with self.assertRaises(TypeError):
            encode_trace_jsonl("not a document")  # type: ignore[arg-type]


if __name__ == "__main__":
    unittest.main()
