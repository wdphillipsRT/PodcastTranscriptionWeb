"""Administrator credentials are provisioned only through explicit CLI actions."""

from unittest.mock import Mock

import pytest
from sqlalchemy.exc import IntegrityError
from werkzeug.security import check_password_hash

from extensions import db
from models import Admin


PASSWORD = "a-long-private-password"


@pytest.fixture
def database(app):
    with app.app_context():
        db.create_all()
        yield app
        db.session.remove()
        db.drop_all()


def create(runner, email="owner@example.com", password=PASSWORD):
    return runner.invoke(args=["admin", "create", "--email", email],
                         input=f"{password}\n{password}\n")


def test_create_normalizes_email_and_hashes_hidden_confirmed_password(database):
    result = create(database.test_cli_runner(), "  OWNER@Example.COM  ")
    assert result.exit_code == 0, result.output
    owner = db.session.scalar(db.select(Admin))
    assert owner.email == "owner@example.com"
    assert check_password_hash(owner.password_hash, PASSWORD)
    assert PASSWORD not in result.output
    assert owner.password_hash not in result.output
    assert "Repeat for confirmation" in result.output


@pytest.mark.parametrize("email", ["not-an-email", "owner@", "@example.com", "x\n@example.com"])
def test_create_rejects_invalid_email(database, email):
    result = create(database.test_cli_runner(), email)
    assert result.exit_code != 0
    assert "email" in result.output.lower()
    assert db.session.scalar(db.select(Admin)) is None


@pytest.mark.parametrize("command", ["create", "reset-password"])
def test_password_requires_fourteen_characters(database, command):
    if command == "reset-password":
        db.session.add(Admin(email="owner@example.com", password_hash="unchanged"))
        db.session.commit()
    args = ["admin", command]
    if command == "create":
        args += ["--email", "owner@example.com"]
    result = database.test_cli_runner().invoke(args=args, input="short-private\nshort-private\n")
    assert result.exit_code != 0
    assert "14" in result.output
    assert "short-private" not in result.output


def test_password_requires_confirmation(database):
    result = database.test_cli_runner().invoke(
        args=["admin", "create", "--email", "owner@example.com"],
        input=f"{PASSWORD}\na-different-password\n")
    assert result.exit_code != 0
    assert db.session.scalar(db.select(Admin)) is None
    assert PASSWORD not in result.output


def test_fourteen_character_password_is_accepted(database):
    assert create(database.test_cli_runner(), password="fourteen-chars").exit_code == 0


def test_second_admin_is_refused_by_command_and_database(database):
    runner = database.test_cli_runner()
    assert create(runner).exit_code == 0
    result = create(runner, "second@example.com")
    assert result.exit_code != 0
    assert "already" in result.output.lower()
    assert len(db.session.scalars(db.select(Admin)).all()) == 1
    db.session.add(Admin(email="bypass@example.com", password_hash="hash"))
    with pytest.raises(IntegrityError):
        db.session.commit()
    db.session.rollback()
    assert len(db.session.scalars(db.select(Admin)).all()) == 1


def test_reset_password_changes_only_existing_admin_hash(database):
    runner = database.test_cli_runner()
    assert create(runner).exit_code == 0
    owner = db.session.scalar(db.select(Admin))
    old_hash = owner.password_hash
    replacement = "another-private-password"
    result = runner.invoke(args=["admin", "reset-password"], input=f"{replacement}\n{replacement}\n")
    assert result.exit_code == 0, result.output
    db.session.refresh(owner)
    assert owner.email == "owner@example.com"
    assert owner.password_hash != old_hash
    assert check_password_hash(owner.password_hash, replacement)
    assert not check_password_hash(owner.password_hash, PASSWORD)
    assert replacement not in result.output
    assert owner.password_hash not in result.output


def test_reset_requires_an_existing_admin(database):
    result = database.test_cli_runner().invoke(args=["admin", "reset-password"])
    assert result.exit_code != 0
    assert "administrator" in result.output.lower()


@pytest.mark.parametrize("command", ["create", "reset-password"])
def test_integrity_failure_is_rolled_back_without_leaking_credentials(database, monkeypatch, command):
    runner = database.test_cli_runner()
    if command == "reset-password":
        assert create(runner).exit_code == 0
    rollback = Mock(wraps=db.session.rollback)
    monkeypatch.setattr(db.session, "rollback", rollback)
    monkeypatch.setattr(db.session, "commit", Mock(side_effect=IntegrityError(
        f"private SQL {PASSWORD}", {"password_hash": "private-hash"}, Exception("database detail"))))
    args = ["admin", command]
    if command == "create":
        args += ["--email", "owner@example.com"]
    result = runner.invoke(args=args, input=f"{PASSWORD}\n{PASSWORD}\n")
    assert result.exit_code != 0
    rollback.assert_called_once()
    assert PASSWORD not in result.output
    assert "private-hash" not in result.output
    assert "database detail" not in result.output
    assert db.session.is_active


@pytest.mark.parametrize("args,input_text", [
    (["admin", "create", "--email", "bad-email"], ""),
    (["admin", "create", "--email", "owner@example.com"], "short\nshort\n"),
    (["admin", "create", "--email", "owner@example.com"], ""),
    (["admin", "reset-password"], ""),
])
def test_command_failures_always_rollback(database, monkeypatch, args, input_text):
    rollback = Mock(wraps=db.session.rollback)
    monkeypatch.setattr(db.session, "rollback", rollback)
    result = database.test_cli_runner().invoke(args=args, input=input_text)
    assert result.exit_code != 0
    rollback.assert_called_once()
