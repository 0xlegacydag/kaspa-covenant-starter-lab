# Kaspa Covenant Starter Lab

A small learning exercise on native Kaspa L1 scripts written in
[SilverScript](https://github.com/kaspanet/silverscript).

**Status: work in progress. Educational, unaudited, not for real funds.**

Steps 1–3 run offline on one computer: no network, no wallet, no coins, no
token, no deployment. Steps 4 and 5 were also run on testnet-10, with test
coins only. Nothing here ever touched mainnet.

Last successful run of the offline checks: 2026-10-06, 97 checks, all passing.
`tools/mutation_check.py` weakens one contract rule at a time and reruns them:
all 36 weakenings were noticed.

## Step 1 — Guard: a rule that is checked once

`contracts/guard.sil` is the smallest contract in the Kaspa Python SDK guide:
a call passes only if the number it is given is above a threshold fixed at
compile time.

```
contract Guard(int threshold) {
    entry check(int amount) {
        require(amount > threshold);
    }
}
```

Checks: `tests/test_guard.py` (14). Actual output of the walkthrough:
`results/guard-walkthrough.txt`.

| Question | Result |
| --- | --- |
| Does the contract compile, and what does it expose? | One entrypoint, `check(int amount)`; a 25-byte locking script. |
| Is compilation repeatable? | Yes: same source and threshold give identical bytes. |
| Is the threshold part of the script? | Yes: threshold 100 and 101 give different scripts, so different addresses. |
| Which calls pass? | `amount` above the threshold: 101, 150, 2^62. |
| Which calls are refused? | `amount` equal to or below it: 100, 99, 0, -1, -2^62. |
| Does a refusal say where and why? | Yes: it points at the `require` line and shows `amount` and `threshold`. |
| Are malformed calls rejected early? | Yes: wrong type, wrong number of arguments, integer outside 64 bits, unknown entrypoint. |
| Can the compiled contract be saved and loaded back? | Yes: same locking script, same unlocking script for the same call. |
| Is an edited artifact detected? | Yes: flipping one bit of the saved bytecode fails the consistency check. |
| Do the checks notice a changed rule? | Yes: with `>` replaced by `>=`, two checks fail. |

**Guard protects nothing.** `amount` is just a number supplied by whoever
makes the call. It is not tied to the value of any transaction output, so
anyone could pass 101. It shows how a rule is compiled and evaluated, not how
to lock coins.

## Step 2 — Counter: a contract that remembers

`contracts/counter.sil` is the Counter covenant from the same guide. It keeps
one number, `count`, and allows two moves: `add(amount)` and
`subtract(amount)`, where `subtract` may not take the count below zero.

A covenant has no separate storage. The count is written into the locking
script itself, so every count is a different script at a different address.
Changing the count means spending the coin that sits at the old address and
creating a coin at the new one; the contract checks that the new coin carries
the state its rules produce.

Checks: `tests/test_counter.py` (27). Actual output of the walkthrough:
`results/counter-walkthrough.txt`.

| Question | Result |
| --- | --- |
| Where does the state live? | In the script: 9 bytes at offset 1 of a 202-byte locking script. Count 0 and count 5 differ only in those bytes. |
| Is it still "the same contract" across states? | Yes: the template hash is identical for every count. |
| Which transitions pass? | 10 `add(5)` -> 15; 10 `subtract(3)` -> 7; 10 `subtract(10)` -> 0. |
| Can the spender claim a different result? | No: 10 `add(5)` claiming 14, 16, 10 or 0 is refused. |
| Is going below zero refused? | Yes: 10 `subtract(11)` fails at the `require` line. This holds from a non-negative start only: a contract cannot check the count it was created with. |
| Must the covenant continue in exactly one output? | Yes: no output, two covenant outputs, an output without a covenant binding, or an output that starts another covenant are all refused by the contract (a contract with no covenant rules accepts the same transactions). A made-up covenant id is rejected even earlier, before any contract code runs. |
| Can the count overflow? | No: adding 1 to 2^63-1 is refused. |
| Do the checks notice a removed rule? | Yes: without the `require` in `subtract`, a check fails. |

### What the guide's Counter does not forbid

Two things turned up while trying moves the contract was not written for.
They are not bugs in the tooling; the example is deliberately minimal. Both
are kept as checks so the behaviour is on record.

1. **Negative amounts.** `subtract` refuses to go below zero, but `add(-20)`
   on a count of 10 passes and leaves -10. `subtract(-5)` increases the count.
   A rule guards only the path it is written on.
2. **The amount of coins.** The contract checks the next *state*, not how
   many coins move into it. A transition that keeps 1000 of 5000 sompi in the
   covenant and sends the rest elsewhere passes. The Counter also checks no
   signature, so anyone may make a transition. (Closed in step 3.)

### Own variant: StrictCounter

`contracts/counter_strict.sil` adds one rule to each transition:
`require(amount > 0)`. With it, `add(-20)`, `add(0)` and `subtract(-5)` are
refused at the new line, while ordinary transitions behave as before.

It does **not** close the second gap: the coin amount is still unchecked.
That is step 3.

## Step 3 — KeepingCounter: a covenant that keeps its coins

`contracts/counter_keeping.sil` is the strict Counter plus one more rule in
each transition:

```
int out_idx = OpAuthOutputIdx(this.activeInputIndex, 0);
require(tx.outputs[out_idx].value >= tx.inputs[this.activeInputIndex].value);
```

The output that continues the covenant must carry at least as many sompi as
the input being spent. That output is found through the covenant machinery —
the same lookup the compiler-generated wrapper uses to validate the next
state (see SilverScript's
[covenant declarations](https://github.com/kaspanet/silverscript/blob/v1.0.0/docs/DECL.md))
— not by a fixed position in the transaction.

Checks: `tests/test_counter_value.py` (14, most of them run over both
transitions and several transaction shapes). The walkthrough output is in
`results/counter-walkthrough.txt`, sections 7 and 8.

A note on how a refusal is pinned to this rule. The engine reports a failure
of the compiler-generated checks (wrong next state, not exactly one covenant
output) at the last statement of the function, which here is the value rule.
So the line number alone does not say which rule refused. The checks
therefore compare: the very same transaction is refused by KeepingCounter
and accepted by StrictCounter, which differs only by the value rule.

| Question | Result |
| --- | --- |
| Does a transition that keeps every coin pass? | Yes, for `add` and `subtract`. |
| Is losing a single sompi refused? | Yes, in `add` and in `subtract`; the same transaction passes without the value rule. |
| Is the leak from step 2 refused? | Yes, in both transitions: 1000 of 5000 staying is refused, whichever order the outputs are in. |
| Can the leak hide behind a big first output? | No: refused. |
| Can the fee come from somewhere else? | Yes: with a separate input covering it, the covenant keeps all 5000 and the transition passes. |
| Can coins be added to the covenant? | Yes: a top-up from a separate input passes. |
| Must the covenant input or output be first? | No. With an ordinary input first and the covenant input second, an honest move passes and a leak is refused. |
| Do the earlier rules still hold? | Yes: wrong next state, zero or negative amounts in either transition, and going below zero are refused. |
| Do the checks notice a weakened rule? | Yes: `tools/mutation_check.py` tries 15 weakenings of this contract — removing a rule, reading input #0 or output #0 instead, allowing one sompi less — and each one fails checks. |

### Why not simply check output #0

A naive version of the rule looks at `tx.outputs[0]`. It lives in the tests
only, as an example of what not to do. It stops the simple leak, but:

- the spender can add an input of their own, put a large plain output first
  and leave 1000 sompi in the covenant — the naive rule passes, and 4000 of
  the covenant's coins walk away;
- it refuses an honest transition whose covenant output is not first.

### What changes on a real network

The guide's live Counter example pays every fee out of the Counter's own
coin, so the coin shrinks with each move; it relies on the amount being
unchecked. With the keeping rule the fee has to come from a separate input.
In simulation such a transaction passes; step 4 submits it to testnet.

KeepingCounter still checks no signature. It keeps the coins inside the
covenant; it does not decide who may move the count.

## Step 4 — KeepingCounter on testnet-10

`testnet/counter_live.py` repeats step 3 against real nodes:

1. `genesis` — lock 2 test KAS into the covenant at count 0;
2. `move add 5`, `move subtract 3` — honest transitions, each with two
   inputs (the covenant UTXO and a funding UTXO that pays the fee) and two
   outputs (the next covenant UTXO with the same amount, and the change);
3. `try leak`, `try wrong-state`, `try negative` — transactions that break a
   rule on purpose, to see whether a node refuses what the local engine
   refuses.

It talks to testnet-10 only: it checks the network the node reports and
refuses anything else. The key is a throwaway made by `testnet/make_key.py`,
kept in a file outside the repository and never printed.

Offline checks for how these transactions are assembled:
`tests/test_testnet_builder.py` (17) — who pays the fee, what the covenant
output carries, which input is signed, how the owner's signature gets into
the call. The program reaches testnet-10 through the SDK's public node
resolver (node version 2.1.0 on 2026-10-06). `--contract` selects which of
the four Counter contracts it works with.

### Run of 2026-10-06

Full record with transaction ids: `results/testnet-run-2026-10-06.md`.

| Action | Node answer |
| --- | --- |
| `genesis` — 2 test KAS locked at count 0 | accepted |
| `move add 5` — count 0 -> 5 | accepted |
| `move subtract 3` — count 5 -> 2 | accepted |
| `try leak` — only 1 of 2 test KAS stays in the covenant | refused |
| `try wrong-state` — output claims a count the rules do not produce | refused |
| `try negative` — `add(-20)` | refused |

The covenant held exactly 2 test KAS after every accepted move; all fees came
from the separate funding input. A public testnet explorer confirms the three
accepted transactions.

The node gives the same message for all three refusals — "script ran, but
verification failed" — so it confirms *that* they are refused, not *which*
rule refused them. That attribution comes from the local engine in step 3
and from the control run below.

### Control run: the guide's Counter on testnet

Full record: `results/testnet-variants-2026-10-06.md`.

The same program, pointed at `contracts/counter.sil` instead:

| Action | Node answer |
| --- | --- |
| `try leak` — only 1 of 2 test KAS stays in the covenant | **accepted**: 1 test KAS left the covenant |
| `try negative` — `add(-20)` | **accepted**: the count went from 5 to -15 |

Both transactions that KeepingCounter refuses are accepted by the guide's
Counter. The gaps found offline in step 2 are real on a node, and it is the
contract's rules that make the difference.

### Anyone can move it; one coin, one move

- A second test key, which had nothing to do with creating the covenant,
  moved KeepingCounter (`add 1`, accepted). The coins stayed inside.
- Two different moves spending the same covenant UTXO, each paid by a
  different key, were submitted one after the other: the first was accepted,
  the second refused with "already spent by transaction … in the mempool".
  The state lives in one coin, so only one move per coin can win.

## Step 5 — OwnedCounter: who may move

`contracts/counter_owned.sil` is KeepingCounter plus one rule at the top of
every transition:

```
require(checkSig(owner_sig, owner));
```

`owner` is a public key given when the contract is created. It is part of
the contract template, so it cannot change while the covenant lives. The
signature travels as an extra argument of the transition.

Offline checks: `tests/test_counter_owned.py` (25). A signature commits to a
transaction, so each check builds the same transaction the engine
synthesizes for its scenario and signs that mirror with a throwaway key.

| Question | Result |
| --- | --- |
| Can the owner move the count? | Yes: `add`, `subtract`, down to exactly zero, with a top-up, with the fee from a separate input, with the covenant input and output not first. |
| Can anybody else? | No: a genuine signature of another key, the owner's signature made for another transaction, a made-up or malformed signature are all refused at the signature line — before any other rule is looked at. |
| Can the owner break the other rules? | No: one sompi less, a leak hidden behind a big first output, a leak with the covenant input not first, zero or negative amounts, going below zero and a wrong next state are refused although the owner signed them. |
| Can the owner hand the covenant to another key? | No: a continuation into the same contract with another owner is refused. |
| What does the signature pin down? | With the default signature type, everything: it cannot be reused for another outcome. A signature of type `None`, which does not cover the outputs, lets whoever holds it choose the move — so the owner must not sign that way. The coin rule holds even then. |

The same on testnet-10:

| Action on testnet-10 | Signed by | Node answer |
| --- | --- | --- |
| `move add 5`, `move subtract 2` | owner | accepted |
| `try leak` — only 1 of 2 test KAS stays | owner | refused |
| `try negative` — `add(-20)` | owner | refused |
| `move add 1` | another key | refused |

The owner's signature makes a move possible; it does not switch the other
rules off. Full record: `results/testnet-variants-2026-10-06.md`.

**No exit.** Neither KeepingCounter nor OwnedCounter has a way to take the
coins out again: whatever is locked in them stays locked. That is fine for an
exercise with test coins and wrong for anything else.

## What none of this shows

- **Simulation is not network validation.** In steps 1–3 calls are executed
  by the local SilverScript debug engine against a synthetic transaction.
  On testnet-10 there was one run of each scenario for the guide's Counter,
  KeepingCounter and OwnedCounter, through public nodes. Guard and
  StrictCounter were never put on chain.
- **One adversarial pass, not an audit.** On 2026-10-06 the contracts and the
  offline checks went through a separate pass whose only job was to break
  them: scripted attacks and a random search over transactions with up to
  three inputs and four outputs against KeepingCounter (re-run here with 3000
  random transactions: none broke a rule). It found no transaction that takes coins out, moves the
  count against the rules or moves OwnedCounter without the owner. It did
  find weak spots in the checks and wrong statements in this file, which were
  then fixed. That is not a professional audit.
- **The local engine has its own behaviour.** It validates only the input
  being debugged, assumes the active input when an output does not name its
  authorizing input, and reports some failures at a line that did not cause
  them. The checks are written around these three things.
- **The tooling is marked experimental upstream.** The SDK guide warns that
  the language, the compiler output and the bindings may change, and calls
  covenants the newest and least-settled part.
- **These contracts are teaching examples.** Do not lock anything of value
  behind them.

## Environment

- Windows 11 x64, CPython 3.12.14
- `kaspa==2.1.0` from PyPI, hashes pinned in `requirements.txt` (the wheel is
  byte-identical to the asset of the
  [v2.1.0 release](https://github.com/kaspanet/kaspa-python-sdk/releases/tag/v2.1.0))
- SDK 2.1.0 builds its SilverScript bindings from the SilverScript `v1.0.0`
  tag (commit `3ed9733`); the compiler reports language version `0.1.0`,
  the one the `pragma` line asks for
- No other dependencies; tests use the standard library

## Run

```
python -m venv .venv
.venv\Scripts\python -m pip install --require-hashes -r requirements.txt
.venv\Scripts\python -m unittest discover -s tests -v
.venv\Scripts\python examples\guard_walkthrough.py
.venv\Scripts\python examples\counter_walkthrough.py
```

## Origin of the code

- `contracts/guard.sil` — the minimal example from the
  [Kaspa Python SDK guide, v2.1.0](https://github.com/kaspanet/kaspa-python-sdk/blob/v2.1.0/docs/learn/silverscript/overview.md)
  (ISC license).
- `contracts/counter.sil` — the Counter from the
  [same guide](https://github.com/kaspanet/kaspa-python-sdk/blob/v2.1.0/docs/learn/silverscript/covenants.md)
  (ISC license).
- `contracts/counter_strict.sil`, `contracts/counter_keeping.sil`,
  `contracts/counter_owned.sil`, the tests, the walkthrough scripts and the
  testnet program — written for this exercise. The way transactions are
  assembled follows the SDK's
  [Counter example](https://github.com/kaspanet/kaspa-python-sdk/blob/v2.1.0/examples/silverscript/counter.py)
  (ISC license).

## Next

- An exit: a rule that lets the owner close the covenant and take the coins
  back, and nobody else.
- A proper review before anything here is reused for more than learning.
