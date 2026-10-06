"""Offline checks for KeepingCounter: a Counter that must keep its coins.

The guide's Counter checks the next state but not how many coins move into
it. KeepingCounter adds one rule to every transition: the output that
continues the covenant must carry at least as many sompi as the input being
spent. The output is located through the covenant machinery
(`OpAuthOutputIdx`), not by a fixed position in the transaction.

How a refusal is attributed to the value rule. The line number in the
engine's report is not enough: a failure in the compiler-generated checks
(wrong next state, not exactly one covenant output) is reported at the last
statement of the function, which here is the value rule. So every "refused
because of the coins" check below is a comparison: the very same transaction
is accepted by StrictCounter, which differs from KeepingCounter only by the
value rule.

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


def covenant_output(count: int, value: int = VALUE, authorizing_input: int = 0) -> dict:
    # Always name the authorizing input: left out, the engine assumes the active one.
    return {
        "value": value,
        "covenant_id": COVENANT_ID,
        "authorizing_input": authorizing_input,
        "state": {"count": count},
    }


def plain_output(value: int) -> dict:
    return {"value": value}


def call(source, function, amount, inputs, outputs, active_input=0):
    scenario = {"inputs": inputs, "outputs": outputs}
    if active_input:
        scenario["active_input_index"] = active_input
    return silverscript.debug_call(
        source, function, args=[amount], constructor_args=[0], tx=scenario
    )


# Scenarios: (inputs, outputs) for a transition on count 10 that lands on `count`.
def same_value(count):
    return [covenant_input(10)], [covenant_output(count, VALUE)]


def one_sompi_less(count):
    return [covenant_input(10)], [covenant_output(count, VALUE - 1)]


def leak(count):
    return [covenant_input(10)], [covenant_output(count, 1000), plain_output(4000)]


def leak_plain_first(count):
    return [covenant_input(10)], [plain_output(4000), covenant_output(count, 1000)]


def leak_behind_a_big_first_output(count):
    # The spender adds 5000 sompi of their own, puts a 9000 plain output first and
    # leaves only 1000 in the covenant: 4000 of the covenant's coins walk away.
    return (
        [covenant_input(10), plain_input(5000)],
        [plain_output(9000), covenant_output(count, 1000)],
    )


def fee_from_another_input(count):
    return [covenant_input(10), plain_input(300)], [covenant_output(count, VALUE)]


def top_up(count):
    return [covenant_input(10), plain_input(1300)], [covenant_output(count, VALUE + 1000)]


def covenant_output_second(count):
    return (
        [covenant_input(10), plain_input(2000)],
        [plain_output(1700), covenant_output(count, VALUE)],
    )


LEAKS = (one_sompi_less, leak, leak_plain_first, leak_behind_a_big_first_output)
HONEST = (same_value, fee_from_another_input, top_up, covenant_output_second)
MOVES = (("add", 5, 15), ("subtract", 3, 7))


class KeepingCounterAccepts(unittest.TestCase):
    def test_transitions_that_keep_every_coin_pass(self):
        for function, amount, count in MOVES:
            for shape in HONEST:
                with self.subTest(function=function, shape=shape.__name__):
                    self.assertTrue(call(KEEPING, function, amount, *shape(count)).success)

    def test_subtract_down_to_exactly_zero_passes(self):
        self.assertTrue(call(KEEPING, "subtract", 10, *same_value(0)).success)

    def test_covenant_input_does_not_have_to_be_first(self):
        for function, amount, count in MOVES:
            with self.subTest(function=function):
                inputs = [plain_input(300), covenant_input(10)]
                outputs = [covenant_output(count, VALUE, authorizing_input=1)]
                result = call(KEEPING, function, amount, inputs, outputs, active_input=1)
                self.assertTrue(result.success)


class KeepingCounterRefusesLeaks(unittest.TestCase):
    def assert_refused_because_of_the_coins(self, function, amount, inputs, outputs, active_input=0):
        """Refused by KeepingCounter, accepted by the contract without the value rule."""
        keeping = call(KEEPING, function, amount, inputs, outputs, active_input)
        strict = call(STRICT, function, amount, inputs, outputs, active_input)
        self.assertFalse(keeping.success)
        self.assertTrue(strict.success)

    def test_every_leak_is_refused_in_both_transitions(self):
        for function, amount, count in MOVES:
            for shape in LEAKS:
                with self.subTest(function=function, shape=shape.__name__):
                    self.assert_refused_because_of_the_coins(function, amount, *shape(count))

    def test_leak_is_refused_when_the_covenant_input_is_not_first(self):
        # A 1-sompi ordinary input first: a rule reading "input #0" would be satisfied by 1 sompi.
        for function, amount, count in MOVES:
            with self.subTest(function=function):
                inputs = [plain_input(1), covenant_input(10)]
                outputs = [covenant_output(count, 1, authorizing_input=1), plain_output(5000)]
                self.assert_refused_because_of_the_coins(
                    function, amount, inputs, outputs, active_input=1
                )

    def test_refusal_report_points_into_the_transition(self):
        # The report names the value rule's line. That alone would not prove the
        # value rule refused (see the module docstring); the comparison above does.
        for function, occurrence in (("add", 1), ("subtract", 2)):
            amount, count = (5, 15) if function == "add" else (3, 7)
            result = call(KEEPING, function, amount, *leak(count))
            self.assertEqual(
                result.failure.frames[0].line,
                line_of(KEEPING, "require(tx.outputs[out_idx].value", occurrence),
            )


class KeepingCounterKeepsTheEarlierRules(unittest.TestCase):
    def test_wrong_next_state_is_refused(self):
        for claimed in (14, 16):
            with self.subTest(claimed=claimed):
                self.assertFalse(call(KEEPING, "add", 5, *same_value(claimed)).success)

    def test_add_refuses_zero_and_negative_amounts(self):
        guard = line_of(KEEPING, "require(amount > 0)", occurrence=1)
        for amount, claimed in ((0, 10), (-20, -10)):
            with self.subTest(amount=amount):
                result = call(KEEPING, "add", amount, *same_value(claimed))
                self.assertFalse(result.success)
                self.assertEqual(result.failure.frames[0].line, guard)

    def test_subtract_refuses_zero_and_negative_amounts(self):
        guard = line_of(KEEPING, "require(amount > 0)", occurrence=2)
        for amount, claimed in ((0, 10), (-5, 15)):
            with self.subTest(amount=amount):
                result = call(KEEPING, "subtract", amount, *same_value(claimed))
                self.assertFalse(result.success)
                self.assertEqual(result.failure.frames[0].line, guard)

    def test_subtract_below_zero_is_refused(self):
        result = call(KEEPING, "subtract", 11, *same_value(-1))
        self.assertFalse(result.success)
        self.assertEqual(
            result.failure.frames[0].line, line_of(KEEPING, "prev_state.count - amount >= 0")
        )


class WhyTheOutputMustBeLocatedProperly(unittest.TestCase):
    """The naive rule on output #0 against the same scenarios."""

    def test_naive_rule_stops_the_simple_leak(self):
        self.assertFalse(call(NAIVE, "add", 5, *leak(15)).success)

    def test_naive_rule_misses_the_leak_behind_a_big_first_output(self):
        self.assertTrue(call(NAIVE, "add", 5, *leak_behind_a_big_first_output(15)).success)

    def test_naive_rule_refuses_an_honest_transition_with_another_output_first(self):
        self.assertFalse(call(NAIVE, "add", 5, *covenant_output_second(15)).success)


class StartingPoint(unittest.TestCase):
    def test_the_contract_cannot_vet_the_count_it_was_created_with(self):
        # "Never below zero" holds from a non-negative start only: a covenant created
        # at -10 can be moved to -5. Recorded as observed behaviour.
        inputs = [covenant_input(-10)]
        self.assertTrue(call(KEEPING, "add", 5, inputs, [covenant_output(-5)]).success)


if __name__ == "__main__":
    unittest.main()
