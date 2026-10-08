"""Offline checks for the transaction builder used by the testnet runs.

No network: the UTXOs are made up, and the keys are random and discarded. The
checks cover how the transactions are put together — who pays the fee, what
the covenant output carries, what gets signed — not whether a node accepts
them.

Run from the repository root:

    python -m unittest discover -s tests -v
"""

import asyncio
import json
import os
import tempfile
import unittest
from unittest import mock

import kaspa.experimental.silverscript as silverscript
from kaspa import Keypair, PrivateKey, pay_to_address_script, sign_transaction

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
        self.assertEqual(len(lines), 1, f"{rule!r} is expected once in the contract source")
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

    `seen` is what it reports at any address afterwards: the ids of the
    transactions whose outputs are "on chain".
    """

    def __init__(self, answer, seen=()):
        self.answer = answer
        self.seen = seen

    async def submit_transaction(self, request):
        if isinstance(self.answer, Exception):
            raise self.answer
        return {"transactionId": self.answer}

    async def get_utxos_by_addresses(self, request):
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
        node = FakeNode(ConnectionError("WebSocket is not connected"), seen=[tx.id])
        with mock.patch.object(live.asyncio, "sleep", no_sleep), mock.patch("builtins.print"):
            outcome = asyncio.run(live.submit_checked(node, tx, self.key))
        self.assertEqual(outcome, (True, tx.id))

    def test_a_lost_answer_stays_unknown_when_nothing_shows_up(self):
        tx, _ = self.transition()
        node = FakeNode(ConnectionError("WebSocket is not connected"), seen=["ee" * 32])
        with mock.patch.object(live.asyncio, "sleep", no_sleep), mock.patch("builtins.print"):
            with self.assertRaises(live.NoAnswer):
                asyncio.run(live.submit_checked(node, tx, self.key))

    def test_a_refusal_is_not_looked_up(self):
        tx, _ = self.transition()
        text = "Rejected transaction 12ab: failed to verify the signature script"
        node = FakeNode(RuntimeError(text), seen=[tx.id])
        self.assertEqual(asyncio.run(live.submit_checked(node, tx, self.key)), (False, text))


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
