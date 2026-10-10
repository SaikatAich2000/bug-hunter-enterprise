"""B.2: one account per address whatever its case, across every entry point."""
from __future__ import annotations

import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

from tests.conftest import new_client, sign_up

PASSWORD = "Passw0rd!x"


def _insert_user(org_id, email):
    """A row written straight to the table, the way a racing writer would."""
    from app.database import SessionLocal
    from app.models import User

    with SessionLocal() as db:
        db.add(User(org_id=org_id, name="Legacy", email=email, role="user", password_hash="x"))
        db.commit()


def test_db_refuses_a_case_variant_of_an_existing_email(client):
    from sqlalchemy.exc import IntegrityError

    _, me = sign_up("Acme", "owner@acme.test")
    with pytest.raises(IntegrityError):
        _insert_user(me["org_id"], "Owner@Acme.test")


def test_every_entry_point_blocks_an_address_taken_in_another_org(client):
    owner, _ = sign_up("Acme", "owner@acme.test")
    other, _ = sign_up("Other Org", "boss@other.test")
    body = {"name": "Owner", "email": "OWNER@acme.test", "password": PASSWORD, "organization_name": "Xylo Inc"}
    assert new_client().post("/api/auth/signup", json=body).status_code == 409
    # an invitation / user / email change to another org's address never succeeds
    assert owner.post("/api/invitations", json={"email": "Boss@Other.test", "role": "user"}).status_code == 409
    res = owner.post("/api/users", json={"name": "Dup", "email": "BOSS@other.test", "role": "user",
                                         "password": "User12345"})
    assert res.status_code == 409
    res = owner.post("/api/auth/email-change/request",
                     json={"current_password": PASSWORD, "new_email": "Boss@Other.test"})
    assert res.status_code == 409
    assert other.get("/api/auth/me").json()["email"] == "boss@other.test"


def test_accepting_an_invitation_cannot_duplicate_an_address_by_case(client):
    from app.auth import generate_token
    from app.database import SessionLocal
    from app.models import Invitation

    owner, me = sign_up("Acme", "owner@acme.test")
    assert owner.post("/api/invitations", json={"email": "new@acme.test", "role": "user"}).status_code == 201
    raw, token_hash = generate_token()
    with SessionLocal() as db:
        db.query(Invitation).filter_by(email="new@acme.test").one().token_hash = token_hash
        db.commit()
    # the address is registered (another spelling) between invite and accept
    from app.database import engine
    from sqlalchemy import text
    with engine.begin() as c:
        c.execute(text("DROP INDEX uq_users_email_lower"))
    _insert_user(me["org_id"], "NEW@acme.test")
    res = new_client().post("/api/invitations/accept", json={"token": raw, "name": "New", "password": PASSWORD})
    assert res.status_code == 409


def test_email_change_cannot_take_a_case_variant_even_when_the_app_check_is_bypassed(client, monkeypatch):
    # the unique index is the backstop when two confirmations race past the application check
    from app.routes import auth as auth_routes

    owner, _ = sign_up("Acme", "owner@acme.test")
    other, _ = sign_up("Other Org", "boss@other.test")
    monkeypatch.setattr(auth_routes, "_email_taken", lambda *a, **k: False)
    assert owner.post("/api/auth/email-change/request",
                      json={"current_password": PASSWORD, "new_email": "boss@other.test"}).status_code == 202
    from app.database import SessionLocal
    from app.models import EmailChangeRequest
    from app.auth import hash_token
    with SessionLocal() as db:
        db.query(EmailChangeRequest).one().code_hash = hash_token("123456")
        db.commit()
    res = owner.post("/api/auth/email-change/confirm", json={"code": "123456"})
    assert res.status_code == 409
    assert owner.get("/api/auth/me").json()["email"] == "owner@acme.test"


def test_login_finds_a_legacy_mixed_case_row(client):
    from app.database import engine
    from sqlalchemy import text

    _, me = sign_up("Acme", "owner@acme.test")
    with engine.begin() as c:
        c.execute(text("UPDATE users SET email = 'Owner@Acme.test' WHERE id = :i"), {"i": me["id"]})
    res = new_client().post("/api/auth/login", json={"email": "owner@acme.test", "password": PASSWORD})
    assert res.status_code == 200


def test_index_exists_after_init_and_init_is_idempotent(client):
    from sqlalchemy import text

    from app.database import engine, init_db

    assert init_db() is True
    with engine.connect() as c:
        sql = c.execute(text("SELECT sql FROM sqlite_master WHERE name = 'uq_users_email_lower'")).scalar()
    assert sql and "lower(email)" in sql.lower()


def test_postgres_enforces_the_lower_email_index():
    admin_url = os.environ.get("BH_TEST_POSTGRES_URL", "")
    if not admin_url:
        pytest.skip("set BH_TEST_POSTGRES_URL to run the PostgreSQL identity test")
    from sqlalchemy import create_engine, text

    name = f"bh_idtest_{uuid.uuid4().hex[:10]}"
    admin = create_engine(admin_url, isolation_level="AUTOCOMMIT")
    with admin.connect() as c:
        c.execute(text(f'CREATE DATABASE "{name}"'))
    url = f"{admin_url.rpartition('/')[0]}/{name}"
    code = (
        "from sqlalchemy.exc import IntegrityError\n"
        "from app.database import SessionLocal, init_db\n"
        "from app.models import Organization, User\n"
        "assert init_db() is True\n"
        "assert init_db() is True\n"
        "with SessionLocal() as db:\n"
        "    org = Organization(name='O', slug='o'); db.add(org); db.flush()\n"
        "    db.add(User(org_id=org.id, name='A', email='a@x.test', password_hash='x')); db.commit()\n"
        "    db.add(User(org_id=org.id, name='B', email='A@X.test', password_hash='x'))\n"
        "    try:\n"
        "        db.commit(); print('NOT REFUSED')\n"
        "    except IntegrityError:\n"
        "        print('REFUSED')\n"
    )
    env = {**os.environ, "DATABASE_URL": url, "APP_VERSION": "0.0.0-test", "PYTHONUTF8": "1",
           "OTEL_EXPORTER_OTLP_ENDPOINT": ""}
    try:
        out = subprocess.run([sys.executable, "-c", code], cwd=Path(__file__).resolve().parents[1],
                             env=env, capture_output=True, text=True, timeout=300)
        assert "REFUSED" in out.stdout and "NOT REFUSED" not in out.stdout, out.stdout + out.stderr[-2000:]
    finally:
        with admin.connect() as c:
            c.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        admin.dispose()
