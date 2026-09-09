---
name: Python workspace integrity
description: Interpreting missing Python submodules in restored task workspaces.
---

Treat missing submodules inside otherwise installed Python packages as a possible workspace dependency-integrity problem before changing application code or dependency versions.

**Why:** A restored workspace retained partial packages and metadata but lacked internal modules from several unrelated locked dependencies. Restoring the existing locked dependencies made the unchanged auth, storage, and extractor tests pass.

**How to apply:** When unrelated import failures cluster in installed packages, check dependency completeness first. Preserve the lockfile and application behavior while repairing the environment; do not weaken tests or security checks to accommodate missing package files.