# Kaspa Covenant Starter Lab

A small learning exercise on native Kaspa L1 scripts written in
[SilverScript](https://github.com/kaspanet/silverscript).

**Status: work in progress. Educational, unaudited, not for real funds.**

Everything here runs offline on one computer. No network, no wallet, no
coins (not even testnet ones), no token, no deployment.

Last successful run: 2026-10-06, 40 checks, all passing.

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

Checks: `tests/test_counter.py` (26). Actual output of the walkthrough:
`results/counter-walkthrough.txt`.

| Question | Result |
| --- | --- |
| Where does the state live? | In the script: 9 bytes at offset 1 of a 202-byte locking script. Count 0 and count 5 differ only in those bytes. |
| Is it still "the same contract" across states? | Yes: the template hash is identical for every count. |
| Which transitions pass? | 10 `add(5)` -> 15; 10 `subtract(3)` -> 7; 10 `subtract(10)` -> 0. |
| Can the spender claim a different result? | No: 10 `add(5)` claiming 14, 16, 10 or 0 is refused. |
| Is going below zero refused? | Yes: 10 `subtract(11)` fails at the `require` line. |
| Must the covenant continue in exactly one output? | Yes: no output, two covenant outputs, an output without a covenant binding, or one bound to another covenant id are all refused. |
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
   signature, so anyone may make a transition.

### Own variant: StrictCounter

`contracts/counter_strict.sil` adds one rule to each transition:
`require(amount > 0)`. With it, `add(-20)`, `add(0)` and `subtract(-5)` are
refused at the new line, while ordinary transitions behave as before.

It does **not** close the second gap: the coin amount is still unchecked.
Doing that properly needs a rule that ties the value of the covenant output
to the value of the input being spent, and that is the next thing to learn
here.

## What none of this shows

- **Simulation is not network validation.** Calls are executed by the local
  SilverScript debug engine against a synthetic transaction. Nothing was
  submitted to any network. Creating a covenant on chain, its covenant id,
  fees, mass and acceptance by a node were not tested.
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
- `contracts/counter_strict.sil`, the tests and the walkthrough scripts —
  written for this exercise.

## Next

1. A covenant that keeps its coins: tie the value of the next covenant output
   to the value being spent, and check that a transition which leaks coins is
   refused.
2. After that, as a separate exercise: the Counter on testnet with test coins
   only.
