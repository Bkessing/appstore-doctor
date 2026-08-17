"""MCP server: expose the diagnostic checks as tools an agent can call directly.

This is a second front door onto the same code the CLI uses, not a new source
of truth. Every tool below calls straight into `checks/` and `asc.py` — the
same functions `__main__.main()` calls — so the read-only guarantee is
inherited rather than re-implemented: `asc.Client` only ever issues GET, and
nothing in this file constructs a request of its own.

MCP's stdio transport is newline-delimited JSON-RPC 2.0, one message per line
in each direction (unlike LSP, there is no Content-Length framing). This
hand-rolls that loop rather than depending on the official SDK, so the
zero-dependency promise in pyproject.toml stays true for the whole package,
not just the CLI half of it.

Protocol version implemented: 2024-11-05. The handshake is intentionally
small: initialize, tools/list, tools/call, and enough notification handling
to not blow up when a client sends one.
"""

import io
import json
import sys

from .__main__ import VERSION
from .asc import Client, MissingCredentials, ASCError
from .checks import signing, listing, readiness, certificates, project, surfaces
from .report import Report

PROTOCOL_VERSION = "2024-11-05"
SERVER_INFO = {"name": "appstore-doctor", "version": VERSION}


# -- shared helpers -----------------------------------------------------------

def _render(report):
    """Same text the CLI prints, minus ANSI — stdout here is JSON-RPC only,
    and color escape codes in a tool result would just be noise to an agent."""
    buf = io.StringIO()
    report.render(stream=buf, color=False)
    return buf.getvalue()


def _connect():
    """Build an ASC client, turning a credential problem into a message a
    tool can return instead of an exception that kills the session."""
    try:
        return Client(), None
    except MissingCredentials as err:
        return None, str(err)


def _find_app(client, bundle_id, app_id):
    if not (bundle_id or app_id):
        raise ValueError("give bundle_id or app_id")
    app = client.find_app(bundle_id=bundle_id, app_id=app_id)
    if not app:
        raise ValueError(f"no app matching {bundle_id or app_id} on this account")
    return app


# -- tool implementations ------------------------------------------------------
#
# Each takes the JSON-RPC `arguments` dict and returns plain text. They are
# kept separate from __main__.main() rather than sharing its body: main()'s
# shape is driven by argparse defaults (e.g. --local-only implies no
# credentials needed), and an MCP tool's arguments are a JSON schema, not a
# CLI flag set. Duplicating the ~15 lines of orchestration was less risk than
# bending one to fit the other's calling convention.

def tool_diagnose(args):
    """Full run: local signing checks, plus every App Store Connect check if
    a bundle id or app id is given. Mirrors `appstore-doctor --bundle ...`."""
    bundle_id = args.get("bundle_id")
    app_id = args.get("app_id")
    project_dir = args.get("project")
    local_only = bool(args.get("local_only"))

    report = Report()
    signing.run(report, bundle_id=bundle_id)

    client, app = None, None
    if not local_only:
        if not (bundle_id or app_id):
            report.skip(
                "listing", "no bundle_id or app_id given, skipping App Store Connect checks",
                fix="Pass bundle_id to also check availability, version state, screenshots\n"
                    "and subscription metadata.",
            )
        else:
            client, err = _connect()
            if err:
                report.skip("listing", "App Store Connect credentials not configured",
                            detail=err)
            else:
                try:
                    certificates.run(report, client)
                    app = client.find_app(bundle_id=bundle_id, app_id=app_id)
                    if not app:
                        report.fail(
                            "listing.app",
                            f"no app matching {bundle_id or app_id} on this account",
                            fix="Check the bundle id, and that the API key belongs to the right team.",
                        )
                    else:
                        version = listing.run(report, client, app)
                        readiness.run(report, client, app, version)
                        surfaces.run(report, client, app)
                except ASCError as err:
                    report.fail("listing", "could not reach App Store Connect", detail=str(err))

    if project_dir:
        project.run(report, project_dir, client=client, app=app)

    return _render(report)


def tool_list_apps(args):
    """Every app the API key can see, with bundle id and numeric App id —
    the inputs every other tool here needs."""
    client, err = _connect()
    if err:
        return f"credentials not configured: {err}"
    try:
        apps = client.apps()
    except ASCError as err:
        return f"could not reach App Store Connect: {err}"

    if not apps:
        return "no apps visible to this API key"

    rows = []
    for app in apps:
        attrs = app.get("attributes", {})
        rows.append(f"- {attrs.get('name', '(unnamed)')}  "
                    f"bundleId={attrs.get('bundleId')}  id={app.get('id')}")
    return "\n".join(rows)


def tool_version_state(args):
    """Current App Store version state, whether a build is attached, whether
    a rejected submission is holding the version hostage, and IDFA status."""
    client, err = _connect()
    if err:
        return f"credentials not configured: {err}"

    report = Report()
    try:
        app = _find_app(client, args.get("bundle_id"), args.get("app_id"))
    except (ValueError, ASCError) as err:
        return str(err)

    version = listing.check_versions(report, client, app)
    listing.check_stuck_submission(report, client, app)
    if version:
        listing.check_idfa(report, version)
    return _render(report)


def tool_iap_status(args):
    """State of every in-app purchase and subscription, and whether the app
    is on sale with none of them approved — the "on sale but takes no
    money" case that reads as healthy from every other signal."""
    client, err = _connect()
    if err:
        return f"credentials not configured: {err}"

    report = Report()
    try:
        app = _find_app(client, args.get("bundle_id"), args.get("app_id"))
    except (ValueError, ASCError) as err:
        return str(err)

    versions = client.versions(app["id"], limit=1)
    if not versions:
        return "no App Store versions exist for this app; cannot resolve IAP eligibility"

    readiness.check_iaps(report, client, app, versions[0])
    return _render(report)


def tool_signing_check(args):
    """Local code-signing checks: identities, keychain lock state, and
    provisioning profiles. Needs no App Store Connect credentials at all."""
    report = Report()
    signing.run(report, bundle_id=args.get("bundle_id"))
    return _render(report)


# name -> (JSON schema for arguments, description, handler)
_TOOLS = {
    "diagnose": {
        "description": "Full read-only diagnosis for one app: local signing checks, plus "
                        "availability, version state, screenshots, subscription metadata, "
                        "IAP readiness and unclaimed free surfaces if a bundle_id or app_id "
                        "is given.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "bundle_id": {"type": "string", "description": "e.g. com.acme.app"},
                "app_id": {"type": "string", "description": "numeric App Store id, if known"},
                "project": {"type": "string",
                            "description": "path to the Xcode project directory, for local "
                                           "icon/privacy-manifest/build-number checks"},
                "local_only": {"type": "boolean",
                               "description": "skip App Store Connect checks entirely"},
            },
        },
        "handler": tool_diagnose,
    },
    "list_apps": {
        "description": "List every app visible to the configured API key, with its bundle "
                        "id and numeric App Store id.",
        "inputSchema": {"type": "object", "properties": {}},
        "handler": tool_list_apps,
    },
    "version_state": {
        "description": "Current App Store version state for one app: is it live, editable, "
                        "or stuck behind a rejected review submission, and is a build "
                        "attached.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "bundle_id": {"type": "string"},
                "app_id": {"type": "string"},
            },
        },
        "handler": tool_version_state,
    },
    "iap_status": {
        "description": "State of every in-app purchase and subscription for one app, "
                        "including whether a live app can currently take money.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "bundle_id": {"type": "string"},
                "app_id": {"type": "string"},
            },
        },
        "handler": tool_iap_status,
    },
    "signing_check": {
        "description": "Local code-signing diagnostics for this Mac: signing identities, "
                        "keychain lock state, provisioning profiles. No credentials needed.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "bundle_id": {"type": "string",
                              "description": "narrow profile checks to this bundle id"},
            },
        },
        "handler": tool_signing_check,
    },
}


def _tool_list():
    return [
        {"name": name, "description": spec["description"], "inputSchema": spec["inputSchema"]}
        for name, spec in _TOOLS.items()
    ]


def _tool_result(text, is_error=False):
    return {"content": [{"type": "text", "text": text}], "isError": is_error}


def _call_tool(name, arguments):
    spec = _TOOLS.get(name)
    if spec is None:
        return _tool_result(f"unknown tool: {name}", is_error=True)
    try:
        return _tool_result(spec["handler"](arguments or {}))
    except Exception as err:  # noqa: BLE001 - a bad call must not take the server down
        return _tool_result(f"{type(err).__name__}: {err}", is_error=True)


# -- JSON-RPC plumbing ---------------------------------------------------------

def _send(message):
    sys.stdout.write(json.dumps(message) + "\n")
    sys.stdout.flush()


def _result(msg_id, result):
    return {"jsonrpc": "2.0", "id": msg_id, "result": result}


def _error(msg_id, code, message):
    return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}


def _handle_initialize(msg_id, params):
    return _result(msg_id, {
        "protocolVersion": PROTOCOL_VERSION,
        "capabilities": {"tools": {}},
        "serverInfo": SERVER_INFO,
    })


def _handle_tools_list(msg_id, params):
    return _result(msg_id, {"tools": _tool_list()})


def _handle_tools_call(msg_id, params):
    params = params or {}
    return _result(msg_id, _call_tool(params.get("name"), params.get("arguments")))


def _handle_ping(msg_id, params):
    return _result(msg_id, {})


_METHODS = {
    "initialize": _handle_initialize,
    "tools/list": _handle_tools_list,
    "tools/call": _handle_tools_call,
    "ping": _handle_ping,
}


def dispatch(msg):
    """Handle one decoded JSON-RPC message, return a response dict or None.

    A JSON-RPC notification is identified by the absence of an "id" member
    (not by id being null) and MUST NOT get a response -- the client sends
    "notifications/initialized" this way right after initialize, and
    answering it would itself be a protocol violation.
    """
    if "id" not in msg:
        return None

    method = msg.get("method")
    msg_id = msg["id"]

    handler = _METHODS.get(method)
    if handler is None:
        return _error(msg_id, -32601, f"method not found: {method}")

    try:
        return handler(msg_id, msg.get("params"))
    except Exception as err:  # noqa: BLE001 - one bad request must not kill the loop
        return _error(msg_id, -32603, f"{type(err).__name__}: {err}")


def run_stdio(in_stream=None, out_stream=None):
    """Read newline-delimited JSON-RPC requests from stdin, write responses
    to stdout, until stdin closes. Kept as a plain function (rather than a
    class) so a test can drive it against StringIO without touching real
    stdio."""
    in_stream = in_stream or sys.stdin
    out_stream = out_stream or sys.stdout

    for line in in_stream:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            out_stream.write(json.dumps(_error(None, -32700, "parse error")) + "\n")
            out_stream.flush()
            continue

        response = dispatch(msg)
        if response is not None:
            out_stream.write(json.dumps(response) + "\n")
            out_stream.flush()


def main():
    run_stdio()


if __name__ == "__main__":
    main()
