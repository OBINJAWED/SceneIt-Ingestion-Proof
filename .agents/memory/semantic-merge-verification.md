---
name: Semantic merge verification
description: Silent Python test corruption observed during automatic task reconciliation.
---

Treat a clean automatic rebase as a merge result, not a correctness check.
Check Python syntax and discovered test identities after reconciliation,
including test files not listed as conflicts.

**Why:** Automatic reconciliation twice interleaved multiline imports and
similarly named setup methods from independent Python test classes without
flagging that test file. Some methods were also moved outside their classes,
which could silently reduce coverage after the syntax was repaired.

**How to apply:** Preserve incoming test cases, compare class/method identities
when reconstruction is needed, and isolate independent feature suites in
focused modules. Do not delete another feature's assertions to make discovery
pass, or rely on validation evidence from before reconciliation.