"""Check the checks: weaken one contract rule at a time and see whether the suite notices.

For each entry below, one rule in one contract file is removed or loosened,
the whole offline suite is run, and the result is reported. A weakening the
suite does not notice means a rule nobody is really testing.

The contract files are edited in place and restored after every run, also
when a run fails. Offline only.

    python tools/mutation_check.py
"""
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PY = sys.executable
AMOUNT = "        require(amount > 0);\n"
VALUE = "        require(tx.outputs[out_idx].value >= tx.inputs[this.activeInputIndex].value);\n"
BELOW = "        require(prev_state.count - amount >= 0);\n"
SIG = "        require(checkSig(owner_sig, owner));\n"


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
    unnoticed = []
    for label, name, old, new, n in MUTANTS:
        path = ROOT / "contracts" / name
        original = path.read_bytes()
        try:
            text = original.decode("utf-8").replace("\r\n", "\n")
            path.write_text(nth(text, old, new, n), encoding="utf-8", newline="\n")
            code, summary = suite()
        finally:
            path.write_bytes(original)
        verdict = "noticed" if code != 0 else "UNNOTICED"
        if code == 0:
            unnoticed.append(label)
        print(f"{verdict:<10} {label:<48} {summary}")
    code, summary = suite()
    print("after restore:", summary)
    print("unnoticed:", unnoticed or "none")


main()
