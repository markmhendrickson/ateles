from execution.daemons.apis.codex_delivery_probe import delivery_probe


def test_delivery_probe_returns_expected_message() -> None:
    assert delivery_probe() == "codex delivery verified"
