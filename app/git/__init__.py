"""Git branch-creation support (GitHub Enterprise only, for now).

Scope is deliberately narrow: create a remote branch from a configured base
branch and record it. There is no pull-request, merge, rebase, pipeline, branch
deletion or commit-tracking code anywhere in this package, and no provider
module performs network I/O at import time or during application startup.

Modules
-------
provider  Provider-agnostic Protocol, error taxonomy and error sanitizer.
github    GitHub Enterprise REST implementation of that Protocol.
naming    Deterministic, backend-owned branch-name generation.
branches  The create/reconcile flow for one work item + one repository.
"""
from __future__ import annotations
