"""Platform re-run verification (v1.6.2 D22-D34).

The platform keeps its own copy of the code and tests (a verification suite), applies the agent's change to the
non-test files and runs the platform's tests before and after, inside the sandbox. Results are monitor-only: they never
change a verdict, re-dispatch a run, publish to Kafka or touch token leases (AGENTS.md 2.3).
"""
