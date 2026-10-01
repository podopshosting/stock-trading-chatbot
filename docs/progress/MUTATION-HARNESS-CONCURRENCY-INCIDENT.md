# I corrupted a mutation run by editing the tree while it ran

2026-10-01. No deployed artifact was affected. The cost was one void
mutation run, one wasted half hour, and a stretch of time in which I
believed two false things.

## What I did

I started `scripts/falsifying_controls.py` in the background — 162
mutations, each one rewriting a source file, running the whole suite,
and restoring the file from an in-memory snapshot — and then carried on
editing source files in the same working tree.

## What that looked like from the inside

It did not look like a concurrency bug. It looked like three unrelated
problems:

1. **Edits "reverting".** I patched
   `agent/autonomy/evidence_class.py`, confirmed the patch had been
   written, and found the function unchanged moments later. Earlier the
   same thing had happened to `web/agent/index.html`. I attributed both
   to iCloud, which does genuinely revert files in this directory and
   had done so earlier in the day.

   **Both were the harness.** It snapshots every target file at startup
   and restores from that snapshot after each mutation, so any edit made
   during a run is erased on the next restore. I confirmed this only
   afterwards, by listing the targets: there are 45, and they include
   `web/agent/index.html` as well as every module I was editing. The
   iCloud explanation was plausible, had precedent the same day, and was
   wrong about the mechanism — which is why it held for as long as it
   did.
2. **An unrelated test failure.** `test_positions` began failing on a
   test I had not touched. That was a mutation, applied by the harness,
   live on disk while I ran the suite.
3. **An inconsistent tree.** `sessions.py` kept my change while
   `evidence_class.py` lost it, leaving a call to a signature that no
   longer existed and a `TypeError` that made no sense from the code I
   thought I had.

What finally identified it was `git status`: three modified files, one of
which — `agent/journal/models.py` — I had never opened. It contained
`pass  # MUTATION`.

## Why the existing guards did not catch it

The harness already refuses to start if a previous run left mutations
behind, and refuses to start if the suite is red. Both are there because
of earlier incidents. Neither covers this one: the tree was clean of
mutations when I started and the baseline was green. The damage came
from writes that arrived *during* the run.

## Two consequences, and the second is the serious one

The run had to be discarded, which is cheap.

The run would have **reported results anyway**. A mutation whose file I
overwrote mid-run would have been measured against neither the original
nor the mutation, and the harness would have printed CAUGHT or SURVIVED
with equal confidence. A SURVIVED verdict would have sent me looking for
a missing test that was never missing. A CAUGHT verdict would have
credited a guard that had not been exercised. This harness exists
specifically to establish that guards can fail; a version of it that can
report unexamined confidence is worse than not running it.

## What now prevents it

Two additions, each verified by making it fire rather than by reading it:

- **A dirty working tree aborts the run**, exit 3 (NOT RUN, counting in
  neither the passed nor the failed denominator). Verified: appending a
  blank line to a mutation target produces the abort and exit 3.
- **Each verdict is discarded if the file changed while the suite ran.**
  The harness compares the file against exactly what it wrote before
  trusting the outcome, and reports `concurrent-write` rather than a
  pass or a fail.

The first makes the mistake hard to make. The second makes it impossible
to make silently, which matters more, because the first can be waived
and the second cannot.

## The lesson I will actually carry

A tool that rewrites the tree owns the tree for its duration. "Running
in the background" described where its output went, not what it was
doing — it was mutating the same files I was editing, and I had read the
code that does so.

And the diagnostic lesson, which is the one that cost the time: I had a
plausible cause for the first symptom — iCloud, observed earlier the
same day, same directory — and stopped looking. It was the wrong cause.
A known prior explained the *shape* of what I saw, so I never checked
the *mechanism*, and checking it was one command: list what the harness
rewrites.

The symptom that finally broke it open was the one I could not explain
away, a file I had never opened appearing in `git status`. That is the
symptom I should have gone hunting for immediately instead of
re-applying a patch and hoping, because "my edit vanished" has several
possible causes and `git status` distinguishes them in a second.

A corollary worth keeping: a committed file is safe, but the working
tree is not. The harness's final restore puts its startup snapshot back,
so commits made mid-run leave the tree behind HEAD afterwards. Check
`git status` and `git checkout --` the tree when a run ends.
