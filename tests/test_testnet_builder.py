"""Offline checks for the transaction builder used by the testnet runs.

No network: the UTXOs are made up, and the keys are random and discarded. The
checks cover how the transactions are put together — who pays the fee, what
the covenant output carries, what gets signed — not whether a node accepts
them.

Run from the repository root:

    python -m unittest discover -s tests -v
"""

import unittest

from kaspa import Keypair, PrivateKey, pay_to_address_script, sign_transaction

from testnet import counter_live as live

FEERATE = 100
FUNDING_AMOUNT = 10 * live.SOMPI_PER_KAS
COVENANT_ID = "f1" * 32
OWNED = live.CONTRACTS["owned"]


def entry(txid: str, index: int, amount: int, script_public_key, covenant_id=None) -> dict:
    """A UTXO entry shaped like the node's `get_utxos_by_addresses` reply."""
    return {
        "address": None,
        "outpoint": {"transactionId": txid, "index": index},
        "utxoEntry": {
            "amount": amount,
            "scriptPublicKey": {
                "version": script_public_key.version,
                "script": script_public_key.script,
            },
            "blockDaaScore": 0,
            "isCoinbase": False,
            "covenantId": covenant_id,
        },
    }


def values(tx) -> list:
    return [output.to_dict()["value"] for output in tx.outputs]


def script_hex(tx, index: int = 0) -> str:
    return tx.outputs[index].to_dict()["scriptPublicKey"]["script"]


class BuilderBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        keypair = Keypair.random()
        cls.key = PrivateKey(keypair.private_key)
        cls.owner = keypair.xonly_public_key
        cls.change_script = pay_to_address_script(keypair.to_address(live.NETWORK_TYPE))
        cls.funding = entry("aa" * 32, 0, FUNDING_AMOUNT, cls.change_script)
        cls.covenant = entry(
            "bb" * 32, 0, live.COVENANT_VALUE, live.lock_script(10), covenant_id=COVENANT_ID
        )

    def transition(self, **overrides):
        plan = live.transition_plan(
            self.covenant, self.funding, self.change_script, 10, "add", 5, **overrides
        )
        tx, fee, _, _ = live.build(plan, FEERATE)
        return tx, fee


class Genesis(BuilderBase):
    def build_genesis(self, **kwargs):
        tx, fee, _, _ = live.build(live.genesis_plan(self.funding, self.change_script, **kwargs), FEERATE)
        return tx, fee

    def test_genesis_locks_the_coins_and_returns_the_change(self):
        tx, fee = self.build_genesis()
        covenant_value, change = values(tx)
        self.assertEqual(covenant_value, live.COVENANT_VALUE)
        self.assertGreater(fee, 0)
        self.assertEqual(covenant_value + change + fee, FUNDING_AMOUNT)

    def test_genesis_output_gets_a_covenant_id(self):
        tx, _ = self.build_genesis()
        covenant, change = (output.to_dict().get("covenant") for output in tx.outputs)
        self.assertEqual(covenant["authorizingInput"], 0)
        self.assertEqual(len(covenant["covenantId"]), 64)
        self.assertIsNone(change)

    def test_genesis_is_signed_by_the_funding_key(self):
        tx, _ = self.build_genesis()
        signed = sign_transaction(tx, [self.key], True)
        self.assertTrue(signed.inputs[0].signature_script_as_hex)

    def test_funding_too_small_is_reported(self):
        small = entry("aa" * 32, 0, live.COVENANT_VALUE, self.change_script)
        with self.assertRaises(ValueError):
            live.build(live.genesis_plan(small, self.change_script), FEERATE)

    def test_each_contract_gets_its_own_genesis_script(self):
        scripts = set()
        for contract in live.CONTRACTS.values():
            owner = self.owner if contract.owned else None
            tx, _ = self.build_genesis(contract=contract, owner=owner)
            scripts.add(script_hex(tx))
        self.assertEqual(len(scripts), len(live.CONTRACTS))

    def test_owned_contract_needs_an_owner(self):
        with self.assertRaises(ValueError):
            live.genesis_plan(self.funding, self.change_script, contract=OWNED)


class Transition(BuilderBase):
    def test_covenant_keeps_its_coins_and_the_funding_input_pays_the_fee(self):
        tx, fee = self.transition()
        covenant_value, change = values(tx)
        self.assertEqual(covenant_value, live.COVENANT_VALUE)
        self.assertGreater(fee, 0)
        self.assertEqual(change + fee, FUNDING_AMOUNT)

    def test_covenant_output_continues_the_same_covenant_at_the_next_count(self):
        tx, _ = self.transition()
        output = tx.outputs[0].to_dict()
        self.assertEqual(output["covenant"]["covenantId"], COVENANT_ID)
        self.assertEqual(output["covenant"]["authorizingInput"], 0)
        self.assertEqual(script_hex(tx), live.lock_script(15).script)

    def test_only_the_funding_input_is_signed(self):
        signed, _ = live.signed_transition(
            self.key,
            FEERATE,
            covenant=self.covenant,
            funding=self.funding,
            change_script=self.change_script,
            count=10,
            function="add",
            amount=5,
        )
        self.assertEqual(
            signed.inputs[0].signature_script_as_hex, live.unlock_script(10, "add", 5).hex()
        )
        self.assertTrue(signed.inputs[1].signature_script_as_hex)

    def test_fee_follows_the_measured_mass(self):
        tx, fee = self.transition()
        budget = live.GRAMS_PER_COMPUTE_BUDGET_UNIT * live.COMPUTE_BUDGET * 2
        self.assertEqual(fee, (tx.mass + budget + live.FEE_MASS_SLACK) * FEERATE)

    def test_subtract_lands_on_the_lower_count(self):
        plan = live.transition_plan(self.covenant, self.funding, self.change_script, 10, "subtract", 3)
        tx, _, _, _ = live.build(plan, FEERATE)
        self.assertEqual(script_hex(tx), live.lock_script(7).script)


class OwnedTransition(BuilderBase):
    def owned_args(self):
        covenant = entry(
            "cc" * 32,
            0,
            live.COVENANT_VALUE,
            live.lock_script(10, OWNED, self.owner),
            covenant_id=COVENANT_ID,
        )
        return dict(
            covenant=covenant,
            funding=self.funding,
            change_script=self.change_script,
            count=10,
            function="add",
            amount=5,
            contract=OWNED,
            owner=self.owner,
        )

    def test_the_acting_key_signature_is_put_into_the_covenant_call(self):
        signed, _ = live.signed_transition(self.key, FEERATE, **self.owned_args())
        with_placeholder = live.unlock_script(10, "add", 5, OWNED, self.owner).hex()
        call = signed.inputs[0].signature_script_as_hex
        self.assertEqual(len(call), len(with_placeholder))
        self.assertNotEqual(call, with_placeholder)
        self.assertNotIn(live.SIGNATURE_PLACEHOLDER.hex(), call)

    def test_placeholder_and_real_signature_give_the_same_amounts(self):
        signed, fee = live.signed_transition(self.key, FEERATE, **self.owned_args())
        draft, draft_fee, _, _ = live.build(live.transition_plan(**self.owned_args()), FEERATE)
        self.assertEqual(values(signed), values(draft))
        self.assertEqual(fee, draft_fee)
        self.assertEqual(signed.mass, draft.mass)

    def test_another_key_produces_a_different_call_for_the_same_covenant(self):
        stranger = PrivateKey(Keypair.random().private_key)
        mine, _ = live.signed_transition(self.key, FEERATE, **self.owned_args())
        theirs, _ = live.signed_transition(stranger, FEERATE, **self.owned_args())
        self.assertNotEqual(
            mine.inputs[0].signature_script_as_hex, theirs.inputs[0].signature_script_as_hex
        )
        # Both still target the owner's covenant: the owner is fixed by the script, not by the signer.
        self.assertEqual(script_hex(mine), script_hex(theirs))


class RuleBreakingAttempts(BuilderBase):
    """The builder will assemble these; refusing them is the node's job."""

    def test_leak_attempt_moves_coins_from_the_covenant_to_the_change(self):
        honest, _ = self.transition()
        leaking, _ = self.transition(covenant_value_out=live.COVENANT_VALUE - live.LEAK)
        self.assertEqual(values(leaking)[0], live.COVENANT_VALUE - live.LEAK)
        self.assertGreater(values(leaking)[1], values(honest)[1])

    def test_wrong_state_attempt_targets_another_count(self):
        tx, _ = self.transition(claimed_count=16)
        self.assertEqual(script_hex(tx), live.lock_script(16).script)


class Transfer(BuilderBase):
    def test_plain_payment_sends_the_amount_and_returns_the_change(self):
        other = pay_to_address_script(Keypair.random().to_address(live.NETWORK_TYPE))
        amount = 3 * live.SOMPI_PER_KAS
        tx, fee, _, _ = live.build(
            live.transfer_plan(self.funding, self.change_script, other, amount), FEERATE
        )
        sent, change = values(tx)
        self.assertEqual(sent, amount)
        self.assertEqual(script_hex(tx), other.script)
        self.assertEqual(sent + change + fee, FUNDING_AMOUNT)
        self.assertIsNone(tx.outputs[0].to_dict().get("covenant"))


if __name__ == "__main__":
    unittest.main()
