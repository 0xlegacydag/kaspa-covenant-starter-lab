# KeepingCounter on testnet-10 — run of 2026-10-06

Test network only. Test coins have no value. Nothing here touched mainnet.

- Network: Kaspa testnet-10, reached through the SDK's public node resolver
  (the answering node reported version 2.1.0 and was synced)
- Contract: `contracts/counter_keeping.sil`
- Program: `testnet/counter_live.py` as of commit `698afa8`
- Covenant id: `c3760878492c5d98f72f48aa55e800917274fcd89ec4bf3b937658279b32dc52`
- Funding: 1000 test KAS from a faucet to a throwaway test address

## What was submitted

| # | Action | Node answer | Count | In the covenant | Fee |
| --- | --- | --- | --- | --- | --- |
| 1 | `genesis` — lock 2 test KAS at count 0 | accepted | 0 | 2.00000000 | 0.02140000 |
| 2 | `move add 5` | accepted | 0 -> 5 | 2.00000000 | 0.02232000 |
| 3 | `move subtract 3` | accepted | 5 -> 2 | 2.00000000 | 0.02232000 |
| 4 | `try leak` — add(5), only 1 of 2 test KAS stays in the covenant | refused | 2 | 2.00000000 | — |
| 5 | `try wrong-state` — add(5), output claims count 8 | refused | 2 | 2.00000000 | — |
| 6 | `try negative` — add(-20) | refused | 2 | 2.00000000 | — |

Accepted transactions:

| # | Transaction id | Time (UTC) | Covenant output address |
| --- | --- | --- | --- |
| 1 | `51cd78f22bd8ec0295c9ed68c94fb41e57dec493e84def3473f963094620222e` | 18:23:37 | `kaspatest:pp7g3hw03c30s5g07n468va3stpce7jnv6pzzaxgvlaym0qwjgggu8nhmen55` (count 0) |
| 2 | `0d480181c9268fa68897ddcb92fbe52b74c4759aaf4b896c6f440dd14c27c588` | 18:23:46 | `kaspatest:prj5p3ek42sk7kpvqratzu93c92mqp2hchvxn9a3msacrps6d6977mvqxntxw` (count 5) |
| 3 | `3054fa44dce6cb8e96e6798a863e249be0c4a65da21588d48bff8258ecbf8a40` | 18:24:15 | `kaspatest:prqqsgvrha03yzmhs5454ylqdxjq3t4yks3ghjzzzerg9advyhydxylrugvu9` (count 2) |

Refused transactions — the node's answer was the same for all three:

```
Rejected transaction <id>: failed to verify the signature script: script ran, but verification failed
```

| # | Id of the rejected transaction |
| --- | --- |
| 4 | `528691be4407d4f0c56f2bd2581e903d5134739415637b850033cc7d461434bf` |
| 5 | `73da81a97e0316875a55966ac9b88f1062a78ed0bcd417c7961ee9e6b8af7733` |
| 6 | `051548b4522eed4d8cc996a4608c305f71a57bfc6fcd0a1613afe303bf74075d` |

## Cross-checks

- A public testnet-10 explorer API (`api-tn10.kaspa.org`) reports transactions
  1–3 as accepted, each with a 200000000-sompi output at the address above and
  a change output back to the test address. It has no record of transaction 4.
- The three covenant outputs sit at three different addresses: the state is
  part of the script, so every count has its own address.
- Balance of the test address after the run: 997.93396000 test KAS =
  1000 − 2 (locked) − 0.0214 − 2 × 0.02232 (fees). The covenant paid nothing.
- `status` after the run: count 2, 2 test KAS in the covenant, last
  transaction 3, present on chain.

## What this shows

- A node accepts the transition shape used here: two inputs (the covenant
  UTXO and an ordinary UTXO that pays the fee), two outputs (the next covenant
  UTXO with the same amount, and the change). The covenant kept exactly 2 test
  KAS through both moves.
- A node refuses the same three rule-breaking transactions that the local
  engine refuses in step 3.

## What it does not show

- **Which rule the node tripped on.** Its message is the same for all three
  refusals. That each one fails on the intended line is known only from the
  local engine, where the same scenarios fail at known lines.
- **A control.** The same leak was not tried on chain against a Counter
  without the value rule.
- **More than one run.** One covenant, one node, refusals at submission time.
  No contention between spenders, no other nodes, no reorg.
- **Who may move.** The contract checks no signature. The covenant UTXO was
  left on testnet at count 2; anyone can move it.

## Correction, 2026-10-08

The list at the top says the test coins came "from a faucet". Which service
gave them out was not recorded. Read that line as: 1000 test KAS were
obtained for a throwaway test address before the run.
