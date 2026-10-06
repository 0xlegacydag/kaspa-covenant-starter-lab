"""Run the Counter contracts of this repository on Kaspa testnet-10.

TEST NETWORK ONLY. The script refuses to talk to any other network, and the
keys it uses must be throwaway keys made with `testnet/make_key.py`.

Contracts (`--contract`):

    counter   the guide's Counter            no amount rule, no value rule
    strict    StrictCounter                  amount > 0
    keeping   KeepingCounter (default)       amount > 0, covenant keeps its coins
    owned     OwnedCounter                   as keeping, plus the owner's signature

The covenant never pays a fee here. Every transition has two inputs — the
covenant UTXO and an ordinary funding UTXO that pays the fee — and two
outputs — the next covenant UTXO and the change.

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

`KASPA_LAB_KEY_FILE` is the key that acts: it pays the fees and, for `owned`,
signs the transition. Point it at another key to act as somebody else.
`try ...` submits a transaction that breaks a rule and reports what the node
answers. `race` submits two different moves that spend the same covenant
UTXO; the second one is paid by `KASPA_LAB_SECOND_KEY_FILE`. `resync N`
repairs the state file after an interrupted run by finding this covenant's
UTXO on chain at count N.
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


# --------------------------------------------------------------------------
# Contracts
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Contract:
    name: str
    file: str
    owned: bool = False

    @property
    def source(self) -> str:
        return (ROOT / "contracts" / self.file).read_text(encoding="utf-8")


CONTRACTS = {
    "counter": Contract("counter", "counter.sil"),
    "strict": Contract("strict", "counter_strict.sil"),
    "keeping": Contract("keeping", "counter_keeping.sil"),
    "owned": Contract("owned", "counter_owned.sil", owned=True),
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


def unlock_script(
    count: int,
    function: str,
    amount: int,
    contract: Contract = KEEPING,
    owner: str | None = None,
    signature: bytes | None = None,
) -> bytes:
    """Covenant call for `function(amount[, signature])` plus the pushed redeem script."""
    program = compiled(contract, count, owner)
    args = [amount]
    if contract.owned:
        args.append(signature or SIGNATURE_PLACEHOLDER)
    call = program.build_sig_script_for_covenant_decl(function, args)
    redeem = bytes.fromhex(ScriptBuilder().add_data(program.bytecode).to_string())
    return call + redeem


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


def make_input(entry: dict, signature_script: bytes = b"") -> TransactionInput:
    outpoint = entry["outpoint"]
    return TransactionInput(
        TransactionOutpoint(Hash(outpoint["transactionId"]), outpoint["index"]),
        signature_script,
        sequence=0,
        sig_op_count=0,
        compute_budget=COMPUTE_BUDGET,
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
    budget_grams = GRAMS_PER_COMPUTE_BUDGET_UNIT * COMPUTE_BUDGET * len(plan.inputs)

    fee, mass = 0, 0
    for _ in range(8):
        change = total_in - fixed_out - fee
        if change <= 0:
            raise ValueError("the funding input cannot cover the outputs and the fee")
        mass = calculate_transaction_mass(NETWORK_ID, assemble(plan, change, 0))
        new_fee = (mass + budget_grams + FEE_MASS_SLACK) * feerate
        if new_fee == fee:
            break
        fee = new_fee
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
) -> Plan:
    """Spend the covenant UTXO at `count` by calling `function(amount)`.

    `claimed_count` and `covenant_value_out` default to the honest values;
    the `try ...` commands override them to break a rule on purpose.
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
        inputs=[make_input(covenant, call), make_input(funding)],
        input_values=[value_in, funding["utxoEntry"]["amount"]],
        fixed_outputs=[
            (covenant_value_out, lock_script(claimed_count, contract, owner), binding)
        ],
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


def signed_transition(key: PrivateKey, feerate: int, **plan_args) -> tuple:
    """Build and sign a transition. Returns (transaction, fee).

    For an owned contract the acting key also signs the covenant input, and
    that signature travels inside the covenant call. A signature does not
    cover signature scripts, so the transaction is first built with a
    placeholder of the same size, signed, and rebuilt with the real one.
    """
    contract = plan_args.get("contract", KEEPING)
    tx, fee, change, mass = build(transition_plan(**plan_args), feerate)
    if contract.owned:
        pushed = bytes.fromhex(create_input_signature(tx, 0, key))
        signature = pushed[1:]  # drop the one-byte push opcode, keep 64 + sighash type
        tx = assemble(transition_plan(**plan_args, signature=signature), change, mass)
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
    return Live(**json.loads(path.read_text(encoding="utf-8")))


def save_state(contract: Contract, live: Live) -> None:
    path = state_path(contract)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(asdict(live), indent=2) + "\n", encoding="utf-8")


def need_state(contract: Contract) -> Live:
    live = load_state(contract)
    if live is None:
        raise SystemExit(f"No {contract.name} covenant yet. Run `genesis` first.")
    return live


# --------------------------------------------------------------------------
# Network
# --------------------------------------------------------------------------


async def connect() -> RpcClient:
    client = RpcClient(resolver=Resolver(), network_id=NETWORK_ID)
    await asyncio.wait_for(client.connect(), timeout=60)
    info = await client.get_server_info()
    if info.get("networkId") != NETWORK_ID:
        await client.disconnect()
        raise SystemExit(f"Refusing: the node reports network {info.get('networkId')!r}.")
    if not info.get("isSynced"):
        await client.disconnect()
        raise SystemExit("Refusing: the node is not synced.")
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
            except Exception:
                pass
    raise last_error


async def feerate(client: RpcClient) -> int:
    estimate = await client.get_fee_estimate()
    return int(estimate["estimate"]["priorityBucket"]["feerate"])


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


async def find_covenant(client: RpcClient, contract: Contract, live: Live) -> dict:
    address = covenant_address(live.count, contract, live.owner)
    for entry in await utxos_at(client, address):
        if entry["outpoint"]["transactionId"] == live.txid and entry["outpoint"]["index"] == 0:
            return entry
    raise SystemExit("The covenant UTXO recorded in the state file is not on chain.")


async def submit(client: RpcClient, tx: Transaction) -> tuple:
    """Return (accepted, transaction id or the node's refusal)."""
    try:
        result = await client.submit_transaction({"transaction": tx, "allowOrphan": False})
    except Exception as error:  # the node's refusal arrives as an exception
        return False, str(error)[:700]
    return True, result["transactionId"]


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
    if live is None:
        print("covenant     : not created yet")
        return
    print(f"covenant id  : {live.covenant_id}")
    if live.owner:
        mine = key.to_keypair().xonly_public_key == live.owner
        print(f"owner        : {live.owner} ({'the acting key' if mine else 'NOT the acting key'})")
    print(f"count        : {live.count}")
    print(f"value        : {kas(live.value)} TKAS")
    print(f"last tx      : {live.txid}")
    on_chain = await utxos_at(client, covenant_address(live.count, contract, live.owner))
    found = [e for e in on_chain if e["outpoint"]["transactionId"] == live.txid]
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
    accepted, answer = await submit(client, sign_transaction(tx, [key], True))
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
    client: RpcClient, key: PrivateKey, contract: Contract, function: str, amount: int
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
    )
    accepted, answer = await submit(client, tx)
    print(f"attempt : {function}({amount}) on count {live.count}, paid by {address_of(key)}", flush=True)
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


async def cmd_try(client: RpcClient, key: PrivateKey, contract: Contract, what: str) -> None:
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
        raise SystemExit("try what? leak | wrong-state | negative")

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
    )
    accepted, answer = await submit(client, tx)
    print(f"attempt : {label}", flush=True)
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
    accepted, answer = await submit(client, sign_transaction(tx, [key], True))
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
    first = await submit(client, first_tx)
    other = await submit(client, second_tx)
    for label, (accepted, answer) in (("add(1), first", first), ("add(2), second", other)):
        print(f"{label:<15}: {'ACCEPTED ' + answer if accepted else 'REFUSED'}")
        if not accepted:
            print(f"reason         : {answer}")
    if first[0] and not other[0]:
        save_state(contract, Live(live.covenant_id, first[1], live.count + 1, live.value, live.owner))
        await wait_for(client, covenant_address(live.count + 1, contract, live.owner), first[1])
    elif other[0] and not first[0]:
        save_state(contract, Live(live.covenant_id, other[1], live.count + 2, live.value, live.owner))
        await wait_for(client, covenant_address(live.count + 2, contract, live.owner), other[1])
    elif first[0] and other[0]:
        print("Both were accepted into the mempool; check `status` to see which one the chain kept.")


async def run(argv: list) -> None:
    contract = KEEPING
    if len(argv) >= 2 and argv[0] == "--contract":
        if argv[1] not in CONTRACTS:
            raise SystemExit(f"--contract must be one of: {', '.join(CONTRACTS)}")
        contract, argv = CONTRACTS[argv[1]], argv[2:]
    if not argv:
        raise SystemExit(__doc__)
    key = load_key()
    client = await connect()
    try:
        command = argv[0]
        if command == "status" and len(argv) == 1:
            await cmd_status(client, key, contract)
        elif command == "genesis" and len(argv) == 1:
            await cmd_genesis(client, key, contract)
        elif command == "move" and len(argv) == 3 and argv[1] in ("add", "subtract"):
            await cmd_move(client, key, contract, argv[1], int(argv[2]))
        elif command == "try" and len(argv) == 2:
            await cmd_try(client, key, contract, argv[1])
        elif command == "send" and len(argv) == 3:
            await cmd_send(client, key, argv[1], argv[2])
        elif command == "race" and len(argv) == 1:
            await cmd_race(client, key, contract)
        elif command == "resync" and len(argv) == 2:
            await cmd_resync(client, contract, int(argv[1]))
        else:
            raise SystemExit(__doc__)
    finally:
        await client.disconnect()


if __name__ == "__main__":
    asyncio.run(run(sys.argv[1:]))
