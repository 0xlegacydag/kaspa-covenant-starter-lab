"""Offline checks for OwnedCounter: only the owner's signature moves the count.

OwnedCounter is KeepingCounter plus one rule at the top of every transition:
`require(checkSig(owner_sig, owner))`. The owner's public key is a constructor
argument, so it is part of the contract template and cannot change while the
covenant lives.

A signature commits to a transaction. To check the accepting side offline,
each test builds the same transaction the debug engine synthesizes for its
scenario (one covenant input, one covenant output) and signs that mirror.

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
OWNED = (CONTRACTS / "counter_owned.sil").read_text(encoding="utf-8")

COVENANT_ID = "11" * 32
PREVIOUS_TX = "aa" * 32
PLAIN_TX = "bb" * 32
VALUE = 5000
SIGHASH_ALL = b"\x01"

_owner = Keypair.random()
_stranger = Keypair.random()
OWNER_KEY = PrivateKey(_owner.private_key)
STRANGER_KEY = PrivateKey(_stranger.private_key)
OWNER = bytes.fromhex(_owner.xonly_public_key)
SOMEONE_ELSE = bytes.fromhex(_stranger.xonly_public_key)
# Where ordinary (non-covenant) coins come from and go to in the scenarios.
PLAIN_SCRIPT = pay_to_address_script(_stranger.to_address("testnet"))

MOVES = (("add", 5, 15), ("subtract", 3, 7))


def line_of(source: str, fragment: str, occurrence: int = 1) -> int:
    seen = 0
    for number, line in enumerate(source.splitlines(), start=1):
        if fragment in line:
            seen += 1
            if seen == occurrence:
                return number
    # Not a failed assertion: a rule line that is missing says nothing about behaviour.
    raise LookupError(f"{fragment!r} (occurrence {occurrence}) not found in the contract source")


def lock(count: int, owner: bytes = OWNER):
    return pay_to_script_hash_script(silverscript.compile(OWNED, [count, owner]).bytecode)


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


def render(
    previous,
    claimed,
    value_out=VALUE,
    next_owner=None,
    plain_in=None,
    covenant_input_first=True,
    plain_out=None,
    plain_output_first=False,
    value_in=VALUE,
    second_plain_out=None,
):
    """One transaction, described once and rendered twice.

    Returns (scenario, mirror): the scenario the debug engine runs, and the
    same transaction as a `kaspa.Transaction`, so that it can be signed.
    Optional extras: one ordinary input (`plain_in` sompi) and one ordinary
    output (`plain_out` sompi), each before or after the covenant one; a second
    ordinary output (`second_plain_out` sompi) goes last. The covenant coin
    holds `value_in` sompi.
    """
    active = 0 if (plain_in is None or covenant_input_first) else 1
    plain_script_bytes = bytes.fromhex(PLAIN_SCRIPT.script)
    binding = CovenantBinding(authorizing_input=active, covenant_id=Hash(COVENANT_ID))

    covenant_in = {
        "utxo_value": value_in,
        "covenant_id": COVENANT_ID,
        "state": {"count": previous},
        "prev_txid": PREVIOUS_TX,
        "prev_index": 0,
    }
    covenant_out = {
        "value": value_out,
        "covenant_id": COVENANT_ID,
        "authorizing_input": active,
        "state": {"count": claimed},
    }
    if next_owner is not None:
        covenant_out["constructor_args"] = [0, next_owner]
    scenario_inputs = [covenant_in]
    mirror_inputs = [
        TransactionInput(
            TransactionOutpoint(Hash(PREVIOUS_TX), 0),
            b"",
            sequence=0,
            sig_op_count=0,
            compute_budget=0,
            utxo=utxo(PREVIOUS_TX, value_in, lock(previous), COVENANT_ID),
        )
    ]
    scenario_outputs = [covenant_out]
    mirror_outputs = [TransactionOutput(value_out, lock(claimed, next_owner or OWNER), binding)]

    if plain_in is not None:
        position = len(scenario_inputs) if covenant_input_first else 0
        scenario_inputs.insert(
            position,
            {
                "utxo_value": plain_in,
                "prev_txid": PLAIN_TX,
                "prev_index": 0,
                "utxo_script": plain_script_bytes,
            },
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
    if plain_out is not None:
        position = 0 if plain_output_first else len(scenario_outputs)
        scenario_outputs.insert(position, {"value": plain_out, "script": plain_script_bytes})
        mirror_outputs.insert(position, TransactionOutput(plain_out, PLAIN_SCRIPT, None))
    if second_plain_out is not None:
        scenario_outputs.append({"value": second_plain_out, "script": plain_script_bytes})
        mirror_outputs.append(TransactionOutput(second_plain_out, PLAIN_SCRIPT, None))

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


def sign(previous, claimed, value_out=VALUE, key=OWNER_KEY, sighash=None, **shape) -> bytes:
    """Signature of `key` over the transaction described by the arguments."""
    _, mirror, active = render(previous, claimed, value_out, **shape)
    args = (mirror, active, key) if sighash is None else (mirror, active, key, sighash)
    return bytes.fromhex(create_input_signature(*args))[1:]  # drop the push opcode


def call(function, amount, signature, previous=10, claimed=15, value_out=VALUE, **shape):
    scenario, _, _ = render(previous, claimed, value_out, **shape)
    return silverscript.debug_call(
        OWNED, function, args=[amount, signature], constructor_args=[0, OWNER], tx=scenario
    )


def signed_call(function, amount, previous=10, claimed=15, value_out=VALUE, key=OWNER_KEY, **shape):
    """The transition with a genuine signature of `key` over exactly this transaction."""
    signature = sign(previous, claimed, value_out, key, **shape)
    return call(function, amount, signature, previous, claimed, value_out, **shape)


def failing_line(result) -> int:
    return result.failure.frames[0].line


SIGNATURE_LINE = "require(checkSig("
AMOUNT_LINE = "require(amount > 0)"
BELOW_ZERO_LINE = "prev_state.count - amount >= 0"


class OwnedCounterCompilation(unittest.TestCase):
    def test_every_transition_takes_a_signature(self):
        contract = silverscript.compile(OWNED, [0, OWNER])
        for entry in contract.abi:
            params = [(p.type_name, p.name) for p in entry.params]
            self.assertEqual(params, [("int", "amount"), ("sig", "owner_sig")])

    def test_owner_is_part_of_the_contract_template(self):
        mine = silverscript.compile(OWNED, [0, OWNER])
        theirs = silverscript.compile(OWNED, [0, SOMEONE_ELSE])
        self.assertNotEqual(bytes(mine.template_hash), bytes(theirs.template_hash))

    def test_count_is_not_part_of_the_template(self):
        at_zero = silverscript.compile(OWNED, [0, OWNER])
        at_five = silverscript.compile(OWNED, [5, OWNER])
        self.assertEqual(bytes(at_zero.template_hash), bytes(at_five.template_hash))
        self.assertNotEqual(at_zero.bytecode, at_five.bytecode)

    def test_signature_argument_must_be_65_bytes(self):
        contract = silverscript.compile(OWNED, [0, OWNER])
        for wrong_length in (0, 64, 66):
            with self.subTest(length=wrong_length):
                with self.assertRaises(silverscript.SilverScriptError):
                    contract.build_sig_script_for_covenant_decl("add", [5, bytes(wrong_length)])


class OwnerMoves(unittest.TestCase):
    def test_owner_can_add(self):
        self.assertTrue(signed_call("add", 5, 10, 15).success)

    def test_owner_can_subtract(self):
        self.assertTrue(signed_call("subtract", 3, 10, 7).success)

    def test_owner_can_subtract_down_to_exactly_zero(self):
        self.assertTrue(signed_call("subtract", 10, 10, 0).success)

    def test_owner_can_top_the_covenant_up(self):
        self.assertTrue(signed_call("add", 5, 10, 15, value_out=VALUE + 1000).success)

    def test_fee_can_come_from_a_separate_input(self):
        for function, amount, count in MOVES:
            with self.subTest(function=function):
                self.assertTrue(signed_call(function, amount, 10, count, plain_in=300).success)

    def test_covenant_input_and_output_do_not_have_to_be_first(self):
        shape = dict(plain_in=2000, covenant_input_first=False, plain_out=1700, plain_output_first=True)
        for function, amount, count in MOVES:
            with self.subTest(function=function):
                self.assertTrue(signed_call(function, amount, 10, count, **shape).success)


class NobodyElseMoves(unittest.TestCase):
    def assert_refused_at_signature_check(self, result, occurrence):
        self.assertFalse(result.success)
        self.assertEqual(failing_line(result), line_of(OWNED, SIGNATURE_LINE, occurrence))

    def test_genuine_signature_of_another_key_is_refused(self):
        self.assert_refused_at_signature_check(signed_call("add", 5, key=STRANGER_KEY), 1)
        self.assert_refused_at_signature_check(
            signed_call("subtract", 3, 10, 7, key=STRANGER_KEY), 2
        )

    def test_owner_signature_made_for_another_transaction_is_refused(self):
        for_another_amount_of_coins = sign(10, 15, VALUE + 1)
        self.assert_refused_at_signature_check(call("add", 5, for_another_amount_of_coins), 1)
        for_another_count = sign(10, 16, VALUE)
        self.assert_refused_at_signature_check(call("add", 5, for_another_count), 1)

    def test_made_up_signature_is_refused(self):
        made_up = os.urandom(64) + SIGHASH_ALL
        self.assert_refused_at_signature_check(call("add", 5, made_up), 1)
        self.assert_refused_at_signature_check(call("subtract", 3, made_up, claimed=7), 2)

    def test_malformed_signature_is_refused(self):
        self.assert_refused_at_signature_check(call("add", 5, bytes(65)), 1)

    def test_signature_is_checked_before_anything_else(self):
        # Also breaks the amount rule and the value rule; the signature check comes first.
        made_up = os.urandom(64) + SIGHASH_ALL
        result = call("add", -20, made_up, claimed=-10, value_out=1000)
        self.assert_refused_at_signature_check(result, 1)


class OwnerCannotBreakTheOtherRules(unittest.TestCase):
    """Every transaction here carries a genuine owner signature over itself."""

    def test_owner_cannot_take_a_single_sompi_out(self):
        self.assertTrue(signed_call("add", 5, value_out=VALUE).success)
        self.assertFalse(signed_call("add", 5, value_out=VALUE - 1).success)
        self.assertTrue(signed_call("subtract", 3, 10, 7, value_out=VALUE).success)
        self.assertFalse(signed_call("subtract", 3, 10, 7, value_out=VALUE - 1).success)

    def test_owner_cannot_hide_a_leak_behind_a_big_first_output(self):
        # The owner adds 5000 sompi of their own, puts a plain output first and
        # leaves 1000 in the covenant. With all 5000 left in, the same shape passes.
        hidden = dict(plain_in=5000, plain_out=9000, plain_output_first=True)
        honest = dict(plain_in=5000, plain_out=5000, plain_output_first=True)
        for function, amount, count in MOVES:
            with self.subTest(function=function):
                self.assertTrue(signed_call(function, amount, 10, count, VALUE, **honest).success)
                self.assertFalse(signed_call(function, amount, 10, count, 1000, **hidden).success)

    def test_owner_cannot_leak_when_the_covenant_input_is_not_first(self):
        # A 1-sompi ordinary input first: a rule reading "input #0" would be satisfied by 1 sompi.
        shape = dict(plain_in=1, covenant_input_first=False, plain_out=VALUE)
        honest = dict(plain_in=1, covenant_input_first=False)
        for function, amount, count in MOVES:
            with self.subTest(function=function):
                self.assertTrue(signed_call(function, amount, 10, count, VALUE, **honest).success)
                self.assertFalse(signed_call(function, amount, 10, count, 1, **shape).success)

    def test_owner_cannot_split_a_leak_over_two_plain_outputs(self):
        # No other scenario has three outputs.
        honest = dict(plain_in=500, plain_out=100, second_plain_out=100)
        for function, amount, count in MOVES:
            with self.subTest(function=function):
                self.assertTrue(signed_call(function, amount, 10, count, VALUE, **honest).success)
                leak = signed_call(function, amount, 10, count, 1000, plain_out=2000, second_plain_out=2000)
                self.assertFalse(leak.success)

    def test_the_coin_rule_follows_the_coin_being_spent_not_a_fixed_amount(self):
        # Every other scenario spends a 5000-sompi coin: a rule comparing with the number 5000 would pass them all.
        for function, amount, count in MOVES:
            with self.subTest(function=function):
                leak = signed_call(function, amount, 10, count, VALUE, value_in=9000, plain_out=4000)
                self.assertFalse(leak.success)
                self.assertFalse(signed_call(function, amount, 10, count, 8999, value_in=9000).success)
                self.assertTrue(signed_call(function, amount, 10, count, 9000, value_in=9000).success)
                self.assertTrue(signed_call(function, amount, 10, count, 3000, value_in=3000).success)

    def test_owner_cannot_add_zero_or_a_negative_amount(self):
        for amount, claimed in ((0, 10), (-20, -10)):
            with self.subTest(amount=amount):
                result = signed_call("add", amount, 10, claimed)
                self.assertFalse(result.success)
                self.assertEqual(failing_line(result), line_of(OWNED, AMOUNT_LINE, 1))

    def test_owner_cannot_subtract_zero_or_a_negative_amount(self):
        for amount, claimed in ((0, 10), (-5, 15)):
            with self.subTest(amount=amount):
                result = signed_call("subtract", amount, 10, claimed)
                self.assertFalse(result.success)
                self.assertEqual(failing_line(result), line_of(OWNED, AMOUNT_LINE, 2))

    def test_owner_cannot_go_below_zero(self):
        result = signed_call("subtract", 11, 10, -1)
        self.assertFalse(result.success)
        self.assertEqual(failing_line(result), line_of(OWNED, BELOW_ZERO_LINE))

    def test_owner_cannot_claim_a_wrong_next_state(self):
        for claimed in (14, 16):
            with self.subTest(claimed=claimed):
                self.assertFalse(signed_call("add", 5, 10, claimed).success)

    def test_owner_cannot_hand_the_covenant_to_another_owner(self):
        handover = sign(10, 15, VALUE, next_owner=SOMEONE_ELSE)
        result = call("add", 5, handover, next_owner=SOMEONE_ELSE)
        self.assertFalse(result.success)


class SignatureScope(unittest.TestCase):
    """What the owner's signature pins down depends on the sighash type the owner uses."""

    def test_default_signature_cannot_be_reused_for_another_outcome(self):
        for_fifteen = sign(10, 15, VALUE)
        self.assertTrue(call("add", 5, for_fifteen, claimed=15).success)
        self.assertFalse(call("add", 6, for_fifteen, claimed=16).success)

    def test_all_six_signature_types_are_accepted(self):
        # OwnedCounter does not restrict the type; ClosableCounter does.
        for name in ("All", "None", "Single", "AllAnyOneCanPay", "NoneAnyOneCanPay", "SingleAnyOneCanPay"):
            with self.subTest(sighash=name):
                signature = sign(10, 15, VALUE, sighash=getattr(SighashType, name))
                self.assertTrue(call("add", 5, signature, claimed=15).success)

    def test_sighash_none_signature_does_not_pin_the_next_count(self):
        # Recorded as observed behaviour: a signature that does not cover the outputs
        # lets whoever holds it choose the move. The coin rule still applies.
        loose = sign(10, 15, VALUE, sighash=getattr(SighashType, "None"))
        self.assertTrue(call("add", 6, loose, claimed=16).success)
        self.assertFalse(call("add", 6, loose, claimed=16, value_out=VALUE - 1).success)


if __name__ == "__main__":
    unittest.main()
