# Changelog

## Unreleased

- Security: on a Neotoma definition timeout, a local Claude child may run
  with a restricted tool floor instead of `*`; Codex, Cursor, and Fly
  degraded children refuse before launch. Notices distinguish no output
  from a restricted child run. Degraded output cannot sign review gates.
