"""Offline checks for the transaction builder used by the testnet run.

No network: the UTXOs are made up, and the key is random and discarded. The
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


class BuilderBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        keypair = Keypair.random()
        cls.key = PrivateKey(keypair.private_key)
        cls.change_script = pay_to_address_script(keypair.to_address(live.NETWORK_TYPE))
        cls.funding = entry("aa" * 32, 0, FUNDING_AMOUNT, cls.change_script)
        cls.covenant = entry(
            "bb" * 32, 0, live.COVENANT_VALUE, live.lock_script(10), covenant_id=COVENANT_ID
        )

    def transition(self, **overrides):
        plan = live.transition_plan(
            self.covenant, self.funding, self.change_script, 10, "add", 5, **overrides
        )
        return live.build(plan, FEERATE)


class Genesis(BuilderBase):
    def test_genesis_locks_the_coins_and_returns_the_change(self):
        tx, fee = live.build(live.genesis_plan(self.funding, self.change_script), FEERATE)
        covenant_value, change = values(tx)
        self.assertEqual(covenant_value, live.COVENANT_VALUE)
        self.assertGreater(fee, 0)
        self.assertEqual(covenant_value + change + fee, FUNDING_AMOUNT)

    def test_genesis_output_gets_a_covenant_id(self):
        tx, _ = live.build(live.genesis_plan(self.funding, self.change_script), FEERATE)
        covenant, change = (output.to_dict().get("covenant") for output in tx.outputs)
        self.assertEqual(covenant["authorizingInput"], 0)
        self.assertEqual(len(covenant["covenantId"]), 64)
        self.assertIsNone(change)

    def test_genesis_is_signed_by_the_funding_key(self):
        tx, _ = live.build(live.genesis_plan(self.funding, self.change_script), FEERATE)
        signed = sign_transaction(tx, [self.key], True)
        self.assertTrue(signed.inputs[0].signature_script_as_hex)

    def test_funding_too_small_is_reported(self):
        small = entry("aa" * 32, 0, live.COVENANT_VALUE, self.change_script)
        with self.assertRaises(ValueError):
            live.build(live.genesis_plan(small, self.change_script), FEERATE)


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
        expected = live.lock_script(15)
        self.assertEqual(output["scriptPublicKey"], {"version": expected.version, "script": expected.script})

    def test_only_the_funding_input_is_signed(self):
        tx, _ = self.transition()
        covenant_call = tx.inputs[0].signature_script_as_hex
        self.assertEqual(covenant_call, live.unlock_script(10, "add", 5).hex())
        self.assertFalse(tx.inputs[1].signature_script_as_hex)
        signed = sign_transaction(tx, [self.key], False)
        self.assertEqual(signed.inputs[0].signature_script_as_hex, covenant_call)
        self.assertTrue(signed.inputs[1].signature_script_as_hex)

    def test_fee_follows_the_measured_mass(self):
        tx, fee = self.transition()
        budget = live.GRAMS_PER_COMPUTE_BUDGET_UNIT * live.COMPUTE_BUDGET * 2
        self.assertEqual(fee, (tx.mass + budget + live.FEE_MASS_SLACK) * FEERATE)

    def test_subtract_lands_on_the_lower_count(self):
        plan = live.transition_plan(self.covenant, self.funding, self.change_script, 10, "subtract", 3)
        tx, _ = live.build(plan, FEERATE)
        expected = live.lock_script(7)
        self.assertEqual(tx.outputs[0].to_dict()["scriptPublicKey"]["script"], expected.script)


class RuleBreakingAttempts(BuilderBase):
    """The builder will assemble these; refusing them is the node's job."""

    def test_leak_attempt_moves_coins_from_the_covenant_to_the_change(self):
        honest, _ = self.transition()
        leaking, _ = self.transition(covenant_value_out=live.COVENANT_VALUE - live.LEAK)
        self.assertEqual(values(leaking)[0], live.COVENANT_VALUE - live.LEAK)
        self.assertGreater(values(leaking)[1], values(honest)[1])

    def test_wrong_state_attempt_targets_another_count(self):
        tx, _ = self.transition(claimed_count=16)
        self.assertEqual(
            tx.outputs[0].to_dict()["scriptPublicKey"]["script"], live.lock_script(16).script
        )


if __name__ == "__main__":
    unittest.main()
