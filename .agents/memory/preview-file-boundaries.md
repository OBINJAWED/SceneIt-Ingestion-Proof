---
name: Preview file boundaries
description: Why API authentication alone cannot protect files inside this monorepo's development workspace.
---

Keep development preview filesystem access scoped to each frontend and its
shared code dependencies, including secondary component-preview artifacts.

**Why:** Vite's default strict filesystem setting can still authorize the whole
pnpm workspace. A direct `/@fs` request returned the original workspace MP4
without touching Flask or its pilot admission checks. Another preview artifact
on the same domain can reopen that path even if the main frontend is restricted.

**How to apply:** When adding or changing a preview service, verify it cannot
read sibling backend files or uploaded/source media directly. Explicitly
imported public design assets are different from private evidence; do not
restore broad workspace access merely to make an asset load.