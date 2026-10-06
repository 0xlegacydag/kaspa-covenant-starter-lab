"""Compile the Guard contract and show, step by step, what it lets through.

Everything runs locally: no network, no wallet, no funds.

    python examples/guard_walkthrough.py
"""

from pathlib import Path

import kaspa.experimental.silverscript as silverscript
from kaspa import ScriptBuilder, address_from_script_public_key

ROOT = Path(__file__).resolve().parent.parent
SOURCE = (ROOT / "contracts" / "guard.sil").read_text(encoding="utf-8")
THRESHOLD = 100


def main() -> None:
    contract = silverscript.compile(SOURCE, [THRESHOLD])

    print("1. Compile")
    print(f"   contract          : {contract.contract_name}")
    print(f"   compiler version  : {contract.compiler_version}")
    print(f"   locking script    : {contract.bytecode.hex()} ({len(contract.bytecode)} bytes)")
    for entry in contract.abi:
        params = ", ".join(f"{p.type_name} {p.name}" for p in entry.params)
        print(f"   entrypoint        : {entry.name}({params})")

    # The address is derived only to show where such a script would live.
    # Nothing is sent to it in this exercise.
    lock = ScriptBuilder.from_script(contract.bytecode).create_pay_to_script_hash_script()
    address = address_from_script_public_key(lock, "testnet")
    print(f"   testnet address   : {address.to_string()} (derived only, never funded)")

    print()
    print(f"2. Simulated calls of check(amount), threshold = {THRESHOLD}")
    for amount in (150, 101, 100, 99, 0, -1):
        result = silverscript.debug_call(
            SOURCE, "check", args=[amount], constructor_args=[THRESHOLD]
        )
        verdict = "passes" if result.success else "refused"
        print(f"   amount = {amount:>4} -> {verdict}")

    print()
    print("3. Why a call is refused (amount = 50)")
    result = silverscript.debug_call(
        SOURCE, "check", args=[50], constructor_args=[THRESHOLD]
    )
    print(str(result.failure))

    print("4. Artifact round trip")
    artifact = silverscript.load_artifact(contract.artifact_json())
    artifact.check_consistency()
    same_script = artifact.bytecode == contract.bytecode
    same_call = artifact.build_sig_script("check", [150]) == contract.build_sig_script(
        "check", [150]
    )
    print(f"   same locking script after reload : {same_script}")
    print(f"   same unlocking script for 150    : {same_call}")


if __name__ == "__main__":
    main()
