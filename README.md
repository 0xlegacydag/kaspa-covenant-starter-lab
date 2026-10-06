# Kaspa Covenant Starter Lab

A small learning exercise on native Kaspa L1 scripts written in
[SilverScript](https://github.com/kaspanet/silverscript).

**Status: work in progress. Educational, unaudited, not for real funds.**

Everything here runs offline on one computer. No network, no wallet, no
coins (not even testnet ones), no token, no deployment.

## Step 1 — Guard

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

### What was checked

Last successful run: 2026-10-06, 14 checks, all passing
(`tests/test_guard.py`). Actual output of the walkthrough is saved in
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

### What this does not show

- **Guard protects nothing.** `amount` is just a number supplied by whoever
  makes the call. It is not tied to the value of any transaction output, so
  anyone could pass 101. The contract is useful for learning how a rule is
  compiled and evaluated, not for locking coins.
- **Simulation is not network validation.** Calls are executed by the local
  SilverScript debug engine against a synthetic transaction. Nothing was
  submitted to any network; fees, mass and acceptance by a node were not
  tested.
- **The tooling is marked experimental upstream.** The SDK guide warns that
  the language, the compiler output and the bindings may change.

## Environment

- Windows 11 x64, CPython 3.12.14
- `kaspa==2.1.0` from PyPI, hashes pinned in `requirements.txt` (the wheel is
  byte-identical to the asset of the
  [v2.1.0 release](https://github.com/kaspanet/kaspa-python-sdk/releases/tag/v2.1.0))
- The bundled SilverScript compiler reports version `0.1.0`
- No other dependencies; tests use the standard library

## Run

```
python -m venv .venv
.venv\Scripts\python -m pip install --require-hashes -r requirements.txt
.venv\Scripts\python -m unittest discover -s tests -v
.venv\Scripts\python examples\guard_walkthrough.py
```

## Origin of the code

- `contracts/guard.sil` — the minimal example from the
  [Kaspa Python SDK guide, v2.1.0](https://github.com/kaspanet/kaspa-python-sdk/blob/v2.1.0/docs/learn/silverscript/overview.md)
  (ISC license).
- Tests and the walkthrough script — written for this exercise.

## Next

Step 2: a stateful Counter covenant, again offline — one allowed state
transition and one forbidden one.
