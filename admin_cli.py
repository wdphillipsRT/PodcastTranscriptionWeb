"""Explicit local provisioning for the database-enforced sole administrator."""

from contextlib import contextmanager

import click
from email_validator import EmailNotValidError, validate_email
from flask.cli import AppGroup
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from werkzeug.security import generate_password_hash

from extensions import db
from models import Admin


admin_cli = AppGroup("admin", help="Manage the sole administrator.")


def normalize_email(value):
    """Validate syntax without DNS or email services, then normalize for login."""
    return validate_email(value.strip(), check_deliverability=False).normalized.casefold()


@contextmanager
def _command_transaction():
    """Rollback validation, prompt, and database failures without exposing SQL."""
    try:
        yield
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        raise click.ClickException("Administrator update conflicts with existing data.") from None
    except SQLAlchemyError:
        db.session.rollback()
        raise click.ClickException("Unable to save administrator credentials.") from None
    except Exception:
        db.session.rollback()
        raise


def _password_hash():
    password = click.prompt("Password", hide_input=True, confirmation_prompt=True)
    if len(password) < 14:
        raise click.ClickException("Password must contain at least 14 characters.")
    return generate_password_hash(password)


@admin_cli.command("create")
@click.option("--email", help="Administrator email; prompted when omitted.")
def create_admin(email):
    """Create the sole administrator; passwords are always prompted secretly."""
    with _command_transaction():
        if db.session.scalar(db.select(Admin)) is not None:
            raise click.ClickException("An administrator already exists.")
        try:
            email = normalize_email(email if email is not None else click.prompt("Email"))
        except EmailNotValidError:
            raise click.ClickException("Enter a valid email address.") from None
        db.session.add(Admin(email=email, password_hash=_password_hash()))
    click.echo("Administrator created.")


@admin_cli.command("reset-password")
def reset_password():
    """Replace the existing administrator's password through a secret prompt."""
    with _command_transaction():
        admin = db.session.scalar(db.select(Admin))
        if admin is None:
            raise click.ClickException("No administrator exists; run admin create first.")
        admin.password_hash = _password_hash()
    click.echo("Administrator password reset.")
