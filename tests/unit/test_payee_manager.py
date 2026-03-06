import pytest

from moneywiz_api.managers.payee_manager import PayeeManager


class _PayeeStub:
    def __init__(self, record_id: int, gid: str, user: int, name: str = "n"):
        self.id = record_id
        self.gid = gid
        self.user = user
        self.name = name

    def __repr__(self) -> str:
        return f"Payee(id={self.id}, name='{self.name}', user={self.user})"


def test_add_allows_same_gid_for_different_users() -> None:
    manager = PayeeManager()
    manager.add(_PayeeStub(1, "same-gid", 2))
    manager.add(_PayeeStub(2, "same-gid", 3))

    assert manager.get(1) is not None
    assert manager.get(2) is not None


def test_add_rejects_same_gid_for_same_user() -> None:
    manager = PayeeManager()
    manager.add(_PayeeStub(1, "same-gid", 2))

    with pytest.raises(RuntimeError, match=r"Duplicate user\+gid"):
        manager.add(_PayeeStub(2, "same-gid", 2))
