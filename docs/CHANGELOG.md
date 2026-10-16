# Changelog

## Unreleased

- Security: on a Neotoma definition timeout, agents now dispatch with a
  minimal read-only allowlist instead of `*` when the harness can bind that
  floor. A harness that cannot bind it refuses the degraded child. Degraded
  output cannot sign review gates.
