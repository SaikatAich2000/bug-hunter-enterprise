"""Onboarding preferences: stored per user on the server, scoped to the caller."""
from __future__ import annotations

URL = "/api/me/preferences"


def test_requires_login(client):
    assert client.get(URL).status_code == 401
    assert client.put(URL, json={"welcome_seen": True}).status_code == 401


def test_defaults_then_partial_merge(admin_client):
    assert admin_client.get(URL).json() == {
        "welcome_seen": False, "welcome_hidden": False, "tour_status": "none", "tour_step": 0,
    }
    r = admin_client.put(URL, json={"welcome_seen": True, "tour_status": "running", "tour_step": 2})
    assert r.status_code == 200
    r = admin_client.put(URL, json={"welcome_hidden": True})
    assert r.json() == {
        "welcome_seen": True, "welcome_hidden": True, "tour_status": "running", "tour_step": 2,
    }
    assert admin_client.get(URL).json()["tour_step"] == 2


def test_rejects_bad_values(admin_client):
    assert admin_client.put(URL, json={"tour_status": "bogus"}).status_code == 422
    assert admin_client.put(URL, json={"tour_step": -1}).status_code == 422
    assert admin_client.put(URL, json={"user_id": 5}).status_code == 422


def test_two_simultaneous_first_writes_both_succeed(admin_client):
    """The first PUT creates the row. Two at once must not collide on the primary key."""
    import threading

    from sqlalchemy import select

    from app.database import SessionLocal
    from app.models import User, UserPreference
    from app.routes.preferences import PreferencesIn, update_preferences

    with SessionLocal() as db:
        user = db.scalar(select(User))
        db.expunge(user)
    barrier = threading.Barrier(2)
    outcomes: list[object] = []

    def worker(payload):
        barrier.wait()
        with SessionLocal() as db:
            try:
                outcomes.append(update_preferences(payload, db, user))
            except Exception as exc:  # noqa: BLE001 - the test asserts there are none
                outcomes.append(exc)

    threads = [
        threading.Thread(target=worker, args=(PreferencesIn(welcome_seen=True),)),
        threading.Thread(target=worker, args=(PreferencesIn(tour_step=3),)),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert len(outcomes) == 2 and not [o for o in outcomes if isinstance(o, Exception)], outcomes
    with SessionLocal() as db:
        assert len(db.scalars(select(UserPreference)).all()) == 1


def test_existing_row_is_kept_not_reset_by_the_create_step(admin_client):
    admin_client.put(URL, json={"tour_step": 4})
    assert admin_client.put(URL, json={"welcome_seen": True}).json()["tour_step"] == 4


def test_state_is_per_user(client, admin_client):
    admin_client.put(URL, json={"welcome_seen": True})
    client.post("/api/users", json={
        "name": "Other", "email": "other@test.local", "role": "user", "password": "User12345",
    })
    client.post("/api/auth/logout")
    assert client.post("/api/auth/login", json={
        "email": "other@test.local", "password": "User12345"}).status_code == 200
    assert client.get(URL).json()["welcome_seen"] is False
