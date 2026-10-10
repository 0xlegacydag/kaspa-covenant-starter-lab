"""Offline checks for ClosableCounter: OwnedCounter with a way out for the owner.

KeepingCounter and OwnedCounter can never release their coins. ClosableCounter
adds a third move, `close`: the owner's signature ends the covenant, and the
coins go wherever the signed transaction sends them. `close` returns a list of
next states and requires it to be empty. It is not a plain entrypoint: a plain
entrypoint would get none of the compiler-generated covenant checks.

Two more differences from OwnedCounter:

- the owner's signature must be of the default type in every move, so a
  signature cannot be handed over as a licence to choose the outcome;
- the rules shared by the moves are written once, in two helper functions,
  `onlyOwner` and `keepCoins`. A move still has to call them, and could call
  something weaker instead, so every shared rule is checked in every move.

A signature commits to a transaction, not to a move. Every check that carries
a genuine signature builds the same transaction the debug engine synthesizes
for its scenario and signs that mirror.

How a refusal is attributed. The engine's report names a line. Failures of
the compiler-generated checks were seen at the coin rule's line (for `add`)
and at the lines of the `onlyOwner(...)` calls (for `subtract` and `close`),
none of which caused them. So a line is used only for the signature, type,
amount and below-zero rules, where no generated failure was seen; refusals by
the coin rule and by the empty-list rule are shown by comparison instead: the
same signed transaction passes a copy of the contract without the rule.
In every check the behaviour is asserted first; the rule's line is looked up,
and the comparison copy is built, only after that.

Nothing here touches a network, a wallet or funds; the keys are random and
discarded.

Run from the repository root:

    python -m unittest discover -s tests -v
"""

import os
import unittest
from pathlib import Path

import kaspa.experimental.silverscript as silverscript
from kaspa import (
    CovenantBinding,
    Hash,
    Keypair,
    PrivateKey,
    SighashType,
    Transaction,
    TransactionInput,
    TransactionOutpoint,
    TransactionOutput,
    UtxoEntryReference,
    create_input_signature,
    pay_to_address_script,
    pay_to_script_hash_script,
)

CONTRACTS = Path(__file__).resolve().parent.parent / "contracts"
CLOSABLE = (CONTRACTS / "counter_closable.sil").read_text(encoding="utf-8")

COVENANT_ID = "11" * 32
PREVIOUS_TX = "aa" * 32
PLAIN_TX = "bb" * 32
VALUE = 5000

_owner = Keypair.random()
_stranger = Keypair.random()
OWNER_KEY = PrivateKey(_owner.private_key)
STRANGER_KEY = PrivateKey(_stranger.private_key)
OWNER = bytes.fromhex(_owner.xonly_public_key)
# Where ordinary (non-covenant) coins come from and go to in the scenarios.
PLAIN_SCRIPT = pay_to_address_script(_stranger.to_address("testnet"))
PLAIN_SCRIPT_BYTES = bytes.fromhex(PLAIN_SCRIPT.script)

DEFAULT_TYPE = "All"
OTHER_TYPES = ("None", "Single", "AllAnyOneCanPay", "NoneAnyOneCanPay", "SingleAnyOneCanPay")
MOVES = (("add", 5, 15), ("subtract", 3, 7))
# (count, move, amount) of honest moves away from count 10 and from the amounts 3, 5 and 10.
HONEST_ELSEWHERE = ((25, "subtract", 20), (2**40, "subtract", 2**40), (0, "add", 2**40), (2**40, "add", 1000))
NO_NEXT_STATE = []

# Rules as they are written in the contract; looked up only after behaviour is asserted.
SIGNATURE_RULE = "require(checkSig(owner_sig, owner));"
TYPE_RULE = "require(owner_sig.slice(64, 65)"
AMOUNT_RULE = "require(amount > 0);"
BELOW_ZERO_RULE = "require(prev_state.count - amount >= 0);"
EMPTY_LIST_RULE = "        require(next_states.length == 0);"
OWNER_CALL = "        onlyOwner(owner_sig);"
KEEP_CALL = "        keepCoins();"
CLOSE_DECLARATION = "#[covenant.singleton(mode = transition, termination = allowed)]"


def line_of(source: str, fragment: str, occurrence: int = 1) -> int:
    seen = 0
    for number, line in enumerate(source.splitlines(), start=1):
        if fragment in line:
            seen += 1
            if seen == occurrence:
                return number
    # Not a failed assertion: a rule line that is missing says nothing about behaviour.
    raise LookupError(f"{fragment!r} (occurrence {occurrence}) not found in the contract source")


def lock(source: str, count: int):
    return pay_to_script_hash_script(silverscript.compile(source, [count, OWNER]).bytecode)


def utxo(txid: str, amount: int, script, covenant_id: str | None) -> UtxoEntryReference:
    return UtxoEntryReference.from_dict(
        {
            "address": None,
            "outpoint": {"transactionId": txid, "index": 0},
            "utxoEntry": {
                "amount": amount,
                "scriptPublicKey": {"version": script.version, "script": script.script},
                "blockDaaScore": 0,
                "isCoinbase": False,
                "covenantId": covenant_id,
            },
        }
    )


def covenant(count: int, value: int = VALUE) -> tuple:
    """An output that continues the covenant at `count`."""
    return ("covenant", count, value)


def plain(value: int) -> tuple:
    """An ordinary output."""
    return ("plain", value)


def render(previous: int, outputs: list, plain_in: int | None = None, source: str = CLOSABLE, plain_first: bool = False, value_in: int = VALUE):
    """One transaction, described once and rendered twice.

    The covenant coin (count `previous`, `value_in` sompi) is input 0; an
    optional ordinary input of `plain_in` sompi follows it, or comes first
    when `plain_first` is set. Returns (scenario, mirror, active): the
    scenario the debug engine runs, the same transaction as a
    `kaspa.Transaction`, so that it can be signed, and the index of the
    covenant input.
    """
    scenario_inputs = [
        {
            "utxo_value": value_in,
            "covenant_id": COVENANT_ID,
            "state": {"count": previous},
            "prev_txid": PREVIOUS_TX,
            "prev_index": 0,
        }
    ]
    mirror_inputs = [
        TransactionInput(
            TransactionOutpoint(Hash(PREVIOUS_TX), 0),
            b"",
            sequence=0,
            sig_op_count=0,
            compute_budget=0,
            utxo=utxo(PREVIOUS_TX, value_in, lock(source, previous), COVENANT_ID),
        )
    ]
    active = 1 if (plain_in is not None and plain_first) else 0
    if plain_in is not None:
        position = 0 if plain_first else 1
        scenario_inputs.insert(
            position,
            {"utxo_value": plain_in, "prev_txid": PLAIN_TX, "prev_index": 0, "utxo_script": PLAIN_SCRIPT_BYTES},
        )
        mirror_inputs.insert(
            position,
            TransactionInput(
                TransactionOutpoint(Hash(PLAIN_TX), 0),
                b"",
                sequence=0,
                sig_op_count=0,
                compute_budget=0,
                utxo=utxo(PLAIN_TX, plain_in, PLAIN_SCRIPT, None),
            ),
        )
    binding = CovenantBinding(authorizing_input=active, covenant_id=Hash(COVENANT_ID))
    scenario_outputs, mirror_outputs = [], []
    for output in outputs:
        if output[0] == "covenant":
            _, count, value = output
            scenario_outputs.append(
                {"value": value, "covenant_id": COVENANT_ID, "authorizing_input": active, "state": {"count": count}}
            )
            mirror_outputs.append(TransactionOutput(value, lock(source, count), binding))
        else:
            _, value = output
            scenario_outputs.append({"value": value, "script": PLAIN_SCRIPT_BYTES})
            mirror_outputs.append(TransactionOutput(value, PLAIN_SCRIPT, None))
    scenario = {"inputs": scenario_inputs, "outputs": scenario_outputs}
    if active:
        scenario["active_input_index"] = active
    mirror = Transaction(
        1,
        mirror_inputs,
        mirror_outputs,
        lock_time=0,
        subnetwork_id=bytes(20),
        gas=0,
        payload=b"",
        mass=0,
    )
    return scenario, mirror, active


# `shape` below: the optional arguments of `render` (plain_in, source, plain_first, value_in).


def sign(previous, outputs, key=OWNER_KEY, sighash=DEFAULT_TYPE, **shape) -> bytes:
    """Signature of `key` over the transaction described by the arguments."""
    _, mirror, active = render(previous, outputs, **shape)
    pushed = create_input_signature(mirror, active, key, getattr(SighashType, sighash))
    return bytes.fromhex(pushed)[1:]  # drop the push opcode


def run(function, arguments, previous, outputs, **shape):
    scenario, _, _ = render(previous, outputs, **shape)
    return silverscript.debug_call(
        shape.get("source", CLOSABLE), function, args=arguments, constructor_args=[0, OWNER], tx=scenario
    )


def move(function, amount, previous, outputs, key=OWNER_KEY, sighash=DEFAULT_TYPE, **shape):
    """A counting move carrying a genuine signature of `key` over exactly this transaction."""
    signature = sign(previous, outputs, key, sighash, **shape)
    return run(function, [amount, signature], previous, outputs, **shape)


def close(previous, outputs, key=OWNER_KEY, sighash=DEFAULT_TYPE, next_states=None, **shape):
    """The closing move carrying a genuine signature of `key` over exactly this transaction."""
    signature = sign(previous, outputs, key, sighash, **shape)
    states = NO_NEXT_STATE if next_states is None else next_states
    return run("close", [states, signature], previous, outputs, **shape)


def failing_line(result) -> int:
    return result.failure.frames[0].line


def without(rule: str, occurrence: int = 1) -> str:
    """The contract with one line removed: the copy used for a comparison."""
    lines = CLOSABLE.splitlines(keepends=True)
    hits = [index for index, line in enumerate(lines) if rule in line]
    if len(hits) < occurrence:
        raise LookupError(f"{rule!r} (occurrence {occurrence}) not found in the contract source")
    return "".join(line for index, line in enumerate(lines) if index != hits[occurrence - 1])


def made_up_signature() -> bytes:
    return os.urandom(64) + b"\x01"


class Checks(unittest.TestCase):
    def assert_refused_at(self, result, rule: str, occurrence: int = 1):
        """Refused, and by this rule. The refusal is asserted before the rule's line is looked up."""
        self.assertFalse(result.success)
        self.assertEqual(failing_line(result), line_of(CLOSABLE, rule, occurrence))


class Compilation(unittest.TestCase):
    """What the compiler makes of the contract. None of these depends on a rule of the contract."""

    def test_three_moves_and_nothing_else_can_be_called(self):
        contract = silverscript.compile(CLOSABLE, [0, OWNER])
        entries = {entry.name: [(p.type_name, p.name) for p in entry.params] for entry in contract.abi}
        self.assertEqual(
            entries,
            {
                "__covenant_entrypoint_auth_add": [("int", "amount"), ("sig", "owner_sig")],
                "__covenant_entrypoint_auth_subtract": [("int", "amount"), ("sig", "owner_sig")],
                "__covenant_entrypoint_auth_close": [("State[]", "next_states"), ("sig", "owner_sig")],
            },
        )

    def test_helper_functions_are_unknown_to_the_call_builder(self):
        contract = silverscript.compile(CLOSABLE, [0, OWNER])
        # `build_sig_script` knows none of the names written in a covenant contract, `add` included
        # (only the generated entrypoint names), so it alone proves nothing about the helpers; the
        # builder that does know the moves is asked too.
        self.assertTrue(contract.build_sig_script_for_covenant_decl("add", [5, made_up_signature()]))
        for helper, arguments in (("onlyOwner", [made_up_signature()]), ("keepCoins", [])):
            for builder in (contract.build_sig_script_for_covenant_decl, contract.build_sig_script):
                with self.subTest(helper=helper, builder=builder.__name__):
                    with self.assertRaises(silverscript.SilverScriptError):
                        builder(helper, arguments)

    def test_owner_is_part_of_the_template_and_the_count_is_not(self):
        other = bytes.fromhex(_stranger.xonly_public_key)
        mine = silverscript.compile(CLOSABLE, [0, OWNER])
        self.assertNotEqual(bytes(mine.template_hash), bytes(silverscript.compile(CLOSABLE, [0, other]).template_hash))
        self.assertEqual(bytes(mine.template_hash), bytes(silverscript.compile(CLOSABLE, [5, OWNER]).template_hash))

    def test_close_does_not_compile_without_the_termination_opt_in(self):
        self.assertEqual(CLOSABLE.count(CLOSE_DECLARATION), 1)
        for weaker in ("#[covenant.singleton(mode = transition)]", CLOSE_DECLARATION.replace("allowed", "disallowed")):
            with self.subTest(declaration=weaker):
                with self.assertRaises(silverscript.SilverScriptError):
                    silverscript.compile(CLOSABLE.replace(CLOSE_DECLARATION, weaker), [0, OWNER])


class OwnerCounts(Checks):
    """The counting moves behave as in OwnedCounter."""

    def test_owner_can_add_and_subtract(self):
        for function, amount, count in MOVES:
            with self.subTest(function=function):
                self.assertTrue(move(function, amount, 10, [covenant(count)]).success)

    def test_owner_can_subtract_down_to_exactly_zero(self):
        self.assertTrue(move("subtract", 10, 10, [covenant(0)]).success)

    def test_honest_moves_pass_at_other_counts_and_for_larger_amounts(self):
        # No other accepted move here starts above count 10 or takes an amount above 10: a rule
        # that is too strict beyond that would pass them all.
        for previous, function, amount in HONEST_ELSEWHERE:
            claimed = previous + amount if function == "add" else previous - amount
            with self.subTest(previous=previous, function=function, amount=amount):
                self.assertTrue(move(function, amount, previous, [covenant(claimed)]).success)

    def test_fee_from_a_separate_input_and_a_top_up_pass(self):
        for function, amount, count in MOVES:
            with self.subTest(function=function):
                self.assertTrue(move(function, amount, 10, [covenant(count)], plain_in=300).success)
                self.assertTrue(move(function, amount, 10, [covenant(count, VALUE + 1000)], plain_in=1300).success)

    def test_owner_cannot_take_coins_out_by_counting(self):
        # Attributed by comparison: the same signed transaction passes once `keepCoins();` is not called.
        shapes = {
            "one sompi less": ([covenant(None, VALUE - 1)], None),
            "leak to a plain output": ([covenant(None, 1000), plain(4000)], None),
            "leak behind a big first output": ([plain(9000), covenant(None, 1000)], 5000),
            "leak split over two plain outputs": ([covenant(None, 1000), plain(2000), plain(2000)], None),
        }
        for occurrence, (function, amount, count) in enumerate(MOVES, start=1):
            for label, (shape, plain_in) in shapes.items():
                outputs = [covenant(count, o[2]) if o[0] == "covenant" else o for o in shape]
                with self.subTest(function=function, shape=label):
                    self.assertFalse(move(function, amount, 10, outputs, plain_in=plain_in).success)
                    control = without(KEEP_CALL, occurrence)
                    self.assertTrue(move(function, amount, 10, outputs, plain_in=plain_in, source=control).success)

    def test_leak_is_refused_when_the_covenant_input_is_not_first(self):
        # A 1-sompi ordinary input first: a rule reading "input #0" would be satisfied by 1 sompi.
        for occurrence, (function, amount, count) in enumerate(MOVES, start=1):
            honest = [covenant(count)]
            leaking = [covenant(count, 1), plain(VALUE)]
            with self.subTest(function=function):
                self.assertTrue(move(function, amount, 10, honest, plain_in=1, plain_first=True).success)
                self.assertFalse(move(function, amount, 10, leaking, plain_in=1, plain_first=True).success)
                control = without(KEEP_CALL, occurrence)
                self.assertTrue(
                    move(function, amount, 10, leaking, plain_in=1, plain_first=True, source=control).success
                )

    def test_coin_rule_follows_the_coin_being_spent_not_a_fixed_amount(self):
        # Every other scenario spends a 5000-sompi coin: a rule comparing with the number 5000 would pass them all.
        for occurrence, (function, amount, count) in enumerate(MOVES, start=1):
            leaking = [covenant(count, VALUE), plain(4000)]
            with self.subTest(function=function):
                self.assertTrue(move(function, amount, 10, [covenant(count, 9000)], value_in=9000).success)
                self.assertTrue(move(function, amount, 10, [covenant(count, 3000)], value_in=3000).success)
                self.assertFalse(move(function, amount, 10, leaking, value_in=9000).success)
                self.assertFalse(move(function, amount, 10, [covenant(count, 8999)], value_in=9000).success)
                control = without(KEEP_CALL, occurrence)
                self.assertTrue(move(function, amount, 10, leaking, value_in=9000, source=control).success)

    def test_amount_rules_still_hold_for_the_owner(self):
        cases = (("add", 1, ((0, 10), (-20, -10))), ("subtract", 2, ((0, 10), (-5, 15))))
        for function, occurrence, amounts in cases:
            for amount, claimed in amounts:
                with self.subTest(function=function, amount=amount):
                    result = move(function, amount, 10, [covenant(claimed)])
                    self.assert_refused_at(result, AMOUNT_RULE, occurrence)

    def test_amount_rules_hold_at_every_count_and_for_any_negative_amount(self):
        # The check above starts at count 10 and tries 0 and one small negative: a rule waived
        # at count 0, or one letting large negatives through, would pass it.
        for previous in (0, 1):
            for function in ("add", "subtract"):
                for amount in (0, -1, -101, -(2**62)):
                    claimed = previous + amount if function == "add" else previous - amount
                    with self.subTest(previous=previous, function=function, amount=amount):
                        self.assertFalse(move(function, amount, previous, [covenant(claimed)]).success)

    def test_owner_cannot_go_below_zero(self):
        self.assert_refused_at(move("subtract", 11, 10, [covenant(-1)]), BELOW_ZERO_RULE)

    def test_below_zero_rule_holds_at_other_counts_and_for_larger_amounts(self):
        # The check above starts at count 10 and overdraws by one: a rule skipped at count 0, or
        # for large amounts, would pass it.
        for previous, amount in ((0, 1), (1, 2), (10, 100), (10, 2**62), (2**40, 2**40 + 1)):
            with self.subTest(previous=previous, amount=amount):
                self.assertFalse(move("subtract", amount, previous, [covenant(previous - amount)]).success)

    def test_owner_cannot_claim_a_wrong_next_state(self):
        for claimed in (14, 16):
            with self.subTest(claimed=claimed):
                self.assertFalse(move("add", 5, 10, [covenant(claimed)]).success)

    def test_counting_moves_cannot_end_the_covenant(self):
        # Compiler-generated behaviour of a move that returns one state, not a rule of this contract.
        for function, amount, _ in MOVES:
            with self.subTest(function=function):
                self.assertFalse(move(function, amount, 10, [plain(VALUE)]).success)


class NobodyElseCounts(Checks):
    def test_genuine_signature_of_another_key_is_refused_in_both_moves(self):
        for function, amount, count in MOVES:
            with self.subTest(function=function):
                self.assert_refused_at(move(function, amount, 10, [covenant(count)], key=STRANGER_KEY), SIGNATURE_RULE)

    def test_made_up_signature_is_refused_in_both_moves(self):
        for function, amount, count in MOVES:
            with self.subTest(function=function):
                result = run(function, [amount, made_up_signature()], 10, [covenant(count)])
                self.assert_refused_at(result, SIGNATURE_RULE)

    def test_the_placeholder_signature_is_refused_in_every_move(self):
        # 64 zero bytes and the default type: what the testnet builder puts in before signing.
        # It passes the type rule, so only the signature rule can stop it; a made-up signature
        # (above) is random and never equals a value a rule might special-case.
        placeholder = bytes(64) + b"\x01"
        for function, amount, count in MOVES:
            with self.subTest(function=function):
                self.assert_refused_at(run(function, [amount, placeholder], 10, [covenant(count)]), SIGNATURE_RULE)
        with self.subTest(function="close"):
            self.assert_refused_at(run("close", [NO_NEXT_STATE, placeholder], 10, [plain(VALUE)]), SIGNATURE_RULE)

    def test_without_the_owner_rule_anyone_could_count(self):
        # The comparison: in each move it is the call of the owner rule that keeps a stranger out.
        for occurrence, (function, amount, count) in enumerate(MOVES, start=1):
            with self.subTest(function=function):
                self.assertFalse(move(function, amount, 10, [covenant(count)], key=STRANGER_KEY).success)
                control = without(OWNER_CALL, occurrence)
                self.assertTrue(
                    move(function, amount, 10, [covenant(count)], key=STRANGER_KEY, source=control).success
                )


class OwnerCloses(unittest.TestCase):
    def test_owner_can_close_and_pay_the_coins_out(self):
        payouts = {
            "all to one ordinary output": [plain(VALUE)],
            "split between two ordinary outputs": [plain(VALUE - 1000), plain(1000)],
            "less than the covenant held, the rest is the fee": [plain(VALUE - 300)],
            "no output at all": [],
        }
        for label, outputs in payouts.items():
            with self.subTest(payout=label):
                self.assertTrue(close(10, outputs).success)

    def test_owner_can_close_from_any_count_and_a_coin_of_any_size(self):
        for count in (0, 10, -10):
            with self.subTest(count=count):
                self.assertTrue(close(count, [plain(VALUE)]).success)
        for value in (1, 9000):
            with self.subTest(value=value):
                self.assertTrue(close(10, [plain(value)], value_in=value).success)

    def test_closing_with_the_fee_from_a_separate_input(self):
        self.assertTrue(close(10, [plain(VALUE), plain(100)], plain_in=400).success)

    def test_closing_when_the_covenant_input_is_not_first(self):
        self.assertTrue(close(10, [plain(VALUE)], plain_in=400, plain_first=True).success)


class NobodyElseCloses(Checks):
    def test_genuine_signature_of_another_key_is_refused(self):
        self.assert_refused_at(close(10, [plain(VALUE)], key=STRANGER_KEY), SIGNATURE_RULE)

    def test_made_up_signature_is_refused(self):
        result = run("close", [NO_NEXT_STATE, made_up_signature()], 10, [plain(VALUE)])
        self.assert_refused_at(result, SIGNATURE_RULE)

    def test_owner_signature_for_another_payout_is_refused(self):
        # The signature pins where the coins go: it cannot be moved onto another payout.
        for_one_payout = sign(10, [plain(VALUE)])
        elsewhere = run("close", [NO_NEXT_STATE, for_one_payout], 10, [plain(VALUE - 1000), plain(1000)])
        self.assert_refused_at(elsewhere, SIGNATURE_RULE)

    def test_owner_signature_for_a_counting_transaction_does_not_close(self):
        for_add = sign(10, [covenant(15)])
        self.assert_refused_at(run("close", [NO_NEXT_STATE, for_add], 10, [plain(VALUE)]), SIGNATURE_RULE)

    def test_owner_signature_for_a_closing_transaction_does_not_count(self):
        for_close = sign(10, [plain(VALUE)])
        self.assert_refused_at(run("add", [5, for_close], 10, [covenant(15)]), SIGNATURE_RULE)

    def test_without_the_owner_rule_anyone_could_close(self):
        # The comparison for the checks above: it is the owner rule that keeps a stranger out.
        self.assertFalse(close(10, [plain(VALUE)], key=STRANGER_KEY).success)
        control = without(OWNER_CALL, 3)
        self.assertTrue(close(10, [plain(VALUE)], key=STRANGER_KEY, source=control).success)


class WhatTheSignatureCovers(Checks):
    def test_signature_commits_to_the_transaction_not_to_the_move(self):
        # One transaction, one owner signature made for it, tried through all three moves.
        # The owner rule is satisfied each time; what the transaction may do is decided by
        # the other rules, and only the move it was built for lets it through.
        transaction = [covenant(15)]
        signature = sign(10, transaction)
        self.assertTrue(run("add", [5, signature], 10, transaction).success)
        self.assertFalse(run("close", [NO_NEXT_STATE, signature], 10, transaction).success)
        self.assert_refused_at(run("subtract", [-5, signature], 10, transaction), AMOUNT_RULE, 2)
        # That `close` did not stop at the owner rule is shown by comparison, on the copy without
        # the empty-list rule: there the owner's signature for this transaction closes into its
        # continuation, and a stranger's signature for the same transaction does not.
        control = without(EMPTY_LIST_RULE)
        continuing = [{"count": 15}]
        by_owner = sign(10, transaction, source=control)
        by_stranger = sign(10, transaction, key=STRANGER_KEY, source=control)
        self.assertTrue(run("close", [continuing, by_owner], 10, transaction, source=control).success)
        self.assertFalse(run("close", [continuing, by_stranger], 10, transaction, source=control).success)


class CloseEndsTheCovenant(Checks):
    """`close` cannot be used to continue the covenant, least of all at a chosen count."""

    def test_close_with_a_continuing_output_is_refused(self):
        # Compiler-generated behaviour: the outputs must match the returned list, which is empty.
        for count in (10, 999):
            with self.subTest(count=count):
                self.assertFalse(close(10, [covenant(count)]).success)

    def test_close_with_a_list_of_next_states_is_refused(self):
        self.assertFalse(close(10, [covenant(999)], next_states=[{"count": 999}]).success)
        self.assertFalse(close(10, [plain(VALUE)], next_states=[{"count": 999}]).success)

    def test_close_cannot_continue_the_covenant_at_any_count(self):
        # The checks above jump from 10 to 999. `close` has no coin rule: a continuation at the
        # current count, or one step away, would be a move that lets coins out while the covenant
        # lives on. Also from count 0, where no other closing check carries a next state.
        pairs = ((10, 10), (10, 11), (10, 9), (10, 0), (10, -1), (0, 0), (0, 1), (0, -1), (0, 999))
        shapes = (
            ("all coins kept", lambda count: [covenant(count)]),
            ("leak", lambda count: [covenant(count, 1000), plain(4000)]),
        )
        for previous, target in pairs:
            for label, outputs in shapes:
                with self.subTest(previous=previous, target=target, case=label):
                    self.assertFalse(close(previous, outputs(target), next_states=[{"count": target}]).success)
        # The comparison, looked up only after every refusal is asserted: without the
        # empty-list rule each of these transactions passes.
        control = without(EMPTY_LIST_RULE)
        for previous, target in pairs:
            for label, outputs in shapes:
                with self.subTest(previous=previous, target=target, case=label, contract="without the rule"):
                    result = close(previous, outputs(target), next_states=[{"count": target}], source=control)
                    self.assertTrue(result.success)

    def test_without_the_empty_list_rule_close_would_set_any_count_and_let_coins_out(self):
        # The comparison: with the rule removed, the owner's `close` moves the covenant to a count
        # of choice and may keep only a part of the coins in it.
        jump = [covenant(999)]
        jump_and_leak = [covenant(999, 1000), plain(4000)]
        for label, outputs in (("jump", jump), ("jump and leak", jump_and_leak)):
            with self.subTest(case=label):
                self.assertFalse(close(10, outputs, next_states=[{"count": 999}]).success)
                control = without(EMPTY_LIST_RULE)
                self.assertTrue(close(10, outputs, next_states=[{"count": 999}], source=control).success)


class SignatureTypeIsPinned(Checks):
    """Only a signature of the default type is accepted, in every move."""

    def test_every_other_signature_type_is_refused_in_every_move(self):
        for sighash in OTHER_TYPES:
            for function, amount, count in MOVES:
                with self.subTest(sighash=sighash, function=function):
                    self.assert_refused_at(move(function, amount, 10, [covenant(count)], sighash=sighash), TYPE_RULE)
            with self.subTest(sighash=sighash, function="close"):
                self.assert_refused_at(close(10, [plain(VALUE)], sighash=sighash), TYPE_RULE)

    def test_without_the_type_rule_a_none_type_signature_lets_its_holder_redirect_the_payout(self):
        # The comparison: why the rule is there. A None-type signature made for one payout
        # is accepted on another once the type rule is removed.
        payout, elsewhere = [plain(VALUE)], [plain(VALUE - 1000), plain(1000)]
        kept = run("close", [NO_NEXT_STATE, sign(10, payout, sighash="None")], 10, elsewhere)
        self.assertFalse(kept.success)
        control = without(TYPE_RULE)
        loose = sign(10, payout, sighash="None", source=control)
        self.assertTrue(run("close", [NO_NEXT_STATE, loose], 10, elsewhere, source=control).success)


if __name__ == "__main__":
    unittest.main()
