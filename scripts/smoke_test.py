"""Verify local login, protected data reads and logout without paid API calls."""
from __future__ import annotations

import argparse
import http.cookiejar
import json
from pathlib import Path
import urllib.error
import urllib.request


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:8765")
    args = parser.parse_args()
    config = Path(__file__).resolve().parents[1] / "backend" / ".env"
    values = {}
    for line in config.read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            k, v = line.split("=", 1)
            values[k.strip()] = v.strip().strip('"').strip("'")
    password = values.get("ACCESS_PASSWORD", "")
    assert len(password) >= 12, "Configure a local password first"
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))

    def request(path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(args.url.rstrip("/") + path, data=data,
                                     headers={"Content-Type": "application/json"})
        try:
            response = opener.open(req, timeout=20)
        except urllib.error.HTTPError as e:
            response = e
        raw = response.read().decode()
        for secret in (password, values.get("SECRET_KEY", "")):
            assert not secret or secret not in raw, "Response must not expose credentials"
        return response.status, json.loads(raw)

    assert request("/api/health")[0] == 200
    assert request("/api/me")[0] == 401
    assert request("/api/sessions")[0] == 401
    assert request("/api/login", {"password": "intentionally-wrong"})[0] == 401
    assert request("/api/login", {"password": password})[0] == 200
    code, me = request("/api/me")
    assert code == 200 and me["auth"] == "password"
    code, sessions = request("/api/sessions")
    assert code == 200 and isinstance(sessions, list)
    assert request("/api/logout", {})[0] == 200
    assert request("/api/me")[0] == 401
    print("PASS: health, authentication, protected records, credential redaction and logout")


if __name__ == "__main__":
    main()
