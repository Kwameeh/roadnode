#!/usr/bin/env python3
"""Mint one device credential and install it in EMQX.

Runs on the Pi. Writes the credential to DEVICE_CREDENTIAL_FILE (0600) and
pushes the same identity into EMQX's built-in authentication database together
with the exact-topic publish rules from `device_identity`, so the broker
enforces what the device already enforces locally.

EMQX's management listener is bound to the server's loopback, so reach it
through an SSH tunnel first:

    ssh -N -L 18083:127.0.0.1:18083 user@your-server

    .venv/bin/python scripts/provision-device.py \
      --emqx-password "$EMQX_DASHBOARD_PASSWORD"
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from car_telemetry.device_identity import (  # noqa: E402
    DeviceCredential,
    load_credential,
    provision,
    rotate,
    save_credential,
)
from car_telemetry.emqx_provision import (  # noqa: E402
    EmqxProvisioner,
    HttpResponse,
    diff_expected_rules,
)


class UrllibClient:
    """Minimal EMQX REST client. Bearer token or API key, never both."""

    def __init__(self, base_url: str, authorization: str, timeout: float = 15.0):
        self._base = base_url.rstrip("/")
        self._authorization = authorization
        self._timeout = timeout

    def request(self, method, path, *, payload=None) -> HttpResponse:
        data = None
        headers = {"Authorization": self._authorization, "Accept": "application/json"}
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(
            f"{self._base}{path}", data=data, headers=headers, method=method
        )
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:
                body = response.read()
                return HttpResponse(response.status, _maybe_json(body))
        except urllib.error.HTTPError as exc:  # 409 and 404 are meaningful here
            return HttpResponse(exc.code, _maybe_json(exc.read()))


def _maybe_json(raw: bytes):
    if not raw:
        return None
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return raw.decode("utf-8", errors="replace")


def dashboard_token(base_url: str, username: str, password: str) -> str:
    """Exchange dashboard credentials for a bearer token."""
    request = urllib.request.Request(
        f"{base_url.rstrip('/')}/api/v5/login",
        data=json.dumps({"username": username, "password": password}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            body = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise SystemExit(f"EMQX login failed ({exc.code}): {exc.read().decode('utf-8', 'replace')}")
    except urllib.error.URLError as exc:
        raise SystemExit(f"EMQX unreachable at {base_url}: {exc.reason}")
    token = body.get("token")
    if not token:
        raise SystemExit(f"EMQX login returned no token: {body}")
    return f"Bearer {token}"


def env_value(env_file: Path, key: str, default: str) -> str:
    if not env_file.is_file():
        return default
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line.startswith(f"{key}=") and not line.startswith("#"):
            return line.split("=", 1)[1].strip() or default
    return default


def main() -> int:
    project_dir = Path(__file__).resolve().parent.parent
    env_file = project_dir / "config" / "telemetry.env"

    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--device-id", default=None, help="defaults to DEVICE_ID in config/telemetry.env")
    parser.add_argument("--credential-file", default=None, help="defaults to DEVICE_CREDENTIAL_FILE in config/telemetry.env")
    parser.add_argument("--valid-for-days", type=int, default=365)
    parser.add_argument("--rotate", action="store_true", help="issue a new secret for an existing credential")
    parser.add_argument("--audit", action="store_true", help="read back what the broker enforces, change nothing")
    parser.add_argument("--emqx-url", default="http://127.0.0.1:18083")
    parser.add_argument("--emqx-username", default="admin")
    parser.add_argument("--emqx-password", default=None, help="dashboard password; omit when using --api-key")
    parser.add_argument("--api-key", default=None)
    parser.add_argument("--api-secret", default=None)
    parser.add_argument("--local-only", action="store_true", help="write the credential file, skip EMQX")
    args = parser.parse_args()

    device_id = args.device_id or env_value(env_file, "DEVICE_ID", "PROTO-001")
    credential_path = Path(
        args.credential_file
        or env_value(
            env_file,
            "DEVICE_CREDENTIAL_FILE",
            "~/.local/share/car-telemetry/device-credential.json",
        )
    ).expanduser()

    provisioner = None
    if not args.local_only:
        if args.api_key and args.api_secret:
            import base64

            raw = f"{args.api_key}:{args.api_secret}".encode("utf-8")
            authorization = "Basic " + base64.b64encode(raw).decode("ascii")
        elif args.emqx_password:
            authorization = dashboard_token(args.emqx_url, args.emqx_username, args.emqx_password)
        else:
            raise SystemExit("Pass --emqx-password, or --api-key with --api-secret, or --local-only")
        provisioner = EmqxProvisioner(UrllibClient(f"{args.emqx_url.rstrip('/')}/api/v5", authorization))

    if args.audit:
        if provisioner is None:
            raise SystemExit("--audit needs broker access; drop --local-only")
        report = provisioner.audit(device_id)
        problems = diff_expected_rules(device_id, report.get("rules"))
        print(json.dumps({**report, "problems": problems}, indent=2))
        return 0 if not problems and report["credentialPresent"] else 1

    if args.rotate:
        credential: DeviceCredential = rotate(
            load_credential(credential_path), valid_for_days=args.valid_for_days
        )
    else:
        if credential_path.exists():
            raise SystemExit(
                f"{credential_path} already exists. Use --rotate to issue a new secret."
            )
        credential = provision(device_id, valid_for_days=args.valid_for_days)

    if provisioner is not None:
        provisioner.apply(credential)
        print(f"EMQX: user and publish rules installed for {credential.device_id}")

    save_credential(credential_path, credential)
    print(f"Credential written to {credential_path} (version {credential.credential_version})")
    print("Restart the engine to pick it up:  sudo systemctl restart car-telemetry.service")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
