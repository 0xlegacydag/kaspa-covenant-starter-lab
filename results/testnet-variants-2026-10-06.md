# Testnet-10 variants — runs of 2026-10-06

Test network only. Test coins have no value. Nothing here touched mainnet.

These runs follow `results/testnet-run-2026-10-06.md` (KeepingCounter: three
accepted transactions, three refusals) and answer what that run left open.

- Network: Kaspa testnet-10 through the SDK's public node resolver
- Program: `testnet/counter_live.py`
- Two throwaway test keys: the **first key** created and funded the
  covenants; the **second key** was given 20 test KAS by the first one
- Times are UTC

## 1. Control: the guide's Counter, without the added rules

Contract `contracts/counter.sil`, covenant id
`7c7cde26d9a37438af64f45ba28e03b50a6056138a03a0cf75ff49153c30d70d`.

| Action | Node answer | Transaction id | Result |
| --- | --- | --- | --- |
| `genesis` — 2 test KAS at count 0 | accepted, 18:39:22 | `9154655bcaec113a967dbbd3617c891ec76cd7785f1c89c1e60721a89d5c72c9` | count 0, 2 test KAS |
| `try leak` — add(5), only 1 of 2 test KAS stays | **accepted**, 18:39:45 | `033fb9def5c540b97b69a0a3e27b0bd230bea5fa7ef788c736a5e52915e3ab9d` | count 5, **1 test KAS left in the covenant** |
| `try negative` — add(-20) | **accepted**, 18:39:49 | `642bcd799f469148316047c63ee6f0d4f1833361e8f5faf5c6ae82ebda3bcf83` | **count -15** |

The same two transactions were refused by KeepingCounter in the first run.
So the difference is the contract's rules, not the way the transactions are
built: both things the guide's Counter leaves unchecked (step 2 of the
README) are visible on a node too.

## 2. OwnedCounter: only the owner's signature moves it

Contract `contracts/counter_owned.sil`, covenant id
`04cbfad3b5f047c51d3d457852054079f415a549ed0ac0f0548f259908cb1b4f`, owner =
the first key.

| Action | Signed by | Node answer | Transaction id |
| --- | --- | --- | --- |
| `genesis` — 2 test KAS at count 0 | first key | accepted, 18:40:08 | `132e8ed8d473e240d1ebed3f63eb19a6399380321204ba1409ed4868931fe5dc` |
| `move add 5` — count 0 -> 5 | owner | accepted, 18:40:11 | `2365761cb9e8769b18222c5f2ebf3e55db88ac9c800a8b142d9738d174d143f9` |
| `try leak` — only 1 of 2 test KAS stays | owner | refused | rejected id `bbc1bd9d06e7dc28134baabb352c69eef8588f12c19ba74212cf042cea3655dc` |
| `try negative` — add(-20) | owner | refused | rejected id `374f18849e242f0d72cebf3eced89b773fb6bb1721a0d3ad940c670c15975ca5` |
| `move subtract 2` — count 5 -> 3 | owner | accepted, 18:42:18 | `0d56e42fc33b46c1da247d7d205813b901ee4a4e1605538369be30385b53a521` |
| `move add 1` | **second key** | refused | rejected id `9b75c0eacd501cf4ffd6a856aeab57aece0ada2a57ed0496ffe58dc408949769` |

The owner's signature is what makes a move possible, and it does not switch
the other rules off: the owner cannot take coins out or use a negative amount
either. After every accepted move the covenant held 2 test KAS.

During `move add 5` the connection to the public node dropped after the node
had accepted the transaction. The program now records the new state before it
waits for confirmation, retries dropped connections, and has a `resync`
command; the state was repaired from the chain.

## 3. Anyone can move KeepingCounter

The covenant from the first run (`c3760878492c5d98f72f48aa55e800917274fcd89ec4bf3b937658279b32dc52`),
created by the first key.

| Action | Paid by | Node answer | Transaction id |
| --- | --- | --- | --- |
| send 20 test KAS to the second key | first key | accepted, 18:42:31 | `180f261f8c40f906ba069a8e6b57c54c62aa1d98e6d4c2a8a5ecc8b654f29718` |
| `move add 1` — count 2 -> 3 | **second key** | accepted, 18:43:14 | `a389a92939dc764e3e75dc351294af50bdb937f494e952c935d58a67e66aafff` |

A key that had nothing to do with creating the covenant moved it. The
covenant still held 2 test KAS afterwards: KeepingCounter protects the coins,
not the right to move.

## 4. One coin, one move

Two different moves were built from the same covenant UTXO (count 3) and
submitted one after the other, each paid by a different key.

| Move | Paid by | Node answer |
| --- | --- | --- |
| add(1) | first key | accepted, 18:44:07 — `747cdde8e9716b152dcfc0202226c43c682b2011e5e9fd02244e6e6590b1a498` |
| add(2) | second key | refused |

The node's answer to the second one:

```
Rejected transaction 7cffbc6f7e7e3cd4305d6e3a5d806b752422482edb799c4ec3a919894f72861c:
output (a389a92939dc764e3e75dc351294af50bdb937f494e952c935d58a67e66aafff, 0)
already spent by transaction 747cdde8e9716b152dcfc0202226c43c682b2011e5e9fd02244e6e6590b1a498 in the mempool
```

The state lives in one coin, so only one move per coin can win. Two users
acting at the same moment do not both succeed; one has to rebuild the move
on the new state.

## Cross-checks

- A public testnet-10 explorer API (`api-tn10.kaspa.org`) reports all nine
  accepted transactions above as accepted, with the covenant output amounts
  given here (100000000 sompi after the leak on the guide's Counter,
  200000000 everywhere else). It has no record of the refused ones it was
  asked about.
- State after the runs, read back from a node:

| Contract | Count | In the covenant |
| --- | --- | --- |
| guide's Counter | -15 | 1 test KAS |
| KeepingCounter | 4 | 2 test KAS |
| OwnedCounter | 3 | 2 test KAS |

## What these runs do not show

- One run of each scenario, through one public node at a time.
- The refusals for broken rules all carry the same message ("script ran, but
  verification failed"). The control run shows the rules make the
  difference; it does not show which line a node stopped at.
- The race was two transactions sent seconds apart from one computer, not
  two independent users or a block-level conflict.
- No outside review of these runs.
- The three covenants were left on testnet. The guide's Counter and
  KeepingCounter can be moved by anyone; the coins in KeepingCounter and
  OwnedCounter can never leave them — these contracts have no exit.
