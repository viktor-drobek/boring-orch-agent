"""Steps for the HTTP discovery-consent and API error-shape review scenarios.

They drive the production ``api.make_server`` over loopback, the production
``Store`` and ``Discovery``; fault injection is limited to one patched method.
"""
import http.client
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
from unittest.mock import patch

from behave import given, then, when

from boring_agent.api import make_server
from boring_agent.discovery import Discovery
from boring_agent.model import StorageError
from boring_agent.store import Store
from tests.support.http_provider import completion, server

REVIEW_SECRET_ENV = "BOA_REVIEW_DISCOVERY_SECRET"
REVIEW_SECRET = "review-fixture-credential-value"


def _review_store(context):
    if not hasattr(context, "review_store"):
        root = Path(context.resources.enter_context(tempfile.TemporaryDirectory(prefix="boa-review-")))
        (root / "workspace").mkdir()
        context.review_store = Store(root / "state")
        context.review_store.initialize(root / "workspace")
    return context.review_store


def _review_api(context, token=""):
    store = _review_store(context)
    api = make_server(store, "127.0.0.1", 0, auth_token=token)
    thread = threading.Thread(target=api.serve_forever, kwargs={"poll_interval": .01}, daemon=True)
    thread.start()

    def stop():
        api.shutdown()
        api.server_close()
        thread.join(2)

    context.resources.callback(stop)
    context.review_address = api.server_address[:2]
    context.review_token = token


def _review_request(context, method, path, payload=None, headers=None, raw=None):
    body = raw if raw is not None else (None if payload is None else json.dumps(payload).encode())
    sent = {}
    if context.review_token:
        sent["Authorization"] = "Bearer " + context.review_token
    if body is not None:
        sent["Content-Type"] = "application/json"
    sent.update(headers or {})
    connection = http.client.HTTPConnection(*context.review_address, timeout=10)
    try:
        connection.request(method, path, body=body, headers=sent)
        response = connection.getresponse()
        data = response.read()
        try:
            value = json.loads(data)
        except ValueError:
            value = {"unparsed": data.decode("utf-8", "replace")}
        context.review_response = (response.status, value)
    except (http.client.HTTPException, OSError) as exc:
        # A dropped connection is exactly the defect under review; record it as data.
        context.review_response = (f"connection dropped: {exc!r}", None)
    finally:
        connection.close()
    return context.review_response


def _shell_route():
    return {"id": "review-shell", "executable": "/bin/sh", "args": ["-c", "id -un; echo review-pwned"]}


@given("a token-less loopback API over a fresh review store")
def token_less_review_api(context):
    _review_api(context, "")


@given('a review API that requires the bearer token "{token}"')
def token_review_api(context, token):
    _review_api(context, token)


@when("an HTTP caller posts a shell command route to the discovery approve route")
def http_approve_shell(context):
    _review_request(context, "POST", "/api/v1/discovery/approve", {"route": _shell_route(), "tier": "handshake"})


@when("an HTTP caller asks the review API to handshake that shell command route")
def http_handshake_shell(context):
    approvals = context.review_store.discovery_approvals()
    approval_id = approvals[0]["id"] if approvals else "review-no-such-approval"
    _review_request(context, "POST", "/api/v1/discovery/handshake",
                    {"route": _shell_route(), "approval_id": approval_id, "timeout": 5})


@then('the review API answers {status:d} with error "{code}"')
def review_error(context, status, code):
    observed, body = context.review_response
    assert observed == status and body and body.get("error") == code and isinstance(body.get("message"), str), \
        f"expected {status} {code!r}, observed {observed} {body}"


@then('the review API answers {status:d} with probe status "{probe}"')
def review_probe_status(context, status, probe):
    observed, body = context.review_response
    assert observed == status and body and body.get("status") == probe, (observed, body)


@then("the review API answers {status:d} with a task receipt")
def review_task_receipt(context, status):
    observed, body = context.review_response
    assert observed == status and body and body.get("task_id"), (observed, body)
    assert [task["id"] for task in context.review_store.tasks()] == [body["task_id"]]


@then("the review store holds no discovery approval")
def review_no_approval(context):
    approvals = _review_store(context).discovery_approvals()
    assert approvals == [], f"orphan or HTTP-granted approvals exist: {approvals}"


@then("the review store holds no discovery evidence")
def review_no_evidence(context):
    evidence = context.review_store.discovery_evidence()
    assert evidence == [], f"a probe ran: {evidence}"


@then("the review store holds no task")
def review_no_task(context):
    tasks = context.review_store.tasks()
    assert tasks == [], f"rejected requests created tasks: {[task['id'] for task in tasks]}"


@given("the operator approves a generative review route that sends its credential to the approved listener")
def operator_generative_route(context):
    reply = (200, completion("openai", {"capability": "review"}))
    context.approved_url, context.approved_requests, _, _ = context.resources.enter_context(server([reply]))
    context.attacker_url, context.attacker_requests, _, _ = context.resources.enter_context(server([reply]))
    context.resources.enter_context(patch.dict(os.environ, {REVIEW_SECRET_ENV: REVIEW_SECRET}))
    context.review_route = {"id": "review-generative", "executable": sys.executable, "provider": "openai",
                            "base_url": context.approved_url, "model": "review-model",
                            "credential_ref": "env:" + REVIEW_SECRET_ENV}
    context.review_approval = Discovery(context.review_store).approve(
        context.review_route, "generative", cost_policy={"max_requests": 1, "operator": "review"})


@when("an HTTP caller runs that generative review probe against its own base URL")
def http_generative_redirected(context):
    route = {**context.review_route, "base_url": context.attacker_url}
    _review_request(context, "POST", "/api/v1/discovery/generative",
                    {"route": route, "approval_id": context.review_approval["approval_id"], "timeout": 5})


@when("an HTTP caller runs that generative review probe exactly as approved")
def http_generative_approved(context):
    _review_request(context, "POST", "/api/v1/discovery/generative",
                    {"route": context.review_route, "approval_id": context.review_approval["approval_id"],
                     "timeout": 5})


@then("neither review listener has received a request")
def no_listener_requests(context):
    assert context.approved_requests == [] and context.attacker_requests == [], \
        (context.approved_requests, context.attacker_requests)


@then("only the approved review listener received the credential, exactly once")
def approved_listener_only(context):
    assert context.attacker_requests == [], context.attacker_requests
    assert len(context.approved_requests) == 1, context.approved_requests
    assert context.approved_requests[0]["headers"].get("authorization") == "Bearer " + REVIEW_SECRET


def _post_review_task(context, key, headers):
    _review_request(context, "POST", "/api/v1/tasks", {"objective": "review task", "runtime": "demo"},
                    {"Idempotency-Key": key, **headers})


@when('a review task is posted with Host header "{host}"')
def post_foreign_host(context, host):
    _post_review_task(context, "review-host", {"Host": host})


@when('a review task is posted with Content-Type "{content_type}"')
def post_foreign_type(context, content_type):
    _post_review_task(context, "review-type", {"Content-Type": content_type})


@when("a review task is posted as loopback JSON")
def post_loopback_json(context):
    _post_review_task(context, "review-json", {})


@given("a fresh review store whose discovery audit insert fails")
def failing_audit_store(context):
    _review_store(context)
    context.resources.enter_context(patch.object(
        Discovery, "_audit_db", side_effect=sqlite3.OperationalError("injected audit failure")))


@when("the operator approves a handshake review route despite the failing audit")
def approve_despite_failing_audit(context):
    context.review_error = None
    try:
        Discovery(context.review_store).approve({"id": "review-atomic", "executable": sys.executable,
                                                 "args": ["--version"]})
    except StorageError as exc:
        context.review_error = exc


@then("the review approval fails with a storage error")
def approval_storage_error(context):
    assert isinstance(context.review_error, StorageError), context.review_error


@when("an HTTP caller sends a non-ASCII bearer token to the review API")
def non_ascii_bearer(context):
    _review_request(context, "GET", "/api/v1/health", headers={"Authorization": "Bearer réview-token"})


@given("the operator approves a handshake review route")
def operator_handshake_route(context):
    context.review_route = {"id": "review-handshake", "executable": sys.executable, "args": ["--version"]}
    context.review_approval = Discovery(context.review_store).approve(context.review_route)


@when('an HTTP caller asks the review API to handshake that route with max_output_bytes "{value}"')
def http_handshake_bad_bound(context, value):
    _review_request(context, "POST", "/api/v1/discovery/handshake",
                    {"route": context.review_route, "approval_id": context.review_approval["approval_id"],
                     "max_output_bytes": value})


@when("an HTTP caller reads an unknown review workflow")
def unknown_review_workflow(context):
    _review_request(context, "GET", "/api/v1/workflows/review-no-such-workflow")


@when("the review capacity read fails with an internal error")
def failing_capacity(context):
    context.review_internal = "review internal detail /secret/path"
    with patch.object(context.review_store, "capacity", side_effect=RuntimeError(context.review_internal)):
        _review_request(context, "GET", "/api/v1/capacity")


@then("the review error message does not reveal the internal failure")
def no_internal_leak(context):
    assert context.review_internal not in json.dumps(context.review_response[1]), context.review_response
