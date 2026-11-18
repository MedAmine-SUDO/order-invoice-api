def test_login_returns_token_and_role(client, users):
    r = client.post(
        "/api/v1/auth/login", data={"username": "ADMIN@test.tn", "password": "secret123"}
    )
    assert r.status_code == 200
    assert r.json()["role"] == "admin"
    assert r.json()["token_type"] == "bearer"


def test_wrong_password_is_rejected(client, users):
    r = client.post("/api/v1/auth/login", data={"username": "admin@test.tn", "password": "nope"})
    assert r.status_code == 401


def test_me_requires_a_valid_token(client, admin_headers):
    assert client.get("/api/v1/auth/me").status_code == 401
    assert (
        client.get("/api/v1/auth/me", headers={"Authorization": "Bearer junk"}).status_code == 401
    )
    r = client.get("/api/v1/auth/me", headers=admin_headers)
    assert r.json()["email"] == "admin@test.tn"


def test_disabled_user_loses_access(client, admin_headers, cashier_headers, users):
    cashier_id = users["cashier"].id
    r = client.patch(
        f"/api/v1/users/{cashier_id}", json={"is_active": False}, headers=admin_headers
    )
    assert r.status_code == 200
    assert client.get("/api/v1/auth/me", headers=cashier_headers).status_code == 401


def test_cashier_cannot_manage_users(client, cashier_headers):
    assert client.get("/api/v1/users", headers=cashier_headers).status_code == 403


def test_admin_creates_user_and_email_is_unique(client, admin_headers):
    body = {"email": "new@test.tn", "full_name": "New", "password": "password1"}
    assert client.post("/api/v1/users", json=body, headers=admin_headers).status_code == 201
    assert client.post("/api/v1/users", json=body, headers=admin_headers).status_code == 409


def test_admin_cannot_demote_themselves(client, admin_headers, users):
    r = client.patch(
        f"/api/v1/users/{users['admin'].id}", json={"role": "cashier"}, headers=admin_headers
    )
    assert r.status_code == 409
