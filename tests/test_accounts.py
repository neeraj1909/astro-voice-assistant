import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from app.accounts import (
    AccountStore,
    InvalidAccountInput,
    InvalidCredentials,
    UsernameTaken,
)

PASSWORD = "correct horse battery staple"


def test_login_preserves_account_identity_across_new_sessions(tmp_path: Path) -> None:
    accounts = AccountStore(tmp_path / "accounts.sqlite3")
    registered = accounts.register("  Alice ", PASSWORD)

    first_login = accounts.login("alice", PASSWORD)
    second_login = accounts.login("ＡLICE", PASSWORD)

    assert registered.id == first_login.id == second_login.id
    assert registered.username == "alice"


def test_normalized_usernames_are_unique_and_accounts_are_distinct(tmp_path: Path) -> None:
    accounts = AccountStore(tmp_path / "accounts.sqlite3")
    alice = accounts.register("Alice", PASSWORD)
    bob = accounts.register("Bob", PASSWORD)

    assert alice.id != bob.id
    with pytest.raises(UsernameTaken):
        accounts.register(" ALICE ", "another sufficiently long password")


def test_unknown_user_and_wrong_password_have_same_failure(tmp_path: Path) -> None:
    accounts = AccountStore(tmp_path / "accounts.sqlite3")
    accounts.register("Alice", PASSWORD)

    with pytest.raises(InvalidCredentials) as wrong:
        accounts.login("Alice", "wrong password is long enough")
    with pytest.raises(InvalidCredentials) as unknown:
        accounts.login("Nobody", "wrong password is long enough")
    assert type(wrong.value) is type(unknown.value)
    assert str(wrong.value) == str(unknown.value)


def test_database_stores_only_salted_password_digest_and_restricts_file(tmp_path: Path) -> None:
    db_path = tmp_path / "accounts.sqlite3"
    accounts = AccountStore(db_path)
    accounts.register("Alice", PASSWORD)

    with sqlite3.connect(db_path) as db:
        salt, digest = db.execute(
            "SELECT password_salt, password_hash FROM local_accounts"
        ).fetchone()
    assert PASSWORD.encode() not in db_path.read_bytes()
    assert salt and digest and salt != digest
    assert db_path.stat().st_mode & 0o077 == 0


def test_registration_validates_bounded_username_and_password(tmp_path: Path) -> None:
    accounts = AccountStore(tmp_path / "accounts.sqlite3")
    invalid_credentials = [
        ("  ", PASSWORD),
        ("x" * 65, PASSWORD),
        ("ok", "short"),
        ("ok", " " * 12),
        ("ok", "x" * 1025),
    ]
    for username, password in invalid_credentials:
        with pytest.raises(InvalidAccountInput):
            accounts.register(username, password)


def test_concurrent_duplicate_registration_creates_one_account(tmp_path: Path) -> None:
    accounts = AccountStore(tmp_path / "accounts.sqlite3")

    def register():
        try:
            return accounts.register("same-user", PASSWORD)
        except UsernameTaken:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: register(), range(2)))
    assert sum(result is not None for result in results) == 1
