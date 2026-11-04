"""Local account credentials for binding chat memory to a server-owned identity."""

import hashlib
import hmac
import os
import secrets
import sqlite3
import unicodedata
from contextlib import closing
from pathlib import Path

from pydantic import BaseModel, ConfigDict

_SCHEMA = """
CREATE TABLE IF NOT EXISTS local_accounts (
    id TEXT PRIMARY KEY,
    username TEXT NOT NULL UNIQUE,
    password_salt BLOB NOT NULL,
    password_hash BLOB NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
"""
_MIN_USERNAME = 3
_MAX_USERNAME = 64
_MIN_PASSWORD = 12
_MAX_PASSWORD = 1024
_SCRYPT_N = 1 << 14
_SCRYPT_R = 8
_SCRYPT_P = 1
_SCRYPT_MAXMEM = 64 * 1024 * 1024
_SALT_BYTES = 16
_HASH_BYTES = 32
_DUMMY_SALT = b"local-account-check"


class AccountError(Exception):
    """Base class for expected account operations failures."""


class InvalidAccountInput(AccountError):
    """Raised for invalid usernames or passwords during registration/login."""


class UsernameTaken(AccountError):
    """Raised when a normalized username already exists."""


class InvalidCredentials(AccountError):
    """Raised identically for unknown usernames and incorrect passwords."""


class Account(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    username: str


def _normalize_username(username: str) -> str:
    if not isinstance(username, str):
        raise InvalidAccountInput("Username must be text")
    normalized = unicodedata.normalize("NFKC", username).strip().casefold()
    if not _MIN_USERNAME <= len(normalized) <= _MAX_USERNAME or any(
        char.isspace() or not char.isprintable() for char in normalized
    ):
        raise InvalidAccountInput("Username must be 3–64 printable characters without spaces")
    return normalized


def _password_bytes(password: str) -> bytes:
    if not isinstance(password, str) or not _MIN_PASSWORD <= len(password) <= _MAX_PASSWORD:
        raise InvalidAccountInput("Password must be 12–1024 characters")
    if not password.strip():
        raise InvalidAccountInput("Password must not be blank")
    try:
        return password.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise InvalidAccountInput("Password must be valid Unicode text") from exc


def _derive(password: bytes, salt: bytes) -> bytes:
    return hashlib.scrypt(
        password,
        salt=salt,
        n=_SCRYPT_N,
        r=_SCRYPT_R,
        p=_SCRYPT_P,
        maxmem=_SCRYPT_MAXMEM,
        dklen=_HASH_BYTES,
    )


_DUMMY_HASH = _derive(b"not-a-real-account-password", _DUMMY_SALT)


class AccountStore:
    """SQLite account registry. The path is explicit and credentials never leave it."""

    def __init__(self, db_path: str | Path) -> None:
        self.path = Path(db_path)
        if str(db_path) == ":memory:":
            raise ValueError("use a file-backed SQLite path")
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._create_private_file()
        with closing(self._connect()) as db:
            db.executescript(_SCHEMA)

    def _create_private_file(self) -> None:
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            self.path.chmod(0o600)
        else:
            os.close(fd)

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        self.path.chmod(0o600)
        return db

    def register(self, username: str, password: str) -> Account:
        normalized = _normalize_username(username)
        password_value = _password_bytes(password)
        salt = secrets.token_bytes(_SALT_BYTES)
        password_hash = _derive(password_value, salt)
        account = Account(id=secrets.token_urlsafe(32), username=normalized)

        with closing(self._connect()) as db:
            try:
                db.execute("BEGIN IMMEDIATE")
                db.execute(
                    "INSERT INTO local_accounts (id, username, password_salt, password_hash) "
                    "VALUES (?, ?, ?, ?)",
                    (account.id, account.username, salt, password_hash),
                )
                db.commit()
            except sqlite3.IntegrityError as exc:
                db.rollback()
                if db.execute(
                    "SELECT 1 FROM local_accounts WHERE username=?", (normalized,)
                ).fetchone():
                    raise UsernameTaken("Username is already registered") from exc
                raise
        return account

    def login(self, username: str, password: str) -> Account:
        normalized = _normalize_username(username)
        password_value = _password_bytes(password)
        with closing(self._connect()) as db:
            row = db.execute(
                "SELECT id, username, password_salt, password_hash "
                "FROM local_accounts WHERE username=?",
                (normalized,),
            ).fetchone()

        if row is None:
            hmac.compare_digest(_derive(password_value, _DUMMY_SALT), _DUMMY_HASH)
            raise InvalidCredentials("Username or password is incorrect")

        actual_hash = _derive(password_value, row["password_salt"])
        if not hmac.compare_digest(actual_hash, row["password_hash"]):
            raise InvalidCredentials("Username or password is incorrect")
        return Account(id=row["id"], username=row["username"])
