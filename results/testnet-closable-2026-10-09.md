# ClosableCounter on testnet-10 — run of 2026-10-09

Test network only. Test coins have no value. Nothing here touched mainnet.

After the run of 2026-10-08 (`results/testnet-closable-2026-10-08.md`) the
testnet program was changed in many places, and until now those changes had
been checked offline only. This run puts the changed program before nodes:
the honest path of ClosableCounter, one refusal, and what the program now
prints. It found one fault in the changed program, which was corrected
during the run.

- Network: Kaspa testnet-10 through the SDK's public node resolver. Each
  command printed the node it reached: `vector-10.kaspa.green`,
  `boson-10.kaspa.red`, `electron-10.kaspa.blue`, `muon-10.kaspa.blue` and
  `quark-10.kaspa.red`; every one reported version 2.1.0 and was synced
- Contract: `contracts/counter_closable.sil`
- Program: `testnet/counter_live.py` as in the revision that adds this
  record, except for A1, which was sent before the one-line correction
  described under "A stale id", and for one message of `race`, a command
  that was not run here
- One throwaway test key, the **first key** of the earlier runs: it created
  both covenants, is their owner and signed everything here
- Compute budget of the covenant input: 10 in every transaction
- Times are UTC, taken from the accepting blocks as a public explorer
  reports them

## Round A — create, count, one refusal, close

Covenant id `68e8b1512b67968ee0de81d119c3bc0acd9d9aba22a22d725853e864f37b90f6`.

| # | Action | Node answer | Transaction id | Feerate | Fee |
| --- | --- | --- | --- | --- | --- |
| A1 | `genesis` — lock 2 test KAS at count 0 | accepted, 23:15:05 | `03f18d8522a794362466eafde149c34467c6e687e5d3ef8b45d9a939e94d3042` | 365 | 0.07811000 |
| A2 | `move add 5` — count 0 -> 5 | accepted, 23:17:48 | `ae84c7a832fb2a1a6344ecd10deef1e055d5d62e9dabb6cfa44a516344221378` | 139 | 0.03102480 |
| A3 | `try leak` — add(5), only 1 of 2 test KAS stays in the covenant | refused | rejected id `7447ceb2afb547e4adbb6f0822668ea68e5837a4f2bed7719ab224b53982be6e` | 255 | — |
| A4 | `close` on count 5 | accepted, 23:18:21 | `4937f809214c817dcfa2ad5315cf4ba1eb45d332311365d6723747a484f9b72c` | 265 | 0.01939800 |

## Round B — create and close

Covenant id `c6f1ab888103e972f5852e0574df6bd6206498ed63928904e24ab689d59132ce`.

| # | Action | Node answer | Transaction id | Feerate | Fee |
| --- | --- | --- | --- | --- | --- |
| B1 | `genesis` — lock 2 test KAS at count 0 | accepted, 23:18:43 | `fcc2ab5e7f40fc836b032d59d5977706ef060c942da5335821481ab3c3fc2914` | 100 | 0.02140000 |
| B2 | `close` on count 0 | accepted, 23:20:13 | `9fc3a1b00004c852c1a36fd69697ef699539dc33405bb286bcedf63a9be0a3f0` | 100 | 0.00732000 |

The feerate is the node's estimate in sompi per gram, as the program now
prints it, rounded up; the fee is in test KAS. The node's answer to A3:

```
Rejected transaction <id>: failed to verify the signature script: script ran, but verification failed
```

## A stale id

Before it submits a genesis, the program now prints the record of the new
covenant, to be saved by hand if the node's answer is lost. In A1 that
record named this transaction:

`f1c8351eca14246c2c308c60c91635bd9107a8dd9470da7a4cfdeccaadff3d2b`

The node accepted the genesis as `03f18d85…`, and the explorer knows no
transaction `f1c8351e…`. The covenant id in the record was right.

The cause: the SDK works out `Transaction.id` when the transaction object
is made. The program fills in the covenant id of the genesis output after
that (`populate_genesis_covenants`), and the id is not refreshed: `tx.id`
then names the transaction as it was without the covenant id.
`Transaction.finalize()` works the id out again. The program was corrected
between A1 and A2, with one line in `assemble`. In B1 the printed record and
the node's answer name the same transaction, `fcc2ab5e…`.

Offline check, written after A1:
`test_the_id_of_a_genesis_transaction_is_worked_out_after_its_covenant_id`
in `tests/test_testnet_builder.py`. Every offline check of this path had
passed before the run, because a made-up node gives back whatever id the
program hands it.

What it would have meant: a record saved by hand after a lost answer to a
genesis would have named a transaction that does not exist, and `status`
would have reported the new covenant as not on chain. The same stale id was
used by the look-up after a lost answer, which exists since the revision
that ran on 2026-10-08. There it could only make the look-up miss a genesis
that a node had taken: a record was written from the node's answer alone,
so no record ever carried the stale id.

## Fees follow the feerate

The masses are the ones of the earlier runs: 21,400 grams for a genesis,
22,320 for a move, 7,320 for a close, the compute budgets and the margin
included. Each fee above is its mass times the feerate printed next to it:
21,400 × 365, 22,320 × 139, 7,320 × 265, 21,400 × 100, 7,320 × 100.

This settles a point the record of 2026-10-08 left open ("Why the fees
differ"): its two closing transactions paid 0.00732 and 0.0225456 test KAS,
which is 7,320 grams at 100 and at 308 sompi per gram. Here the estimate
moved between 100 and 365 within five minutes.

## Cross-checks

- A public testnet-10 explorer API (`api-tn10.kaspa.org`), asked at 23:20:28,
  reports A1, A2, A4, B1 and B2 as accepted. The outputs it shows:
  - A1 and B1: 200000000 sompi at
    `kaspatest:pp5apxrtpwde0wx4ec4vl7wsqea7sj94y9c0vqpt64h6klfw2xtqkhs62vd3r`
    carrying the covenant id given above for each round, and the change.
    This is the address both covenants of 2026-10-08 started at: same
    contract, same owner, same count;
  - A2: 200000000 sompi at
    `kaspatest:pzwxlrgd06uwmfurde94xqd89z5vg9l5kytpshg3h7hvqxaf07f6xfz5qlmwp`
    with the same covenant id as A1, and the change;
  - A4 and B2: 200000000 sompi at the owner's ordinary address and the
    change; no output carries a covenant id.
  It has no record of the rejected id of A3, nor of the stale id.
- `status` after A3: count 5, 2 test KAS, present on chain — the refused
  transaction changed nothing. `status` after B2: no live covenant recorded.
- Balance of the key: 974.59571280 test KAS before the run, 974.43846000
  after it. The difference, 0.15725280, is the five fees (0.07811 +
  0.0310248 + 0.019398 + 0.0214 + 0.00732): both pairs of locked coins came
  back in full, and the refused transaction cost nothing.

## What this shows

- Nodes accept a genesis, a counting move and a `close` as the changed
  program builds them, and refuse the leak, as before.
- The question "what are you" that the program now puts to a node right
  before every submission, and the feerate rounded up, ran in each of the
  six commands that submitted a transaction here.
- After the correction, the record printed before a genesis names the
  transaction a node accepts.

## What this does not show

- One run of each scenario, through public nodes.
- Anything about a lost answer, a dropped connection or a node that does
  not say what it is: none of that happened here. The reconnect path, the
  look-up after a lost answer and the advice printed with NO ANSWER are
  checked offline only.
- `race`, `send`, `resync`, the other rule-breaking attempts and the other
  four contracts with this revision of the program.
- Which rule a node stopped at in A3: the message is the same for every
  script failure. That it is the coin rule is what the local engine says
  about a transaction built the same way.
- No outside review of this run.

Nothing of these two covenants is left on testnet. The three covenants of
2026-10-06 were not touched by this run.
