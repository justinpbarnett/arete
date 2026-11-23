from __future__ import annotations

import sys

from .. import client, credential_files


def cmd_login(args) -> None:
    text = sys.stdin.readline(257).strip()
    parts = credential_files.CLIENT_TEXT.fullmatch(text)
    if not parts or len(text) > 255:
        raise client.Failed({"error": "send one Arete bearer token on stdin"})
    where = client.server()
    if parts[1] != where.tag:
        raise client.Failed({"error": "the token belongs to another Arete instance"})
    response = client.request("GET", client.META, where=where, token=text, read_seconds=client.CONNECT_SECONDS)
    client.confirm_instance(where, client.instance_of(response))
    try:
        credential_files.write_client(where.tag, text)
    except OSError as e:
        raise client.Failed({"error": f"cannot store the token: {e}"}) from None
    print(f"Logged in to {client.address(where)}.")


def cmd_logout(args) -> None:
    where = client.server()
    text, directory = client.stored_credential(where)
    if not text:
        print("No stored token for this Arete instance.")
        return
    identity = credential_files.CLIENT_TEXT.fullmatch(text)[2]
    try:
        response = client.request("POST", f"/api/v1/tokens/{identity}/revoke", key=client.write_key(),
                                  where=where, token=text)
        client.close_response(response)
    except client.Failed as e:
        if e.code != 401 or client.reason(e.answer) not in ("unauthenticated", "token_rotated", "token_expired"):
            raise
    try:
        credential_files.forget_client(where.tag, text, directory)
    except OSError as e:
        raise client.Failed({"error": f"cannot remove the stored token: {e}"}) from None
    print(f"Logged out of {client.address(where)}.")


def cmd_pair(args) -> None:
    if client.server().scheme != "https":
        raise client.Failed({"error": "browser pairing needs HTTPS; set https = true in [dashboard]",
                             "reason": "tls_required"})
    answer = client.call("POST", "/api/v1/auth/pairings")
    print(f"Pairing code {client.field(answer, 'code')}")
    print(client.field(answer, "link"))


def register(sub) -> None:
    login = sub.add_parser("login", help="store a bearer token for this Arete instance")
    login.add_argument("--token-stdin", action="store_true", required=True, help="read the token from standard input")
    login.set_defaults(fn=cmd_login)
    sub.add_parser("logout", help="revoke and forget the stored bearer token").set_defaults(fn=cmd_logout)
    sub.add_parser("pair", help="print the code and link for the first browser pairing").set_defaults(fn=cmd_pair)
