# ClosableCounter on testnet-10 — run of 2026-10-08

Test network only. Test coins have no value. Nothing here touched mainnet.

This run follows `results/testnet-variants-2026-10-06.md`, which ended with
"these contracts have no exit". ClosableCounter (step 6 of the README) has
one; until today it had been checked offline only.

- Network: Kaspa testnet-10 through the SDK's public node resolver. Each
  command printed the node it reached: `boson-10.kaspa.red`,
  `muon-10.kaspa.blue`, `vector-10.kaspa.green` and
  `proton-10.kaspa.stream`; every one reported version 2.1.0 and was synced
- Contract: `contracts/counter_closable.sil`
- Program: `testnet/counter_live.py` as of commit `d4ab0c8`
- Two throwaway test keys: the **first key** created both covenants and is
  their owner; the **second key** plays somebody else
- Compute budget of the covenant input: 10 in every transaction, the same
  value the earlier contracts ran with
- Times are UTC, taken from the accepting blocks as a public explorer reports
  them

## Why two rounds

A node gives one and the same message for every script failure. If the
closing transaction were built wrongly, or the larger script did not fit the
compute budget, every "expected refusal" below would be refused too — for a
reason that has nothing to do with the contract's rules. So the honest path
was run first, on a covenant made only for that: create, close. The refusals
of round B are counted only because an honest `close` was accepted with the
same budget before them and after them.

## Round A — create and close

Covenant id `f30f12912b39ae4e644cd9dc4aae715da30cd56732698dd1caa4ebe7f28064b7`.

| # | Action | Signed by | Node answer | Transaction id | Fee |
| --- | --- | --- | --- | --- | --- |
| A1 | `genesis` — lock 2 test KAS at count 0 | first key | accepted, 08:57:53 | `7e349e8db7624d4b13b06b6eb5da28bb628ff1e53cbb8fa6b48dc4446ab3bc6e` | 0.02140000 |
| A2 | `close` on count 0 | owner | accepted, 08:58:21 | `7189b9f8747e12b0f8d660d675b0066e16cc001853a5d3079b56c5704677ff7b` | 0.00732000 |

A2 has two inputs — the covenant UTXO and an ordinary UTXO of the owner that
pays the fee — and two ordinary outputs: 2 test KAS paid out to the owner's
address, and the change. No output continues the covenant.

## Round B — count, four refusals, close

Covenant id `6baadda5c38aec5733d562ff1060112d181fb2f503e076635520f730b7500ca1`.

| # | Action | Signed by | Node answer | Transaction id |
| --- | --- | --- | --- | --- |
| B1 | `genesis` — lock 2 test KAS at count 0 | first key | accepted, 08:58:52 | `bde9768b45a42a048f9c9a38cd1d3a452f920f595d7fbd80b03548aaf694479f` |
| B2 | `move add 5` — count 0 -> 5 | owner | accepted, 08:59:15 | `ebafa965bc85c42e4b179197c2f3506290fe0d98793f178aaf65baca37e2f87b` |
| B3 | `try leak` — add(5), only 1 of 2 test KAS stays in the covenant | owner | refused | rejected id `463b524bb1b59ecc57f40a2c2b154adb60b9ef7f81b4e60c7d9d496da6497187` |
| B4 | `try close-continue` — `close`, but the coins stay in a covenant output at count 11, named as the next state | owner | refused | rejected id `8895c64b6fa7126f002fabb72c310b56e85873d140a556ce47c74f0f0df5590f` |
| B5 | `try close-none` — `close` with the owner's signature of type `None` | owner | refused | rejected id `bb8fec128b38ddbecf82de8636b73f1a607fa17aed9485daf3982505d48b4ff6` |
| B6 | `close` | **second key** | refused | rejected id `7c1107d0bbb11152fae30d0d150aa2431139a27c3ba9f31c3fa78095797995d4` |
| B7 | `close` on count 5 | owner | accepted, 09:00:34 | `bb8fec128b38ddbecf82de8636b73f1a607fa17aed9485daf3982505d48b4ff6` |

Fees of the accepted ones: B1 0.02140000, B2 0.06874560, B7 0.02254560 test
KAS. The covenant held exactly 2 test KAS until B7 paid them out.

The node's answer to B3–B6 was the same:

```
Rejected transaction <id>: failed to verify the signature script: script ran, but verification failed
```

### One transaction id, refused and then accepted

B5 and B7 have the same transaction id. A transaction id does not cover
signature scripts, and the two transactions spend the same coins into the
same outputs: they differ only in the owner's signature inside the covenant
call — type `None` in B5, the default type in B7. A node refused the first
and accepted the second. So for this one rule the network itself gives the
comparison that the node's message does not: the same transaction passes
once the signature is of the default type. Offline, the `None` signature is
a genuine one, and the refusal comes at the contract's type rule
(`tests/test_testnet_builder.py`, class `Closable`).

It also means that an id printed in a refusal says nothing final about that
id: the explorer lists `bb8fec12…` as accepted.

## Cross-checks

- A public testnet-10 explorer API (`api-tn10.kaspa.org`), asked at 09:01:43,
  reports A1, A2, B1, B2 and B7 as accepted. The outputs it shows:
  - A1 and B1: 200000000 sompi at
    `kaspatest:pp5apxrtpwde0wx4ec4vl7wsqea7sj94y9c0vqpt64h6klfw2xtqkhs62vd3r`
    carrying the covenant id given above for each round, and the change. Both
    covenants started at the same address: same contract, same owner, same
    count. Only the covenant id told them apart, and the first was closed
    before the second was created;
  - B2: 200000000 sompi at
    `kaspatest:pzwxlrgd06uwmfurde94xqd89z5vg9l5kytpshg3h7hvqxaf07f6xfz5qlmwp`
    with the same covenant id as B1, and the change;
  - A2 and B7: 200000000 sompi at the owner's ordinary address and the
    change; no output carries a covenant id.
  It has no record of the rejected ids of B3, B4 and B6.
- Read back from a node at 09:02:06: no UTXO at the covenant's addresses for
  counts 0, 5 and 11.
- Balance of the first key: 974.73712400 test KAS before the run,
  974.59571280 after it. The difference, 0.14141120, is the five fees
  (0.0214 + 0.00732 + 0.0214 + 0.0687456 + 0.0225456): both pairs of locked
  coins came back in full, and the refused transactions cost nothing.
- Balance of the second key: 19.98118900 test KAS before and after.
- `status` between B6 and B7: count 5, 2 test KAS, present on chain — the
  refused transactions changed nothing.

## What this shows

- A node accepts the closing transaction as the program builds it, and after
  it the covenant is gone: the coins sit at an ordinary address.
- The default compute budget of 10 was enough for `add` and for `close` of
  this contract, whose script is about 1.7 times the size of OwnedCounter's.
- The four transactions that the local engine refuses were refused by nodes
  as well, and the owner's signature did not switch the other rules off: the
  owner could not leak coins, could not use `close` to jump to another count,
  and could not close with a signature of a looser type.

## What this does not show

- One run of each scenario, through public nodes.
- Which rule a node stopped at in B3, B4 and B6: the message is the same.
  That B3 is the coin rule, B4 the empty-list rule and B6 the signature rule
  is what the local engine says about transactions built the same way; for
  B5 see above.
- How much of the compute budget the script used: a node is silent while the
  budget is enough. No transaction was sent with a smaller budget.
- Why the fees differ. A2 and B7 were built the same way and paid 0.00732 and
  0.0225456 test KAS, a ratio of exactly 3.08. The fee is the measured mass
  times the feerate a node suggests; the program does not print that
  feerate, so a changing estimate is the likely cause, not a recorded one.
- `subtract` and the other refusals of the counting moves on a node for this
  contract; anything about two covenant inputs.
- No outside review of these runs.

Nothing of these two covenants is left on testnet. The three covenants of
2026-10-06 were not touched by this run.
