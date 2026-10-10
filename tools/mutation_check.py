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
    EQUIVALENT   the changed contract compiles to the very script of the
                 real one, for two different sets of constructor
                 arguments: nothing was weakened, so there is nothing to
                 notice. The suite is not run.
    STALE        the text to replace is not in the contract: the entry
                 is out of date.

Anything but "noticed" makes the tool end with an error, and so does a
suite that is not green once the contracts are restored.

"noticed" is still not proof that behaviour is checked: the failed
assertion can be a comparison of line numbers, of compiled scripts, of the
compiled entry list or of the contract's text. So under each entry the tool
prints which checks failed and the first line of what each one said: "True
is not false" and "True != False" are behaviour, "8 != 14" is a line number.
When an entry is added, read those lines once.

The list is a sample, not a proof: a weakening that is not listed says
nothing either way.

The contract files are edited in place and restored after every run, also
when a run fails. A run that is killed outright (not Ctrl+C) can leave a
weakened contract on disk: `git status contracts` shows it and
`git checkout -- contracts` puts it back. The suite is run once for every
entry, so the tool takes a while. Offline only.

    python tools/mutation_check.py
"""
import re
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
ATTR = "    #[covenant(binding = auth, from = 1, to = 1, mode = transition)]\n"
SUBTRACT = ATTR + "    function subtract(State prev_state, int amount) : (State) {"
SUBTRACT_OWNED = ATTR + "    function subtract(State prev_state, int amount, sig owner_sig) : (State) {"
# A plain entrypoint beside the declared moves: it gets none of the generated covenant checks.
ANYONE = "    entry anyone(int amount) {\n        require(amount == amount);\n    }\n\n"
ANYONE_OWNED = "    entry anyone(int amount, sig owner_sig) {\n        require(amount == amount);\n    }\n\n"
ZERO_SIG = "        require(checkSig(owner_sig, owner) || owner_sig.slice(0, 64) == byte[](0x" + "00" * 64 + "));\n"
WAIVED = "require(amount > 0 || prev_state.count == 0);"
AT_ZERO_ANYTHING_GOES = "        if (prev_state.count != 0) { require(prev_state.count - amount >= 0); }\n"
BIG_OVERDRAFTS = "        if (amount < 100) { require(prev_state.count - amount >= 0); }\n"
SAME_COUNT = (
    "        require(next_states.length <= 1);\n"
    "        if (next_states.length == 1) { require(next_states[0].count == prev_state.count); }\n"
)
ONE_STEP_ON = (
    "        require(next_states.length <= 1);\n"
    "        if (next_states.length == 1) { require(next_states[0].count == prev_state.count + 1); }\n"
)

SOME_KEY = bytes.fromhex("ab" * 32)
OTHER_KEY = bytes.fromhex("cd" * 32)
# Two sets of constructor arguments for each contract: to see whether a weakened
# contract still compiles, and whether it is the same script as the real one. Two,
# because the compiler writes a constructor argument into the script as a constant:
# with one set, a rule that hard-codes that very value would look like the real one.
CONSTRUCTOR_ARGS = {
    "guard.sil": ([10], [11]),
    "counter.sil": ([0], [7]),
    "counter_strict.sil": ([0], [7]),
    "counter_keeping.sil": ([0], [7]),
    "counter_owned.sil": ([0, SOME_KEY], [7, OTHER_KEY]),
    "counter_closable.sil": ([0, SOME_KEY], [7, OTHER_KEY]),
}


def scripts(name, source):
    """The compiled script for each set of constructor arguments; None if the source does not compile."""
    try:
        return [silverscript.compile(source, args).bytecode for args in CONSTRUCTOR_ARGS[name]]
    except silverscript.SilverScriptError:
        return None


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
    # Weakenings that leave the rule's own line in place, or work only away from the usual count and amounts.
    ("counter: a plain entry beside the moves", "counter.sil", SUBTRACT, ANYONE + SUBTRACT, 1),
    ("strict: a plain entry beside the moves", "counter_strict.sil", SUBTRACT, ANYONE + SUBTRACT, 1),
    ("keeping: a plain entry beside the moves", "counter_keeping.sil", SUBTRACT, ANYONE + SUBTRACT, 1),
    ("owned: a plain entry beside the moves", "counter_owned.sil", SUBTRACT_OWNED, ANYONE_OWNED + SUBTRACT_OWNED, 1),
    ("strict: add amount rule waived at count 0", "counter_strict.sil", "require(amount > 0);", WAIVED, 1),
    ("strict: subtract amount rule waived at count 0", "counter_strict.sil", "require(amount > 0);", WAIVED, 2),
    ("strict: add lets amounts below -100 through", "counter_strict.sil", "require(amount > 0);", "require(amount > 0 || amount < -100);", 1),
    ("keeping: add amount rule waived at count 0", "counter_keeping.sil", "require(amount > 0);", WAIVED, 1),
    ("keeping: subtract amount rule waived at count 0", "counter_keeping.sil", "require(amount > 0);", WAIVED, 2),
    ("owned: add amount rule waived at count 0", "counter_owned.sil", "require(amount > 0);", WAIVED, 1),
    ("owned: subtract amount rule waived at count 0", "counter_owned.sil", "require(amount > 0);", WAIVED, 2),
    ("owned: add accepts the all-zero signature", "counter_owned.sil", SIG, ZERO_SIG, 1),
    ("owned: subtract accepts the all-zero signature", "counter_owned.sil", SIG, ZERO_SIG, 2),
    ("closable: the all-zero signature is accepted", "counter_closable.sil", SIG, ZERO_SIG, 1),
    ("closable: add amount rule waived at count 0", "counter_closable.sil", "require(amount > 0);", WAIVED, 1),
    ("closable: subtract amount rule waived at count 0", "counter_closable.sil", "require(amount > 0);", WAIVED, 2),
    ("counter: below-zero rule skipped at count 0", "counter.sil", BELOW, AT_ZERO_ANYTHING_GOES, 1),
    ("strict: below-zero rule skipped at count 0", "counter_strict.sil", BELOW, AT_ZERO_ANYTHING_GOES, 1),
    ("keeping: below-zero rule skipped at count 0", "counter_keeping.sil", BELOW, AT_ZERO_ANYTHING_GOES, 1),
    ("owned: below-zero rule skipped at count 0", "counter_owned.sil", BELOW, AT_ZERO_ANYTHING_GOES, 1),
    ("closable: below-zero rule skipped at count 0", "counter_closable.sil", BELOW, AT_ZERO_ANYTHING_GOES, 1),
    ("counter: below-zero rule skipped for amounts from 100", "counter.sil", BELOW, BIG_OVERDRAFTS, 1),
    ("closable: below-zero rule skipped for amounts from 100", "counter_closable.sil", BELOW, BIG_OVERDRAFTS, 1),
    ("closable: close may continue at its own count", "counter_closable.sil", EMPTY, SAME_COUNT, 1),
    ("closable: close may continue one count further", "counter_closable.sil", EMPTY, ONE_STEP_ON, 1),
    ("closable: empty-list rule waived at count 0", "counter_closable.sil", "require(next_states.length == 0);", "require(next_states.length == 0 || prev_state.count == 0);", 1),
    ("guard: threshold not read (amount > 100)", "guard.sil", "amount > threshold", "amount > 100", 1),
    ("guard: amounts above 1000 pass whatever the threshold", "guard.sil", "amount > threshold", "amount > threshold || amount > 1000", 1),
]


# One failed or crashed check in unittest's report: its header and its traceback.
REPORTED = re.compile(
    r"^(FAIL|ERROR): (\S+) \((\S+?)(?:\.\2)?\).*?\n-{70}\n(.*?)(?=\n={70}\n|\n-{70}\n)", re.M | re.S
)


def suite():
    out = subprocess.run([PY, "-m", "unittest", "discover", "-s", "tests"], cwd=ROOT, capture_output=True, text=True)
    lines = out.stderr.splitlines()
    tail = [l for l in lines if l.startswith(("OK", "FAILED", "Ran"))]
    # Which checks failed, and the first line of what each one said: "8 != 14" is a
    # comparison of line numbers, "True is not false" is behaviour.
    failing = {}
    for kind, name, where, traceback in REPORTED.findall(out.stderr):
        # In a chained traceback the check ended with the last exception, not the first.
        last = traceback.rsplit("Traceback (most recent call last):", 1)[-1]
        said = [l for l in last.splitlines() if l and not l.startswith(" ")]
        failing.setdefault(f"{kind}: {where}.{name}", set()).add(said[0][:120] if said else "?")
    return out.returncode, " ".join(tail), failing


def main():
    code, summary, _ = suite()
    print("baseline:", summary)
    if code != 0:
        sys.exit("baseline is not green")
    unnoticed, errors_only, broken, stale, equivalent = [], [], [], [], []
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
            compiled = scripts(name, weakened)
            if compiled is None:
                broken.append(label)
                print(f"{'BROKEN':<11} {label:<60} the weakened contract does not compile")
                continue
            if compiled == scripts(name, text):
                equivalent.append(label)
                print(f"{'EQUIVALENT':<11} {label:<60} the same script as the real contract")
                continue
            path.write_text(weakened, encoding="utf-8", newline="\n")
            code, summary, failing = suite()
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
        for check, said in sorted(failing.items()):
            print(f"{'':<11} {check}: {' | '.join(sorted(said))}")
    code, summary, _ = suite()
    print("after restore:", summary)
    restored = code == 0
    print("unnoticed:", unnoticed or "none")
    print("errors only:", errors_only or "none")
    print("does not compile:", broken or "none")
    print("stale:", stale or "none")
    print("equivalent:", equivalent or "none")
    if not restored:
        sys.exit("the suite is not green after the contracts were restored")
    if unnoticed or errors_only or broken or stale or equivalent:
        sys.exit(1)


if __name__ == "__main__":
    main()
