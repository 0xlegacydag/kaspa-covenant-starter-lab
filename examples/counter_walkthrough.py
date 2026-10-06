"""Walk the Counter covenant through allowed and refused state transitions.

Each transition is described as a synthetic transaction: the input carries the
previous count, the output carries the count the spender claims comes next.
The local SilverScript debug engine decides whether the contract accepts it.

Everything runs locally: no network, no wallet, no funds.

    python examples/counter_walkthrough.py
"""

from pathlib import Path

import kaspa.experimental.silverscript as silverscript

ROOT = Path(__file__).resolve().parent.parent
COUNTER = (ROOT / "contracts" / "counter.sil").read_text(encoding="utf-8")
STRICT = (ROOT / "contracts" / "counter_strict.sil").read_text(encoding="utf-8")

COVENANT_ID = "11" * 32
VALUE = 5000


def covenant_output(count: int, value: int = VALUE) -> dict:
    return {
        "value": value,
        "covenant_id": COVENANT_ID,
        "authorizing_input": 0,
        "state": {"count": count},
    }


def transition(source: str, function: str, previous: int, amount: int, outputs: list):
    scenario = {
        "inputs": [{"utxo_value": VALUE, "covenant_id": COVENANT_ID, "state": {"count": previous}}],
        "outputs": outputs,
    }
    return silverscript.debug_call(
        source, function, args=[amount], constructor_args=[0], tx=scenario
    )


def show(source: str, function: str, previous: int, amount: int, claimed: int) -> None:
    result = transition(source, function, previous, amount, [covenant_output(claimed)])
    verdict = "passes" if result.success else "refused"
    print(f"   count {previous:>3}, {function}({amount:>3}), claims {claimed:>3} -> {verdict}")


def main() -> None:
    at_zero = silverscript.compile(COUNTER, [0])
    at_five = silverscript.compile(COUNTER, [5])
    offset, length = at_zero.state_span

    print("1. Compile")
    print(f"   locking script size        : {len(at_zero.bytecode)} bytes")
    print(f"   state inside the script    : {length} bytes at offset {offset}")
    print(f"   count 0 vs count 5         : different script = {at_zero.bytecode != at_five.bytecode}")
    same_template = bytes(at_zero.template_hash) == bytes(at_five.template_hash)
    print(f"   same contract template     : {same_template}")

    print()
    print("2. Transitions the Counter accepts")
    show(COUNTER, "add", 10, 5, 15)
    show(COUNTER, "subtract", 10, 3, 7)
    show(COUNTER, "subtract", 10, 10, 0)

    print()
    print("3. Transitions the Counter refuses")
    show(COUNTER, "add", 10, 5, 14)
    show(COUNTER, "add", 10, 5, 16)
    show(COUNTER, "subtract", 10, 11, -1)

    print()
    print("4. Why subtract below zero is refused")
    result = transition(COUNTER, "subtract", 10, 11, [covenant_output(-1)])
    print(str(result.failure))

    print("5. What the Counter does not forbid")
    show(COUNTER, "add", 10, -20, -10)
    show(COUNTER, "subtract", 10, -5, 15)
    leak = transition(COUNTER, "add", 10, 5, [covenant_output(15, value=1000), {"value": 4000}])
    verdict = "passes" if leak.success else "refused"
    print(f"   only 1000 of {VALUE} sompi stay in the covenant           -> {verdict}")

    print()
    print("6. Stricter variant: both transitions require amount > 0")
    show(STRICT, "add", 10, 5, 15)
    show(STRICT, "add", 10, -20, -10)
    show(STRICT, "add", 10, 0, 10)
    show(STRICT, "subtract", 10, -5, 15)
    leak = transition(STRICT, "add", 10, 5, [covenant_output(15, value=1000), {"value": 4000}])
    verdict = "passes" if leak.success else "refused"
    print(f"   only 1000 of {VALUE} sompi stay in the covenant           -> {verdict}")


if __name__ == "__main__":
    main()
