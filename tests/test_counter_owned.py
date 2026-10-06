"""Offline checks for OwnedCounter: only the owner's signature moves the count.

OwnedCounter is KeepingCounter plus one rule at the top of every transition:
`require(checkSig(owner_sig, owner))`. The owner's public key is a constructor
argument, so it is part of the contract template and cannot change while the
covenant lives.

What can be checked offline is the refusing side: transitions without a valid
owner signature. A valid signature commits to a real transaction, so the
accepting side is checked on testnet (see the testnet results).

Nothing here touches a network, a wallet or funds; the keys are random and
discarded.

Run from the repository root:

    python -m unittest discover -s tests -v
"""

import os
import unittest
from pathlib import Path

import kaspa.experimental.silverscript as silverscript
from kaspa import Keypair

CONTRACTS = Path(__file__).resolve().parent.parent / "contracts"
OWNED = (CONTRACTS / "counter_owned.sil").read_text(encoding="utf-8")
KEEPING = (CONTRACTS / "counter_keeping.sil").read_text(encoding="utf-8")

COVENANT_ID = "11" * 32
VALUE = 5000
SIGHASH_ALL = b"\x01"
OWNER = bytes.fromhex(Keypair.random().xonly_public_key)
SOMEONE_ELSE = bytes.fromhex(Keypair.random().xonly_public_key)


def line_of(source: str, fragment: str, occurrence: int = 1) -> int:
    seen = 0
    for number, line in enumerate(source.splitlines(), start=1):
        if fragment in line:
            seen += 1
            if seen == occurrence:
                return number
    raise AssertionError(f"{fragment!r} (occurrence {occurrence}) not found")


def well_formed_but_wrong_signature() -> bytes:
    """65 bytes with a valid sighash type, signing nothing in particular."""
    return os.urandom(64) + SIGHASH_ALL


def call(function, amount, signature, previous=10, claimed=15, value_out=VALUE):
    scenario = {
        "inputs": [{"utxo_value": VALUE, "covenant_id": COVENANT_ID, "state": {"count": previous}}],
        "outputs": [
            {
                "value": value_out,
                "covenant_id": COVENANT_ID,
                "authorizing_input": 0,
                "state": {"count": claimed},
            }
        ],
    }
    return silverscript.debug_call(
        OWNED, function, args=[amount, signature], constructor_args=[0, OWNER], tx=scenario
    )


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


class OwnedCounterRefusals(unittest.TestCase):
    def assert_refused_at_signature_check(self, result, occurrence):
        self.assertFalse(result.success)
        self.assertEqual(
            result.failure.frames[0].line, line_of(OWNED, "require(checkSig(", occurrence)
        )

    def test_add_without_a_valid_signature_is_refused(self):
        result = call("add", 5, well_formed_but_wrong_signature())
        self.assert_refused_at_signature_check(result, occurrence=1)

    def test_subtract_without_a_valid_signature_is_refused(self):
        result = call("subtract", 3, well_formed_but_wrong_signature(), claimed=7)
        self.assert_refused_at_signature_check(result, occurrence=2)

    def test_malformed_signature_is_refused(self):
        result = call("add", 5, bytes(65))
        self.assert_refused_at_signature_check(result, occurrence=1)

    def test_signature_is_checked_before_anything_else(self):
        # Also breaks the amount rule and the value rule; the signature check comes first.
        result = call("add", -20, well_formed_but_wrong_signature(), claimed=-10, value_out=1000)
        self.assert_refused_at_signature_check(result, occurrence=1)


class OwnedCounterVersusKeepingCounter(unittest.TestCase):
    def test_the_same_transition_needs_no_signature_in_keeping_counter(self):
        scenario = {
            "inputs": [{"utxo_value": VALUE, "covenant_id": COVENANT_ID, "state": {"count": 10}}],
            "outputs": [
                {
                    "value": VALUE,
                    "covenant_id": COVENANT_ID,
                    "authorizing_input": 0,
                    "state": {"count": 15},
                }
            ],
        }
        result = silverscript.debug_call(
            KEEPING, "add", args=[5], constructor_args=[0], tx=scenario
        )
        self.assertTrue(result.success)


if __name__ == "__main__":
    unittest.main()
