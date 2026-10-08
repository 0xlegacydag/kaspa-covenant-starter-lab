"""Run the Counter contracts of this repository on Kaspa testnet-10.

TEST NETWORK ONLY. The script refuses to talk to any other network, and the
keys it uses must be throwaway keys made with `testnet/make_key.py`.

Contracts (`--contract`):

    counter   the guide's Counter            no amount rule, no value rule
    strict    StrictCounter                  amount > 0
    keeping   KeepingCounter (default)       amount > 0, covenant keeps its coins
    owned     OwnedCounter                   as keeping, plus the owner's signature
    closable  ClosableCounter                as owned, default signature type only,
                                             plus `close`: the owner ends the covenant

The covenant never pays a fee here. Every transition has two inputs — the
covenant UTXO and an ordinary funding UTXO that pays the fee — and two
outputs — the next covenant UTXO and the change. A closing transaction has
the same two inputs; its outputs are the payout of the covenant's coins to
the acting key and the change.

    set KASPA_LAB_KEY_FILE=C:\\path\\outside\\the\\repo\\tn10-test-key.txt
    set KASPA_LAB_STATE_DIR=C:\\path\\outside\\the\\repo\\state

    python -m testnet.counter_live status
    python -m testnet.counter_live genesis
    python -m testnet.counter_live move add 5
    python -m testnet.counter_live try leak
    python -m testnet.counter_live try wrong-state
    python -m testnet.counter_live try negative
    python -m testnet.counter_live --contract owned genesis
    python -m testnet.counter_live send kaspatest:... 20
    python -m testnet.counter_live race
    python -m testnet.counter_live --contract owned resync 5
    python -m testnet.counter_live --contract closable close
    python -m testnet.counter_live --contract closable try close-continue
    python -m testnet.counter_live --contract closable try close-none
    python -m testnet.counter_live --contract closable --budget 12 move add 5

`KASPA_LAB_KEY_FILE` is the key that acts: it pays the fees and, for `owned`
and `closable`, signs the covenant input. Point it at another key to act as
somebody else. `try ...` submits a transaction that breaks a rule and reports
what the node answers. `race` submits two different moves that spend the same
covenant UTXO; the second one is paid by `KASPA_LAB_SECOND_KEY_FILE`.
`resync N` repairs the state file after an interrupted run by finding this
covenant's UTXO on chain at count N. `close` ends a `closable` covenant: its
coins are paid out to the acting key, and the state file is set aside.
`--budget N` sets the compute budget of the covenant input for `move`, `try`
and `close`; the default is 10, and it is not remembered between commands.

Only a node's own answer ("Rejected transaction ...") is reported as a
refusal. When a submission ends without an answer, the program looks for the
transaction's change output; if it cannot find it, it stops with NO ANSWER
and records nothing. A record set aside by `close` does not come back by
itself: if the closing transaction did not stay on chain, copy the record
back to `tn10-closable.json` by hand.
"""

import asyncio
import json
import os
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

import kaspa.experimental.silverscript as silverscript
from kaspa import (
    CovenantBinding,
    GenesisCovenantGroup,
    Hash,
    PrivateKey,
    Resolver,
    RpcClient,
    ScriptBuilder,
    SighashType,
    Transaction,
    TransactionInput,
    TransactionOutpoint,
    TransactionOutput,
    UtxoEntryReference,
    address_from_script_public_key,
    calculate_transaction_mass,
    create_input_signature,
    pay_to_address_script,
    sign_transaction,
)

NETWORK_ID = "testnet-10"
NETWORK_TYPE = "testnet"
ADDRESS_PREFIX = "kaspatest:"

ROOT = Path(__file__).resolve().parent.parent

SUBNETWORK_ID = bytes(20)
TX_VERSION = 1
COMPUTE_BUDGET = 10
# The node prices each unit of an input's compute budget at 100 grams, which
# the wallet-side mass estimator leaves out (see the SDK's Counter example).
GRAMS_PER_COMPUTE_BUDGET_UNIT = 100
FEE_MASS_SLACK = 400

SOMPI_PER_KAS = 100_000_000
COVENANT_VALUE = 2 * SOMPI_PER_KAS  # coins locked in the covenant at genesis
LEAK = 1 * SOMPI_PER_KAS  # what the `try leak` transaction tries to take out
SIGNATURE_PLACEHOLDER = bytes(64) + b"\x01"  # same size as a real signature
JUMP = 6  # how far `try close-continue` tries to move the count through `close`


# --------------------------------------------------------------------------
# Contracts
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Contract:
    name: str
    file: str
    owned: bool = False
    closable: bool = False

    @property
    def source(self) -> str:
        return (ROOT / "contracts" / self.file).read_text(encoding="utf-8")


CONTRACTS = {
    "counter": Contract("counter", "counter.sil"),
    "strict": Contract("strict", "counter_strict.sil"),
    "keeping": Contract("keeping", "counter_keeping.sil"),
    "owned": Contract("owned", "counter_owned.sil", owned=True),
    "closable": Contract("closable", "counter_closable.sil", owned=True, closable=True),
}
KEEPING = CONTRACTS["keeping"]


def compiled(contract: Contract, count: int, owner: str | None):
    """Compile `contract` at state `count`. `owner` is an x-only public key in hex."""
    args = [count]
    if contract.owned:
        if not owner:
            raise ValueError("this contract needs an owner public key")
        args.append(bytes.fromhex(owner))
    return silverscript.compile(contract.source, args)


def lock_script(count: int, contract: Contract = KEEPING, owner: str | None = None):
    """P2SH locking script of the contract whose state is `count`."""
    redeem = compiled(contract, count, owner).bytecode
    return ScriptBuilder.from_script(redeem).create_pay_to_script_hash_script()


def covenant_address(count: int, contract: Contract = KEEPING, owner: str | None = None) -> str:
    script = lock_script(count, contract, owner)
    return address_from_script_public_key(script, NETWORK_TYPE).to_string()


def covenant_call(
    count: int,
    function: str,
    arguments: list,
    contract: Contract = KEEPING,
    owner: str | None = None,
    signature: bytes | None = None,
) -> bytes:
    """Covenant call for `function(*arguments[, signature])` plus the pushed redeem script."""
    program = compiled(contract, count, owner)
    args = list(arguments)
    if contract.owned:
        args.append(signature or SIGNATURE_PLACEHOLDER)
    call = program.build_sig_script_for_covenant_decl(function, args)
    redeem = bytes.fromhex(ScriptBuilder().add_data(program.bytecode).to_string())
    return call + redeem


def unlock_script(
    count: int,
    function: str,
    amount: int,
    contract: Contract = KEEPING,
    owner: str | None = None,
    signature: bytes | None = None,
) -> bytes:
    """Covenant call for the counting move `function(amount[, signature])`."""
    return covenant_call(count, function, [amount], contract, owner, signature)


def next_count(count: int, function: str, amount: int) -> int:
    if function == "add":
        return count + amount
    if function == "subtract":
        return count - amount
    raise ValueError(f"unknown transition: {function}")


# --------------------------------------------------------------------------
# Transaction building (pure: no network access)
# --------------------------------------------------------------------------


@dataclass
class Plan:
    """What a transaction is meant to do, before fees are worked out."""

    inputs: list  # TransactionInput
    input_values: list  # sompi, same order as inputs
    fixed_outputs: list  # (value, script_public_key, covenant_binding_or_None)
    change_script: object  # where the remainder goes
    genesis: bool = False

    @property
    def compute_budget(self) -> int:
        """Units of compute budget over all inputs: the node charges mass for each of them."""
        return sum(tx_input.compute_budget for tx_input in self.inputs)


def make_input(
    entry: dict, signature_script: bytes = b"", compute_budget: int = COMPUTE_BUDGET
) -> TransactionInput:
    outpoint = entry["outpoint"]
    return TransactionInput(
        TransactionOutpoint(Hash(outpoint["transactionId"]), outpoint["index"]),
        signature_script,
        sequence=0,
        sig_op_count=0,
        compute_budget=compute_budget,
        utxo=UtxoEntryReference.from_dict(entry),
    )


def assemble(plan: Plan, change: int, mass: int) -> Transaction:
    outputs = [TransactionOutput(value, spk, binding) for value, spk, binding in plan.fixed_outputs]
    outputs.append(TransactionOutput(change, plan.change_script, None))
    tx = Transaction(
        TX_VERSION,
        plan.inputs,
        outputs,
        lock_time=0,
        subnetwork_id=SUBNETWORK_ID,
        gas=0,
        payload=b"",
        mass=mass,
    )
    if plan.genesis:
        tx.populate_genesis_covenants([GenesisCovenantGroup(authorizing_input=0, outputs=[0])])
    return tx


def build(plan: Plan, feerate: int) -> tuple:
    """Return (transaction, fee, change, mass). The change output absorbs the fee.

    Fee and mass depend on each other (a smaller change output weighs more),
    so iterate to a fixed point, as the SDK's Counter example does.
    """
    total_in = sum(plan.input_values)
    fixed_out = sum(value for value, _, _ in plan.fixed_outputs)
    budget_grams = GRAMS_PER_COMPUTE_BUDGET_UNIT * plan.compute_budget

    fee, mass, settled = 0, 0, False
    for _ in range(8):
        change = total_in - fixed_out - fee
        if change <= 0:
            raise ValueError("the funding input cannot cover the outputs and the fee")
        mass = calculate_transaction_mass(NETWORK_ID, assemble(plan, change, 0))
        new_fee = (mass + budget_grams + FEE_MASS_SLACK) * feerate
        if new_fee == fee:
            settled = True
            break
        fee = new_fee
    if not settled:
        # Fee and mass kept chasing each other: do not hand back numbers that disagree.
        raise ValueError("the fee did not settle in eight rounds; use a larger funding input")
    change = total_in - fixed_out - fee
    if change <= 0:
        raise ValueError("the funding input cannot cover the outputs and the fee")
    return assemble(plan, change, mass), fee, change, mass


def genesis_plan(
    funding: dict,
    change_script,
    value: int = COVENANT_VALUE,
    contract: Contract = KEEPING,
    owner: str | None = None,
) -> Plan:
    return Plan(
        inputs=[make_input(funding)],
        input_values=[funding["utxoEntry"]["amount"]],
        fixed_outputs=[(value, lock_script(0, contract, owner), None)],
        change_script=change_script,
        genesis=True,
    )


def transition_plan(
    covenant: dict,
    funding: dict,
    change_script,
    count: int,
    function: str,
    amount: int,
    *,
    contract: Contract = KEEPING,
    owner: str | None = None,
    signature: bytes | None = None,
    claimed_count: int | None = None,
    covenant_value_out: int | None = None,
    covenant_budget: int = COMPUTE_BUDGET,
) -> Plan:
    """Spend the covenant UTXO at `count` by calling `function(amount)`.

    `claimed_count` and `covenant_value_out` default to the honest values;
    the `try ...` commands override them to break a rule on purpose.
    `covenant_budget` is the compute budget of the covenant input.
    """
    value_in = covenant["utxoEntry"]["amount"]
    covenant_id = covenant["utxoEntry"]["covenantId"]
    if claimed_count is None:
        claimed_count = next_count(count, function, amount)
    if covenant_value_out is None:
        covenant_value_out = value_in
    binding = CovenantBinding(authorizing_input=0, covenant_id=Hash(covenant_id))
    call = unlock_script(count, function, amount, contract, owner, signature)
    return Plan(
        inputs=[make_input(covenant, call, covenant_budget), make_input(funding)],
        input_values=[value_in, funding["utxoEntry"]["amount"]],
        fixed_outputs=[
            (covenant_value_out, lock_script(claimed_count, contract, owner), binding)
        ],
        change_script=change_script,
    )


def closing_plan(
    covenant: dict,
    funding: dict,
    change_script,
    count: int,
    payout_script,
    *,
    contract: Contract,
    owner: str | None = None,
    signature: bytes | None = None,
    continue_at: int | None = None,
    covenant_budget: int = COMPUTE_BUDGET,
) -> Plan:
    """Spend the covenant UTXO at `count` by calling `close`: the covenant ends.

    There is no covenant output: the covenant's coins go to `payout_script` in
    full, and the funding input pays the fee. `continue_at` breaks the rule on
    purpose (`try close-continue`): the coins then stay in a covenant output
    at that count, and the call names it as the next state.
    """
    if not contract.closable:
        raise ValueError(f"the {contract.name} contract has no closing move")
    value_in = covenant["utxoEntry"]["amount"]
    if continue_at is None:
        next_states = []
        fixed_outputs = [(value_in, payout_script, None)]
    else:
        binding = CovenantBinding(
            authorizing_input=0, covenant_id=Hash(covenant["utxoEntry"]["covenantId"])
        )
        next_states = [{"count": continue_at}]
        fixed_outputs = [(value_in, lock_script(continue_at, contract, owner), binding)]
    call = covenant_call(count, "close", [next_states], contract, owner, signature)
    return Plan(
        inputs=[make_input(covenant, call, covenant_budget), make_input(funding)],
        input_values=[value_in, funding["utxoEntry"]["amount"]],
        fixed_outputs=fixed_outputs,
        change_script=change_script,
    )


def transfer_plan(funding: dict, change_script, to_script, amount: int) -> Plan:
    """An ordinary payment, used to give a second test key some test coins."""
    return Plan(
        inputs=[make_input(funding)],
        input_values=[funding["utxoEntry"]["amount"]],
        fixed_outputs=[(amount, to_script, None)],
        change_script=change_script,
    )


def signed_transition(
    key: PrivateKey, feerate: int, *, planner=None, sighash: str | None = None, **plan_args
) -> tuple:
    """Build and sign a transaction that spends the covenant UTXO. Returns (transaction, fee).

    `planner` is `transition_plan` (the default) or `closing_plan`.

    For an owned contract the acting key also signs the covenant input, and
    that signature travels inside the covenant call. A signature does not
    cover signature scripts, so the transaction is first built with a
    placeholder of the same size, signed, and rebuilt with the real one.
    `sighash` names a signature type other than the default one, for the
    attempts that break the type rule on purpose (`try close-none`).
    """
    planner = planner or transition_plan
    contract = plan_args.get("contract", KEEPING)
    tx, fee, change, mass = build(planner(**plan_args), feerate)
    if contract.owned:
        if sighash is None:
            pushed = create_input_signature(tx, 0, key)
        else:
            pushed = create_input_signature(tx, 0, key, getattr(SighashType, sighash))
        signature = bytes.fromhex(pushed)[1:]  # drop the one-byte push opcode, keep 64 + sighash type
        tx = assemble(planner(**plan_args, signature=signature), change, mass)
    # The funding input is an ordinary one; the covenant call is left as it is.
    return sign_transaction(tx, [key], False), fee


# --------------------------------------------------------------------------
# Local files: keys and the state of the live covenants
# --------------------------------------------------------------------------


def path_from_env(name: str) -> Path:
    value = os.environ.get(name)
    if not value:
        raise SystemExit(f"Set {name} to a path outside the repository.")
    path = Path(value).resolve()
    if path == ROOT or ROOT in path.parents:
        raise SystemExit(f"{name} must point outside the repository.")
    return path


def load_key(name: str = "KASPA_LAB_KEY_FILE") -> PrivateKey:
    key = PrivateKey(path_from_env(name).read_text(encoding="ascii").strip())
    if not address_of(key).startswith(ADDRESS_PREFIX):
        raise SystemExit("Refusing: the key does not produce a testnet address.")
    return key


def address_of(key: PrivateKey) -> str:
    return key.to_keypair().to_address(NETWORK_TYPE).to_string()


@dataclass
class Live:
    covenant_id: str
    txid: str
    count: int
    value: int
    owner: str | None = None  # x-only public key (public data), owned contracts only


def state_path(contract: Contract) -> Path:
    return path_from_env("KASPA_LAB_STATE_DIR") / f"tn10-{contract.name}.json"


def load_state(contract: Contract) -> Live | None:
    path = state_path(contract)
    if not path.exists():
        return None
    record = json.loads(path.read_text(encoding="utf-8"))
    # A record that was set aside carries one more key; it loads all the same.
    return Live(**{name: record[name] for name in Live.__dataclass_fields__ if name in record})


def save_state(contract: Contract, live: Live) -> None:
    path = state_path(contract)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(asdict(live), indent=2) + "\n", encoding="utf-8")


def need_state(contract: Contract) -> Live:
    live = load_state(contract)
    if live is None:
        raise SystemExit(f"No {contract.name} covenant yet. Run `genesis` first.")
    return live


def set_aside_state(contract: Contract, live: Live, closing_txid: str) -> Path:
    """After a covenant has been closed: keep its last record under another name.

    The live state file is removed, so `genesis` can start a new covenant of
    this contract; the record of the closed one stays next to it.
    """
    path = state_path(contract)
    closed = path.with_name(f"tn10-{contract.name}-closed-{closing_txid[:12]}.json")
    record = {**asdict(live), "closed_by": closing_txid}
    closed.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    path.unlink()
    return closed


# --------------------------------------------------------------------------
# Network
# --------------------------------------------------------------------------


async def check_node(client: RpcClient) -> dict:
    """Refuse a node that is not a synced testnet-10 node.

    Asked after every connection: the first one, and each reconnect, which
    may land on another node.
    """
    info = await client.get_server_info()
    if info.get("networkId") != NETWORK_ID:
        await client.disconnect()
        raise SystemExit(f"Refusing: the node reports network {info.get('networkId')!r}.")
    if not info.get("isSynced"):
        await client.disconnect()
        raise SystemExit("Refusing: the node is not synced.")
    return info


async def connect() -> RpcClient:
    client = RpcClient(resolver=Resolver(), network_id=NETWORK_ID)
    await asyncio.wait_for(client.connect(), timeout=60)
    info = await check_node(client)
    print(f"via     : {client.url}, node version {info.get('serverVersion')}", flush=True)
    return client


async def utxos_at(client: RpcClient, address: str) -> list:
    """UTXOs at `address`. Public nodes drop connections now and then: retry a few times."""
    last_error = None
    for attempt in range(4):
        try:
            result = await client.get_utxos_by_addresses({"addresses": [address]})
            return result["entries"]
        except Exception as error:
            last_error = error
            await asyncio.sleep(2 + attempt)
            try:
                await asyncio.wait_for(client.connect(), timeout=60)
                await check_node(client)  # a wrong or unsynced node stops the program here
            except Exception:
                pass
    raise last_error


async def feerate(client: RpcClient) -> int:
    """The feerate the node suggests, in sompi per gram. Printed: fees follow it, and it changes."""
    estimate = await client.get_fee_estimate()
    rate = int(estimate["estimate"]["priorityBucket"]["feerate"])
    print(f"feerate : {rate} sompi per gram (the node's estimate)", flush=True)
    return rate


def largest_plain(entries: list) -> dict:
    plain = [e for e in entries if not e["utxoEntry"].get("covenantId")]
    if not plain:
        raise SystemExit("No funding UTXO at the acting key's address. Send it testnet KAS first.")
    return max(plain, key=lambda e: e["utxoEntry"]["amount"])


async def funding_for(client: RpcClient, key: PrivateKey) -> dict:
    return largest_plain(await utxos_at(client, address_of(key)))


async def wait_for(client: RpcClient, address: str, txid: str, seconds: int = 60) -> dict:
    for _ in range(seconds):
        for entry in await utxos_at(client, address):
            if entry["outpoint"]["transactionId"] == txid:
                return entry
        await asyncio.sleep(1)
    raise SystemExit(f"Transaction {txid} was submitted but its output did not appear in {seconds}s.")


def is_recorded_covenant(entry: dict, live: Live) -> bool:
    """Whether a UTXO is the one the state file points at: the recorded transaction and this covenant id.

    The address alone does not say it: another covenant of the same contract
    at the same count sits at the same address.
    """
    return (
        entry["outpoint"]["transactionId"] == live.txid
        and entry["utxoEntry"].get("covenantId") == live.covenant_id
    )


async def find_covenant(client: RpcClient, contract: Contract, live: Live) -> dict:
    address = covenant_address(live.count, contract, live.owner)
    for entry in await utxos_at(client, address):
        if is_recorded_covenant(entry, live):
            return entry
    raise SystemExit("The covenant UTXO recorded in the state file is not on chain.")


REFUSAL_MARK = "Rejected transaction"  # how a node's refusal begins


class NoAnswer(Exception):
    """A submission ended without the node's answer: the node may have taken the transaction."""

    def __init__(self, reason: str, txid: str):
        super().__init__(reason)
        self.txid = txid


async def submit(client: RpcClient, tx: Transaction) -> tuple:
    """Return (accepted, transaction id or the node's refusal).

    Only the node's own answer counts as a refusal. Any other failure — a
    dropped connection, a timeout — raises `NoAnswer`: the transaction may
    have been accepted all the same.
    """
    try:
        result = await client.submit_transaction({"transaction": tx, "allowOrphan": False})
    except Exception as error:  # the node's refusal arrives as an exception too
        reason = str(error)[:700]
        if REFUSAL_MARK in reason:
            return False, reason
        raise NoAnswer(reason, tx.id) from error
    return True, result["transactionId"]


async def seen_at(client: RpcClient, address: str, txid: str, seconds: int = 30) -> bool:
    """Whether an output of transaction `txid` shows up at `address` within `seconds`."""
    for _ in range(seconds):
        if any(entry["outpoint"]["transactionId"] == txid for entry in await utxos_at(client, address)):
            return True
        await asyncio.sleep(1)
    return False


async def submit_checked(client: RpcClient, tx: Transaction, key: PrivateKey) -> tuple:
    """`submit`; when the answer is lost, look for the transaction's change output.

    Every transaction built here returns its change to the acting key. If
    that output shows up at the key's address, the node had accepted the
    transaction although its answer did not arrive. If it does not show up,
    `NoAnswer` is raised: nothing is known, and nothing is recorded.
    """
    try:
        return await submit(client, tx)
    except NoAnswer as lost:
        print(f"node    : NO ANSWER ({lost}); looking for the change output of {lost.txid}", flush=True)
        if await seen_at(client, address_of(key), lost.txid):
            return True, lost.txid
        raise


def kas(sompi: int) -> str:
    return f"{sompi / SOMPI_PER_KAS:.8f}"


def script_of(key: PrivateKey):
    return pay_to_address_script(key.to_keypair().to_address(NETWORK_TYPE))


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------


async def cmd_status(client: RpcClient, key: PrivateKey, contract: Contract) -> None:
    address = address_of(key)
    entries = await utxos_at(client, address)
    balance = sum(e["utxoEntry"]["amount"] for e in entries)
    print(f"acting key   : {address}")
    print(f"balance      : {kas(balance)} TKAS in {len(entries)} UTXO(s)")
    live = load_state(contract)
    print(f"contract     : {contract.name}")
    closed = sorted(state_path(contract).parent.glob(f"tn10-{contract.name}-closed-*.json"))
    if closed:
        print(f"closed before: {len(closed)} covenant(s), records {', '.join(path.name for path in closed)}")
    if live is None:
        print("covenant     : no live covenant recorded")
        return
    print(f"covenant id  : {live.covenant_id}")
    if live.owner:
        mine = key.to_keypair().xonly_public_key == live.owner
        print(f"owner        : {live.owner} ({'the acting key' if mine else 'NOT the acting key'})")
    print(f"count        : {live.count}")
    print(f"value        : {kas(live.value)} TKAS")
    print(f"last tx      : {live.txid}")
    on_chain = await utxos_at(client, covenant_address(live.count, contract, live.owner))
    found = [e for e in on_chain if is_recorded_covenant(e, live)]
    print(f"on chain     : {bool(found)}")
    if found:
        print(f"value on chain: {kas(found[0]['utxoEntry']['amount'])} TKAS")


async def cmd_genesis(client: RpcClient, key: PrivateKey, contract: Contract) -> None:
    if load_state(contract) is not None:
        raise SystemExit(f"A {contract.name} covenant is already recorded in the state file.")
    owner = key.to_keypair().xonly_public_key if contract.owned else None
    plan = genesis_plan(await funding_for(client, key), script_of(key), contract=contract, owner=owner)
    tx, fee, _, _ = build(plan, await feerate(client))
    covenant_id = tx.outputs[0].to_dict()["covenant"]["covenantId"]
    accepted, answer = await submit_checked(client, sign_transaction(tx, [key], True), key)
    if not accepted:
        raise SystemExit(f"genesis REFUSED: {answer}")
    print(f"genesis accepted: {answer}", flush=True)
    print(f"covenant id     : {covenant_id}", flush=True)
    save_state(contract, Live(covenant_id, answer, 0, COVENANT_VALUE, owner))
    await wait_for(client, covenant_address(0, contract, owner), answer)
    print(f"count 0, {kas(COVENANT_VALUE)} TKAS locked, fee {kas(fee)} TKAS")


async def cmd_resync(client: RpcClient, contract: Contract, count: int) -> None:
    """Point the state file at the covenant UTXO found on chain at `count`.

    For recovery after an interrupted run: the covenant id must match the one
    already recorded, so this cannot adopt somebody else's covenant.
    """
    live = need_state(contract)
    for entry in await utxos_at(client, covenant_address(count, contract, live.owner)):
        utxo = entry["utxoEntry"]
        if utxo.get("covenantId") == live.covenant_id:
            txid = entry["outpoint"]["transactionId"]
            save_state(contract, Live(live.covenant_id, txid, count, utxo["amount"], live.owner))
            print(f"state now: count {count}, {kas(utxo['amount'])} TKAS, last tx {txid}")
            return
    raise SystemExit(f"No UTXO of this covenant at count {count}.")


async def cmd_move(
    client: RpcClient,
    key: PrivateKey,
    contract: Contract,
    function: str,
    amount: int,
    budget: int = COMPUTE_BUDGET,
) -> None:
    live = need_state(contract)
    tx, fee = signed_transition(
        key,
        await feerate(client),
        covenant=await find_covenant(client, contract, live),
        funding=await funding_for(client, key),
        change_script=script_of(key),
        count=live.count,
        function=function,
        amount=amount,
        contract=contract,
        owner=live.owner,
        covenant_budget=budget,
    )
    print(f"attempt : {function}({amount}) on count {live.count}, paid by {address_of(key)}", flush=True)
    print(f"budget  : {budget} (covenant input)", flush=True)
    accepted, answer = await submit_checked(client, tx, key)
    if not accepted:
        print("node    : REFUSED")
        print(f"reason  : {answer}")
        return
    print(f"node    : ACCEPTED {answer}", flush=True)
    new_count = next_count(live.count, function, amount)
    # Record the new state first: if the connection drops while waiting, nothing is lost.
    save_state(contract, Live(live.covenant_id, answer, new_count, live.value, live.owner))
    await wait_for(client, covenant_address(new_count, contract, live.owner), answer)
    print(f"result  : count {live.count} -> {new_count}, covenant holds {kas(live.value)} TKAS, fee {kas(fee)} TKAS")


async def cmd_close(
    client: RpcClient, key: PrivateKey, contract: Contract, budget: int = COMPUTE_BUDGET
) -> None:
    """End a closable covenant: its coins are paid out to the acting key."""
    if not contract.closable:
        raise SystemExit(f"The {contract.name} contract has no closing move.")
    live = need_state(contract)
    mine = key.to_keypair().xonly_public_key == live.owner
    tx, fee = signed_transition(
        key,
        await feerate(client),
        planner=closing_plan,
        covenant=await find_covenant(client, contract, live),
        funding=await funding_for(client, key),
        change_script=script_of(key),
        count=live.count,
        payout_script=script_of(key),
        contract=contract,
        owner=live.owner,
        covenant_budget=budget,
    )
    who = "the owner" if mine else "a key that is NOT the owner"
    print(f"attempt : close on count {live.count}, signed and paid by {who}, {address_of(key)}", flush=True)
    print(f"budget  : {budget} (covenant input)", flush=True)
    accepted, answer = await submit_checked(client, tx, key)
    if not accepted:
        print("node    : REFUSED")
        print(f"reason  : {answer}")
        return
    print(f"node    : ACCEPTED {answer}", flush=True)
    # Set the record aside before waiting, so that a dropped connection does not
    # leave a live record of a covenant that no longer exists.
    closed = set_aside_state(contract, live, answer)
    await wait_for(client, address_of(key), answer)
    print(f"result  : the covenant is closed, {kas(live.value)} TKAS paid out to {address_of(key)}, fee {kas(fee)} TKAS")
    print(f"record  : {closed.name}")


async def cmd_try_close(
    client: RpcClient, key: PrivateKey, contract: Contract, what: str, budget: int = COMPUTE_BUDGET
) -> None:
    """Closing transactions that break a rule of `close` on purpose."""
    if not contract.closable:
        raise SystemExit(f"The {contract.name} contract has no closing move.")
    live = need_state(contract)
    jump = live.count + JUMP
    who = "the owner" if key.to_keypair().xonly_public_key == live.owner else "a key that is NOT the owner"
    if what == "close-continue":
        extra, sighash = dict(continue_at=jump), None
        label = f"close signed by {who}, but the coins stay in a covenant output at count {jump}, named as the next state"
    else:
        extra, sighash = {}, "None"
        label = f"close signed by {who} with a signature of type None"
    tx, _ = signed_transition(
        key,
        await feerate(client),
        planner=closing_plan,
        sighash=sighash,
        covenant=await find_covenant(client, contract, live),
        funding=await funding_for(client, key),
        change_script=script_of(key),
        count=live.count,
        payout_script=script_of(key),
        contract=contract,
        owner=live.owner,
        covenant_budget=budget,
        **extra,
    )
    print(f"attempt : {label}", flush=True)
    print(f"budget  : {budget} (covenant input)", flush=True)
    accepted, answer = await submit_checked(client, tx, key)
    if not accepted:
        print("node    : REFUSED")
        print(f"reason  : {answer}")
        return
    print(f"node    : ACCEPTED {answer}", flush=True)
    # The rule did not hold on chain: follow the covenant to where it went.
    if what == "close-continue":
        save_state(contract, Live(live.covenant_id, answer, jump, live.value, live.owner))
        await wait_for(client, covenant_address(jump, contract, live.owner), answer)
        print(f"result  : count {live.count} -> {jump} through `close`")
    else:
        closed = set_aside_state(contract, live, answer)
        await wait_for(client, address_of(key), answer)
        print(f"result  : the covenant is closed by a signature of type None; record {closed.name}")


async def cmd_try(
    client: RpcClient, key: PrivateKey, contract: Contract, what: str, budget: int = COMPUTE_BUDGET
) -> None:
    if what in ("close-continue", "close-none"):
        await cmd_try_close(client, key, contract, what, budget)
        return
    live = need_state(contract)
    value_out, claimed, amount = live.value, None, 5
    if what == "leak":
        value_out = live.value - LEAK
        label = f"add(5), but only {kas(value_out)} of {kas(live.value)} TKAS stay in the covenant"
    elif what == "wrong-state":
        claimed = live.count + 6
        label = f"add(5), but the output claims count {claimed}"
    elif what == "negative":
        amount = -20
        label = f"add(-20) on count {live.count}"
    else:
        raise SystemExit("try what? leak | wrong-state | negative | close-continue | close-none")

    tx, _ = signed_transition(
        key,
        await feerate(client),
        covenant=await find_covenant(client, contract, live),
        funding=await funding_for(client, key),
        change_script=script_of(key),
        count=live.count,
        function="add",
        amount=amount,
        contract=contract,
        owner=live.owner,
        claimed_count=claimed,
        covenant_value_out=value_out,
        covenant_budget=budget,
    )
    print(f"attempt : {label}", flush=True)
    print(f"budget  : {budget} (covenant input)", flush=True)
    accepted, answer = await submit_checked(client, tx, key)
    if not accepted:
        print("node    : REFUSED")
        print(f"reason  : {answer}")
        return
    print(f"node    : ACCEPTED {answer}", flush=True)
    # The rule did not hold on chain: follow the covenant to where it went.
    new_count = claimed if claimed is not None else live.count + amount
    save_state(contract, Live(live.covenant_id, answer, new_count, value_out, live.owner))
    await wait_for(client, covenant_address(new_count, contract, live.owner), answer)
    print(f"result  : count {live.count} -> {new_count}, covenant now holds {kas(value_out)} TKAS")


async def cmd_send(client: RpcClient, key: PrivateKey, to_address: str, amount_kas: str) -> None:
    if not to_address.startswith(ADDRESS_PREFIX):
        raise SystemExit("Refusing: the destination is not a testnet address.")
    amount = int(round(float(amount_kas) * SOMPI_PER_KAS))
    from kaspa import Address

    plan = transfer_plan(
        await funding_for(client, key), script_of(key), pay_to_address_script(Address(to_address)), amount
    )
    tx, fee, _, _ = build(plan, await feerate(client))
    accepted, answer = await submit_checked(client, sign_transaction(tx, [key], True), key)
    if not accepted:
        raise SystemExit(f"send REFUSED: {answer}")
    await wait_for(client, to_address, answer)
    print(f"sent {kas(amount)} TKAS to {to_address}: {answer} (fee {kas(fee)} TKAS)")


async def cmd_race(client: RpcClient, key: PrivateKey, contract: Contract) -> None:
    """Two different moves spending the same covenant UTXO, each paid by its own key."""
    second = load_key("KASPA_LAB_SECOND_KEY_FILE")
    live = need_state(contract)
    covenant = await find_covenant(client, contract, live)
    rate = await feerate(client)
    common = dict(covenant=covenant, count=live.count, function="add", contract=contract, owner=live.owner)
    first_tx, _ = signed_transition(
        key, rate, funding=await funding_for(client, key), change_script=script_of(key), amount=1, **common
    )
    second_tx, _ = signed_transition(
        second, rate, funding=await funding_for(client, second), change_script=script_of(second), amount=2, **common
    )
    async def raced(tx: Transaction) -> tuple:
        try:
            return await submit(client, tx)
        except NoAnswer as lost:
            return None, f"NO ANSWER ({lost}); transaction {lost.txid}"

    first = await raced(first_tx)
    other = await raced(second_tx)
    for label, (accepted, answer) in (("add(1), first", first), ("add(2), second", other)):
        verdict = "ACCEPTED " + answer if accepted else "REFUSED" if accepted is False else "UNKNOWN"
        print(f"{label:<15}: {verdict}")
        if not accepted:
            print(f"reason         : {answer}")
    if first[0] is None or other[0] is None:
        print("An answer was lost: nothing is recorded. Run `status`, then `resync N`.")
        return
    if first[0] and not other[0]:
        save_state(contract, Live(live.covenant_id, first[1], live.count + 1, live.value, live.owner))
        await wait_for(client, covenant_address(live.count + 1, contract, live.owner), first[1])
    elif other[0] and not first[0]:
        save_state(contract, Live(live.covenant_id, other[1], live.count + 2, live.value, live.owner))
        await wait_for(client, covenant_address(live.count + 2, contract, live.owner), other[1])
    elif first[0] and other[0]:
        print("Both were accepted into the mempool; check `status` to see which one the chain kept.")


MAX_COMPUTE_BUDGET = 65535  # the field is 16 bits wide
BUDGET_COMMANDS = ("move", "try", "close")


def options(argv: list) -> tuple:
    """Split `--contract NAME` and `--budget N`, in either order, from the command. No network."""
    contract, budget = KEEPING, None
    while len(argv) >= 2 and argv[0] in ("--contract", "--budget"):
        option, value, argv = argv[0], argv[1], argv[2:]
        if option == "--contract":
            if value not in CONTRACTS:
                raise SystemExit(f"--contract must be one of: {', '.join(CONTRACTS)}")
            contract = CONTRACTS[value]
        else:
            if not value.isdecimal() or int(value) > MAX_COMPUTE_BUDGET:
                raise SystemExit(f"--budget must be a whole number from 0 to {MAX_COMPUTE_BUDGET}")
            budget = int(value)
    if not argv or argv[0].startswith("--"):
        raise SystemExit(__doc__)
    if budget is not None and argv[0] not in BUDGET_COMMANDS:
        raise SystemExit(f"--budget applies to {', '.join(BUDGET_COMMANDS)} only")
    return contract, COMPUTE_BUDGET if budget is None else budget, argv


async def run(argv: list) -> None:
    contract, budget, argv = options(argv)
    key = load_key()
    client = await connect()
    try:
        command = argv[0]
        if command == "status" and len(argv) == 1:
            await cmd_status(client, key, contract)
        elif command == "genesis" and len(argv) == 1:
            await cmd_genesis(client, key, contract)
        elif command == "move" and len(argv) == 3 and argv[1] in ("add", "subtract"):
            await cmd_move(client, key, contract, argv[1], int(argv[2]), budget)
        elif command == "try" and len(argv) == 2:
            await cmd_try(client, key, contract, argv[1], budget)
        elif command == "close" and len(argv) == 1:
            await cmd_close(client, key, contract, budget)
        elif command == "send" and len(argv) == 3:
            await cmd_send(client, key, argv[1], argv[2])
        elif command == "race" and len(argv) == 1:
            await cmd_race(client, key, contract)
        elif command == "resync" and len(argv) == 2:
            await cmd_resync(client, contract, int(argv[1]))
        else:
            raise SystemExit(__doc__)
    except NoAnswer as lost:
        raise SystemExit(
            f"NO ANSWER from the node: {lost}\n"
            f"Transaction {lost.txid} may have been accepted all the same, and nothing was recorded.\n"
            "Run `status` before anything else; if the covenant moved, `resync N` repairs the record."
        )
    finally:
        await client.disconnect()


if __name__ == "__main__":
    asyncio.run(run(sys.argv[1:]))
