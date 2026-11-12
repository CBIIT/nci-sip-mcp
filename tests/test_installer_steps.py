import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from nci_si_mcp import installer_state

API = installer_state


class Actions:
    step_names = ("first", "second", "third")

    def __init__(self):
        self.present = set()
        self.observations: dict[str, list[bool]] = {}
        self.missing = {}
        self.calls = []
        self.bug: str | None = None

    def missing_inputs(self, step):
        return tuple(self.missing.get(step, ()))

    def apply(self, step):
        self.calls.append(("apply", step))
        if self.bug == step:
            raise AssertionError("PRIVATE-CANARY")
        self.present.add(step)

    def verify(self, step):
        self.calls.append(("verify", step))
        if self.observations.get(step):
            return self.observations[step].pop(0)
        return step in self.present


class InstallerStepsTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.data_dir = Path(directory.name)
        self.actions = Actions()

    @staticmethod
    def state(*outcomes):
        return API.InstallationState(API.STATE_VERSION, "local", ("server",), outcomes)

    def reconcile(self, restart=False):
        return API.reconcile(
            self.data_dir,
            "local",
            ("server",),
            self.actions,
            restart=restart,
        )

    def test_resume_reverifies_from_the_first_step_and_restarts_at_the_first_stale_step(self):
        self.reconcile()
        self.actions.calls.clear()
        self.actions.present.remove("second")

        state = self.reconcile()

        self.assertEqual(
            self.actions.calls,
            [
                ("verify", "first"),
                ("verify", "second"),
                ("apply", "second"),
                ("verify", "second"),
                ("apply", "third"),
                ("verify", "third"),
            ],
        )
        self.assertEqual([outcome.status for outcome in state.outcomes], ["completed"] * 3)

    def test_running_every_change_twice_converges_to_the_same_state_and_outcome(self):
        first = self.reconcile()
        calls_after_first = tuple(self.actions.calls)
        second = self.reconcile()

        self.assertEqual(first, second)
        self.assertEqual(self.actions.present, {"first", "second", "third"})
        self.assertEqual(
            self.actions.calls[len(calls_after_first) :],
            [("verify", "first"), ("verify", "second"), ("verify", "third")],
        )

    def test_blocked_is_not_pending_and_names_the_missing_input(self):
        self.actions.missing["second"] = ("Install git.", "Restore EVS access.")
        state = self.reconcile()
        status = API.current_status(state, self.actions)

        self.assertEqual(
            status,
            {
                "components": ["server"],
                "completed": ["first"],
                "failed": [],
                "blocked": [{"step": "second", "missing": ["Install git.", "Restore EVS access."]}],
                "pending": ["third"],
                "nextAction": "Resolve the failed prerequisites: Install git.; Restore EVS access.",
            },
        )
        empty = self.state()
        self.assertEqual(API.current_status(empty, self.actions)["nextAction"], "Run first.")

    def test_failed_step_stays_failed_in_status_instead_of_becoming_pending(self):
        state = self.state(
            API.StepOutcome("first", "completed"), API.StepOutcome("second", "failed")
        )
        self.actions.present.add("first")
        status = API.current_status(state, self.actions)
        self.assertEqual(status["completed"], ["first"])
        self.assertEqual(status["failed"], ["second"])
        self.assertEqual(status["pending"], ["third"])
        self.assertEqual(status["nextAction"], "Retry failed step second.")

    def test_reverify_marks_stale_and_previously_failed_steps_failed(self):
        state = self.state(
            API.StepOutcome("first", "completed"), API.StepOutcome("second", "failed")
        )
        stale = self.state(*state.outcomes[:1])
        self.assertEqual(API.reverify(stale, self.actions).outcomes[0].status, "failed")
        previously_failed = self.state(*state.outcomes)
        self.assertEqual(
            API.reverify(previously_failed, self.actions).outcomes[-1].status, "failed"
        )

    def test_stale_completed_step_reports_current_missing_inputs_as_blocked(self):
        state = self.state(API.StepOutcome("first", "completed"))
        self.actions.missing["first"] = ("Restore access.",)
        current = API.reverify(state, self.actions)
        status = API.current_status(current, self.actions)
        self.assertEqual(status["blocked"], [{"step": "first", "missing": ["Restore access."]}])
        self.assertEqual(status["pending"], ["second", "third"])
        self.actions.missing.clear()
        self.actions.present.add("first")
        self.assertEqual(API.reverify(current, self.actions).outcomes[0].status, "completed")

    def recovering_state(self, *recorded):
        self.actions.present.update(("first", "second", "third"))
        self.actions.observations["first"] = [False, True]
        return self.state(*(API.StepOutcome(step, "completed") for step in recorded))

    def test_recovered_prerequisite_rechecks_all_recorded_later_stages(self):
        state = self.recovering_state("first", "second", "third")
        current = API.reverify(state, self.actions)
        self.assertEqual(
            API.current_status(current, self.actions),
            {
                "components": ["server"],
                "completed": ["first", "second", "third"],
                "failed": [],
                "blocked": [],
                "pending": [],
                "nextAction": None,
            },
        )
        self.assertEqual(
            self.actions.calls,
            [("verify", "first"), ("verify", "first"), ("verify", "second"), ("verify", "third")],
        )

    def test_recovery_keeps_later_failure_or_missing_input_incomplete(self):
        cases = (
            ("second", (), ["first"], ["second"], [], ["third"], "Retry failed step second."),
            (
                "second",
                ("Repair second.",),
                ["first"],
                [],
                [{"step": "second", "missing": ["Repair second."]}],
                ["third"],
                "Resolve the failed prerequisites: Repair second.",
            ),
            ("third", (), ["first", "second"], ["third"], [], [], "Retry failed step third."),
            (
                "third",
                ("Repair third.",),
                ["first", "second"],
                [],
                [{"step": "third", "missing": ["Repair third."]}],
                [],
                "Resolve the failed prerequisites: Repair third.",
            ),
        )
        for step, missing, complete, failed, blocked, pending, next_action in cases:
            with self.subTest(step=step, missing=missing):
                self.actions = Actions()
                state = self.recovering_state("first", "second", "third")
                self.actions.present.remove(step)
                self.actions.missing[step] = missing
                current = API.reverify(state, self.actions)
                self.assertEqual(
                    API.current_status(current, self.actions),
                    {
                        "components": ["server"],
                        "completed": complete,
                        "failed": failed,
                        "blocked": blocked,
                        "pending": pending,
                        "nextAction": next_action,
                    },
                )
                self.assertEqual({kind for kind, _step in self.actions.calls}, {"verify"})

    def test_recovery_never_completes_unrecorded_stages(self):
        current = API.reverify(self.recovering_state("first"), self.actions)
        self.assertEqual(
            API.current_status(current, self.actions),
            {
                "components": ["server"],
                "completed": ["first"],
                "failed": [],
                "blocked": [],
                "pending": ["second", "third"],
                "nextAction": "Run second.",
            },
        )
        self.assertEqual(self.actions.calls, [("verify", "first"), ("verify", "first")])

    def observe_later_verifier(self, missing):
        state = self.recovering_state("first", "second", "third")
        self.actions.present.remove("second")
        self.actions.missing["second"] = missing
        sentinel = self.data_dir / "later-verifier-ran"
        original = self.actions.verify

        def verify(step):
            if step == "third":
                sentinel.write_text("unexpected verification", encoding="utf-8")
            return original(step)

        with patch.object(self.actions, "verify", side_effect=verify):
            current = API.reverify(state, self.actions)
        return API.current_status(current, self.actions), sentinel.exists()

    def test_failed_stage_prevents_later_verifier_activity(self):
        status, later_active = self.observe_later_verifier(())
        self.assertEqual(
            status,
            {
                "components": ["server"],
                "completed": ["first"],
                "failed": ["second"],
                "blocked": [],
                "pending": ["third"],
                "nextAction": "Retry failed step second.",
            },
        )
        self.assertFalse(later_active)

    def test_blocked_stage_prevents_later_verifier_activity(self):
        status, later_active = self.observe_later_verifier(("Repair second.",))
        self.assertEqual(
            status,
            {
                "components": ["server"],
                "completed": ["first"],
                "failed": [],
                "blocked": [{"step": "second", "missing": ["Repair second."]}],
                "pending": ["third"],
                "nextAction": "Resolve the failed prerequisites: Repair second.",
            },
        )
        self.assertFalse(later_active)

    def test_restart_resets_progress_for_the_same_selection(self):
        self.reconcile()
        self.actions.calls.clear()
        state = self.reconcile(restart=True)
        self.assertEqual([call[0] for call in self.actions.calls], ["apply", "verify"] * 3)
        self.assertEqual([outcome.status for outcome in state.outcomes], ["completed"] * 3)

    def test_an_action_bug_propagates_without_recording_clean_completion(self):
        self.actions.bug = "second"

        with self.assertRaisesRegex(AssertionError, "PRIVATE-CANARY"):
            self.reconcile()
        state = API.read_state(self.data_dir, "local", ("server",))
        self.assertEqual(state.outcomes, (API.StepOutcome("first", "completed"),))

    def test_post_apply_verification_failure_is_persisted_and_stops_later_steps(self):
        original_verify = self.actions.verify
        self.actions.verify = lambda step: False if step == "second" else original_verify(step)
        state = self.reconcile()
        status = API.current_status(state, self.actions)
        self.assertEqual(state.outcomes[-1], API.StepOutcome("second", "failed"))
        self.assertNotIn(("apply", "third"), self.actions.calls)
        self.assertEqual(status["nextAction"], "Retry failed step second.")

    def test_terminal_outcomes_must_be_last_and_steps_must_not_repeat(self):
        invalid = (
            (API.StepOutcome("first", "blocked", ("fix",)), API.StepOutcome("second", "completed")),
            (API.StepOutcome("first", "failed"), API.StepOutcome("second", "completed")),
            (API.StepOutcome("first", "completed"), API.StepOutcome("first", "completed")),
        )
        for outcomes in invalid:
            with (
                self.subTest(outcomes=outcomes),
                self.assertRaisesRegex(API.InstallerStateError, "malformed"),
            ):
                API.validate_outcomes(self.state(*outcomes), self.actions.step_names)
        with self.assertRaisesRegex(API.InstallerStateError, "malformed"):
            API.validate_outcomes(self.state(), ("first", "first"))
