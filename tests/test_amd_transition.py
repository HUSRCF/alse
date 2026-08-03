from __future__ import annotations

import ast
import sys
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

SOURCE = (SCRIPTS / "run_amd_transition.py").read_text(encoding="utf-8")


def _function(name: str) -> ast.FunctionDef:
    for node in ast.walk(ast.parse(SOURCE)):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{name} not found")


class ScheduleTest(unittest.TestCase):
    def setUp(self):
        # Importing the module loads libamdhip64, which is not present on the
        # CUDA host, so the pure function is exercised through its source.
        namespace: dict = {}
        exec(ast.unparse(_function("schedule_for")), namespace)
        self.schedule_for = namespace["schedule_for"]

    def test_each_quota_holds_for_the_dwell_before_changing(self):
        self.assertEqual(
            self.schedule_for(9, [8, 16, 32], 3),
            [8, 8, 8, 16, 16, 16, 32, 32, 32],
        )

    def test_it_wraps_round_robin_past_the_last_quota(self):
        self.assertEqual(
            self.schedule_for(8, [8, 16], 2), [8, 8, 16, 16, 8, 8, 16, 16]
        )

    def test_a_dwell_of_one_changes_every_step(self):
        self.assertEqual(self.schedule_for(4, [8, 32], 1), [8, 32, 8, 32])

    def test_the_plan_contains_at_least_one_change(self):
        """A plan that never changes quota would score transitions it never
        made, and its MAPE would silently be a steady-state MAPE."""
        plan = self.schedule_for(30, [8, 16, 32], 5)
        self.assertGreater(len({a for a in plan}), 1)
        changes = sum(1 for i in range(1, len(plan)) if plan[i] != plan[i - 1])
        self.assertGreater(changes, 0)


class MeasurementDisciplineTest(unittest.TestCase):
    """The two ways this measurement silently reports the wrong thing."""

    def test_steps_are_timed_with_cuda_events_not_a_host_clock(self):
        run = ast.unparse(_function("main"))
        self.assertIn("Event(enable_timing=True)", run)
        self.assertIn("elapsed_time", run)
        self.assertNotIn("perf_counter", run)

    def test_a_quota_handover_is_ordered_by_an_event(self):
        """The next step reads what the previous one wrote.

        A bare set_stream would race. A full synchronize would be correct
        but would charge the transition for a barrier a scheduler would not
        pay, inflating exactly the number being measured.
        """
        source = ast.unparse(_function("main"))
        self.assertIn("wait_event", source)
        self.assertNotIn("cuda.synchronize()\n            ", source)

    def test_the_mask_is_read_back_before_any_stream_is_used(self):
        source = ast.unparse(_function("masked_stream"))
        self.assertIn("hipExtStreamGetCUMask", source)
        self.assertIn("runtime installed", source)

    def test_the_predictor_has_no_fitted_term(self):
        """A step is predicted to cost what its quota costs at rest.

        Any transient the change introduces must land in the error rather
        than in a fitted correction -- the question is whether the
        steady-state table alone is enough to plan with. Checked on the
        expression's shape, not by matching its text: a scale factor or an
        added constant would still read as a lookup to a substring search.
        """
        for node in ast.walk(_function("main")):
            if (
                isinstance(node, ast.Assign)
                and any(getattr(t, "id", None) == "predicted"
                        for t in node.targets)
            ):
                self.assertIsInstance(node.value, ast.ListComp)
                element = node.value.elt
                self.assertIsInstance(
                    element, ast.Subscript,
                    "the prediction is not a plain lookup of the steady-state "
                    f"value: {ast.unparse(element)}",
                )
                self.assertEqual(
                    getattr(element.value, "id", None), "steady_median"
                )
                # No arithmetic anywhere inside the predicted expression.
                self.assertFalse(
                    [n for n in ast.walk(element)
                     if isinstance(n, (ast.BinOp, ast.Constant))],
                    f"the predictor carries a term: {ast.unparse(element)}",
                )
                return
        self.fail("no assignment to `predicted` found")


class MapeReuseTest(unittest.TestCase):
    def test_it_uses_the_shared_scorer_that_refuses_empty_comparisons(self):
        from burstserve.quota_model import QuotaModelError, mape

        self.assertIn("from burstserve.quota_model import mape", SOURCE)
        with self.assertRaises(QuotaModelError):
            mape([], [])



class ResultInvarianceTest(unittest.TestCase):
    """Moving work between streams must not change what is computed.

    The caching allocator does not know about the event dependency used to
    order a quota handover, so cross-stream reuse is a real hazard. Arguing
    that it is safe is not enough: the run checksums the latent and the
    switching schedule has to match a steady-state run bit for bit. If it
    does not, the timings are timing a different computation.
    """

    def test_the_run_checksums_its_result(self):
        source = ast.unparse(_function("main"))
        self.assertIn("sha256", source)
        self.assertIn("latent_digests", source)

    def test_the_report_states_whether_switching_matched(self):
        source = ast.unparse(_function("main"))
        self.assertIn("switching_matches_steady", source)

    def test_an_absent_switching_digest_does_not_read_as_a_match(self):
        """None must never compare equal to a steady digest."""
        digests = {"steady_8": "abc", "steady_32": "def"}
        matched = (
            digests.get("switching") is not None
            and digests.get("switching") in {
                v for k, v in digests.items() if k.startswith("steady_")
            }
        )
        self.assertFalse(matched)

    def test_a_differing_digest_is_reported_as_a_mismatch(self):
        digests = {"steady_8": "abc", "steady_32": "abc", "switching": "zzz"}
        matched = digests["switching"] in {
            v for k, v in digests.items() if k.startswith("steady_")
        }
        self.assertFalse(matched)

    def test_a_matching_digest_is_reported_as_a_match(self):
        digests = {"steady_8": "abc", "steady_32": "abc", "switching": "abc"}
        matched = digests["switching"] in {
            v for k, v in digests.items() if k.startswith("steady_")
        }
        self.assertTrue(matched)


class ReportIsSerialisableTest(unittest.TestCase):
    """The report is written last, so a serialisation error costs the run.

    The first smoke run measured everything and then died writing the file:
    quotas are ints and canonical_json requires string keys. Nothing about
    the measurement was wrong, and all of it was lost.
    """

    def test_every_quota_keyed_map_is_stringified(self):
        source = ast.unparse(_function("main"))
        for field in ("masks", "steady_per_step_median_s", "steady_samples"):
            with self.subTest(field):
                index = source.index(f"'{field}'")
                window = source[index:index + 200]
                self.assertIn(
                    "str(", window,
                    f"{field} is keyed by an int quota and canonical_json "
                    "rejects non-string keys",
                )

    def test_canonical_json_still_rejects_int_keys(self):
        """The constraint this guards against must still exist."""
        from burstserve.provenance import canonical_json

        with self.assertRaises(TypeError):
            canonical_json({"a": {1: 2}})
        self.assertIn('"1"', canonical_json({"a": {"1": 2}}))

if __name__ == "__main__":
    unittest.main()
