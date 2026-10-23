import pytest


@pytest.fixture(autouse=True)
def isolated_user_state(tmp_path_factory, monkeypatch):
    """Tests must never reuse or write the operator's submission receipts."""
    from vaspsolkit.operations import activity

    monkeypatch.delenv("VASPSOLKIT_STATE_ROOT", raising=False)
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    monkeypatch.setattr(activity, "DEFAULT_STATE_ROOT", tmp_path_factory.mktemp("user-state"))
