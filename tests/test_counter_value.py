"""Offline checks for KeepingCounter: a Counter that must keep its coins.

The guide's Counter checks the next state but not how many coins move into
it. KeepingCounter adds one rule to every transition: the output that
continues the covenant must carry at least as many sompi as the input being
spent. The output is located through the covenant machinery
(`OpAuthOutputIdx`), not by a fixed position in the transaction.

A second, deliberately naive contract checks output number 0 instead. It is
kept here, inside the tests only, to show why that is not enough.

Nothing here touches a network, a wallet or funds.

Run from the repository root:

    python -m unittest discover -s tests -v
"""

import unittest
from pathlib import Path

import kaspa.experimental.silverscript as silverscript

CONTRACTS = Path(__file__).resolve().parent.parent / "contracts"
KEEPING = (CONTRACTS / "counter_keeping.sil").read_text(encoding="utf-8")
STRICT = (CONTRACTS / "counter_strict.sil").read_text(encoding="utf-8")

# What NOT to do: the value rule looks at output #0, wherever the covenant
# output really is.
NAIVE = """pragma silverscript ^0.1.0;

contract NaiveCounter(int init_count) {
    int count = init_count;

    #[covenant(binding = auth, from = 1, to = 1, mode = transition)]
    function add(State prev_state, int amount) : (State) {
        require(amount > 0);
        require(tx.outputs[0].value >= tx.inputs[this.activeInputIndex].value);
        return(State { count: prev_state.count + amount });
    }
}
"""

COVENANT_ID = "11" * 32
VALUE = 5000  # sompi carried by the synthetic covenant UTXO
VALUE_RULE = "require(tx.outputs[out_idx].value"


def line_of(source: str, fragment: str, occurrence: int = 1) -> int:
    seen = 0
    for number, line in enumerate(source.splitlines(), start=1):
        if fragment in line:
            seen += 1
            if seen == occurrence:
                return number
    raise AssertionError(f"{fragment!r} (occurrence {occurrence}) not found")


def covenant_input(count: int, value: int = VALUE) -> dict:
    return {"utxo_value": value, "covenant_id": COVENANT_ID, "state": {"count": count}}


def plain_input(value: int) -> dict:
    return {"utxo_value": value}


def covenant_output(count: int, value: int = VALUE) -> dict:
    return {
        "value": value,
        "covenant_id": COVENANT_ID,
        "authorizing_input": 0,
        "state": {"count": count},
    }


def plain_output(value: int) -> dict:
    return {"value": value}


def call(source, function, amount, inputs, outputs):
    return silverscript.debug_call(
        source,
        function,
        args=[amount],
        constructor_args=[0],
        tx={"inputs": inputs, "outputs": outputs},
    )


# Scenarios shared by several checks. Each is (inputs, outputs) for add(5) on count 10.
SAME_VALUE = ([covenant_input(10)], [covenant_output(15, VALUE)])
ONE_SOMPI_LESS = ([covenant_input(10)], [covenant_output(15, VALUE - 1)])
LEAK = ([covenant_input(10)], [covenant_output(15, 1000), plain_output(4000)])
LEAK_PLAIN_FIRST = ([covenant_input(10)], [plain_output(4000), covenant_output(15, 1000)])
# The spender adds 5000 sompi of their own, puts a 9000 plain output first and
# leaves only 1000 in the covenant: 4000 of the covenant's coins walk away.
LEAK_BEHIND_A_BIG_FIRST_OUTPUT = (
    [covenant_input(10), plain_input(5000)],
    [plain_output(9000), covenant_output(15, 1000)],
)
FEE_FROM_ANOTHER_INPUT = ([covenant_input(10), plain_input(300)], [covenant_output(15, VALUE)])
TOP_UP = ([covenant_input(10), plain_input(1300)], [covenant_output(15, VALUE + 1000)])
COVENANT_OUTPUT_SECOND = (
    [covenant_input(10), plain_input(2000)],
    [plain_output(1700), covenant_output(15, VALUE)],
)


class KeepingCounterTransitions(unittest.TestCase):
    def test_transition_that_keeps_every_coin_passes(self):
        self.assertTrue(call(KEEPING, "add", 5, *SAME_VALUE).success)

    def test_subtract_that_keeps_every_coin_passes(self):
        result = call(KEEPING, "subtract", 3, [covenant_input(10)], [covenant_output(7)])
        self.assertTrue(result.success)

    def test_fee_can_be_paid_from_a_separate_input(self):
        self.assertTrue(call(KEEPING, "add", 5, *FEE_FROM_ANOTHER_INPUT).success)

    def test_covenant_can_be_topped_up(self):
        self.assertTrue(call(KEEPING, "add", 5, *TOP_UP).success)

    def test_covenant_output_does_not_have_to_be_first(self):
        self.assertTrue(call(KEEPING, "add", 5, *COVENANT_OUTPUT_SECOND).success)


class KeepingCounterRefusals(unittest.TestCase):
    def assert_refused_by_value_rule(self, function, amount, inputs, outputs, occurrence):
        result = call(KEEPING, function, amount, inputs, outputs)
        self.assertFalse(result.success)
        self.assertEqual(
            result.failure.frames[0].line, line_of(KEEPING, VALUE_RULE, occurrence)
        )

    def test_losing_a_single_sompi_is_refused(self):
        self.assert_refused_by_value_rule("add", 5, *ONE_SOMPI_LESS, occurrence=1)

    def test_leak_to_a_plain_output_is_refused(self):
        self.assert_refused_by_value_rule("add", 5, *LEAK, occurrence=1)

    def test_leak_is_refused_wherever_the_outputs_are_placed(self):
        self.assert_refused_by_value_rule("add", 5, *LEAK_PLAIN_FIRST, occurrence=1)

    def test_leak_behind_a_big_first_output_is_refused(self):
        self.assert_refused_by_value_rule(
            "add", 5, *LEAK_BEHIND_A_BIG_FIRST_OUTPUT, occurrence=1
        )

    def test_subtract_is_guarded_by_the_same_rule(self):
        inputs, outputs = [covenant_input(10)], [covenant_output(7, 1000), plain_output(4000)]
        self.assert_refused_by_value_rule("subtract", 3, inputs, outputs, occurrence=2)

    def test_earlier_rules_still_hold(self):
        cases = {
            "wrong next state": ("add", 5, [covenant_input(10)], [covenant_output(14)]),
            "negative amount": ("add", -20, [covenant_input(10)], [covenant_output(-10)]),
            "zero amount": ("add", 0, [covenant_input(10)], [covenant_output(10)]),
            "below zero": ("subtract", 11, [covenant_input(10)], [covenant_output(-1)]),
        }
        for label, (function, amount, inputs, outputs) in cases.items():
            with self.subTest(label):
                self.assertFalse(call(KEEPING, function, amount, inputs, outputs).success)


class WhyTheOutputMustBeLocatedProperly(unittest.TestCase):
    """The naive rule on output #0 against the same scenarios."""

    def test_naive_rule_stops_the_simple_leak(self):
        self.assertFalse(call(NAIVE, "add", 5, *LEAK).success)

    def test_naive_rule_misses_the_leak_behind_a_big_first_output(self):
        self.assertTrue(call(NAIVE, "add", 5, *LEAK_BEHIND_A_BIG_FIRST_OUTPUT).success)

    def test_naive_rule_refuses_an_honest_transition_with_another_output_first(self):
        self.assertFalse(call(NAIVE, "add", 5, *COVENANT_OUTPUT_SECOND).success)

    def test_strict_counter_without_a_value_rule_lets_every_leak_through(self):
        for inputs, outputs in (ONE_SOMPI_LESS, LEAK, LEAK_PLAIN_FIRST, LEAK_BEHIND_A_BIG_FIRST_OUTPUT):
            self.assertTrue(call(STRICT, "add", 5, inputs, outputs).success)


if __name__ == "__main__":
    unittest.main()
