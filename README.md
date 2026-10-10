# Kaspa Covenant Starter Lab

A small learning exercise on native Kaspa L1 scripts written in
[SilverScript](https://github.com/kaspanet/silverscript).

**Status: work in progress. Educational, unaudited, not for real funds.**

Steps 1–3 run offline on one computer: no network, no wallet, no coins, no
token, no deployment. Steps 4, 5 and 6 were also run on testnet-10, with test
coins only. Nothing here ever touched mainnet.

Last successful run of the offline checks: 2026-10-10, 213 checks, all
passing. `tools/mutation_check.py` weakens one contract rule at a time and
reruns them: each of its 94 weakenings is noticed — 90 by a check of
behaviour, the four that add a plain entrypoint by the check of what a
contract exposes.

## At a glance

Six small contracts, each one step further than the one before it. Every
step says what was asked, what came out, and what it does not show.

| Step | Contract | What it adds | Where it was checked |
| --- | --- | --- | --- |
| 1 | Guard | a rule that is checked once | offline |
| 2 | Counter (from the SDK guide) | a state that moves from coin to coin | offline; a control run on testnet-10 |
| 2 | StrictCounter | the amount must be positive | offline |
| 3, 4 | KeepingCounter | the covenant keeps its coins | offline; testnet-10 |
| 5 | OwnedCounter | only the owner's signature moves it | offline; testnet-10 |
| 6 | ClosableCounter | a way out for the owner; default signature type only | offline; testnet-10 |

Where to look:

- `contracts/` — the six contracts, 6 to 39 lines each.
- `tests/` — the offline checks: what each contract accepts and, above all,
  what it refuses. They need no network, no key and no coins.
- `testnet/` — the program that ran the Counter contracts on testnet-10.
- `results/` — what actually came out: walkthrough output and the records of
  the testnet runs, with transaction ids.
- `tools/mutation_check.py` — checks the checks: it weakens one rule at a
  time and expects the checks to notice.

To run the offline checks, see [Run](#run). Each claim below names the
check, the record or the upstream source it rests on.

## Step 1 — Guard: a rule that is checked once

`contracts/guard.sil` is the minimal example from the Kaspa Python SDK guide:
a call passes only if the number it is given is above a threshold fixed at
compile time.

```
contract Guard(int threshold) {
    entry check(int amount) {
        require(amount > threshold);
    }
}
```

Checks: `tests/test_guard.py` (15). Actual output of the walkthrough:
`results/guard-walkthrough.txt`.

| Question | Result |
| --- | --- |
| Does the contract compile, and what does it expose? | One entrypoint, `check(int amount)`; a 25-byte script. That is the redeem script: on chain the coin is locked by a 35-byte P2SH script that holds its hash, and the script itself is revealed when the coin is spent. (The walkthrough output calls it the locking script, as the guide does.) |
| Is compilation repeatable? | Yes: same source and threshold give identical bytes. |
| Is the threshold part of the script? | Yes: threshold 100 and 101 give different scripts, so different addresses. |
| Which calls pass? | `amount` above the threshold: 101, 150, 2^62; and the same with other thresholds, zero and a negative one among them. |
| Which calls are refused? | `amount` equal to or below it: 100, 99, 0, -1, -2^62. |
| Does a refusal say where and why? | Yes: it points at the `require` line and shows `amount` and `threshold`. |
| Are malformed calls rejected early? | Yes: wrong type, wrong number of arguments, integer outside 64 bits, unknown entrypoint. |
| Can the compiled contract be saved and loaded back? | Yes: the same script, and the same unlocking script for the same call. |
| Is an edited artifact detected? | Yes: flipping one bit of the saved bytecode fails the consistency check. |
| Do the checks notice a changed rule? | Yes: with `>` replaced by `>=`, three checks fail. |

**Guard protects nothing.** `amount` is just a number supplied by whoever
makes the call. It is not tied to the value of any transaction output, so
anyone could pass 101. It shows how a rule is compiled and evaluated, not how
to lock coins.

## Step 2 — Counter: a contract that remembers

`contracts/counter.sil` is the Counter covenant from the same guide. It keeps
one number, `count`, and allows two moves: `add(amount)` and
`subtract(amount)`, where `subtract` may not take the count below zero.

A covenant has no separate storage. The count is written into the contract's
script itself, so every count is a different script at a different address.
Changing the count means spending the coin that sits at the old address and
creating a coin at the new one; the contract checks that the new coin carries
the state its rules produce.

Checks: `tests/test_counter.py` (34). Actual output of the walkthrough:
`results/counter-walkthrough.txt`.

| Question | Result |
| --- | --- |
| Where does the state live? | In the script: 9 bytes at offset 1 of the 202-byte redeem script — a one-byte push opcode and the count as 8 bytes. Count 0 and count 5 differ only there. |
| Is it still "the same contract" across states? | Yes: the template hash (the SDK's `template_hash`, a hash of the script bytes before and after the state) is identical for every count. |
| Which transitions pass? | 10 `add(5)` -> 15; 10 `subtract(3)` -> 7; 10 `subtract(10)` -> 0. |
| Can the spender claim a different result? | No: 10 `add(5)` claiming 14, 16, 10 or 0 is refused. |
| Is going below zero refused? | Yes: 10 `subtract(11)` fails at the `require` line, and overdrafts from other counts are refused too. This holds from a non-negative start only: no code of a covenant runs when it is created, so nothing vets the count it starts with (step 3 records this as a check). |
| Must the covenant continue in exactly one output? | Yes: no output, two covenant outputs, an output without a covenant binding, or an output that starts another covenant in place of the continuation are all refused by the contract (a contract with no covenant rules accepts the same transactions). |
| May another covenant start in the same transaction? | Yes. "Exactly one" counts the outputs that continue this covenant: an output that starts a new covenant may stand beside the continuation. |
| Can a covenant id be made up? | No: it is rejected by a covenant-structure check before any contract code runs. Seen here in the local engine; by the node's source code a node runs the same check before the scripts. |
| Can the count overflow? | No: adding 1 to 2^63-1 stops the script with an arithmetic error instead of wrapping around. A count runs from -(2^63-1) to 2^63-1: script numbers are sign and magnitude in at most 8 bytes, so -2^63 cannot even be written. |
| Do the checks notice a removed rule? | Yes: without the `require` in `subtract`, checks fail. |

### What the guide's Counter leaves unchecked

Two things turned up while trying moves the contract was not written for.
They are not bugs in the tooling: the guide itself calls this contract a
teaching example, not a template. Both are kept as checks so the behaviour
is on record.

1. **Negative amounts.** `subtract` refuses to go below zero, but `add(-20)`
   on a count of 10 passes and leaves -10. `subtract(-5)` increases the count.
   A rule guards only the path it is written on.
2. **The amount of coins.** The contract checks the next *state*, not how
   many coins move into it. A transition that keeps 1000 of 5000 sompi in the
   covenant and sends the rest elsewhere passes. (Closed in step 3.) The
   Counter also checks no signature, so anyone may make a transition.
   (Step 5.)

### Own variant: StrictCounter

`contracts/counter_strict.sil` adds one rule to each transition:
`require(amount > 0)`. With it, `add(-20)`, `add(0)` and `subtract(-5)` are
refused at the new line, while ordinary transitions behave as before.

It does **not** add the second check: the coin amount is still unchecked.
That is step 3.

## Step 3 — KeepingCounter: a covenant that keeps its coins

`contracts/counter_keeping.sil` is the strict Counter plus one more rule in
each transition:

```
int out_idx = OpAuthOutputIdx(this.activeInputIndex, 0);
require(tx.outputs[out_idx].value >= tx.inputs[this.activeInputIndex].value);
```

The output that continues the covenant must carry at least as many sompi as
the input being spent. That output is found through the covenant opcode
`OpAuthOutputIdx` — the same lookup the compiler-generated wrapper uses to
validate the next state (see SilverScript's
[covenant declarations](https://github.com/kaspanet/silverscript/blob/v1.0.0/docs/DECL.md))
— not by a fixed position in the transaction.

Checks: `tests/test_counter_value.py` (20; the checks of the coin rule run
over both transitions, two of them over four honest and five leaking
transaction shapes). The walkthrough output is in
`results/counter-walkthrough.txt`, sections 7 and 8.

A note on how a refusal is pinned to this rule. The engine reports a failure
of the compiler-generated checks (wrong next state, two covenant outputs) at
the last source position it stepped to, not at a line that caused it. In
this contract that is the last `require` of the move, which is the value
rule; in Counter it is line 1; in ClosableCounter (step 6) it is a line
inside a helper function or the line of a helper call. So the line number
alone does not say which rule refused. The checks therefore compare: the
very same transaction is refused by KeepingCounter and accepted by
StrictCounter, which differs only by the value rule.

| Question | Result |
| --- | --- |
| Does a transition that keeps every coin pass? | Yes, for `add` and `subtract`. |
| Is losing a single sompi refused? | Yes, in `add` and in `subtract`; the same transaction passes without the value rule. |
| Is the leak from step 2 refused? | Yes, in both transitions: 1000 of 5000 staying is refused, whichever order the outputs are in. |
| Can the leak hide behind a big first output? | No: refused. |
| Can the fee come from somewhere else? | Yes: with a separate input covering it, the covenant keeps all 5000 and the transition passes. |
| Can coins be added to the covenant? | Yes: a top-up from a separate input passes. |
| Must the covenant input or output be first? | No. With an ordinary input first and the covenant input second, an honest move passes and a leak is refused. |
| Do the earlier rules still hold? | Yes: wrong next state, zero or negative amounts in either transition, and going below zero are refused; the amount rules and the below-zero rule also from count 0 and count 1, and for large amounts. Honest moves pass away from count 10 as well. |
| Can the starting count be vetted? | No: a covenant created at -10 moves on to -5 with `add`. No code of a covenant runs when it is created; a node only recomputes the new covenant's id (by its source code). |
| Do the checks notice a weakened rule? | Yes: `tools/mutation_check.py` tries 21 weakenings of this contract — removing a rule, reading input #0 or output #0 instead, allowing one sompi less, comparing with a fixed amount, waiving a rule at count 0, a plain entrypoint beside the moves — and each one fails checks. |

### Why not simply check output #0

A naive version of the rule looks at `tx.outputs[0]`. It lives in the tests
only, as an example of what not to do. It stops the simple leak, but:

- the spender can add an input of their own, put a large plain output first
  and leave 1000 sompi in the covenant — the naive rule passes, and 4000 of
  the covenant's coins walk away;
- it refuses an honest transition whose covenant output is not first.

### What changes on a real network

The guide's live Counter example pays every fee out of the Counter's own
coin, so the coin shrinks with each move. That works only because the
Counter puts no rule on the amount, which is fine for an example whose
point is the state transition. With the keeping rule the fee has to come
from a separate input. In simulation such a transaction passes; step 4
submits it to testnet.

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

Offline checks for how these transactions are assembled, and for whole
commands of the program run against a made-up node:
`tests/test_testnet_builder.py` (71) — who pays the fee, what the covenant
output carries, which input is signed, how the owner's signature gets into
the call, what counts as a node's refusal and what is only a lost answer,
which coin is taken for the covenant's, which node is read and which is
handed a transaction, what is recorded and when. The program reaches
testnet-10 through the SDK's public node resolver (node version 2.1.0 on
2026-10-06, 2026-10-08 and 2026-10-09). `--contract` selects which of the
five Counter contracts it works with.

The program was changed in many places after its run of 2026-10-08, and was
then run once more on 2026-10-09 (the record is named at the end of step 6).
Since the 2026-10-08 run it:

- prints the feerate a node suggests, and rounds a fractional one up;
- asks a node what it is after each reconnect it makes itself, reads
  nothing from a node that has not answered, and asks again right before
  every submission;
- recognises the covenant's coin by the recorded transaction id and its
  covenant id, at any output index (before: the transaction id and output
  index 0);
- stops when a fee calculation does not settle;
- prints the record of a new covenant before `genesis` submits it, and
  works the transaction id out again after the covenant id is filled in;
- takes the covenant's amount from the coin on chain instead of from its
  own record;
- reads the shape of the command and its numbers before it loads a key or
  reaches a node;
- says what to do next when an answer is lost, for the command at hand.

The run of 2026-10-09 exercised what an honest run touches: the feerate,
the question before each submission, the printed record, the amount taken
from the chain. Everything about a lost answer, a reconnect or a wrong
node is checked offline only.

What is still rough in it: it pays with the largest coin of the acting key
and gives no choice of coin (with 2 test KAS in the covenant, the fee of a
move is lowest for a funding coin of about 1 test KAS and higher for a
larger one: class `Transition` in `tests/test_testnet_builder.py`); without
`--contract` it acts on KeepingCounter; `race` does not look for a lost
answer, and records nothing when both moves are accepted or an answer is
lost; the SDK's client reconnects by itself, and after such a reconnect the
program asks the node what it is before a submission but not before a read;
the keys in the offline checks are random, so a check that failed once
cannot be replayed with the same key.

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
Counter. The two things the example leaves unchecked (step 2) are visible on
a node too, and it is the contract's rules that make the difference.

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

Offline checks: `tests/test_counter_owned.py` (35). A signature commits to a
transaction, so each check builds the same transaction the engine
synthesizes for its scenario and signs that mirror with a throwaway key.

| Question | Result |
| --- | --- |
| Can the owner move the count? | Yes: `add`, `subtract`, down to exactly zero, with a top-up, with the fee from a separate input, with the covenant input and output not first. |
| Can anybody else? | No: a genuine signature of another key, the owner's signature made for another transaction, a made-up signature and the testnet builder's placeholder (64 zero bytes with the default type) are all refused at the signature line — before any other rule is looked at. |
| Can the owner break the other rules? | No: one sompi less, a leak hidden behind a big first output, a leak with the covenant input not first, zero or negative amounts, going below zero and a wrong next state are refused although the owner signed them. |
| Can the owner hand the covenant to another key? | No: a continuation into the same contract with another owner is refused. |
| What does the signature pin down? | With the default signature type (`All`, type byte 0x01), the whole outcome: it cannot be reused for another one. OwnedCounter accepts all six signature types, and the other five cover less, so the owner must sign with the default type only. The coin rule holds even then. What each type covers is listed below the table; step 6 turns this into a rule of the contract. |

What a signature covers, by rusty-kaspa v2.1.0,
`consensus/core/src/hashing/sighash.rs`:

- The default type (`All`, type byte 0x01) covers every input's outpoint
  and sequence, the covenant coin's own script and amount, every output
  with its value, script and covenant binding, and the lock time,
  subnetwork, gas and payload.
- No type covers signature scripts, or the other inputs' amounts and
  scripts (on chain their outpoints fix them). In the version-1
  transactions used here, none covers compute budgets or mass either.
- `None` covers no output. `Single` covers only the output at the signed
  input's own index. Neither covers the other inputs' sequences.
- The three `AnyOneCanPay` variants cover, of the inputs, only the signed
  one.

A signature of type `None` lets whoever holds it choose the move, and so
does `Single` when the covenant output is not at the input's index. The
checks in class `SignatureScope` show the part about outputs, an added
input and input amounts for `All`, `None`, `Single` and `AllAnyOneCanPay`;
the rest of the list is from the source alone.

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
exercise with test coins and wrong for anything else. Step 6 adds a way out.

## Step 6 — ClosableCounter: a way out for the owner

`contracts/counter_closable.sil` is OwnedCounter with three changes. Checked
offline first; the testnet-10 run is at the end of this step.

**A third move, `close`.** The owner's signature ends the covenant, and the
coins go wherever the signed transaction sends them:

```
#[covenant.singleton(mode = transition, termination = allowed)]
function close(State prev_state, State[] next_states, sig owner_sig) : (State[]) {
    onlyOwner(owner_sig);
    require(next_states.length == 0);
    return(next_states);
}
```

The move returns a list of next states, and an empty list ends the covenant.
This is the "Singleton termination opt-in" of SilverScript's
[covenant declarations](https://github.com/kaspanet/silverscript/blob/v1.0.0/docs/DECL.md);
without the words `termination = allowed` this declaration does not compile.
What the compiler adds to such a move: the number of outputs that continue
the covenant from this input must be at most one and equal to the length of
the returned list, and each such output must carry the returned state.
Nothing else: no rule about who may call the move or how many coins leave.
A move that returns a list may just as well continue the covenant. The
example in that document shows the syntax only and has no rules of its own;
a contract has to add them. Without the empty-list rule the owner's `close`
would move the covenant to any count and keep only a part of its coins
(`tests/test_counter_closable.py`, class `CloseEndsTheCovenant`). So `close`
has two rules of its own: the owner's signature, and an empty list.

A plain `entry` would be the wrong tool for an exit: the covenant checks are
generated for the declared moves only. An ordinary entrypoint added to
KeepingCounter gets none of them, and through it the covenant is continued
at any count, with fewer coins, or not at all (`tests/test_counter_value.py`,
class `APlainEntryGetsNoCovenantChecks`). For the same reason each contract
is checked to expose exactly its moves and nothing else.

**Only the default signature type.** `onlyOwner` checks the signature and then
requires its type byte to be the default one, `All` (0x01). Step 5 showed
what a looser type allows; for a move that pays the coins out, a `None`-type
signature would let its holder redirect the whole payout.

**Shared rules in helper functions.** The owner rule and the coin rule are
written once, in `onlyOwner` and `keepCoins`. All three moves call
`onlyOwner`; the two counting moves call `keepCoins`, and `close` does not,
since paying the coins out is what it is for. Writing a rule once does not
make a move safe by itself: a move can leave the call out, or call something
weaker. So the checks try each shared rule in each move that should have it.

Checks: `tests/test_counter_closable.py` (38). Every check that carries a
genuine signature signs the mirror of its own transaction, as in step 5.

| Question | Result |
| --- | --- |
| Can the owner close and take the coins? | Yes: to one ordinary output, split between two, with part left as the fee, with the fee from a separate input, from any count, with a coin of another size. |
| Can anybody else close or count? | No: a genuine signature of another key, a made-up signature and the testnet builder's placeholder (64 zero bytes with the default type) are refused at the signature line, in all three moves. With the owner rule not called, the same transaction passes. |
| Can a signature be reused? | Not on another transaction: the owner's signature for one payout is refused on another, and a signature made for a counting transaction does not close, nor the other way round. A signature commits to the transaction, not to a move: on one and the same transaction it satisfies the owner rule of all three moves, and the other rules let only the intended move through. |
| Can `close` be used to jump to a chosen count, or to stay? | No: a continuing output or a non-empty list of next states is refused — to 999, to the count the covenant is at, one step either way, from count 10 and from count 0. Without the empty-list rule the owner's `close` moves the covenant to any count and lets coins out. |
| Do the counting moves still keep the coins? | Yes: one sompi less, a leak to one or two plain outputs, a leak behind a big first output, a leak with the covenant input not first and a leak from a bigger coin are refused although the owner signed them; the same transactions pass once the coin rule is not called. |
| Which signature types are accepted? | Only the default one, in all three moves; the other five are refused at the type rule. |
| Do the checks notice a weakened rule? | Yes: `tools/mutation_check.py` tries 34 weakenings of this contract — among them a move that leaves a helper out, a move that checks only the signature type, a coin rule that compares with a fixed amount, a `close` that may continue at its own count, and a signature rule that also takes 64 zero bytes — and each one fails a check of behaviour. |

### ClosableCounter on testnet-10

Run of 2026-10-08, full record with transaction ids:
`results/testnet-closable-2026-10-08.md`. Two covenants were created. The
first one only to see the honest path work — create, close — because a node
gives the same message for every script failure: had the closing transaction
been built wrongly, every "refusal" after it would have meant nothing.

| Action on testnet-10 | Signed by | Node answer |
| --- | --- | --- |
| `genesis`, then `close` | owner | accepted: the 2 test KAS came back to the owner's address |
| `genesis`, `move add 5` | owner | accepted |
| `try leak` — only 1 of 2 test KAS stays | owner | refused |
| `try close-continue` — `close` that keeps the covenant at another count | owner | refused |
| `try close-none` — `close` with a signature of type `None` | owner | refused |
| `close` | another key | refused |
| `close` | owner | accepted: the 2 test KAS came back |

The compute budget that the earlier contracts ran with was enough for this
larger script: 10 units on each of the two inputs. (A node counts 100 grams
of mass for each unit; the SDK's estimator leaves that out, so the program
adds it to the fee.) The local engine does not hold an input to its budget,
so only a node could say.

The refused `None`-type attempt and the accepted `close` that followed have
the same transaction id: an id does not cover signature scripts, and the two
transactions differ only in the owner's signature inside the call. For this
one rule the network itself shows the comparison — the same transaction
passes once the signature is of the default type.

Run of 2026-10-09, after the testnet program had been changed (step 4):
`results/testnet-closable-2026-10-09.md`. Two more covenants: `genesis`,
`move add 5`, a refused `try leak` and `close`; then `genesis` and `close`.
The answers were the expected ones, and the run found one fault. Before a
genesis the program prints the record of the new covenant, and the first
time that record named a transaction id no node knows: the SDK works out
`Transaction.id` when the object is made and does not refresh it when the
covenant id is filled in afterwards. The program now works the id out again
(`Transaction.finalize()`), and in the second genesis the printed record
and the node's answer agree. Every offline check had passed before, because
a made-up node gives back whatever id it is handed. The same run shows the
fees following the feerate a node suggests, which moved between 100 and 365
sompi per gram within five minutes.

Offline checks of how these transactions are built:
`tests/test_testnet_builder.py`, class `Closable`. Each transaction is built
and signed as for the network and then put before the local engine. This
works because, in the version-1 transactions the program builds, a signature
commits to neither the compute budget nor the mass. It does not show that a
node reads the call bytes the same way: the engine runs the arguments it is
handed, not the bytes of the call.

What this step does not show: `subtract` and the other refusals of the
counting moves on a node; how much of the compute budget the script used.

## What none of this shows

- **None of this is a security report.** Nothing here claims a security
  flaw in Kaspa, SilverScript or the SDK. The guide's contracts are
  teaching examples and are used here beyond what they were written for.
- **Simulation is not network validation.** In steps 1–3, and in the offline
  checks of steps 5 and 6, calls are executed by the local SilverScript
  debug engine against a synthetic transaction.
  On testnet-10 each scenario was run once, through public nodes, for the
  guide's Counter, KeepingCounter, OwnedCounter and ClosableCounter; only
  the honest path of ClosableCounter and its refused leak were run on two
  days.
  Guard and StrictCounter were never put on chain.
- **Attempts to break the contracts, not an audit.** On 2026-10-06 the
  contracts of steps 1–5 and the offline checks were attacked on purpose:
  scripted attacks, and a random search over transactions with up to three
  inputs and four outputs against KeepingCounter (a run of 3000 random
  transactions broke no rule). No transaction was found that takes coins
  out of KeepingCounter or OwnedCounter, moves a count against its
  contract's rules, or moves OwnedCounter without the owner. Weak spots in
  the checks and wrong statements in this file did turn up, and were fixed.
  On 2026-10-07 the same was done for ClosableCounter before it was added:
  scripted attacks and a random search against a written model of the
  contract. Again no transaction broke a rule, and again the checks and
  this file had to be corrected. On 2026-10-09 and 2026-10-10 everything
  here was gone over once more: 319 scripted cases and 5,100 random
  transactions over the five Counter contracts broke no rule, while the
  checks, the testnet program and this file were corrected again. Among
  other things, 28 weakenings turned up that no check of behaviour noticed
  at the time: 12 left the suite green, the other 16 failed only
  comparisons of line numbers, source text or compiled bytes, or stopped
  checks with an error. All 28 are now entries of
  `tools/mutation_check.py`. The attack scripts and the random searches are
  not in this repository, so apart from those entries none of this can be
  re-run from it. None of it is a professional audit.
- **The local engine has its own behaviour.** It validates only the input
  being debugged, assumes the active input when an output does not name its
  authorizing input, and reports a failure of the compiler-generated checks
  at the last source position it stepped to, which is not the line that
  caused it. The checks are written around these three things. Two more
  things the checks cannot make up for: the engine does not hold an input to
  its compute budget, and it does not compare the script it is given with
  the coin's lock. By the node's source code, a node does both (rusty-kaspa
  v2.1.0: `check_scripts_sequential` in `tx_validation_in_utxo_context.rs`,
  under `consensus/src/processes/transaction_validator/`, builds the script
  engine with the input's committed limit; `execute_inner` in
  `crypto/txscript/src/lib.rs` runs the P2SH hash check before the pushed
  script). The engine inside the SDK is built from rusty-kaspa `a41a333`,
  13 commits before v2.1.0; what is said here about a node is about v2.1.0.
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
  tag (commit `3ed9733`); the compiler reports its own version as `0.1.0`
  (the SDK's `compiler_version`), and the contracts' line
  `pragma silverscript ^0.1.0` is matched against that number, not against
  the tag
- No other dependencies; tests use the standard library
- `kaspa` 2.1.0 supports CPython 3.10–3.14, and the 25 pinned hashes are the
  25 wheels of its release (Windows x64, macOS, glibc Linux), so the pinned
  install is not tied to Windows. Only Windows 11 with CPython 3.12 was run
  here

## Run

```
python -m venv .venv
.venv\Scripts\python -m pip install --require-hashes -r requirements.txt
.venv\Scripts\python -m unittest discover -s tests -v
.venv\Scripts\python examples\guard_walkthrough.py
.venv\Scripts\python examples\counter_walkthrough.py
```

These five commands were run as written on a fresh clone on 2026-10-10. On
Linux or macOS the same should work with `python3 -m venv .venv`, with
`.venv/bin/python` in place of `.venv\Scripts\python` and with
`examples/...` in place of `examples\...`; that was not run. Redirected to a
file from `cmd`, the walkthrough output has CRLF line ends; the files in
`results/` have LF. (Windows PowerShell also re-encodes what it redirects.)

`tools/mutation_check.py` is run the same way. It reruns the whole suite
once for each weakening, which took over 20 minutes here, and
it edits the contract files in place while it runs, restoring them
afterwards. A run that is killed outright can leave one contract weakened:
`git checkout -- contracts` puts it back.

None of this needs a key or a node: the offline checks import the testnet
program and hand it a made-up node. How the program itself is run — the
environment variables, the key file made by `testnet/make_key.py`, the
commands — is in the docstring of `testnet/counter_live.py`.

## Origin of the code

- `contracts/guard.sil` — the minimal example from the
  [Kaspa Python SDK guide, v2.1.0](https://github.com/kaspanet/kaspa-python-sdk/blob/v2.1.0/docs/learn/silverscript/overview.md)
  (ISC license).
- `contracts/counter.sil` — the Counter from the
  [same guide](https://github.com/kaspanet/kaspa-python-sdk/blob/v2.1.0/docs/learn/silverscript/covenants.md)
  (ISC license).
- `contracts/counter_strict.sil`, `contracts/counter_keeping.sil`,
  `contracts/counter_owned.sil`, `contracts/counter_closable.sil`, the tests,
  the walkthrough scripts and the testnet program — written for this
  exercise. The way transactions are assembled follows the SDK's
  [Counter example](https://github.com/kaspanet/kaspa-python-sdk/blob/v2.1.0/examples/silverscript/counter.py)
  (ISC license).

## Next

- A proper review before anything here is reused for more than learning.

## License

ISC — see `LICENSE`. The two contracts taken from the Kaspa Python SDK guide
keep that project's ISC notice, which is reproduced in `NOTICE`.
