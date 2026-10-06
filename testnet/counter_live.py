"""Run KeepingCounter on Kaspa testnet-10, one transaction at a time.

TEST NETWORK ONLY. The script refuses to talk to any other network, and the
key it uses must be a throwaway key made with `testnet/make_key.py`.

Unlike the SDK's Counter example, the covenant here never pays its own fee:
`counter_keeping.sil` requires the covenant output to carry at least as many
sompi as the covenant input. So every transition has two inputs — the
covenant UTXO and an ordinary funding UTXO that pays the fee — and two
outputs — the next covenant UTXO and the change.

    set KASPA_LAB_KEY_FILE=C:\\path\\outside\\the\\repo\\tn10-test-key.txt
    set KASPA_LAB_STATE_FILE=C:\\path\\outside\\the\\repo\\tn10-counter.json

    python -m testnet.counter_live status
    python -m testnet.counter_live genesis
    python -m testnet.counter_live move add 5
    python -m testnet.counter_live move subtract 3
    python -m testnet.counter_live try leak
    python -m testnet.counter_live try wrong-state
    python -m testnet.counter_live try negative

`try ...` submits a transaction that breaks a rule and reports what the node
answers. It is expected to be refused.
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
    pay_to_address_script,
    sign_transaction,
)

NETWORK_ID = "testnet-10"
NETWORK_TYPE = "testnet"
ADDRESS_PREFIX = "kaspatest:"

ROOT = Path(__file__).resolve().parent.parent
SOURCE = (ROOT / "contracts" / "counter_keeping.sil").read_text(encoding="utf-8")

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


# --------------------------------------------------------------------------
# Contract helpers
# --------------------------------------------------------------------------


def lock_script(count: int):
    """P2SH locking script of the KeepingCounter whose state is `count`."""
    redeem = silverscript.compile(SOURCE, [count]).bytecode
    return ScriptBuilder.from_script(redeem).create_pay_to_script_hash_script()


def covenant_address(count: int) -> str:
    return address_from_script_public_key(lock_script(count), NETWORK_TYPE).to_string()


def unlock_script(count: int, function: str, amount: int) -> bytes:
    """Covenant call for `function(amount)` followed by the pushed redeem script."""
    contract = silverscript.compile(SOURCE, [count])
    call = contract.build_sig_script_for_covenant_decl(function, [amount])
    redeem = bytes.fromhex(ScriptBuilder().add_data(contract.bytecode).to_string())
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
    """Return (transaction, fee). The change output absorbs the fee.

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
    return assemble(plan, change, mass), fee


def genesis_plan(funding: dict, change_script, value: int = COVENANT_VALUE) -> Plan:
    return Plan(
        inputs=[make_input(funding)],
        input_values=[funding["utxoEntry"]["amount"]],
        fixed_outputs=[(value, lock_script(0), None)],
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
    return Plan(
        inputs=[
            make_input(covenant, unlock_script(count, function, amount)),
            make_input(funding),
        ],
        input_values=[value_in, funding["utxoEntry"]["amount"]],
        fixed_outputs=[(covenant_value_out, lock_script(claimed_count), binding)],
        change_script=change_script,
    )


# --------------------------------------------------------------------------
# Local files: the key and the state of the live covenant
# --------------------------------------------------------------------------


def path_from_env(name: str) -> Path:
    value = os.environ.get(name)
    if not value:
        raise SystemExit(f"Set {name} to a file path outside the repository.")
    path = Path(value).resolve()
    if ROOT in path.parents:
        raise SystemExit(f"{name} must point outside the repository.")
    return path


def load_key() -> PrivateKey:
    path = path_from_env("KASPA_LAB_KEY_FILE")
    return PrivateKey(path.read_text(encoding="ascii").strip())


@dataclass
class Live:
    covenant_id: str
    txid: str
    count: int
    value: int


def load_state() -> Live | None:
    path = path_from_env("KASPA_LAB_STATE_FILE")
    if not path.exists():
        return None
    return Live(**json.loads(path.read_text(encoding="utf-8")))


def save_state(live: Live) -> None:
    path = path_from_env("KASPA_LAB_STATE_FILE")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(asdict(live), indent=2) + "\n", encoding="utf-8")


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
    result = await client.get_utxos_by_addresses({"addresses": [address]})
    return result["entries"]


async def feerate(client: RpcClient) -> int:
    estimate = await client.get_fee_estimate()
    return int(estimate["estimate"]["priorityBucket"]["feerate"])


def largest_plain(entries: list) -> dict:
    plain = [e for e in entries if not e["utxoEntry"].get("covenantId")]
    if not plain:
        raise SystemExit("No funding UTXO at the test address. Send it some testnet KAS first.")
    return max(plain, key=lambda e: e["utxoEntry"]["amount"])


async def wait_for(client: RpcClient, address: str, txid: str, seconds: int = 60) -> dict:
    for _ in range(seconds):
        for entry in await utxos_at(client, address):
            if entry["outpoint"]["transactionId"] == txid:
                return entry
        await asyncio.sleep(1)
    raise SystemExit(f"Transaction {txid} was submitted but its output did not appear in {seconds}s.")


async def find_covenant(client: RpcClient, live: Live) -> dict:
    for entry in await utxos_at(client, covenant_address(live.count)):
        if entry["outpoint"]["transactionId"] == live.txid and entry["outpoint"]["index"] == 0:
            return entry
    raise SystemExit("The covenant UTXO recorded in the state file is not on chain.")


def kas(sompi: int) -> str:
    return f"{sompi / SOMPI_PER_KAS:.8f}"


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------


async def cmd_status(client: RpcClient, key: PrivateKey) -> None:
    address = key.to_keypair().to_address(NETWORK_TYPE).to_string()
    entries = await utxos_at(client, address)
    balance = sum(e["utxoEntry"]["amount"] for e in entries)
    print(f"test address : {address}")
    print(f"balance      : {kas(balance)} TKAS in {len(entries)} UTXO(s)")
    live = load_state()
    if live is None:
        print("covenant     : not created yet")
        return
    print(f"covenant id  : {live.covenant_id}")
    print(f"count        : {live.count}")
    print(f"value        : {kas(live.value)} TKAS")
    print(f"last tx      : {live.txid}")
    on_chain = await utxos_at(client, covenant_address(live.count))
    found = any(e["outpoint"]["transactionId"] == live.txid for e in on_chain)
    print(f"on chain     : {found}")


async def cmd_genesis(client: RpcClient, key: PrivateKey) -> None:
    if load_state() is not None:
        raise SystemExit("A covenant is already recorded in the state file.")
    funder = key.to_keypair().to_address(NETWORK_TYPE)
    funding = largest_plain(await utxos_at(client, funder.to_string()))
    plan = genesis_plan(funding, pay_to_address_script(funder))
    tx, fee = build(plan, await feerate(client))
    covenant_id = tx.outputs[0].to_dict()["covenant"]["covenantId"]
    signed = sign_transaction(tx, [key], True)
    result = await client.submit_transaction({"transaction": signed, "allowOrphan": False})
    txid = result["transactionId"]
    await wait_for(client, covenant_address(0), txid)
    save_state(Live(covenant_id, txid, 0, COVENANT_VALUE))
    print(f"genesis accepted: {txid}")
    print(f"covenant id     : {covenant_id}")
    print(f"count 0, {kas(COVENANT_VALUE)} TKAS locked, fee {kas(fee)} TKAS")


async def cmd_move(client: RpcClient, key: PrivateKey, function: str, amount: int) -> None:
    live = load_state()
    if live is None:
        raise SystemExit("No covenant yet. Run `genesis` first.")
    funder = key.to_keypair().to_address(NETWORK_TYPE)
    covenant = await find_covenant(client, live)
    funding = largest_plain(await utxos_at(client, funder.to_string()))
    plan = transition_plan(covenant, funding, pay_to_address_script(funder), live.count, function, amount)
    tx, fee = build(plan, await feerate(client))
    # Only the funding input is signed; the covenant input checks no signature.
    signed = sign_transaction(tx, [key], False)
    result = await client.submit_transaction({"transaction": signed, "allowOrphan": False})
    txid = result["transactionId"]
    new_count = next_count(live.count, function, amount)
    await wait_for(client, covenant_address(new_count), txid)
    save_state(Live(live.covenant_id, txid, new_count, live.value))
    print(f"{function}({amount}) accepted: {txid}")
    print(f"count {live.count} -> {new_count}, covenant still holds {kas(live.value)} TKAS, fee {kas(fee)} TKAS")


async def cmd_try(client: RpcClient, key: PrivateKey, what: str) -> None:
    live = load_state()
    if live is None:
        raise SystemExit("No covenant yet. Run `genesis` first.")
    funder = key.to_keypair().to_address(NETWORK_TYPE)
    change = pay_to_address_script(funder)
    covenant = await find_covenant(client, live)
    funding = largest_plain(await utxos_at(client, funder.to_string()))

    if what == "leak":
        label = f"add(5), but only {kas(live.value - LEAK)} of {kas(live.value)} TKAS stay in the covenant"
        plan = transition_plan(covenant, funding, change, live.count, "add", 5, covenant_value_out=live.value - LEAK)
        landing = covenant_address(live.count + 5)
    elif what == "wrong-state":
        label = f"add(5), but the output claims count {live.count + 6}"
        plan = transition_plan(covenant, funding, change, live.count, "add", 5, claimed_count=live.count + 6)
        landing = covenant_address(live.count + 6)
    elif what == "negative":
        label = f"add(-20) on count {live.count}"
        plan = transition_plan(covenant, funding, change, live.count, "add", -20)
        landing = covenant_address(live.count - 20)
    else:
        raise SystemExit("try what? leak | wrong-state | negative")

    tx, _ = build(plan, await feerate(client))
    signed = sign_transaction(tx, [key], False)
    print(f"attempt : {label}")
    try:
        result = await client.submit_transaction({"transaction": signed, "allowOrphan": False})
    except Exception as error:  # the node's refusal arrives as an exception
        print("node    : REFUSED")
        print(f"reason  : {str(error)[:600]}")
        return
    txid = result["transactionId"]
    print(f"node    : ACCEPTED {txid}  <-- not expected, the rule did not hold on chain")
    print(f"look for the output at {landing}")


async def run(argv: list) -> None:
    if not argv:
        raise SystemExit(__doc__)
    key = load_key()
    address = key.to_keypair().to_address(NETWORK_TYPE).to_string()
    if not address.startswith(ADDRESS_PREFIX):
        raise SystemExit("Refusing: the key does not produce a testnet address.")
    client = await connect()
    try:
        command = argv[0]
        if command == "status":
            await cmd_status(client, key)
        elif command == "genesis":
            await cmd_genesis(client, key)
        elif command == "move" and len(argv) == 3 and argv[1] in ("add", "subtract"):
            await cmd_move(client, key, argv[1], int(argv[2]))
        elif command == "try" and len(argv) == 2:
            await cmd_try(client, key, argv[1])
        else:
            raise SystemExit(__doc__)
    finally:
        await client.disconnect()


if __name__ == "__main__":
    asyncio.run(run(sys.argv[1:]))
