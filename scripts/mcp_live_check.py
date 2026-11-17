"""
Live end-to-end check of the Birdy MCP connector against a running local backend.

Drives the real OAuth flow the way Claude / ChatGPT / Cursor do (discovery, dynamic
client registration, /authorize with PKCE, owner consent, token exchange, refresh
rotation) and then talks to /mcp with the FastMCP streamable-HTTP client.

NOT part of the pytest run (it lives in scripts/, outside tests/). It writes to the
database named in .env (a shared dev cluster) using one throwaway user, and removes
everything it created in a finally block.

Run (backend must already be up):
    API_PUBLIC_URL=http://localhost:8000 FRONTEND_URL=http://localhost:3005 \
      uvicorn main:app --port 8000
    MCP_LIVE_CHECK=1 python scripts/mcp_live_check.py
"""

import asyncio
import base64
import hashlib
import json
import os
import secrets
import sys
import time
from datetime import datetime
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

if os.getenv("MCP_LIVE_CHECK") != "1":
    sys.exit("Refusing to run: set MCP_LIVE_CHECK=1 (this writes a throwaway user to the .env database).")

import httpx  # noqa: E402
from fastmcp import Client  # noqa: E402
from motor.motor_asyncio import AsyncIOMotorClient  # noqa: E402

import core.config  # noqa: E402,F401  (loads .env before DB_NAME is read)
from core.database import DB_NAME  # noqa: E402
from dependencies import generate_tokens  # noqa: E402
from ai.agents import store as agent_store  # noqa: E402

BASE = os.getenv("LIVE_BASE", "http://localhost:8000")
FRONTEND = os.getenv("FRONTEND_URL", "http://localhost:3005").rstrip("/")
# Runs after core.config has loaded .env, so FRONTEND_URL from .env is checked too. No network or DB call has happened yet.
# Refuse non-local targets: this script registers OAuth clients and writes a user to the .env database.
_remote = [u for u in (BASE, FRONTEND)
           if (urlparse(u).hostname or "") not in ("localhost", "127.0.0.1", "::1")]
if _remote and os.getenv("MCP_LIVE_CHECK_ALLOW_REMOTE") != "1":
    sys.exit(f"Refusing to run against a non-local target: {', '.join(_remote)}. "
             "LIVE_BASE and FRONTEND_URL must be localhost, 127.0.0.1 or ::1 "
             "(set MCP_LIVE_CHECK_ALLOW_REMOTE=1 as well to override).")

USER = f"mcp-verify-{int(time.time())}@example.com"
REDIRECT = "http://localhost:9999/callback"

results: list[tuple[str, bool, str]] = []
registered_clients: list[str] = []


def check(name: str, ok: bool, detail: str = ""):
    results.append((name, bool(ok), detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f"  -- {detail}" if detail else ""))


def short(obj, n=420):
    s = json.dumps(obj, default=str)
    return s if len(s) <= n else s[:n] + f"...(+{len(s) - n} chars)"


# --------------------------------------------------------------------------- seed

async def seed(db):
    await db["users"].insert_one({
        "user_id": USER, "email": USER, "name": "MCP Verify",
        "subscription": {"status": "active", "plan": "starter"},
        "created_at": datetime.utcnow(),
    })
    await db["client_groups"].insert_one({
        "id": f"verify-group-{int(time.time())}", "user_id": USER, "name": "Verify Dental", "created_at": datetime.utcnow(),
    })
    agent = await agent_store.create_agent(db, USER, type=agent_store.TYPE_STOCK)
    run_id, card_id = f"verify-run-{int(time.time())}", f"verify-card-{int(time.time())}"
    await db[agent_store.RUNS].insert_one({
        "_id": run_id, "agent_id": agent["_id"], "user_id": USER, "kind": "batch", "phase": "done",
        "as_of": datetime.utcnow(), "summary": {"cards": 1}, "clients": {}, "alerts": [],
    })
    await db[agent_store.CARDS].insert_one({
        "_id": card_id, "agent_id": agent["_id"], "user_id": USER, "run_id": run_id,
        "client_group_id": "verify-group", "client_name": "Verify Dental", "tag": "changes", "status": "open",
        "lines": [{"id": "l1", "kind": "remove", "ad_name": "Verify Ad", "mode": "ask", "state": "proposed"}],
        "thought_process": "t", "next_steps": "n", "follow_ups": [], "created_at": datetime.utcnow(),
    })
    return agent, run_id, card_id


# --------------------------------------------------------------------------- OAuth

async def oauth_flow(http: httpx.AsyncClient, meta: dict, session_cookie: str, access: str, label: str) -> dict:
    """Register a client, authorize with PKCE, approve via the consent API, exchange the code."""
    reg = await http.post(meta["registration_endpoint"], json={
        "client_name": f"live-check-{label}", "redirect_uris": [REDIRECT], "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"], "token_endpoint_auth_method": "none",
    })
    check(f"[{label}] dynamic client registration", reg.status_code in (200, 201), f"HTTP {reg.status_code}")
    client = reg.json()
    registered_clients.append(client["client_id"])

    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    state = secrets.token_urlsafe(8)
    auth = await http.get(meta["authorization_endpoint"], params={
        "response_type": "code", "client_id": client["client_id"], "redirect_uri": REDIRECT, "code_challenge": challenge,
        "code_challenge_method": "S256", "state": state, "scope": "birdy.read birdy.write",
        "resource": f"{BASE}/mcp",
    }, follow_redirects=False)
    loc = auth.headers.get("location", "")
    check(f"[{label}] /authorize redirects to the consent page", auth.status_code in (302, 303, 307)
          and loc.startswith(f"{FRONTEND}/connect/authorize?request="), f"HTTP {auth.status_code} -> {loc}")
    request_id = parse_qs(urlparse(loc).query)["request"][0]

    cookies = {"auth_token": session_cookie}
    view = await http.get(f"{BASE}/api/oauth/requests/{request_id}", cookies=cookies)
    check(f"[{label}] consent view loads", view.status_code == 200, short(view.json()))

    bad = await http.post(f"{BASE}/api/oauth/requests/{request_id}/decision", cookies=cookies,
                          content=json.dumps({"allow": True, "access": access}), headers={"Content-Type": "text/plain", "Origin": FRONTEND})
    check(f"[{label}] decision POST as text/plain is refused (CSRF guard)", bad.status_code == 415, f"HTTP {bad.status_code}")

    dec = await http.post(f"{BASE}/api/oauth/requests/{request_id}/decision", cookies=cookies,
                          json={"allow": True, "access": access}, headers={"Origin": FRONTEND})
    check(f"[{label}] owner approves with access={access}", dec.status_code == 200, f"HTTP {dec.status_code}")
    redirect_to = dec.json()["redirect_to"]
    qs = parse_qs(urlparse(redirect_to).query)
    check(f"[{label}] redirect carries code and state", "code" in qs and qs.get("state") == [state], redirect_to.split("?")[0])

    tok = await http.post(meta["token_endpoint"], data={
        "grant_type": "authorization_code", "code": qs["code"][0], "redirect_uri": REDIRECT,
        "client_id": client["client_id"], "code_verifier": verifier,
    })
    check(f"[{label}] code exchange returns tokens", tok.status_code == 200, f"HTTP {tok.status_code}")
    tokens = tok.json()
    replay = await http.post(meta["token_endpoint"], data={
        "grant_type": "authorization_code", "code": qs["code"][0], "redirect_uri": REDIRECT,
        "client_id": client["client_id"], "code_verifier": verifier,
    })
    check(f"[{label}] code replay is refused", replay.status_code == 400, f"HTTP {replay.status_code}")
    return {"client": client, "tokens": tokens}


async def call(token: str, name: str, args: dict | None = None):
    async with Client(f"{BASE}/mcp", auth=token) as c:
        return await c.call_tool(name, args or {}, raise_on_error=False)


async def main():
    mongo = AsyncIOMotorClient(os.environ["MONGODB_URI"])
    db = mongo[DB_NAME]
    try:
        agent, run_id, card_id = await seed(db)
        print(f"Seeded user {USER}, stock agent {agent['_id']}, run {run_id}, card {card_id}")
        session_access, _ = await generate_tokens(USER)

        async with httpx.AsyncClient(timeout=60) as http:
            # 1. discovery
            as_meta = (await http.get(f"{BASE}/.well-known/oauth-authorization-server")).json()
            check("discovery: authorization-server metadata", all(
                k in as_meta for k in ("issuer", "authorization_endpoint", "token_endpoint", "registration_endpoint")),
                short({k: as_meta.get(k) for k in ("issuer", "authorization_endpoint", "token_endpoint", "registration_endpoint",
                                                  "code_challenge_methods_supported", "scopes_supported")}))
            pr = await http.get(f"{BASE}/.well-known/oauth-protected-resource/mcp")
            check("discovery: protected-resource metadata", pr.status_code == 200
                  and pr.json().get("resource", "").rstrip("/") == f"{BASE}/mcp", short(pr.json()))
            # /mcp answers 307 -> /mcp/ (Starlette trailing-slash redirect); clients follow it.
            unauth = await http.post(f"{BASE}/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"}, follow_redirects=True)
            check("/mcp without a token is 401 with a resource_metadata hint", unauth.status_code == 401
                  and "resource_metadata" in unauth.headers.get("www-authenticate", ""), f"HTTP {unauth.status_code} {unauth.headers.get('www-authenticate', '')[:140]}")

            # 2. write flow
            w = await oauth_flow(http, as_meta, session_access, "write", "write")
            wtok = w["tokens"]["access_token"]

            # tool list
            async with Client(f"{BASE}/mcp", auth=wtok) as c:
                tools = await c.list_tools()
            check("tools/list over streamable HTTP", len(tools) > 100, f"{len(tools)} tools")

            r = await call(wtok, "check_my_accounts")
            check("check_my_accounts", not r.is_error, short(r.structured_content or r.content))
            r = await call(wtok, "get_agent_overview")
            ov = r.structured_content or {}
            check("get_agent_overview sees the seeded card", not r.is_error and ov.get("open_cards") == 1, short(ov))

            before_card = await db[agent_store.CARDS].find_one({"_id": card_id})
            r = await call(wtok, "apply_card", {"card_id": card_id})
            sc = r.structured_content or {}
            check("apply_card preview returns a confirm_code, nothing applied", not r.is_error and bool(sc.get("confirm_code")), short(sc))
            after_card = await db[agent_store.CARDS].find_one({"_id": card_id})
            check("card and lines unchanged after the preview", before_card == after_card,
                  f"status={after_card.get('status')} lines={[(l['id'], l['state']) for l in after_card['lines']]}")

            r = await call(wtok, "refresh_status", {"client": "Verify Dental"})
            check("refresh_status", not r.is_error, short(r.structured_content or r.content))
            r = await call(wtok, "list_slack_reports")
            check("list_slack_reports", not r.is_error, short(r.structured_content or r.content))
            r = await call(wtok, "get_fired_alerts")
            check("get_fired_alerts", not r.is_error, short(r.structured_content or r.content))

            # refresh rotation
            rt = w["tokens"]["refresh_token"]
            rot = await http.post(as_meta["token_endpoint"], data={
                "grant_type": "refresh_token", "refresh_token": rt, "client_id": w["client"]["client_id"]})
            check("refresh token rotates once", rot.status_code == 200 and rot.json()["refresh_token"] != rt, f"HTTP {rot.status_code}")
            new = rot.json()
            r = await call(new["access_token"], "check_my_accounts")
            check("rotated access token works", not r.is_error)
            old = await http.post(as_meta["token_endpoint"], data={
                "grant_type": "refresh_token", "refresh_token": rt, "client_id": w["client"]["client_id"]})
            check("old refresh token replay is refused", old.status_code == 400, f"HTTP {old.status_code}")
            newer = await http.post(as_meta["token_endpoint"], data={
                "grant_type": "refresh_token", "refresh_token": new["refresh_token"], "client_id": w["client"]["client_id"]})
            check("the newest refresh token still rotates after a replay attempt", newer.status_code == 200,
                  f"HTTP {newer.status_code} (informational: replay may revoke the grant)")

            # 3. read flow
            rd = await oauth_flow(http, as_meta, session_access, "read", "read")
            rtok = rd["tokens"]["access_token"]
            r = await call(rtok, "check_my_accounts")
            check("read token allowed on check_my_accounts", not r.is_error)
            r = await call(rtok, "apply_card", {"card_id": card_id})
            check("read token refused on apply_card", r.is_error, short([getattr(x, "text", x) for x in r.content]))
            check("no confirm code issued to the read token", await db["mcp_confirm_codes"].count_documents(
                {"user_id": USER, "tool": "apply_card"}) <= 1)

            # 4. revoke
            conns = await http.get(f"{BASE}/api/mcp/connections", cookies={"auth_token": session_access})
            rows = conns.json()
            rows = rows.get("connections", rows) if isinstance(rows, dict) else rows
            check("connections list shows both OAuth grants", len([x for x in rows if x.get("kind") == "oauth"]) == 2, short(rows, 300))
            target = next(x for x in rows if x.get("name") == "live-check-write")
            before = await call(wtok, "check_my_accounts")
            dele = await http.delete(f"{BASE}/api/mcp/connections/{target['id']}", cookies={"auth_token": session_access},
                                     headers={"Origin": FRONTEND})
            check("DELETE /api/mcp/connections/{id}", dele.status_code in (200, 204), f"HTTP {dele.status_code}")
            check("(sanity) the call worked just before revoking", not before.is_error)
            try:
                after = await call(wtok, "check_my_accounts")
                refused = after.is_error
                detail = short([getattr(x, "text", x) for x in after.content])
            except Exception as e:  # a 401 from the transport surfaces as an exception
                refused, detail = True, f"{type(e).__name__}: {str(e)[:160]}"
            check("very next call with the old access token is refused after revoke", refused, detail)
            dead = await http.post(as_meta["token_endpoint"], data={
                "grant_type": "refresh_token", "refresh_token": newer.json().get("refresh_token", ""),
                "client_id": w["client"]["client_id"]})
            check("refresh token of the revoked grant is dead", dead.status_code == 400, f"HTTP {dead.status_code}")
    finally:
        try:
            clean = await cleanup(db)
        finally:
            mongo.close()

    if not clean:
        print("CLEANUP INCOMPLETE: documents for the throwaway user remain (see above).")
        sys.exit(2)
    failed = [r for r in results if not r[1]]
    print(f"\n{len(results) - len(failed)}/{len(results)} checks passed")
    if failed:
        print("FAILED:", *[f"\n  - {n}: {d}" for n, _, d in failed])
        sys.exit(1)


async def cleanup(db):
    print("\n--- cleanup ---")
    names = await db.list_collection_names()
    deleted: dict[str, int] = {}
    for n in names:
        try:
            res = await db[n].delete_many({"user_id": USER}, comment="mcp_live_check cleanup") if n != "users" else None
        except Exception:
            continue
        if res is not None and res.deleted_count:
            deleted[n] = res.deleted_count
    res = await db["users"].delete_many({"$or": [{"user_id": USER}, {"email": USER}]})
    deleted["users"] = res.deleted_count
    if registered_clients:
        res = await db["oauth_clients"].delete_many({"client_id": {"$in": registered_clients}})
        deleted["oauth_clients"] = res.deleted_count
        for coll in ("oauth_requests", "oauth_codes", "oauth_grants", "oauth_refresh_tokens", "mcp_tokens"):
            res = await db[coll].delete_many({"client_id": {"$in": registered_clients}})
            if res.deleted_count:
                deleted[coll] = deleted.get(coll, 0) + res.deleted_count
    print("deleted:", json.dumps(deleted, sort_keys=True))
    # verify zero everywhere
    leftovers = {}
    for n in names:
        try:
            c = await db[n].count_documents({"user_id": USER}, maxTimeMS=8000)
        except Exception:
            continue
        if c:
            leftovers[n] = c
    leftovers_u = await db["users"].count_documents({"$or": [{"user_id": USER}, {"email": USER}]})
    named = ["users", "client_groups", "agents", "agent_runs", "agent_cards", "oauth_requests", "oauth_codes",
             "oauth_refresh_tokens", "oauth_grants", "mcp_tokens", "mcp_rate", "mcp_confirm_codes", "report_drafts",
             "ai_chat_sessions", "ai_conversation_log", "ai_usage", "alert_notifications"]
    print(f"verify (user_id={USER}):")
    for n in named:
        if n == "users":
            c = leftovers_u
        else:
            c = await db[n].count_documents({"user_id": USER}) if n in names else 0
        print(f"  {n:24s} {c}{'' if n in names else '  (collection absent)'}")
    c = await db["oauth_clients"].count_documents({"client_id": {"$in": registered_clients}})
    print(f"  {'oauth_clients (ours)':24s} {c}")
    print(f"  scanned {len(names)} collections for user_id; leftovers: {leftovers or 'none'}")
    return not leftovers and not leftovers_u


if __name__ == "__main__":
    asyncio.run(main())
