"""Offline checks for the Counter covenant and a stricter variant of it.

A covenant carries state from one UTXO to the next. Each test describes one
state transition as a synthetic transaction (previous state on the input,
claimed next state on the output) and lets the local SilverScript debug
engine decide whether the contract accepts it.

Nothing here touches a network, a wallet or funds.

Run from the repository root:

    python -m unittest discover -s tests -v
"""

import unittest
from pathlib import Path

import kaspa.experimental.silverscript as silverscript
from kaspa import (
    Hash,
    TransactionOutpoint,
    TransactionOutput,
    covenant_id,
    pay_to_script_hash_script,
)

CONTRACTS = Path(__file__).resolve().parent.parent / "contracts"
COUNTER = (CONTRACTS / "counter.sil").read_text(encoding="utf-8")
STRICT = (CONTRACTS / "counter_strict.sil").read_text(encoding="utf-8")

COVENANT_ID = "11" * 32
OTHER_COVENANT_ID = "22" * 32
PREVIOUS_TX = "aa" * 32
VALUE = 5000  # sompi carried by the synthetic covenant UTXO
# (count, amount) of a `subtract` that would land below zero, away from the 10 - 11 of the first checks.
OVERDRAFTS = ((0, 1), (1, 2), (10, 100), (10, 2**62), (2**40, 2**40 + 1))
# (count, move, amount) of honest moves away from count 10 and from the amounts 3, 5 and 10.
HONEST_ELSEWHERE = ((25, "subtract", 20), (2**40, "subtract", 2**40), (0, "add", 2**40), (2**40, "add", 1000))

# A contract with a state but no covenant rules at all: the control for checks
# that are about the transaction's structure.
RULELESS = """pragma silverscript ^0.1.0;

contract Nothing(int init_count) {
    int count = init_count;

    entry add(int amount) {
        require(amount == amount);
    }
}
"""


def line_of(source: str, fragment: str, occurrence: int = 1) -> int:
    """1-based line number of the n-th line containing `fragment`."""
    seen = 0
    for number, line in enumerate(source.splitlines(), start=1):
        if fragment in line:
            seen += 1
            if seen == occurrence:
                return number
    # Not a failed assertion: a rule line that is missing says nothing about behaviour.
    raise LookupError(f"{fragment!r} (occurrence {occurrence}) not found in the contract source")


def covenant_output(count: int, value: int = VALUE, covenant_id: str = COVENANT_ID) -> dict:
    return {
        "value": value,
        "covenant_id": covenant_id,
        "authorizing_input": 0,
        "state": {"count": count},
    }


def transition(source, function, previous, amount, outputs, inputs=None):
    """Simulate `function(amount)` on a covenant UTXO whose count is `previous`."""
    scenario = {
        "inputs": inputs
        or [{"utxo_value": VALUE, "covenant_id": COVENANT_ID, "state": {"count": previous}}],
        "outputs": outputs,
    }
    return silverscript.debug_call(
        source, function, args=[amount], constructor_args=[0], tx=scenario
    )


def failing_line(result) -> int:
    return result.failure.frames[0].line


class CounterCompilation(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.at_zero = silverscript.compile(COUNTER, [0])
        cls.at_five = silverscript.compile(COUNTER, [5])

    def test_state_is_part_of_the_script(self):
        self.assertNotEqual(self.at_zero.bytecode, self.at_five.bytecode)

    def test_only_the_state_bytes_differ_between_two_counts(self):
        offset, length = self.at_zero.state_span
        self.assertEqual(self.at_five.state_span, (offset, length))
        self.assertGreater(length, 0)
        before, after = self.at_zero.bytecode, self.at_five.bytecode
        self.assertEqual(before[:offset], after[:offset])
        self.assertEqual(before[offset + length :], after[offset + length :])

    def test_template_hash_is_the_same_for_every_count(self):
        self.assertEqual(bytes(self.at_zero.template_hash), bytes(self.at_five.template_hash))

    def test_a_count_is_a_sign_and_a_magnitude_in_eight_bytes(self):
        # So the lowest count is -(2^63 - 1): one short of what a 64-bit integer holds.
        offset, length = self.at_zero.state_span
        self.assertEqual((offset, length), (1, 9))  # after one opcode: a push opcode, then the eight bytes
        cases = (
            (1, "0100000000000000"),
            (-1, "0100000000000080"),  # the same magnitude, and the top bit of the last byte for the sign
            (2**63 - 1, "ffffffffffffff7f"),
            (-(2**63 - 1), "ffffffffffffffff"),
        )
        for count, eight_bytes in cases:
            with self.subTest(count=count):
                contract = silverscript.compile(COUNTER, [count])
                self.assertEqual(contract.state_span, (offset, length))
                self.assertEqual(contract.bytecode[offset : offset + length].hex(), "08" + eight_bytes)
        with self.assertRaisesRegex(silverscript.SilverScriptError, "exceeds 8 bytes"):
            silverscript.compile(COUNTER, [-(2**63)])

    def test_the_two_moves_are_the_only_entrypoints(self):
        # A plain `entry` beside the moves gets none of the compiler-generated covenant checks:
        # the engine lets it spend the covenant coin to any output, at any count, by anyone.
        for label, source in (("counter", COUNTER), ("strict", STRICT)):
            with self.subTest(contract=label):
                contract = silverscript.compile(source, [0])
                self.assertEqual(
                    {entry.name: [(p.type_name, p.name) for p in entry.params] for entry in contract.abi},
                    {
                        "__covenant_entrypoint_auth_add": [("int", "amount")],
                        "__covenant_entrypoint_auth_subtract": [("int", "amount")],
                    },
                )

    def test_transitions_are_called_through_the_covenant_builder(self):
        add = self.at_zero.build_sig_script_for_covenant_decl("add", [5])
        subtract = self.at_zero.build_sig_script_for_covenant_decl("subtract", [5])
        self.assertTrue(add)
        self.assertNotEqual(add, subtract)  # each transition has its own entrypoint
        with self.assertRaises(silverscript.SilverScriptError):
            self.at_zero.build_sig_script("add", [5])


class CounterTransitions(unittest.TestCase):
    def test_add_moves_the_count_forward(self):
        self.assertTrue(transition(COUNTER, "add", 10, 5, [covenant_output(15)]).success)

    def test_subtract_moves_the_count_back(self):
        self.assertTrue(transition(COUNTER, "subtract", 10, 3, [covenant_output(7)]).success)

    def test_subtract_down_to_exactly_zero_is_allowed(self):
        self.assertTrue(transition(COUNTER, "subtract", 10, 10, [covenant_output(0)]).success)

    def test_claiming_a_wrong_next_state_is_refused(self):
        for claimed in (14, 16, 10, 0):
            with self.subTest(claimed=claimed):
                result = transition(COUNTER, "add", 10, 5, [covenant_output(claimed)])
                self.assertFalse(result.success)

    def test_subtract_below_zero_is_refused_at_the_require_statement(self):
        result = transition(COUNTER, "subtract", 10, 11, [covenant_output(-1)])
        self.assertFalse(result.success)
        self.assertEqual(failing_line(result), line_of(COUNTER, "prev_state.count - amount >= 0"))

    def test_below_zero_rule_holds_at_other_counts_and_for_larger_amounts(self):
        # The check above starts at count 10 and overdraws by one: a rule skipped at count 0, or
        # for large amounts, would pass it.
        for previous, amount in OVERDRAFTS:
            with self.subTest(previous=previous, amount=amount):
                result = transition(COUNTER, "subtract", previous, amount, [covenant_output(previous - amount)])
                self.assertFalse(result.success)

    def test_honest_moves_pass_at_other_counts_and_for_larger_amounts(self):
        # No other accepted move here starts above count 10 or takes an amount above 10: a rule
        # that is too strict beyond that (`... && amount < 11`) would pass them all.
        for label, source in (("counter", COUNTER), ("strict", STRICT)):
            for previous, function, amount in HONEST_ELSEWHERE:
                claimed = previous + amount if function == "add" else previous - amount
                with self.subTest(contract=label, previous=previous, function=function, amount=amount):
                    result = transition(source, function, previous, amount, [covenant_output(claimed)])
                    self.assertTrue(result.success)

    def test_count_cannot_overflow_a_64_bit_integer(self):
        top = 2**63 - 1
        result = transition(COUNTER, "add", top, 1, [covenant_output(top)])
        self.assertFalse(result.success)
        self.assertIn("64-bit", str(result.error))


class CounterStructure(unittest.TestCase):
    """The transition must continue the same covenant in exactly one output.

    A covenant id cannot be made up: the engine rejects a transaction whose
    ids do not add up before any contract code runs. So the checks about
    "another covenant" use a correctly derived id of a brand-new covenant,
    and a contract with no covenant rules as a control: if the control passes
    and the Counter does not, it is the Counter's own rule that refused.
    """

    def new_covenant_scenario(self, source, with_covenant_input):
        """Input: the Counter at 10. Output: a well-formed NEW covenant at 15."""
        script = pay_to_script_hash_script(silverscript.compile(source, [15]).bytecode)
        new_id = str(
            covenant_id(
                TransactionOutpoint(Hash(PREVIOUS_TX), 0), [TransactionOutput(VALUE, script, None)]
            )
        )
        spent = {"utxo_value": VALUE, "state": {"count": 10}, "prev_txid": PREVIOUS_TX, "prev_index": 0}
        if with_covenant_input:
            spent["covenant_id"] = COVENANT_ID
        return {
            "inputs": [spent],
            "outputs": [
                {"value": VALUE, "covenant_id": new_id, "authorizing_input": 0, "state": {"count": 15}}
            ],
        }

    def run_add(self, source, scenario):
        return silverscript.debug_call(source, "add", args=[5], constructor_args=[0], tx=scenario)

    def test_output_that_starts_another_covenant_is_refused_by_the_contract(self):
        control = self.run_add(RULELESS, self.new_covenant_scenario(RULELESS, True))
        counter = self.run_add(COUNTER, self.new_covenant_scenario(COUNTER, True))
        self.assertTrue(control.success)
        self.assertFalse(counter.success)
        self.assertIsNotNone(counter.failure)  # refused by the script, not by the engine's id check

    def test_input_without_a_covenant_id_is_refused_by_the_contract(self):
        control = self.run_add(RULELESS, self.new_covenant_scenario(RULELESS, False))
        counter = self.run_add(COUNTER, self.new_covenant_scenario(COUNTER, False))
        self.assertTrue(control.success)
        self.assertFalse(counter.success)
        self.assertIsNotNone(counter.failure)

    def test_made_up_covenant_id_is_rejected_before_the_contract_runs(self):
        result = transition(
            COUNTER, "add", 10, 5, [covenant_output(15, covenant_id=OTHER_COVENANT_ID)]
        )
        self.assertFalse(result.success)
        self.assertIsNone(result.failure)
        self.assertIn("Covenant structure error", str(result.error))

    def assert_refused_by_the_contract(self, outputs):
        """Refused by the Counter's script, accepted by the contract with no covenant rules."""
        control = transition(RULELESS, "add", 10, 5, outputs)
        counter = transition(COUNTER, "add", 10, 5, outputs)
        self.assertTrue(control.success)
        self.assertFalse(counter.success)
        self.assertIsNotNone(counter.failure)  # refused by the script, not by the engine's id check

    def test_output_without_a_covenant_binding_is_refused(self):
        plain = {"value": VALUE, "state": {"count": 15}}
        self.assert_refused_by_the_contract([plain])

    def test_two_covenant_outputs_are_refused(self):
        halves = [covenant_output(15, value=VALUE // 2), covenant_output(15, value=VALUE // 2)]
        self.assert_refused_by_the_contract(halves)

    def test_no_output_at_all_is_refused(self):
        self.assert_refused_by_the_contract([])

    def test_a_new_covenant_may_start_beside_the_continuation(self):
        # "Exactly one output" is about outputs that continue this covenant's id. An output that
        # starts another covenant is not counted by the generated check: it stands beside the
        # continuation. Here the Counter's own coin authorises it and a second input brings its
        # 300 sompi. Recorded as observed behaviour.
        script = pay_to_script_hash_script(silverscript.compile(COUNTER, [0]).bytecode)
        new_id = str(
            covenant_id(TransactionOutpoint(Hash(PREVIOUS_TX), 0), [TransactionOutput(300, script, None)])
        )
        spent = {"utxo_value": VALUE, "covenant_id": COVENANT_ID, "state": {"count": 10}}
        spent.update(prev_txid=PREVIOUS_TX, prev_index=0)
        # The id of a new covenant depends on where its output stands; this one is derived for output #0.
        started = {"value": 300, "covenant_id": new_id, "authorizing_input": 0, "state": {"count": 0}}
        scenario = {"inputs": [spent, {"utxo_value": 300}], "outputs": [started, covenant_output(15)]}
        self.assertTrue(self.run_add(COUNTER, scenario).success)
        # Authorised by the second input instead (the id then comes from that input's outpoint), it passes as well.
        other_id = str(covenant_id(TransactionOutpoint(Hash("cc" * 32), 0), [TransactionOutput(300, script, None)]))
        by_second = dict(started, covenant_id=other_id, authorizing_input=1)
        payer = {"utxo_value": 300, "prev_txid": "cc" * 32, "prev_index": 0}
        other = {"inputs": [spent, payer], "outputs": [by_second, covenant_output(15)]}
        self.assertTrue(self.run_add(COUNTER, other).success)
        # Instead of the continuation it is refused (the check above), and so is a second continuation.
        twice = dict(scenario, outputs=[covenant_output(15, value=300), covenant_output(15)])
        self.assertFalse(self.run_add(COUNTER, twice).success)

    def test_unknown_state_field_is_a_usage_error(self):
        wrong = dict(covenant_output(15), state={"total": 15})
        with self.assertRaises(silverscript.SilverScriptError):
            transition(COUNTER, "add", 10, 5, [wrong])


class CounterGaps(unittest.TestCase):
    """Things the guide's Counter does NOT forbid. Recorded as observed behaviour."""

    def test_add_with_a_negative_amount_takes_the_count_below_zero(self):
        # `subtract` guards against going below zero, `add` does not.
        self.assertTrue(transition(COUNTER, "add", 10, -20, [covenant_output(-10)]).success)

    def test_subtract_with_a_negative_amount_increases_the_count(self):
        self.assertTrue(transition(COUNTER, "subtract", 10, -5, [covenant_output(15)]).success)

    def test_the_contract_does_not_check_how_many_coins_move_forward(self):
        # 1000 of 5000 sompi continue in the covenant, the rest leaves it.
        outputs = [covenant_output(15, value=1000), {"value": 4000}]
        self.assertTrue(transition(COUNTER, "add", 10, 5, outputs).success)


class StrictCounter(unittest.TestCase):
    """Own variant: both transitions additionally require `amount > 0`."""

    def test_it_compiles_to_a_different_script_than_the_guide_counter(self):
        strict = silverscript.compile(STRICT, [0])
        plain = silverscript.compile(COUNTER, [0])
        self.assertNotEqual(strict.bytecode, plain.bytecode)
        self.assertNotEqual(bytes(strict.template_hash), bytes(plain.template_hash))

    def test_ordinary_transitions_still_pass(self):
        self.assertTrue(transition(STRICT, "add", 10, 5, [covenant_output(15)]).success)
        self.assertTrue(transition(STRICT, "subtract", 10, 3, [covenant_output(7)]).success)
        self.assertTrue(transition(STRICT, "subtract", 10, 10, [covenant_output(0)]).success)

    def test_add_refuses_zero_and_negative_amounts(self):
        for amount, claimed in ((0, 10), (-20, -10)):
            with self.subTest(amount=amount):
                result = transition(STRICT, "add", 10, amount, [covenant_output(claimed)])
                self.assertFalse(result.success)
                self.assertEqual(failing_line(result), line_of(STRICT, "require(amount > 0)", occurrence=1))

    def test_subtract_refuses_zero_and_negative_amounts(self):
        for amount, claimed in ((0, 10), (-5, 15)):
            with self.subTest(amount=amount):
                result = transition(STRICT, "subtract", 10, amount, [covenant_output(claimed)])
                self.assertFalse(result.success)
                self.assertEqual(failing_line(result), line_of(STRICT, "require(amount > 0)", occurrence=2))

    def test_amount_rules_hold_at_every_count_and_for_any_negative_amount(self):
        # The checks above start at count 10 and try 0 and one small negative: a rule waived at
        # count 0, or one letting large negatives through, would pass them all.
        for previous in (0, 1, 2**40):
            for function in ("add", "subtract"):
                for amount in (0, -1, -20, -101, -(2**62)):
                    claimed = previous + amount if function == "add" else previous - amount
                    with self.subTest(previous=previous, function=function, amount=amount):
                        result = transition(STRICT, function, previous, amount, [covenant_output(claimed)])
                        self.assertFalse(result.success)

    def test_subtract_below_zero_is_still_refused(self):
        result = transition(STRICT, "subtract", 10, 11, [covenant_output(-1)])
        self.assertFalse(result.success)
        self.assertEqual(failing_line(result), line_of(STRICT, "prev_state.count - amount >= 0"))

    def test_below_zero_rule_holds_at_other_counts_and_for_larger_amounts(self):
        for previous, amount in OVERDRAFTS:
            with self.subTest(previous=previous, amount=amount):
                result = transition(STRICT, "subtract", previous, amount, [covenant_output(previous - amount)])
                self.assertFalse(result.success)

    def test_wrong_next_state_is_still_refused(self):
        self.assertFalse(transition(STRICT, "add", 10, 5, [covenant_output(14)]).success)

    def test_the_coin_amount_gap_is_not_closed_by_this_variant(self):
        outputs = [covenant_output(15, value=1000), {"value": 4000}]
        self.assertTrue(transition(STRICT, "add", 10, 5, outputs).success)


if __name__ == "__main__":
    unittest.main()
