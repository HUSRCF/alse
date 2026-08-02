from __future__ import annotations

import unittest
from pathlib import Path

import burstserve.amd_cu_runner as amd
import burstserve.smctrl_runner as smctrl_runner
from burstserve.gate_a_results import validate_masked_tpc_matrix


def _manifest(**overrides):
    manifest = {
        "schema_version": amd.AMD_MANIFEST_SCHEMA_VERSION,
        "manifest_id": "amd-r9700-gfx1201-test",
        "hardware": {
            "gpu_name": "AMD Radeon AI PRO R9700",
            "gcn_arch": "gfx1201",
            "gpu_uuid": "GPU-55d91e3d9b7d11e6",
            "maskable_units": 32,
            "device_ordinal": 0,
        },
        "source": {"probe": "native/amd_cu_probe/cu_probe.hip"},
        "matrix": {
            "modes": ["global_cu_mask", "stream_cu_mask"],
            "mask_bits": [0, 15, 16, 31],
            "trials_per_cell": 3,
            "allowed_observed_unit_count": [1],
            "iterations": 256,
            "blocks": 256,
            "threads_per_block": 256,
        },
        "reduced_contract": {
            "document": "docs/amd-reduced-contract.md",
            "applies_to": "single-card single-operator gfx1201",
            "dropped_guards": ["xid_monitor", "exclusive_reservation"],
        },
    }
    manifest.update(overrides)
    return manifest


class ScopeOfTheReductionTest(unittest.TestCase):
    """The AMD reduction must not be reachable from the CUDA line.

    Prose in a design document does not prevent a relaxed contract from
    spreading. These assertions do: the CUDA gate must still refuse what it
    refused before, and an AMD cell must be unusable as CUDA evidence.
    """

    def test_cuda_gate_still_refuses_every_masked_mode_unpromoted(self):
        from test_smctrl_runner import _gate_manifest

        content = _gate_manifest()["content"]
        gpu = {"name": "GPU", "uuid": "uuid"}
        for mode in ("global", "next", "stream"):
            with self.subTest(mode):
                _, allowed = smctrl_runner.evaluate_gate_manifest_policy(
                    content,
                    mode=mode,
                    physical_gpu=3,
                    gpu=gpu,
                    driver_version=13030,
                    experimental_mask_off=None,
                    timeout_s=10,
                    maximum_used_mib=1024,
                    iterations=100,
                    blocks=4096,
                    threads_per_block=256,
                    trial=0,
                    enabled_tpc=0,
                )
                self.assertFalse(allowed)

    def test_amd_cells_cannot_be_counted_as_cuda_evidence(self):
        # The schema strings must differ, and the CUDA cell schema is pinned
        # exactly by its validators, so an AMD cell cannot slip into a CUDA
        # aggregate even if it were placed in the same run root.
        self.assertNotEqual(
            amd.AMD_CELL_SCHEMA_VERSION, smctrl_runner.CELL_SCHEMA_VERSION
        )
        self.assertNotIn("amd", smctrl_runner.CELL_SCHEMA_VERSION)

    def test_amd_module_does_not_reach_into_the_cuda_gate(self):
        """Check real imports, not prose.

        The docstring names smctrl_runner to say the module deliberately does
        not use it, so a substring search would flag the very sentence that
        documents the separation.
        """

        import ast

        tree = ast.parse(Path(amd.__file__).read_text(encoding="utf-8"))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imported.add(node.module or "")
                imported.update(
                    f"{node.module or ''}.{alias.name}" for alias in node.names
                )
        self.assertFalse(
            [name for name in imported if "smctrl_runner" in name],
            f"the AMD runner must not import the CUDA gate: {sorted(imported)}",
        )
        # And it must still import the shared, unmodified primitives.
        self.assertTrue(any("git_provenance" in n for n in imported))
        self.assertTrue(any("provenance" in n for n in imported))


class AmdManifestContractTest(unittest.TestCase):
    def test_a_well_formed_manifest_is_accepted(self):
        self.assertEqual(
            amd.validate_amd_manifest(_manifest())["manifest_id"],
            "amd-r9700-gfx1201-test",
        )

    def test_the_two_mechanism_requirement_is_not_part_of_the_reduction(self):
        """One mechanism confirms only the observation it came from.

        This is the check that makes a mapping falsifiable rather than
        self-reported, and AMD has two first-class mechanisms, so there is no
        reason for it to be relaxed here.
        """

        for label, modes in (
            ("single mechanism", ["stream_cu_mask"]),
            ("reordered", ["stream_cu_mask", "global_cu_mask"]),
            ("empty", []),
        ):
            with self.subTest(label):
                manifest = _manifest()
                manifest["matrix"]["modes"] = modes
                with self.assertRaises(amd.AmdContractError):
                    amd.validate_amd_manifest(manifest)

    def test_degenerate_bit_and_trial_counts_are_refused(self):
        for label, key, value in (
            ("one bit", "mask_bits", [0]),
            ("duplicate bits", "mask_bits", [0, 0]),
            ("out of range bit", "mask_bits", [0, 32]),
            ("one trial", "trials_per_cell", 1),
        ):
            with self.subTest(label):
                manifest = _manifest()
                manifest["matrix"][key] = value
                with self.assertRaises(amd.AmdContractError):
                    amd.validate_amd_manifest(manifest)

    def test_a_manifest_must_declare_that_it_is_reduced(self):
        for label, contract in (
            ("missing ledger", {"applies_to": "x", "dropped_guards": ["y"]}),
            ("wrong document", {"document": "README.md",
                                "applies_to": "single-card single-operator gfx1201",
                                "dropped_guards": ["y"]}),
            ("nothing declared dropped",
             {"document": "docs/amd-reduced-contract.md",
              "applies_to": "single-card single-operator gfx1201",
              "dropped_guards": []}),
            ("broader scope claimed",
             {"document": "docs/amd-reduced-contract.md",
              "applies_to": "all AMD hardware",
              "dropped_guards": ["y"]}),
        ):
            with self.subTest(label):
                manifest = _manifest()
                manifest["reduced_contract"] = contract
                with self.assertRaises(amd.AmdContractError):
                    amd.validate_amd_manifest(manifest)


class AmdCellContractTest(unittest.TestCase):
    def _cell(self, mode="stream_cu_mask", bit=0, **native_overrides):
        config = {
            "schema_version": amd.AMD_CELL_SCHEMA_VERSION,
            "mode": mode,
            "mask_bit": bit,
            "trial": 0,
            "blocks": 256,
            "gpu_uuid": "GPU-55d91e3d9b7d11e6",
        }
        native = {
            "schema_version": amd.AMD_PROBE_SCHEMA_VERSION,
            "status": "ok",
            "mode": mode,
            "requested_enabled_cu": bit,
            "readback_supported": True,
            "readback_matches_request": True,
            "observed_histogram": {"536870912": 256},
        }
        native.update(native_overrides)
        overrides = {}
        if mode == "global_cu_mask":
            overrides["ROC_GLOBAL_CU_MASK"] = hex(1 << bit)
        outcome = {
            "exit_code": 0,
            "native": native,
            "environment_overrides": overrides,
        }
        return config, outcome

    def test_a_clean_cell_yields_an_observation(self):
        config, outcome = self._cell()
        observation, errors = amd.validate_amd_cell(
            config=config, outcome=outcome, manifest=_manifest()
        )
        self.assertEqual(errors, [])
        self.assertEqual(observation["raw_unit_ids"], [536870912])

    def test_a_mask_the_runtime_ignored_is_refused(self):
        """The readback is why bits 32..63 were caught being ignored.

        Without it a mask that the runtime silently dropped looks exactly
        like a mask that worked, until the histogram happens to disagree.
        """

        config, outcome = self._cell(readback_matches_request=False)
        observation, errors = amd.validate_amd_cell(
            config=config, outcome=outcome, manifest=_manifest()
        )
        self.assertIsNone(observation)
        self.assertIn(
            "the runtime did not honour the requested CU mask", errors
        )

    def test_a_global_mask_may_not_leak_into_other_modes(self):
        config, outcome = self._cell(mode="baseline", bit=None)
        config["mask_bit"] = None
        outcome["environment_overrides"] = {"ROC_GLOBAL_CU_MASK": "0x1"}
        observation, errors = amd.validate_amd_cell(
            config=config, outcome=outcome, manifest=_manifest()
        )
        self.assertIsNone(observation)
        self.assertIn("a global mask leaked into a non-global cell", errors)


class DenseIndexTest(unittest.TestCase):
    def test_the_baseline_defines_the_index_space(self):
        raw = [536872960, 536870912, 536871936]
        mapping = amd.dense_index_map(raw)
        self.assertEqual(mapping, {536870912: 0, 536871936: 1, 536872960: 2})

    def test_the_shared_matrix_validator_is_used_unmodified(self):
        """The AMD line reuses the CUDA validator rather than a relaxed copy."""

        observations = [
            {
                "mode": mode,
                "tpc_bit": bit,
                "trial": trial,
                "physical_gpu": 0,
                "gpu_uuid": "GPU-55d91e3d9b7d11e6",
                "blocks": 256,
                "observed_blocks": 256,
                "observed_sms": [bit],
            }
            for mode in ("global_cu_mask", "stream_cu_mask")
            for bit in (0, 15, 16, 31)
            for trial in range(3)
        ]
        report = validate_masked_tpc_matrix(
            observations,
            matrix={
                "modes": ["global_cu_mask", "stream_cu_mask"],
                "tpc_bits": [0, 15, 16, 31],
                "trials_per_cell": 3,
                "allowed_observed_sm_count": [1],
                "iterations": 256,
                "blocks": 256,
                "threads_per_block": 256,
            },
            hardware={"sm_count": 32, "expected_tpc_count": 32},
            baseline_observed_sm_count=32,
            baseline_observed_sms=list(range(32)),
            baseline_gpu_uuid="GPU-55d91e3d9b7d11e6",
        )
        self.assertTrue(report["accepted"], report["errors"])


if __name__ == "__main__":
    unittest.main()
