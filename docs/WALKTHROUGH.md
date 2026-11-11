# One real run, step by step

This walkthrough replays one real Veridian run against a historical bug in the open-source
[more-itertools](https://github.com/more-itertools/more-itertools) library (issue #707,
upstream fix `cd0a3a8`). Nothing was merged or pushed.

| Repo | Bug | Wall time | Est. cost | Result |
| --- | --- | --- | --- | --- |
| more-itertools | `iterate()` can't be stopped by `func` | 36 s | $0.50 (Veridian's estimate) | Same source change as upstream |

## 1. An issue comes in

Veridian got the repo as it was just before the real fix (`7c1b648`), this issue text and the
name of the file to change. It never saw the real fix or the upstream tests added with it.

> **Bug:** `iterate(func, start)` cannot be stopped by `func`: when `func` raises
> `StopIteration` the generator fails with `RuntimeError` ('generator raised StopIteration')
> instead of finishing.
>
> **Acceptance:** with a `func` that returns `n * 2` but raises `StopIteration` once `n` is
> greater than 100, taking at most 10 items from `iterate(func, start=1)` must give exactly
> `[1, 2, 4, 8, 16, 32, 64, 128]` and then end cleanly. Only a `StopIteration` raised by `func`
> may end the iteration: throwing `StopIteration` into the generator must still raise
> `RuntimeError`. The documented doubling example must be unchanged, and all existing tests must
> keep passing.

Decision 1 of 2 is normally yours: approving the proposal before any code is written. In this
replay the test harness approved it for you.

## 2. The builder works in a sealed copy

The builder gets its own git worktree on its own branch. It can't touch the main checkout, and
it saves three checkpoint commits as it goes. This is the change it produced:

```diff
--- a/more_itertools/more.py
+++ b/more_itertools/more.py
@@ -475,7 +475,10 @@ def iterate(func, start):
     """
     while True:
         yield start
-        start = func(start)
+        try:
+            start = func(start)
+        except StopIteration:
+            break
```

```diff
--- a/tests/test_more.py
+++ b/tests/test_more.py
+class IterateTest(TestCase):
+    def test_basic(self):
+        res = list(islice(mi.iterate(lambda x: 2 * x, 1), 10))
+        self.assertEqual(res, [1, 2, 4, 8, 16, 32, 64, 128, 256, 512])
+
+    def test_func_controls_iteration_stop(self):
+        def func(num):
+            if num > 100:
+                raise StopIteration
+            return num * 2
+
+        res = list(islice(mi.iterate(func, start=1), 10))
+        self.assertEqual(res, [1, 2, 4, 8, 16, 32, 64, 128])
+
+    def test_runtime_error_keeps_to_raise(self):
+        it = mi.iterate(lambda x: 2 * x, start=1)
+        next(it)
+        with self.assertRaises(RuntimeError):
+            it.throw(StopIteration)
```

The source change is byte-for-byte the same as the real upstream fix: both turn file blob
`41a2fd8` into `1facd8e`. The tests went into the existing test file. See the limits below for
what that does and doesn't prove.

## 3. Nine checks before anyone looks at it

The verifier picks its own checks. Veridian then adds two that the verifier can't skip.

| # | Check | Run by | Result |
| --- | --- | --- | --- |
| 1 | `git status --short` | verifier | pass |
| 2 | `git diff --stat` | verifier | pass |
| 3 | `git diff -- more_itertools/more.py tests/test_more.py` | verifier | pass |
| 4 | `python3 -m py_compile more_itertools/more.py tests/test_more.py` | verifier | pass |
| 5 | `pytest tests/test_more.py -k "iterate or …" -q` | verifier | 3 passed |
| 6 | `pytest tests/test_more.py -q` | verifier | 481 passed |
| 7 | `pytest -q` | verifier | 595 passed |
| 8 | **new tests, run against the original code** | Veridian, always | **1 failed, as required** |
| 9 | `pytest -q` (whole suite) | Veridian, always | 595 passed |

Check 8 is the fake-test check. Veridian copies the new tests onto the code as it was before the
change and runs them there. A test that really tests the bug must fail on the old code:

```
FAILED tests/test_more.py::IterateTest::test_func_controls_iteration_stop
1 failed, 480 passed, 5 warnings, 2135 subtests passed in 1.57s
```

If every new test passed on the old code, the run would be flagged, because those tests would
prove nothing. Check 9 exists because a verifier may quietly leave out a suite its change breaks.

## 4. It stops here and waits for you

A verified run goes into review and stays there. By default nothing merges until you say so;
the only opt-in exception is a docs-only or tests-only change. Safety-relevant work also gets a
cold audit from a fresh reviewer before it reaches you.

- **Merge:** Veridian merges the run's branch into `main` with a merge commit (`--no-ff`), so
  the change stays one unit you can revert, records the decision and removes the run's worktree.
- **Reject with a reason:** Veridian stores the reason in that repo's reviewer memory and offers
  a **Fix** button. Fix rebuilds with your reason in the builder's context, runs every check
  again and stops for you again.

In this replay nothing was merged: the harness stops at the decision.

## 5. Graded against tests it never saw

After the run stopped, a separate harness applied Veridian's change to a fresh copy and ran the
real upstream tests that came with the fix. They passed. The full suite passed with no new
failures. Veridian stayed inside the expected folder and weakened no existing test.

This replay is one of six re-run with clearer acceptance text, and all six were correct. Across
19 distinct replays from this repo, 15 were correct, 2 missed because the issue text was
ambiguous, and 2 were honest fails, where Veridian's own checks failed and it rolled back. None
passed broken work that its own checks could have caught.

## What this run doesn't prove

- **The model may have seen this fix in training.** more-itertools is a popular public library,
  and a byte-identical fix could be recall rather than reasoning.
- **It was told which file to change.** The harness passed the file name, so this run doesn't
  test whether Veridian can find the right file.
- **No cold audit ran here.** In replays the run stops at the merge gate. The fresh-reviewer
  audit isn't simulated.
- **The sample is small.** It covers one small library with a fast, strong test suite, tuned on
  the same replays it was then re-run on.
