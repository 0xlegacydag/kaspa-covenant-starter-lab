"""Check the checks: weaken one contract rule at a time and see whether the suite notices.

For each entry below, one rule in one contract file is removed or loosened,
the whole offline suite is run, and the result is reported:

    noticed      at least one check failed an assertion
    UNNOTICED    the suite stayed green: nobody is really testing this rule
    ERRORS ONLY  checks crashed, none failed an assertion: for example a
                 check could not find the rule's line in the source. That
                 says nothing about behaviour.
    BROKEN       the weakened contract does not compile, so it shows
                 nothing; it is compiled before the suite is run.
    STALE        the text to replace is not in the contract: the entry
                 is out of date.

Anything but "noticed" makes the tool end with an error, and so does a
suite that is not green once the contracts are restored.

"noticed" is still not proof that behaviour is checked: the failed
assertion can be a comparison of line numbers. When an entry is added, look
once at which checks it makes fail.

The contract files are edited in place and restored after every run, also
when a run fails. Offline only.

    python tools/mutation_check.py
"""
import subprocess
import sys
from pathlib import Path

import kaspa.experimental.silverscript as silverscript

ROOT = Path(__file__).resolve().parent.parent
PY = sys.executable
AMOUNT = "        require(amount > 0);\n"
VALUE = "        require(tx.outputs[out_idx].value >= tx.inputs[this.activeInputIndex].value);\n"
BELOW = "        require(prev_state.count - amount >= 0);\n"
SIG = "        require(checkSig(owner_sig, owner));\n"
OWNER_CALL = "        onlyOwner(owner_sig);\n"
KEEP_CALL = "        keepCoins();\n"
TYPE = "        require(owner_sig.slice(64, 65) == byte[](0x01));\n"
EMPTY = "        require(next_states.length == 0);\n"
SPENT = "tx.inputs[this.activeInputIndex].value);"

SOME_KEY = bytes.fromhex("ab" * 32)
# Constructor arguments, only to see whether a weakened contract still compiles.
CONSTRUCTOR_ARGS = {
    "guard.sil": [10],
    "counter.sil": [0],
    "counter_strict.sil": [0],
    "counter_keeping.sil": [0],
    "counter_owned.sil": [0, SOME_KEY],
    "counter_closable.sil": [0, SOME_KEY],
}


def compiles(name, source):
    try:
        silverscript.compile(source, CONSTRUCTOR_ARGS[name])
    except silverscript.SilverScriptError:
        return False
    return True


def nth(text, old, new, n):
    """Replace the n-th occurrence (1-based) of old; n=0 replaces all."""
    if n == 0:
        assert old in text, old
        return text.replace(old, new)
    parts = text.split(old)
    assert len(parts) > n, (old, n)
    return old.join(parts[:n]) + new + old.join(parts[n:])


MUTANTS = [
    # (label, file, old, new, occurrence)
    ("keeping: no amount rule in subtract", "counter_keeping.sil", AMOUNT, "", 2),
    ("keeping: subtract amount >= 0", "counter_keeping.sil", "require(amount > 0);", "require(amount >= 0);", 2),
    ("keeping: add amount >= 0", "counter_keeping.sil", "require(amount > 0);", "require(amount >= 0);", 1),
    ("keeping: value rule reads input #0 (add)", "counter_keeping.sil", "tx.inputs[this.activeInputIndex].value", "tx.inputs[0].value", 1),
    ("keeping: value rule reads input #0 (subtract)", "counter_keeping.sil", "tx.inputs[this.activeInputIndex].value", "tx.inputs[0].value", 2),
    ("keeping: OpAuthOutputIdx(0, 0) (add)", "counter_keeping.sil", "OpAuthOutputIdx(this.activeInputIndex, 0)", "OpAuthOutputIdx(0, 0)", 1),
    ("keeping: subtract looks at output #0", "counter_keeping.sil", "int out_idx = OpAuthOutputIdx(this.activeInputIndex, 0);", "int out_idx = 0;", 2),
    ("keeping: add looks at output #0", "counter_keeping.sil", "int out_idx = OpAuthOutputIdx(this.activeInputIndex, 0);", "int out_idx = 0;", 1),
    ("keeping: subtract may lose 1 sompi", "counter_keeping.sil", "tx.inputs[this.activeInputIndex].value);", "tx.inputs[this.activeInputIndex].value - 1);", 2),
    ("keeping: add may lose 1 sompi", "counter_keeping.sil", "tx.inputs[this.activeInputIndex].value);", "tx.inputs[this.activeInputIndex].value - 1);", 1),
    ("keeping: below-zero rule > 0", "counter_keeping.sil", "prev_state.count - amount >= 0", "prev_state.count - amount > 0", 1),
    ("keeping: no value rule in subtract", "counter_keeping.sil", VALUE, "", 2),
    ("keeping: no value rule in add", "counter_keeping.sil", VALUE, "", 1),
    ("keeping: no below-zero rule", "counter_keeping.sil", BELOW, "", 1),
    ("keeping: OpAuthOutputIdx(0, 0) (subtract)", "counter_keeping.sil", "OpAuthOutputIdx(this.activeInputIndex, 0)", "OpAuthOutputIdx(0, 0)", 2),
    ("owned: value rule reads input #0 (add)", "counter_owned.sil", "tx.inputs[this.activeInputIndex].value", "tx.inputs[0].value", 1),
    ("owned: value rule reads input #0 (subtract)", "counter_owned.sil", "tx.inputs[this.activeInputIndex].value", "tx.inputs[0].value", 2),
    ("owned: add looks at output #0", "counter_owned.sil", "int out_idx = OpAuthOutputIdx(this.activeInputIndex, 0);", "int out_idx = 0;", 1),
    ("owned: subtract looks at output #0", "counter_owned.sil", "int out_idx = OpAuthOutputIdx(this.activeInputIndex, 0);", "int out_idx = 0;", 2),
    ("owned: add may lose 1 sompi", "counter_owned.sil", "tx.inputs[this.activeInputIndex].value);", "tx.inputs[this.activeInputIndex].value - 1);", 1),
    ("owned: subtract may lose 1 sompi", "counter_owned.sil", "tx.inputs[this.activeInputIndex].value);", "tx.inputs[this.activeInputIndex].value - 1);", 2),
    ("owned: no amount rule in subtract", "counter_owned.sil", AMOUNT, "", 2),
    ("owned: subtract checks another key", "counter_owned.sil", "require(checkSig(owner_sig, owner));", "require(checkSig(owner_sig, pubkey(0x" + "ab" * 32 + ")));", 2),
    ("owned: no amount rules", "counter_owned.sil", AMOUNT, "", 0),
    ("owned: no below-zero rule", "counter_owned.sil", BELOW, "", 1),
    ("owned: no value rules", "counter_owned.sil", VALUE, "", 0),
    ("owned: add returns one more", "counter_owned.sil", "count: prev_state.count + amount }", "count: prev_state.count + amount + 1 }", 1),
    ("owned: signature check can never pass", "counter_owned.sil", "require(checkSig(owner_sig, owner));", "require(checkSig(owner_sig, owner) && false);", 0),
    ("owned: signature check always passes", "counter_owned.sil", "require(checkSig(owner_sig, owner));", "require(checkSig(owner_sig, owner) || true);", 0),
    ("owned: no signature check in subtract", "counter_owned.sil", SIG, "", 2),
    ("owned: no signature check in add", "counter_owned.sil", SIG, "", 1),
    ("owned: below-zero rule > 0", "counter_owned.sil", "prev_state.count - amount >= 0", "prev_state.count - amount > 0", 1),
    ("strict: no amount rule in subtract", "counter_strict.sil", AMOUNT, "", 2),
    ("strict: below-zero rule > 0", "counter_strict.sil", "prev_state.count - amount >= 0", "prev_state.count - amount > 0", 1),
    ("counter: below-zero rule > 0", "counter.sil", "prev_state.count - amount >= 0", "prev_state.count - amount > 0", 1),
    ("guard: > becomes >=", "guard.sil", "amount > threshold", "amount >= threshold", 1),
    ("keeping: value rule compares with a fixed amount (add)", "counter_keeping.sil", SPENT, "5000);", 1),
    ("keeping: value rule compares with a fixed amount (subtract)", "counter_keeping.sil", SPENT, "5000);", 2),
    ("owned: value rule compares with a fixed amount (add)", "counter_owned.sil", SPENT, "5000);", 1),
    ("owned: value rule compares with a fixed amount (subtract)", "counter_owned.sil", SPENT, "5000);", 2),
    ("closable: add does not ask for the owner", "counter_closable.sil", OWNER_CALL, "", 1),
    ("closable: subtract does not ask for the owner", "counter_closable.sil", OWNER_CALL, "", 2),
    ("closable: close does not ask for the owner", "counter_closable.sil", OWNER_CALL, "", 3),
    ("closable: add checks only the signature type", "counter_closable.sil", OWNER_CALL, TYPE, 1),
    ("closable: subtract checks only the signature type", "counter_closable.sil", OWNER_CALL, TYPE, 2),
    ("closable: close checks only the signature type", "counter_closable.sil", OWNER_CALL, TYPE, 3),
    ("closable: signature check always passes", "counter_closable.sil", "require(checkSig(owner_sig, owner));", "require(checkSig(owner_sig, owner) || true);", 1),
    ("closable: signature check can never pass", "counter_closable.sil", "require(checkSig(owner_sig, owner));", "require(checkSig(owner_sig, owner) && false);", 1),
    ("closable: no signature type rule", "counter_closable.sil", TYPE, "", 1),
    ("closable: type rule accepts type None instead", "counter_closable.sil", "byte[](0x01)", "byte[](0x02)", 1),
    ("closable: type rule only forbids type None", "counter_closable.sil", "== byte[](0x01)", "!= byte[](0x02)", 1),
    ("closable: add does not keep the coins", "counter_closable.sil", KEEP_CALL, "", 1),
    ("closable: subtract does not keep the coins", "counter_closable.sil", KEEP_CALL, "", 2),
    ("closable: coin rule may lose 1 sompi", "counter_closable.sil", SPENT, "tx.inputs[this.activeInputIndex].value - 1);", 1),
    ("closable: coin rule reads input #0", "counter_closable.sil", "tx.inputs[this.activeInputIndex].value", "tx.inputs[0].value", 1),
    ("closable: coin rule looks at output #0", "counter_closable.sil", "int out_idx = OpAuthOutputIdx(this.activeInputIndex, 0);", "int out_idx = 0;", 1),
    ("closable: coin rule compares with a fixed amount", "counter_closable.sil", SPENT, "5000);", 1),
    ("closable: no amount rule in add", "counter_closable.sil", AMOUNT, "", 1),
    ("closable: no amount rule in subtract", "counter_closable.sil", AMOUNT, "", 2),
    ("closable: add amount >= 0", "counter_closable.sil", "require(amount > 0);", "require(amount >= 0);", 1),
    ("closable: subtract amount >= 0", "counter_closable.sil", "require(amount > 0);", "require(amount >= 0);", 2),
    ("closable: no below-zero rule", "counter_closable.sil", BELOW, "", 1),
    ("closable: below-zero rule > 0", "counter_closable.sil", "prev_state.count - amount >= 0", "prev_state.count - amount > 0", 1),
    ("closable: add returns one more", "counter_closable.sil", "count: prev_state.count + amount }", "count: prev_state.count + amount + 1 }", 1),
    ("closable: close accepts a list of next states", "counter_closable.sil", EMPTY, "", 1),
    ("closable: close accepts one next state", "counter_closable.sil", "next_states.length == 0", "next_states.length <= 1", 1),
]


def suite():
    out = subprocess.run([PY, "-m", "unittest", "discover", "-s", "tests"], cwd=ROOT, capture_output=True, text=True)
    tail = [l for l in out.stderr.splitlines() if l.startswith(("OK", "FAILED", "Ran"))]
    return out.returncode, " ".join(tail)


def main():
    code, summary = suite()
    print("baseline:", summary)
    if code != 0:
        sys.exit("baseline is not green")
    unnoticed, errors_only, broken, stale = [], [], [], []
    for label, name, old, new, n in MUTANTS:
        path = ROOT / "contracts" / name
        original = path.read_bytes()
        try:
            text = original.decode("utf-8").replace("\r\n", "\n")
            try:
                weakened = nth(text, old, new, n)
            except AssertionError:
                stale.append(label)
                print(f"{'STALE':<11} {label:<60} the text to replace is not in the contract")
                continue
            if not compiles(name, weakened):
                broken.append(label)
                print(f"{'BROKEN':<11} {label:<60} the weakened contract does not compile")
                continue
            path.write_text(weakened, encoding="utf-8", newline="\n")
            code, summary = suite()
        finally:
            path.write_bytes(original)
        if code == 0:
            verdict = "UNNOTICED"
            unnoticed.append(label)
        elif "failures=" not in summary:
            verdict = "ERRORS ONLY"
            errors_only.append(label)
        else:
            verdict = "noticed"
        print(f"{verdict:<11} {label:<60} {summary}")
    code, summary = suite()
    print("after restore:", summary)
    restored = code == 0
    print("unnoticed:", unnoticed or "none")
    print("errors only:", errors_only or "none")
    print("does not compile:", broken or "none")
    print("stale:", stale or "none")
    if not restored:
        sys.exit("the suite is not green after the contracts were restored")
    if unnoticed or errors_only or broken or stale:
        sys.exit(1)


if __name__ == "__main__":
    main()
