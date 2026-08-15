#!/usr/bin/env python3
"""End-to-end auth smoke test against the running local API."""

import getpass
import json
import secrets
import urllib.error
import urllib.request
from http.cookiejar import CookieJar

import server


BASE_URL = "http://127.0.0.1:8011"
VIEWER_EMAIL = "smoke-viewer@local.invalid"


def client():
    return urllib.request.build_opener(urllib.request.HTTPCookieProcessor(CookieJar()))


def request(opener, path, method="GET", body=None, csrf="", expected=200):
    headers = {"Origin": "http://127.0.0.1:8010"}
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    if csrf:
        headers["X-CSRF-Token"] = csrf
    try:
        response = opener.open(
            urllib.request.Request(BASE_URL + path, data=data, headers=headers, method=method)
        )
    except urllib.error.HTTPError as error:
        response = error
    payload = json.loads(response.read() or b"{}")
    assert response.status == expected, (path, response.status, payload)
    return payload


def main():
    admin_password = getpass.getpass("Admin password: ")
    anonymous = client()
    request(anonymous, "/api/stats", expected=401)

    admin = client()
    signed_in = request(
        admin,
        "/api/auth/login",
        "POST",
        {"email": "admin@kanidata.com", "password": admin_password},
    )["user"]
    assert signed_in["role"] == "admin"
    csrf = signed_in["csrf_token"]
    assert request(admin, "/api/auth/me")["user"]["role"] == "admin"
    request(admin, "/api/interviews", "POST", {}, csrf, expected=400)

    viewer_password = secrets.token_urlsafe(16)
    server.execute_sql(
        f"DELETE FROM app_users WHERE LOWER(BTRIM(email)) = {server.sql_text(VIEWER_EMAIL)};"
    )
    try:
        viewer = request(
            admin,
            "/api/admin/users",
            "POST",
            {"email": VIEWER_EMAIL, "password": viewer_password, "role": "user"},
            csrf,
            expected=201,
        )
        readonly = client()
        viewer_session = request(
            readonly,
            "/api/auth/login",
            "POST",
            {"email": VIEWER_EMAIL, "password": viewer_password},
        )["user"]
        request(readonly, "/api/stats")
        request(readonly, "/api/interviews", "POST", {}, viewer_session["csrf_token"], expected=403)
        request(
            admin,
            f"/api/admin/users/{viewer['id']}",
            "PATCH",
            {"is_active": False},
            csrf,
        )
    finally:
        server.execute_sql(
            f"DELETE FROM app_users WHERE LOWER(BTRIM(email)) = {server.sql_text(VIEWER_EMAIL)};"
        )
        request(admin, "/api/auth/logout", "POST", {}, csrf)
    print("smoke test passed: anonymous blocked, Admin manages, User reads only")


if __name__ == "__main__":
    main()
