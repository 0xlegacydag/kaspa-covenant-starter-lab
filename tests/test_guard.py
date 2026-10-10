"""Offline checks for the Guard contract.

Nothing here touches a network, a wallet or funds: the contract is compiled
and its calls are simulated locally by the SilverScript debug engine that
ships with the Kaspa Python SDK.

Run from the repository root:

    python -m unittest discover -s tests -v
"""

import json
import unittest
from pathlib import Path

import kaspa.experimental.silverscript as silverscript

SOURCE = (Path(__file__).resolve().parent.parent / "contracts" / "guard.sil").read_text(
    encoding="utf-8"
)
THRESHOLD = 100

# Locking script produced for threshold = 100 by the compiler bundled with
# kaspa 2.1.0. If this changes after an upgrade, the compiler output changed
# and every result in the README has to be re-checked.
PINNED_BYTECODE_HEX = "7604b08239998763757682599f6975760164a0697551676a68"


def require_line() -> int:
    """1-based line of the `require` statement in the contract source."""
    for number, line in enumerate(SOURCE.splitlines(), start=1):
        if "require(" in line:
            return number
    # Not a failed assertion: a rule line that is missing says nothing about behaviour.
    raise LookupError("no require statement in the contract source")


def simulate(amount: int):
    return silverscript.debug_call(
        SOURCE, "check", args=[amount], constructor_args=[THRESHOLD]
    )


class GuardCompilation(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.contract = silverscript.compile(SOURCE, [THRESHOLD])

    def test_contract_exposes_one_entrypoint(self):
        self.assertEqual(self.contract.contract_name, "Guard")
        entries = [
            (entry.name, [(p.type_name, p.name) for p in entry.params])
            for entry in self.contract.abi
        ]
        self.assertEqual(entries, [("check", [("int", "amount")])])

    def test_compilation_is_deterministic(self):
        again = silverscript.compile(SOURCE, [THRESHOLD])
        self.assertEqual(again.bytecode, self.contract.bytecode)

    def test_bytecode_matches_the_pinned_compiler_output(self):
        self.assertEqual(self.contract.bytecode.hex(), PINNED_BYTECODE_HEX)

    def test_threshold_is_part_of_the_script(self):
        other = silverscript.compile(SOURCE, [THRESHOLD + 1])
        self.assertNotEqual(other.bytecode, self.contract.bytecode)

    def test_broken_source_does_not_compile(self):
        with self.assertRaises(silverscript.SilverScriptError):
            silverscript.compile(SOURCE.replace("require(", "requir("), [THRESHOLD])


class GuardCalls(unittest.TestCase):
    def test_amount_above_threshold_passes(self):
        for amount in (THRESHOLD + 1, 150, 2**62):
            with self.subTest(amount=amount):
                self.assertTrue(simulate(amount).success)

    def test_amount_at_or_below_threshold_is_refused(self):
        for amount in (THRESHOLD, THRESHOLD - 1, 0, -1, -(2**62)):
            with self.subTest(amount=amount):
                result = simulate(amount)
                self.assertFalse(result.success)

    def test_the_threshold_is_read_from_the_contract_not_hard_coded(self):
        # Every other call here is made with threshold 100, and the compiler folds the constructor
        # argument into the script: a rule reading `amount > 100` has the very same bytecode at
        # threshold 100 and would pass every other call (only the comparison of the scripts for
        # 100 and 101 would notice). So the threshold is varied at call time.
        cases = (
            (200, 150, False), (200, 200, False), (200, 201, True),
            (2000, 1500, False), (2000, 2001, True),
            (0, 0, False), (0, 1, True), (-5, -5, False), (-5, -4, True),
        )
        for threshold, amount, accepted in cases:
            with self.subTest(threshold=threshold, amount=amount):
                result = silverscript.debug_call(SOURCE, "check", args=[amount], constructor_args=[threshold])
                self.assertEqual(result.success, accepted)

    def test_refusal_points_at_the_require_statement(self):
        result = simulate(50)
        self.assertFalse(result.success)
        frame = result.failure.frames[0]
        self.assertEqual(frame.function_name, "check")
        self.assertEqual(frame.line, require_line())
        values = {v.name: (v.origin, v.value) for v in frame.variables}
        self.assertEqual(values, {"amount": ("arg", 50), "threshold": ("ctor", THRESHOLD)})

    def test_trace_of_a_refused_call_ends_at_the_require_statement(self):
        result = silverscript.debug_call(
            SOURCE, "check", args=[50], constructor_args=[THRESHOLD], trace=True
        )
        self.assertEqual(result.trace[-1].line, require_line())


class GuardArguments(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.contract = silverscript.compile(SOURCE, [THRESHOLD])

    def test_unlocking_script_is_deterministic(self):
        first = self.contract.build_sig_script("check", [150])
        second = self.contract.build_sig_script("check", [150])
        self.assertEqual(first, second)

    def test_malformed_calls_are_rejected_before_a_script_is_built(self):
        bad_calls = {
            "integer outside signed 64-bit": [2**63],
            "bool instead of int": [True],
            "string instead of int": ["150"],
            "missing argument": [],
            "extra argument": [1, 2],
        }
        for label, args in bad_calls.items():
            with self.subTest(label):
                with self.assertRaises(silverscript.SilverScriptError):
                    self.contract.build_sig_script("check", args)

    def test_unknown_entrypoint_is_rejected(self):
        with self.assertRaises(silverscript.SilverScriptError):
            self.contract.build_sig_script("withdraw", [150])


class GuardArtifact(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.contract = silverscript.compile(SOURCE, [THRESHOLD])
        cls.artifact_text = cls.contract.artifact_json()

    def test_artifact_round_trip_gives_the_same_contract(self):
        artifact = silverscript.load_artifact(self.artifact_text)
        artifact.check_consistency()
        self.assertEqual(artifact.contract_name, "Guard")
        self.assertEqual(artifact.bytecode, self.contract.bytecode)
        self.assertEqual(
            artifact.build_sig_script("check", [150]),
            self.contract.build_sig_script("check", [150]),
        )

    def test_edited_artifact_fails_the_consistency_check(self):
        data = json.loads(self.artifact_text)
        data["contracts"]["Guard"]["compiled"]["bytecode"][0] ^= 1
        tampered = silverscript.load_artifact(json.dumps(data))
        with self.assertRaises(silverscript.SilverScriptError):
            tampered.check_consistency()


if __name__ == "__main__":
    unittest.main()
