# Demo script — 4 to 5 minutes

Rehearsed against the deployment. Every number below is one I have actually
seen; if something takes longer on the day, the drawer explains why.

**Before recording**

1. Open the demo URL in a clean window (no prior session in `sessionStorage`).
2. Click **Break something → Reset demo data**. INV-1001 back to $40.00 with
   nothing refunded, INV-1002 at $38.00 already refunded.
3. Warm the revision: send one throwaway turn, then reset again. The first
   turn after a container starts pays ~4.5 s for credential acquisition; every
   later one is ~0.5 s to first status, and the cold one is not what you want
   on camera.
4. Grant the microphone *before* you start recording, so the permission bubble
   is not part of the video.

---

## 0:00 – 0:30 — What this is

Show the sign-in screen. Three synthetic accounts, chosen so that **the same
request produces a different correct outcome on each**: Premium active, Basic
active, Premium with an inactive contract. Point at the "synthetic demo data"
badge.

> "A support agent that can actually move money — so the interesting part is
> what stops it moving money it shouldn't."

Sign in as **Avery Chen** (Premium, active).

---

## 0:30 – 1:30 — The happy path, and the guard underneath it

Click **Refund request** (INV-1001, $15.00, duplicate charge).

Narrate the steps as they appear — verify identity, search policy library,
issue refund — then stop on the three things that make it more than a chat
wrapper:

* **Grounding chip**: `top 0.677 vs τ 0.38`, five chunks accepted.
* **Sources**: the Premium policy at version 2.0. There is a superseded v1 in
  the corpus and it never appears.
* **Receipt**: a reference, the intent id, and the policy version captured at
  commit time.

Now **New session → Jordan Patel** (Basic) and send the same request against
his invoice (INV-2001).

Five steps appear, and the third is the one to stop on — it is the best
thirty seconds in the demo:

```
✓ Verify identity
✓ Search policy library            grounded 0.590, Basic policy 2.0
✗ Escalate to a human              "I can't record that as a blocked action,
                                    because no action has been attempted yet."
✗ Issue refund                     "$15.00 is above the $10.00 per-request
                                    limit on this plan."
✓ Escalate to a human              ESC-CD3A89 (action_blocked)
```

> "The model tried to file a ticket saying the action was blocked — before
> anything had been blocked. That ticket would be a durable record of a
> refusal that never happened. The executor checks the claim against what the
> server actually did, refuses it, and the model's recovery is to attempt the
> refund and get a real, citable refusal. Then the same ticket is allowed,
> because now it is true."

Three prompt revisions failed to stop that behaviour. The invariant lives in
the executor instead, where it cannot be talked out of.

---

## 1:30 – 2:15 — Voice

**Start voice.** Say:

> "Hi, invoice INV-1001 was charged twice. Can you refund fifteen dollars?"

Points to make while it runs:

* The tool cards are the **same cards** as the typed turn, because the voice
  endpoint returns the same typed events into the same reducer.
* Each of those tool calls is a **separate HTTP request**. The state machine is
  rebuilt from the audit rows every time, so the guard chain survives a
  reconnect.
* Interrupt it mid-sentence to show barge-in — server-side VAD cancels the
  reply and opens a new turn.

Then type a follow-up in chat referring to what you just said out loud, to show
one shared history across both modes.

---

## 2:15 – 3:00 — Being wrong, safely

Three clicks, in this order:

1. **Another account's invoice** (INV-2001, Jordan's) → denied,
   *invoice belongs to another account*. The model asked for it; the server
   said no. Authority comes from the session binding, never from the model's
   arguments.
2. **Out of scope** (shipping) → nothing in the corpus supports it, so it
   escalates instead of inventing a shipping policy.
3. **Already refunded** (INV-1002) → denied on remaining balance, with the
   clause.

> "Every refusal here is a database read, not a model opinion."

---

## 3:00 – 3:45 — Failure, handled in the open

**Break something → Lost confirmation**, then click **Refund request** again.

This is the one worth slowing down for:

* `Issue refund` fails — *"I lost the connection while confirming that
  refund."*
* The turn does not end there. Before writing a reply it asks the database
  whether this turn has a receipt, finds one, and reports it.
* The card shows **recovered after a failed call** — a different claim from
  *replayed, not re-charged*, and a different field.

> "The receipt row is the record of a refund, not whatever the tool returned.
> A turn that told the customer nothing happened while their balance said
> otherwise would be the worst bug in the system."

If time is short, **Model rate limit** is the faster version: bounded retry,
a named error, the provider's own Retry-After, and a **Try again** control.

---

## 3:45 – 4:30 — Show your work

**Inspect** on the successful turn.

* **Waterfall on the turn clock.** The bars are small; the gaps are the model.
  On a typical turn 3.3 s of 3.9 s is model latency, which is why the bars are
  positioned rather than stacked.
* **Grounding**, drawn as the actual decision rule — unsupported / weak /
  grounded bands in their real positions, with this turn's score marked and
  the threshold *this decision* was taken under.
* **Tokens and cost**, labelled an estimate with the rate card's date.
* **Session metrics** in the rail: p50 and p95 with their sample counts,
  latency split by mode because a spoken turn's span includes the time the
  customer spent talking.

Close with the trade-offs: no Kubernetes for two stateless containers, no
Redis because the only shared state is already in Postgres (which is also why
the rate limiter is correct at one replica), exact vector search over 40
chunks, and answers buffered rather than token-streamed so citations can be
validated before any text is released.

---

## If something goes wrong on camera

* **A turn takes 7 s** — a cold revision. Say so and open the drawer; the
  waterfall shows where the time went.
* **An answer is withheld with a citation error** — that is the guard working;
  it refuses to state a policy it cannot attribute. Press **Try again**.
* **Voice will not connect** — the mic permission or the network. Chat keeps
  working in the same session, which is itself the designed behaviour.
* **A fault is still armed** — the red `armed:` chip in the rail is the tell.
  Clear it.
