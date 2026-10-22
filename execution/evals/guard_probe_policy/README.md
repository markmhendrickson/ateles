# Guard verification policy eval

Runs the actual sandbox preflight with synthetic fixtures, without provider
inference, network calls or real credentials. Protected ref read, create,
overwrite and rename effects must be denied while ordinary controls succeed.
The command instrument accepts only its fixed file-operation program.

Run with `python3 -m pytest execution/evals/guard_probe_policy -q` on macOS.
For one scenario, run `python3 execution/evals/guard_probe_policy/guard_probe_policy_harness.py --scenario intact`.

The removed-deny, bypassed-validation and forced-success mutations must fail
the scenario's acceptance check. Prohibited command plans exist only in memory
and are intercepted before subprocess execution, including mutation controls.
Linux retains the platform-independent fail-closed and plan-validation tests in
`execution/scripts/test_guard_probe_policy.py`; real Seatbelt effects cannot be
claimed from that platform.
