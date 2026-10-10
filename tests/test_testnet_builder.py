"""Offline checks for the transaction builder used by the testnet runs.

No network: the UTXOs are made up, and the keys are random and discarded. The
checks cover how the transactions are put together — who pays the fee, what
the covenant output carries, what gets signed — not whether a node accepts
them.

Run from the repository root:

    python -m unittest discover -s tests -v
"""

import asyncio
import contextlib
import json
import os
import tempfile
import unittest
from unittest import mock

import kaspa.experimental.silverscript as silverscript
from kaspa import (
    Hash,
    Keypair,
    PrivateKey,
    TransactionOutpoint,
    TransactionOutput,
    covenant_id,
    pay_to_address_script,
    sign_transaction,
)

from testnet import counter_live as live

FEERATE = 100
FUNDING_AMOUNT = 10 * live.SOMPI_PER_KAS
COVENANT_ID = "f1" * 32
OWNED = live.CONTRACTS["owned"]
CLOSABLE = live.CONTRACTS["closable"]

# Rules of ClosableCounter as they are written in the contract.
SIGNATURE_RULE = "require(checkSig(owner_sig, owner));"
TYPE_RULE = "require(owner_sig.slice(64, 65)"


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


def assert_same_weight(case, signed, draft, key) -> None:
    """The real signature takes exactly the room of the placeholder the fee was worked out with.

    `signed.mass == draft.mass` cannot show it: the builder copies that number
    from the draft. So the two covenant calls are compared byte for byte in
    length, and both transactions are measured again, the funding input signed
    in both. The second comparison is the weaker one: the SDK's function is
    meant for unsigned transactions (it adds one signature per input, so both
    numbers are 66 grams above `draft.mass` when the compute mass decides),
    and for a move paid from a large coin the storage mass decides, which does
    not depend on the calls at all.
    """
    with_placeholder = sign_transaction(draft, [key], False)
    case.assertEqual(
        len(signed.inputs[0].signature_script_as_hex), len(with_placeholder.inputs[0].signature_script_as_hex)
    )
    case.assertEqual(
        live.calculate_transaction_mass(live.NETWORK_ID, signed),
        live.calculate_transaction_mass(live.NETWORK_ID, with_placeholder),
    )


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
        # The id is the one the SDK derives from the coin being spent and the covenant's first output.
        spent = TransactionOutpoint(Hash("aa" * 32), 0)
        first_output = TransactionOutput(live.COVENANT_VALUE, live.lock_script(0), None)
        self.assertEqual(covenant["covenantId"], str(covenant_id(spent, [first_output])))
        self.assertIsNone(change)

    def test_the_id_of_a_genesis_transaction_is_worked_out_after_its_covenant_id(self):
        # The SDK works out `Transaction.id` when the object is made and does not refresh it when
        # the covenant id is filled in afterwards: on testnet a node named a genesis by another id
        # than the one the program had printed. So the builder works the id out again, and doing it
        # once more, or signing, changes nothing.
        tx, _ = self.build_genesis()
        printed = tx.id
        self.assertEqual(str(tx.finalize()), printed)
        self.assertEqual(str(sign_transaction(tx, [self.key], True).finalize()), printed)

    def test_genesis_is_signed_by_the_funding_key(self):
        tx, _ = self.build_genesis()
        signed = sign_transaction(tx, [self.key], True)
        self.assertTrue(signed.inputs[0].signature_script_as_hex)
        # A key that does not own the funding coin cannot sign it: the signer is the funding key, not any key.
        # On a fresh transaction: `sign_transaction` signs in place and leaves alone an input that none of
        # the given keys fits, so on the transaction signed above a stranger's key would raise nothing.
        fresh, _ = self.build_genesis()
        with self.assertRaises(Exception) as raised:
            sign_transaction(fresh, [PrivateKey(Keypair.random().private_key)], True)
        self.assertIn("Signature is empty for input: 0", str(raised.exception))

    def test_funding_too_small_is_reported(self):
        small = entry("aa" * 32, 0, live.COVENANT_VALUE, self.change_script)
        with self.assertRaises(ValueError) as raised:
            live.build(live.genesis_plan(small, self.change_script), FEERATE)
        self.assertIn("cannot cover", str(raised.exception))  # not the other ValueError, "did not settle"

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

    def test_the_fee_of_a_move_depends_on_the_size_of_the_funding_coin(self):
        # The fee follows the mass the SDK measures, and for a move that mass depends on the funding
        # coin. With 2 TKAS in the covenant it is lowest for a coin of about 1 TKAS, higher for a coin
        # of 2 TKAS and more, and higher than at 1 TKAS for a coin of half a TKAS or less as well. The
        # program pays with the largest coin of the acting key: among coins of 2 TKAS and more that
        # is the dear choice, below that it need not be.
        def fee(sompi):
            funding = entry("aa" * 32, 0, sompi, self.change_script)
            plan = live.transition_plan(self.covenant, funding, self.change_script, 10, "add", 5)
            return live.build(plan, FEERATE)[1]

        kas = live.SOMPI_PER_KAS
        self.assertLess(fee(kas), fee(2 * kas))
        self.assertLess(fee(2 * kas), fee(20 * kas))
        self.assertLess(fee(20 * kas), fee(1000 * kas))
        self.assertGreater(fee(1000 * kas), 4 * fee(kas))
        self.assertGreater(fee(4 * kas // 10), 2 * fee(kas))  # here the smaller coin is the dearer one

    def test_a_fee_that_does_not_settle_is_reported(self):
        plan = live.transition_plan(self.covenant, self.funding, self.change_script, 10, "add", 5)
        masses = [2000, 3000] * 4  # the mass keeps jumping, so the fee never repeats
        with mock.patch.object(live, "calculate_transaction_mass", side_effect=masses):
            with self.assertRaises(ValueError) as raised:
                live.build(plan, FEERATE)
        self.assertIn("did not settle", str(raised.exception))

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
        assert_same_weight(self, signed, draft, self.key)

    def test_only_the_owner_s_signature_passes_the_local_engine(self):
        # Two signatures never look alike, even of one key, so comparing the calls proves nothing:
        # the built transactions are put before the local engine instead.
        stranger = PrivateKey(Keypair.random().private_key)
        args = self.owned_args()
        mine, _ = live.signed_transition(self.key, FEERATE, **args)
        theirs, _ = live.signed_transition(stranger, FEERATE, **args)

        def verdict(signed):
            return local_verdict(signed, args["covenant"], self.funding, OWNED, self.owner, 10, "add", [5])

        self.assertTrue(verdict(mine).success)
        self.assertFalse(verdict(theirs).success)
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


def local_verdict(signed, covenant: dict, funding: dict, contract, owner: str, count: int, function: str, arguments: list):
    """Ask the local engine about a transaction built for the network.

    The scenario repeats what a signature covers: the same two coins on the
    inputs, and every output with the value, the script and the covenant
    binding the builder gave it. The signature is the one the builder put into
    the covenant call. A signature commits to neither the compute budget nor
    the mass, so the engine's synthetic transaction and the built one are the
    same thing to it.

    What this does not check: the engine runs the `arguments` it is handed,
    not the bytes of the call in the transaction. That the builder encodes a
    call the way a node reads it is shown only by a node accepting it.
    """
    with_placeholder = live.covenant_call(count, function, arguments, contract, owner)
    call = bytes.fromhex(signed.inputs[0].signature_script_as_hex)
    at = with_placeholder.index(live.SIGNATURE_PLACEHOLDER)
    signature = call[at : at + len(live.SIGNATURE_PLACEHOLDER)]
    # Outside the signature the call is exactly the one the arguments describe.
    assert call[:at] + live.SIGNATURE_PLACEHOLDER + call[at + len(signature) :] == with_placeholder

    outputs = []
    for output in signed.outputs:
        described = output.to_dict()
        item = {"value": described["value"], "script": bytes.fromhex(described["scriptPublicKey"]["script"])}
        if described.get("covenant"):
            item["covenant_id"] = described["covenant"]["covenantId"]
            item["authorizing_input"] = described["covenant"]["authorizingInput"]
        outputs.append(item)
    scenario = {
        "inputs": [
            {
                "utxo_value": covenant["utxoEntry"]["amount"],
                "covenant_id": covenant["utxoEntry"]["covenantId"],
                "state": {"count": count},
                "prev_txid": covenant["outpoint"]["transactionId"],
                "prev_index": covenant["outpoint"]["index"],
            },
            {
                "utxo_value": funding["utxoEntry"]["amount"],
                "prev_txid": funding["outpoint"]["transactionId"],
                "prev_index": funding["outpoint"]["index"],
                "utxo_script": bytes.fromhex(funding["utxoEntry"]["scriptPublicKey"]["script"]),
            },
        ],
        "outputs": outputs,
    }
    return silverscript.debug_call(
        contract.source,
        function,
        args=[*arguments, signature],
        constructor_args=[0, bytes.fromhex(owner)],
        tx=scenario,
    )


class Closable(BuilderBase):
    """ClosableCounter: the counting moves and the closing transaction, as built for the network.

    Each transaction is built and signed exactly as the commands do it, and
    then the local engine is asked about it: the engine's answer is about the
    coins, the outputs and the signature of the very transaction that would be
    submitted, with the arguments the call was built from. Whether a node
    reads the call bytes the same way, and whether the compute budget is
    enough, only a node can say.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.stranger = PrivateKey(Keypair.random().private_key)
        # Where a payout goes in these checks: not the change address, so the two cannot be swapped unnoticed.
        cls.payout_script = pay_to_address_script(Keypair.random().to_address(live.NETWORK_TYPE))
        cls.closable = entry(
            "dd" * 32,
            0,
            live.COVENANT_VALUE,
            live.lock_script(10, CLOSABLE, cls.owner),
            covenant_id=COVENANT_ID,
        )

    def common(self) -> dict:
        return dict(
            covenant=self.closable,
            funding=self.funding,
            change_script=self.change_script,
            count=10,
            contract=CLOSABLE,
            owner=self.owner,
        )

    def moved(self, key=None, **overrides):
        signed, fee = live.signed_transition(
            key or self.key, FEERATE, function="add", amount=5, **self.common(), **overrides
        )
        return signed, fee

    def closed(self, key=None, sighash=None, **overrides):
        signed, fee = live.signed_transition(
            key or self.key,
            FEERATE,
            planner=live.closing_plan,
            sighash=sighash,
            payout_script=self.payout_script,
            **self.common(),
            **overrides,
        )
        return signed, fee

    def budgets(self, tx) -> list:
        return [tx_input.compute_budget for tx_input in tx.inputs]

    def verdict(self, signed, function: str, arguments: list):
        return local_verdict(signed, self.closable, self.funding, CLOSABLE, self.owner, 10, function, arguments)

    def assert_refused_at(self, result, rule: str):
        """Refused, and at the line of this rule. Used for the signature and type rules only:
        no failure of the compiler-generated checks was seen at their lines. Which rule refuses
        a leak or a continuing output is shown by comparison in `test_counter_closable.py`."""
        self.assertFalse(result.success)
        lines = [number for number, line in enumerate(CLOSABLE.source.splitlines(), start=1) if rule in line]
        if len(lines) != 1:
            # Not a failed assertion: a rule line that is missing says nothing about behaviour.
            raise LookupError(f"{rule!r} is expected once in the contract source, found {len(lines)} times")
        self.assertEqual(result.failure.frames[0].line, lines[0])

    # --- counting moves

    def test_a_counting_move_built_for_the_network_passes_the_local_engine(self):
        signed, _ = self.moved()
        self.assertTrue(self.verdict(signed, "add", [5]).success)
        self.assertEqual(script_hex(signed), live.lock_script(15, CLOSABLE, self.owner).script)

    def test_a_leak_built_for_the_network_is_refused_by_the_local_engine(self):
        signed, _ = self.moved(covenant_value_out=live.COVENANT_VALUE - live.LEAK)
        self.assertEqual(values(signed)[0], live.COVENANT_VALUE - live.LEAK)
        self.assertFalse(self.verdict(signed, "add", [5]).success)

    def test_a_counting_move_signed_by_another_key_is_refused_by_the_local_engine(self):
        signed, _ = self.moved(key=self.stranger)
        self.assert_refused_at(self.verdict(signed, "add", [5]), SIGNATURE_RULE)

    # --- the closing transaction

    def test_closing_pays_the_covenant_coins_out_and_the_funding_input_pays_the_fee(self):
        signed, fee = self.closed()
        payout, change = values(signed)
        self.assertEqual(payout, live.COVENANT_VALUE)
        self.assertGreater(fee, 0)
        self.assertEqual(payout + change + fee, live.COVENANT_VALUE + FUNDING_AMOUNT)
        self.assertEqual(script_hex(signed, 0), self.payout_script.script)
        self.assertEqual(script_hex(signed, 1), self.change_script.script)
        self.assertNotEqual(self.payout_script.script, self.change_script.script)

    def test_closing_leaves_no_covenant_output(self):
        signed, _ = self.closed()
        self.assertEqual(len(signed.outputs), 2)
        for output in signed.outputs:
            self.assertIsNone(output.to_dict().get("covenant"))

    def test_the_closing_transaction_built_for_the_network_passes_the_local_engine(self):
        signed, _ = self.closed()
        self.assertTrue(self.verdict(signed, "close", [[]]).success)
        # Both inputs carry something: the covenant call, and the funding key's signature.
        self.assertTrue(signed.inputs[1].signature_script_as_hex)

    def test_closing_signed_by_another_key_is_refused_by_the_local_engine(self):
        signed, _ = self.closed(key=self.stranger)
        self.assert_refused_at(self.verdict(signed, "close", [[]]), SIGNATURE_RULE)

    def test_closing_with_a_signature_of_type_none_is_refused_by_the_local_engine(self):
        signed, _ = self.closed(sighash="None")
        # The signature itself is genuine: the refusal comes one line later, at the type rule.
        self.assert_refused_at(self.verdict(signed, "close", [[]]), TYPE_RULE)

    def test_closing_that_keeps_the_covenant_is_refused_by_the_local_engine(self):
        signed, _ = self.closed(continue_at=16)
        kept = signed.outputs[0].to_dict()
        self.assertEqual(kept["covenant"]["covenantId"], COVENANT_ID)
        self.assertEqual(kept["value"], live.COVENANT_VALUE)
        self.assertEqual(script_hex(signed), live.lock_script(16, CLOSABLE, self.owner).script)
        self.assertFalse(self.verdict(signed, "close", [[{"count": 16}]]).success)

    def test_placeholder_and_real_signature_give_the_same_amounts_when_closing(self):
        signed, fee = self.closed()
        plan = live.closing_plan(payout_script=self.payout_script, **self.common())
        draft, draft_fee, _, _ = live.build(plan, FEERATE)
        self.assertEqual(values(signed), values(draft))
        self.assertEqual(fee, draft_fee)
        self.assertEqual(signed.mass, draft.mass)
        assert_same_weight(self, signed, draft, self.key)

    def test_a_contract_without_a_closing_move_cannot_be_closed(self):
        owned = entry(
            "cc" * 32, 0, live.COVENANT_VALUE, live.lock_script(10, OWNED, self.owner), covenant_id=COVENANT_ID
        )
        with self.assertRaises(ValueError):
            live.closing_plan(
                owned, self.funding, self.change_script, 10, self.change_script, contract=OWNED, owner=self.owner
            )

    # --- the compute budget of the covenant input

    def test_a_raised_budget_lands_on_the_covenant_input_and_adds_one_hundred_grams_per_unit(self):
        raised_budget = live.COMPUTE_BUDGET + 7
        for name, made in (("move", self.moved), ("close", self.closed)):
            with self.subTest(transaction=name):
                usual, usual_fee = made()
                raised, raised_fee = made(covenant_budget=raised_budget)
                self.assertEqual(self.budgets(usual), [live.COMPUTE_BUDGET, live.COMPUTE_BUDGET])
                self.assertEqual(self.budgets(raised), [raised_budget, live.COMPUTE_BUDGET])
                self.assertEqual(raised.mass, usual.mass)
                self.assertEqual(raised_fee - usual_fee, 7 * live.GRAMS_PER_COMPUTE_BUDGET_UNIT * FEERATE)
        raised, _ = self.moved(covenant_budget=raised_budget)
        self.assertTrue(self.verdict(raised, "add", [5]).success)

    def test_the_fee_counts_the_budget_that_is_on_the_inputs(self):
        plan = live.closing_plan(payout_script=self.payout_script, covenant_budget=25, **self.common())
        tx, fee, _, _ = live.build(plan, FEERATE)
        grams = live.GRAMS_PER_COMPUTE_BUDGET_UNIT * sum(self.budgets(tx))
        self.assertEqual(sum(self.budgets(tx)), 25 + live.COMPUTE_BUDGET)
        self.assertEqual(fee, (tx.mass + grams + live.FEE_MASS_SLACK) * FEERATE)


class StateFiles(unittest.TestCase):
    """The record of a covenant on disk; the folder is a temporary one outside the repository."""

    def test_a_closed_covenant_is_set_aside_whole_and_a_new_one_can_start(self):
        record = live.Live("ab" * 32, "cd" * 32, 5, live.COVENANT_VALUE, "ef" * 32)
        closing_txid = "12" * 32
        with tempfile.TemporaryDirectory() as folder, mock.patch.dict(os.environ, {"KASPA_LAB_STATE_DIR": folder}):
            live.save_state(CLOSABLE, record)
            self.assertEqual(live.load_state(CLOSABLE), record)
            closed = live.set_aside_state(CLOSABLE, record, closing_txid)
            self.assertIsNone(live.load_state(CLOSABLE))
            kept = json.loads(closed.read_text(encoding="utf-8"))
            self.assertEqual(
                kept,
                {
                    "covenant_id": record.covenant_id,
                    "txid": record.txid,
                    "count": 5,
                    "value": live.COVENANT_VALUE,
                    "owner": record.owner,
                    "closed_by": closing_txid,
                },
            )

    def test_two_closed_covenants_keep_two_records(self):
        first = live.Live("ab" * 32, "cd" * 32, 5, live.COVENANT_VALUE, "ef" * 32)
        second = live.Live("ba" * 32, "dc" * 32, 0, live.COVENANT_VALUE, "ef" * 32)
        with tempfile.TemporaryDirectory() as folder, mock.patch.dict(os.environ, {"KASPA_LAB_STATE_DIR": folder}):
            live.save_state(CLOSABLE, first)
            one = live.set_aside_state(CLOSABLE, first, "12" * 32)
            live.save_state(CLOSABLE, second)
            two = live.set_aside_state(CLOSABLE, second, "34" * 32)
            self.assertNotEqual(one, two)
            self.assertTrue(one.exists() and two.exists())
            self.assertEqual(json.loads(one.read_text(encoding="utf-8"))["covenant_id"], first.covenant_id)

    def test_a_record_copied_back_by_hand_loads_again(self):
        record = live.Live("ab" * 32, "cd" * 32, 5, live.COVENANT_VALUE, "ef" * 32)
        with tempfile.TemporaryDirectory() as folder, mock.patch.dict(os.environ, {"KASPA_LAB_STATE_DIR": folder}):
            live.save_state(CLOSABLE, record)
            closed = live.set_aside_state(CLOSABLE, record, "12" * 32)
            live.state_path(CLOSABLE).write_text(closed.read_text(encoding="utf-8"), encoding="utf-8")
            self.assertEqual(live.load_state(CLOSABLE), record)


class FakeNode:
    """Stands in for the RPC client in the checks of `submit`: answers as told.

    `seen` is what it reports afterwards at the address `at`, and at no
    other: the ids of the transactions whose outputs are "on chain".
    `network` and `synced` are what it says about itself; `silent` makes that
    question fail. `calls` are the question, the submissions and the
    disconnects, in order; reads are not listed.
    """

    def __init__(self, answer, seen=(), at=None, network=live.NETWORK_ID, synced=True, silent=False):
        self.answer = answer
        self.seen = seen
        self.at = at
        self.network, self.synced, self.silent = network, synced, silent
        self.calls = []

    async def get_server_info(self):
        self.calls.append("what are you")
        if self.silent:
            raise TimeoutError()
        return {"networkId": self.network, "isSynced": self.synced, "serverVersion": "0.0.0"}

    async def disconnect(self):
        self.calls.append("disconnect")

    async def submit_transaction(self, request):
        self.calls.append("submit")
        if isinstance(self.answer, Exception):
            raise self.answer
        return {"transactionId": self.answer}

    async def get_utxos_by_addresses(self, request):
        if request["addresses"] != [self.at]:
            return {"entries": []}
        return {"entries": [{"outpoint": {"transactionId": txid, "index": 1}} for txid in self.seen]}


async def no_sleep(seconds):
    return None


class Submission(BuilderBase):
    """What counts as a node's refusal, and what is only a lost answer."""

    def submitted(self, answer):
        tx, _ = self.transition()
        return tx, asyncio.run(live.submit(FakeNode(answer), tx))

    def test_an_accepted_transaction_returns_its_id(self):
        _, outcome = self.submitted("ab" * 32)
        self.assertEqual(outcome, (True, "ab" * 32))

    def test_the_node_s_own_answer_is_a_refusal(self):
        text = "Rejected transaction 12ab: failed to verify the signature script: script ran, but verification failed"
        _, outcome = self.submitted(RuntimeError(text))
        self.assertEqual(outcome, (False, text))

    def test_a_dropped_connection_is_not_a_refusal(self):
        tx, _ = self.transition()
        with self.assertRaises(live.NoAnswer) as raised:
            asyncio.run(live.submit(FakeNode(ConnectionError("WebSocket is not connected")), tx))
        self.assertEqual(raised.exception.txid, tx.id)
        self.assertEqual(len(raised.exception.txid), 64)

    def test_a_lost_answer_counts_as_accepted_once_the_change_output_is_seen(self):
        tx, _ = self.transition()
        node = FakeNode(ConnectionError("WebSocket is not connected"), seen=[tx.id], at=live.address_of(self.key))
        with mock.patch.object(live.asyncio, "sleep", no_sleep), mock.patch("builtins.print"):
            outcome = asyncio.run(live.submit_checked(node, tx, self.key))
        self.assertEqual(outcome, (True, tx.id))

    def test_a_lost_answer_stays_unknown_when_nothing_shows_up(self):
        tx, _ = self.transition()
        node = FakeNode(ConnectionError("WebSocket is not connected"), seen=["ee" * 32], at=live.address_of(self.key))
        with mock.patch.object(live.asyncio, "sleep", no_sleep), mock.patch("builtins.print"):
            with self.assertRaises(live.NoAnswer):
                asyncio.run(live.submit_checked(node, tx, self.key))

    def test_a_refusal_is_not_looked_up(self):
        tx, _ = self.transition()
        text = "Rejected transaction 12ab: failed to verify the signature script"
        node = FakeNode(RuntimeError(text), seen=[tx.id], at=live.address_of(self.key))
        self.assertEqual(asyncio.run(live.submit_checked(node, tx, self.key)), (False, text))

    def test_a_lost_answer_stays_a_lost_answer_when_the_look_up_cannot_be_made(self):
        # The look-up may end by itself: no connection at all, or a wrong node after a reconnect.
        # Nothing is known about the transaction then, and the user is still told so.
        tx, _ = self.transition()
        stops = (ConnectionError("WebSocket is not connected"), TimeoutError(), SystemExit("Refusing: a wrong node."))
        for stop in stops:
            with self.subTest(stop=type(stop).__name__):
                node = FakeNode(ConnectionError("WebSocket is not connected"))
                look_up = mock.patch.object(live, "seen_at", mock.AsyncMock(side_effect=stop))
                with look_up, mock.patch("builtins.print"), self.assertRaises(live.NoAnswer) as raised:
                    asyncio.run(live.submit_checked(node, tx, self.key))
                self.assertEqual(raised.exception.txid, tx.id)
                self.assertIn("the look-up then stopped", str(raised.exception))

    def test_the_node_is_asked_what_it_is_right_before_the_transaction_is_handed_over(self):
        tx, _ = self.transition()
        node = FakeNode("ab" * 32)
        asyncio.run(live.submit(node, tx))
        self.assertEqual(node.calls, ["what are you", "submit"])
        # A wrong or unsynced node, and a node that does not answer the question, are handed nothing.
        cases = ((dict(network="mainnet"), "Refusing"), (dict(synced=False), "Refusing"), (dict(silent=True), "Not sent"))
        for wrong, text in cases:
            with self.subTest(**wrong):
                node = FakeNode("ab" * 32, **wrong)
                with self.assertRaises(SystemExit) as stopped:
                    asyncio.run(live.submit(node, tx))
                self.assertIn(text, str(stopped.exception))
                self.assertNotIn("submit", node.calls)


class LostGenesis(BuilderBase):
    """`genesis` through the whole program, with a node that takes the transaction and never says so."""

    def run_genesis(self, funding, printed):
        class Node(FakeNode):
            async def get_utxos_by_addresses(node, request):
                # The acting key's coin before the submission; afterwards nothing shows up anywhere.
                return {"entries": [] if "submit" in node.calls else [funding]}

            async def get_fee_estimate(node):
                return {"estimate": {"priorityBucket": {"feerate": FEERATE}}}

            async def submit_transaction(node, request):
                node.printed_before = [call.args[0] for call in printed.call_args_list]
                node.handed = request["transaction"]
                return await super().submit_transaction(request)

        node = Node(ConnectionError("WebSocket is not connected"))
        connected = mock.patch.object(live, "connect", mock.AsyncMock(return_value=node))
        with connected, mock.patch.object(live, "load_key", return_value=self.key):
            with mock.patch.object(live.asyncio, "sleep", no_sleep), self.assertRaises(SystemExit) as stopped:
                asyncio.run(live.run(["--contract", "closable", "genesis"]))
        return node, str(stopped.exception)

    def test_the_record_is_printed_before_the_transaction_is_handed_over(self):
        with tempfile.TemporaryDirectory() as folder, mock.patch.dict(os.environ, {"KASPA_LAB_STATE_DIR": folder}):
            with mock.patch("builtins.print") as printed:
                node, last_words = self.run_genesis(self.funding, printed)
            self.assertIsNone(live.load_state(CLOSABLE))  # nothing was recorded
            self.assertIn("NO ANSWER", last_words)
            self.assertIn(live.state_path(CLOSABLE).name, last_words)
            self.assertNotIn("`resync N` repairs", last_words)  # there is no record for `resync` to repair
            lines = [line for line in node.printed_before if line.startswith("record  : ")]
            self.assertEqual(len(lines), 1)
            # Saved by hand, as the last words say, the printed text is a record the program loads.
            # A text editor may put a byte order mark in front of it.
            live.state_path(CLOSABLE).write_text(lines[0].removeprefix("record  : "), encoding="utf-8-sig")
            record = live.load_state(CLOSABLE)
            self.assertIn(record.txid, last_words)
            self.assertEqual((record.count, record.value, record.owner), (0, live.COVENANT_VALUE, self.owner))
            self.assertEqual(len(record.covenant_id), 64)
            # It is the record of the transaction that was handed over, by the id worked out from
            # what that transaction holds, not by the one stored in the object.
            self.assertEqual(record.txid, str(node.handed.finalize()))
            self.assertEqual(record.covenant_id, node.handed.outputs[0].to_dict()["covenant"]["covenantId"])

    def test_a_coin_too_small_for_the_fee_ends_in_one_line_and_nothing_is_sent(self):
        small = entry("aa" * 32, 0, live.COVENANT_VALUE, self.change_script)
        with tempfile.TemporaryDirectory() as folder, mock.patch.dict(os.environ, {"KASPA_LAB_STATE_DIR": folder}):
            with mock.patch("builtins.print") as printed:
                node, last_words = self.run_genesis(small, printed)
        self.assertEqual(last_words, "Nothing was sent: the funding input cannot cover the outputs and the fee.")
        self.assertNotIn("submit", node.calls)


class Ledger:
    """A node that holds coins by address and takes what it is handed: enough to run whole commands.

    `answers` are its answers to the submissions, in order: None takes the transaction, an
    exception is raised. `unanswered` are the questions about itself, counted from 0, that it
    leaves without an answer. `sent` are the transactions handed over; `calls` are the questions
    about itself and the submissions, in order (reads, the fee estimate and the disconnect are not
    listed).
    """

    def __init__(self, coins, answers=(), unanswered=()):
        self.coins, self.answers, self.unanswered = coins, list(answers), set(unanswered)
        self.sent, self.calls, self.questions = [], [], 0
        self.url = "wss://ledger.example"

    async def get_server_info(self):
        self.calls.append("what are you")
        self.questions += 1
        if self.questions - 1 in self.unanswered:
            raise TimeoutError("no answer")
        return {"networkId": live.NETWORK_ID, "isSynced": True, "serverVersion": "0.0.0"}

    async def disconnect(self):
        return None

    async def get_fee_estimate(self):
        return {"estimate": {"priorityBucket": {"feerate": FEERATE}}}

    async def get_utxos_by_addresses(self, request):
        return {"entries": list(self.coins.get(request["addresses"][0], []))}

    async def submit_transaction(self, request):
        self.calls.append("submit")
        self.sent.append(request["transaction"])
        answer = self.answers.pop(0) if self.answers else None
        if isinstance(answer, Exception):
            raise answer
        return {"transactionId": request["transaction"].id}


class WholeCommands(BuilderBase):
    """Commands through the whole program against a node that holds coins: what is built from, and what is recorded."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        other = Keypair.random()
        cls.second = PrivateKey(other.private_key)
        cls.second_funding = entry("cc" * 32, 0, FUNDING_AMOUNT, pay_to_address_script(other.to_address(live.NETWORK_TYPE)))

    def world(self, held=live.COVENANT_VALUE, contract=live.KEEPING, **node_args):
        """A record that says COVENANT_VALUE, and a node where the covenant's coin holds `held`."""
        owner = self.owner if contract.owned else None
        record = live.Live(COVENANT_ID, "bb" * 32, 10, live.COVENANT_VALUE, owner)
        coin = entry("bb" * 32, 0, held, live.lock_script(10, contract, owner), covenant_id=COVENANT_ID)
        coins = {
            live.covenant_address(10, contract, owner): [coin],
            live.address_of(self.key): [self.funding],
            live.address_of(self.second): [self.second_funding],
        }
        return record, Ledger(coins, **node_args)

    def run_command(self, argv, record, node, contract=live.KEEPING, wait=None):
        """Returns (the record afterwards, the lines printed, the last words or None).

        `wait` stands in for the wait for the transaction's output; left out, the output is there at once.
        """
        keys = {"KASPA_LAB_KEY_FILE": self.key, "KASPA_LAB_SECOND_KEY_FILE": self.second}
        printed = []
        with tempfile.TemporaryDirectory() as folder, contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.dict(os.environ, {"KASPA_LAB_STATE_DIR": folder}))
            live.save_state(contract, record)
            stack.enter_context(mock.patch.object(live, "connect", mock.AsyncMock(return_value=node)))
            stack.enter_context(mock.patch.object(live, "load_key", lambda name="KASPA_LAB_KEY_FILE": keys[name]))
            stack.enter_context(mock.patch.object(live, "wait_for", wait or mock.AsyncMock()))
            stack.enter_context(mock.patch.object(live.asyncio, "sleep", no_sleep))
            stack.enter_context(mock.patch("builtins.print", lambda *args, **kwargs: printed.append(str(args[0]))))
            last_words = None
            try:
                asyncio.run(live.run(argv))
            except SystemExit as stop:
                last_words = str(stop)
            except ConnectionError as dropped:
                last_words = repr(dropped)
            return live.load_state(contract), printed, last_words

    def test_a_move_records_what_the_coin_holds_not_what_the_file_said(self):
        record, node = self.world(held=150_000_000)
        after, _, _ = self.run_command(["move", "add", "5"], record, node)
        self.assertEqual(values(node.sent[0])[0], 150_000_000)
        self.assertEqual((after.count, after.value, after.txid), (15, 150_000_000, node.sent[0].id))

    def test_a_rule_breaking_attempt_starts_from_what_the_coin_holds(self):
        record, node = self.world(held=150_000_000)
        after, _, _ = self.run_command(["try", "leak"], record, node)
        self.assertEqual(values(node.sent[0])[0], 150_000_000 - live.LEAK)
        self.assertEqual((after.count, after.value), (15, 150_000_000 - live.LEAK))
        record, node = self.world(held=150_000_000)
        after, _, _ = self.run_command(["try", "wrong-state"], record, node)
        self.assertEqual((values(node.sent[0])[0], after.count, after.value), (150_000_000, 16, 150_000_000))

    def test_try_leak_needs_a_coin_that_holds_more_than_it_takes(self):
        for held in (live.LEAK, live.LEAK - 1):
            with self.subTest(held=held):
                record, node = self.world(held=held)
                after, _, last_words = self.run_command(["try", "leak"], record, node)
                self.assertIn("try leak takes", last_words)
                self.assertEqual((node.sent, after), ([], record))

    def test_the_amount_of_a_send_is_the_one_read_from_the_command_line(self):
        record, node = self.world()
        to = Keypair.random().to_address(live.NETWORK_TYPE).to_string()
        self.run_command(["send", to, "0.5"], record, node)
        self.assertEqual(values(node.sent[0])[0], 50_000_000)

    def test_race_asks_the_node_before_each_submission_and_records_what_the_coin_holds(self):
        refused = RuntimeError("Rejected transaction ab: transaction is already spent")
        record, node = self.world(held=150_000_000, answers=[None, refused])
        after, _, _ = self.run_command(["race"], record, node)
        self.assertEqual(node.calls, ["what are you", "submit", "what are you", "submit"])
        self.assertEqual((after.count, after.value, after.txid), (11, 150_000_000, node.sent[0].id))

    def test_closing_through_close_continue_records_what_the_coin_holds(self):
        record, node = self.world(held=150_000_000, contract=CLOSABLE)
        after, _, _ = self.run_command(["--contract", "closable", "try", "close-continue"], record, node, CLOSABLE)
        self.assertEqual((after.count, after.value), (10 + live.JUMP, 150_000_000))

    def test_the_new_state_is_on_disk_before_the_wait_for_the_output(self):
        # The connection may drop while the program waits: the record must not wait for that.
        dropped = mock.AsyncMock(side_effect=ConnectionError("WebSocket is not connected"))
        record, node = self.world()
        after, _, last_words = self.run_command(["move", "add", "5"], record, node, wait=dropped)
        self.assertIn("ConnectionError", last_words)
        self.assertEqual((after.count, after.txid), (15, node.sent[0].id))
        record, node = self.world(contract=CLOSABLE)
        after, _, last_words = self.run_command(["--contract", "closable", "close"], record, node, CLOSABLE, wait=dropped)
        self.assertIn("ConnectionError", last_words)
        self.assertIsNone(after)  # set aside: no live record of a covenant that is gone

    def test_the_advice_after_a_lost_answer_fits_the_command(self):
        lost = ConnectionError("WebSocket is not connected")
        record, node = self.world(answers=[lost])
        after, _, last_words = self.run_command(["move", "add", "5"], record, node)
        self.assertEqual(after, record)  # nothing was recorded
        self.assertIn(node.sent[0].id, last_words)
        self.assertIn("`resync N` repairs", last_words)
        record, node = self.world(contract=CLOSABLE, answers=[lost])
        after, _, last_words = self.run_command(["--contract", "closable", "close"], record, node, CLOSABLE)
        self.assertEqual(after, record)
        self.assertIn("move tn10-closable.json out of KASPA_LAB_STATE_DIR", last_words)
        self.assertNotIn("`resync N` repairs", last_words)  # `resync` cannot find a covenant that is gone

    def test_race_keeps_the_answer_to_the_first_move_when_the_second_is_not_sent(self):
        # The node takes the first move and then does not say what it is: the second is not handed over.
        record, node = self.world(unanswered={1})
        after, printed, last_words = self.run_command(["race"], record, node)
        self.assertEqual(node.calls, ["what are you", "submit", "what are you"])
        self.assertTrue(any("ACCEPTED " + node.sent[0].id in line for line in printed))
        self.assertTrue(any("NOT SENT" in line for line in printed))
        self.assertEqual((after.count, after.txid), (11, node.sent[0].id))
        self.assertNotIn("Nothing was sent", last_words)

    def test_a_send_says_that_the_node_took_it_before_it_waits(self):
        record, node = self.world()
        to = Keypair.random().to_address(live.NETWORK_TYPE).to_string()
        waited = mock.AsyncMock(side_effect=ConnectionError("WebSocket is not connected"))
        printed = []
        with mock.patch.object(live, "wait_for", waited), mock.patch("builtins.print", lambda *a, **k: printed.append(str(a[0]))):
            with self.assertRaises(ConnectionError):
                asyncio.run(live.cmd_send(node, self.key, to, 50_000_000))
        self.assertTrue(any("ACCEPTED " + node.sent[0].id in line for line in printed))

    def test_the_advice_names_the_contract_it_is_about(self):
        lost = ConnectionError("WebSocket is not connected")
        record, node = self.world(contract=CLOSABLE, answers=[lost])
        _, _, last_words = self.run_command(["--contract", "closable", "close"], record, node, CLOSABLE)
        self.assertIn("`--contract closable status`", last_words)
        record, node = self.world(contract=CLOSABLE, answers=[lost])
        _, _, last_words = self.run_command(["--contract", "closable", "try", "close-none"], record, node, CLOSABLE)
        self.assertIn("move tn10-closable.json out of KASPA_LAB_STATE_DIR", last_words)
        record, node = self.world(answers=[lost])
        _, _, last_words = self.run_command(["move", "add", "5"], record, node)
        self.assertIn("Run `status` before anything else", last_words)

    def test_a_record_saved_from_windows_powershell_loads(self):
        record = live.Live(COVENANT_ID, "bb" * 32, 10, live.COVENANT_VALUE)
        with tempfile.TemporaryDirectory() as folder, mock.patch.dict(os.environ, {"KASPA_LAB_STATE_DIR": folder}):
            path = live.state_path(live.KEEPING)
            path.write_text(json.dumps(live.asdict(record)), encoding="utf-16")  # what `>` writes there
            self.assertEqual(live.load_state(live.KEEPING), record)
            path.write_text("record  : " + json.dumps(live.asdict(record)), encoding="utf-8")
            with self.assertRaises(SystemExit) as stopped:
                live.load_state(live.KEEPING)
            self.assertIn(path.name, str(stopped.exception))


class ScriptedNode:
    """Stands in for the RPC client where a command reads the network.

    `replies` are the answers to `get_utxos_by_addresses`, in order; an
    exception among them is raised. `network` and `synced` are what the node
    says about itself after a reconnect; `silent` is how many times it first
    fails to say anything. `asked` are the addresses read, in order, and
    `unchecked` counts the reads made after a reconnect before the node had
    said what it is.
    """

    def __init__(self, replies, network=live.NETWORK_ID, synced=True, rate=100, silent=0):
        self.replies = list(replies)
        self.network, self.synced, self.rate, self.silent = network, synced, rate, silent
        self.reconnects = 0
        self.url = "wss://scripted.example"
        self.checked, self.unchecked = True, 0
        self.asked = []

    async def get_utxos_by_addresses(self, request):
        self.asked += request["addresses"]
        self.unchecked += not self.checked
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return {"entries": reply}

    async def connect(self):
        self.reconnects += 1
        self.checked = False

    async def disconnect(self):
        return None

    async def get_server_info(self):
        if self.silent:
            self.silent -= 1
            raise TimeoutError("no answer")
        self.checked = True
        return {"networkId": self.network, "isSynced": self.synced, "serverVersion": "0.0.0"}

    async def get_fee_estimate(self):
        return {"estimate": {"priorityBucket": {"feerate": self.rate}}}


class ReadingTheNetwork(BuilderBase):
    """What the program trusts when it reads a node: which coin is the covenant's, and which node it talks to."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.record = live.Live(COVENANT_ID, "bb" * 32, 10, live.COVENANT_VALUE)
        cls.ours = entry("bb" * 32, 0, live.COVENANT_VALUE, live.lock_script(10), covenant_id=COVENANT_ID)

    def found(self, entries):
        node = ScriptedNode([entries])
        try:
            return asyncio.run(live.find_covenant(node, live.KEEPING, self.record))
        finally:
            # Whatever comes of it, the one address read is the covenant's at the recorded count.
            self.assertEqual(node.asked, [live.covenant_address(self.record.count)])

    def test_the_covenant_coin_is_found_by_its_transaction_and_covenant_id(self):
        self.assertEqual(self.found([self.ours]), self.ours)

    def test_a_coin_of_another_covenant_at_the_same_address_is_not_taken(self):
        look_alike = entry("bb" * 32, 0, live.COVENANT_VALUE, live.lock_script(10), covenant_id="e2" * 32)
        plain = entry("bb" * 32, 0, live.COVENANT_VALUE, live.lock_script(10))
        for other in (look_alike, plain):
            with self.subTest(covenant_id=other["utxoEntry"]["covenantId"]):
                with self.assertRaises(SystemExit):
                    self.found([other])
        self.assertEqual(self.found([look_alike, self.ours]), self.ours)

    def test_the_covenant_coin_need_not_be_the_first_output(self):
        second = entry("bb" * 32, 1, live.COVENANT_VALUE, live.lock_script(10), covenant_id=COVENANT_ID)
        self.assertEqual(self.found([second]), second)

    def test_a_coin_of_another_transaction_is_not_taken(self):
        stale = entry("cc" * 32, 0, live.COVENANT_VALUE, live.lock_script(10), covenant_id=COVENANT_ID)
        with self.assertRaises(SystemExit):
            self.found([stale])

    def test_after_a_dropped_connection_the_node_is_asked_again_what_it_is(self):
        dropped = ConnectionError("WebSocket is not connected")
        with mock.patch.object(live.asyncio, "sleep", no_sleep), mock.patch("builtins.print") as printed:
            same = ScriptedNode([dropped, [self.ours]])
            self.assertEqual(asyncio.run(live.utxos_at(same, "kaspatest:any")), [self.ours])
            self.assertEqual(same.reconnects, 1)
            self.assertIn("wss://scripted.example, node version 0.0.0", printed.call_args.args[0])
            self.assertEqual(same.asked, ["kaspatest:any", "kaspatest:any"])
            for wrong in (dict(network="mainnet"), dict(synced=False)):
                with self.subTest(**wrong):
                    with self.assertRaises(SystemExit):
                        asyncio.run(live.utxos_at(ScriptedNode([dropped, [self.ours]], **wrong), "kaspatest:any"))

    def test_a_node_that_does_not_say_what_it_is_is_not_read(self):
        dropped = ConnectionError("WebSocket is not connected")
        with mock.patch.object(live.asyncio, "sleep", no_sleep), mock.patch("builtins.print"):
            # One question without an answer: the program reconnects and asks again before it reads.
            once = ScriptedNode([dropped, [self.ours]], silent=1)
            self.assertEqual(asyncio.run(live.utxos_at(once, "kaspatest:any")), [self.ours])
            self.assertEqual((once.reconnects, once.unchecked), (2, 0))
            # The node behind the unanswered question may be a wrong one: it is refused all the same.
            for wrong in (dict(network="mainnet"), dict(synced=False)):
                with self.subTest(**wrong):
                    with self.assertRaises(SystemExit):
                        asyncio.run(
                            live.utxos_at(ScriptedNode([dropped, [self.ours]], silent=1, **wrong), "kaspatest:any")
                        )
            # A node that never answers is never read: the attempts run out.
            never = ScriptedNode([dropped, [self.ours]], silent=9)
            with self.assertRaises(TimeoutError):
                asyncio.run(live.utxos_at(never, "kaspatest:any"))
            self.assertEqual((never.replies, never.unchecked), ([[self.ours]], 0))

    def test_the_feerate_the_node_suggests_is_printed(self):
        with mock.patch("builtins.print") as printed:
            rate = asyncio.run(live.feerate(ScriptedNode([], rate=308)))
        self.assertEqual(rate, 308)
        self.assertIn("308", printed.call_args.args[0])

    def test_a_fractional_feerate_is_rounded_up(self):
        # A node's estimate is a fraction: rounded down, the program would pay less than it says.
        with mock.patch("builtins.print"):
            self.assertEqual(asyncio.run(live.feerate(ScriptedNode([], rate=308.2))), 309)
            self.assertEqual(asyncio.run(live.feerate(ScriptedNode([], rate=100.0))), 100)


class CommandLine(unittest.TestCase):
    """Reading the options: no key and no network are involved."""

    def test_options_are_read_in_either_order(self):
        for argv in (
            ["--contract", "closable", "--budget", "12", "move", "add", "5"],
            ["--budget", "12", "--contract", "closable", "move", "add", "5"],
        ):
            with self.subTest(argv=argv):
                self.assertEqual(live.options(argv), (CLOSABLE, 12, ["move", "add", "5"]))

    def test_defaults(self):
        self.assertEqual(live.options(["status"]), (live.KEEPING, live.COMPUTE_BUDGET, ["status"]))
        self.assertEqual(live.options(["--contract", "closable", "close"]), (CLOSABLE, live.COMPUTE_BUDGET, ["close"]))

    def test_bad_options_stop_before_anything_else(self):
        for argv in (
            ["--contract", "nope", "status"],
            ["--budget", "abc", "close"],
            ["--budget", "-1", "close"],
            ["--budget", "12.5", "close"],
            ["--budget", "70000", "close"],
            ["--budget", "12", "genesis"],
            ["--budget", "12", "status"],
            ["--budget", "12"],
            ["--contract", "closable"],
            [],
        ):
            with self.subTest(argv=argv):
                with self.assertRaises(SystemExit):
                    live.options(argv)

    def test_numbers_are_read_from_the_command_line(self):
        self.assertEqual((live.whole("5"), live.whole("-20")), (5, -20))
        self.assertEqual(live.sompi("20"), 20 * live.SOMPI_PER_KAS)
        self.assertEqual(live.sompi("0.00000001"), 1)
        for text in ("abc", "1.5", ""):
            with self.subTest(whole=text), self.assertRaises(SystemExit):
                live.whole(text)
        # An amount that comes to nothing would reach the SDK as an output of zero sompi.
        for text in ("0", "-1", "1e-9", "nan", "inf", "abc", "1,5"):
            with self.subTest(sompi=text), self.assertRaises(SystemExit):
                live.sompi(text)

    def test_a_bad_command_line_stops_before_the_key_is_read(self):
        # A bad number, an unknown command, a wrong count of arguments, an unknown move. Not covered:
        # what only a command itself can tell, such as `try nonsense` or a destination on another network.
        bad_numbers = (["move", "add", "abc"], ["resync", "x"], ["send", "kaspatest:any", "0"], ["move", "add", str(2**63)])
        bad_shapes = (["frobnicate"], ["move", "add", "5", "extra"], ["move", "mul", "5"], ["status", "x"], ["try"])
        with mock.patch.object(live, "load_key", side_effect=AssertionError("the key was read")):
            for argv in bad_numbers + bad_shapes:
                with self.subTest(argv=argv), self.assertRaises(SystemExit):
                    asyncio.run(live.run(argv))


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
