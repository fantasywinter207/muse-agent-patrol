"""Generic Phase 5 Agent Action Protocol adapter.

No client-known business endpoint table. The authenticated platform supplies
the action catalog, authoritative project scope and exact durable receipts.
All permissions remain default deny locally and server-authoritative remotely.
"""
import hashlib
from urllib.parse import quote

from ...persistence.index import canonical


def can_execute(sender,row):
    if row["action"]!="platform_action":
        return False,"not an Agent Action envelope"
    command=row["payload"]
    if command["action"] not in sender.config.allowed_platform_actions:
        return False,"action not explicitly enabled"
    try:
        catalog=sender.client.request("GET","/api/v1/agent/actions")
    except Exception:
        return False,"action catalog unavailable"
    if catalog.status!=200 or not isinstance(catalog.body,dict):
        return False,"action catalog not accessible"
    if (catalog.body.get("protocolVersion")!="1" or
            catalog.body.get("actorId")!=row["actor"]):
        return False,"catalog actor or protocol mismatch"
    supported=any(
        isinstance(item,dict) and
        item.get("name")==command["action"] and
        item.get("version")==command["version"] and
        item.get("resourceType")==command["resource"]["type"]
        for item in catalog.body.get("actions",[])
    )
    if not supported:
        return False,"action absent from authenticated platform catalog"
    resource=command["resource"]
    path="/api/v1/agent/resources/"+quote(resource["type"],safe="")+"/"+quote(resource["id"],safe="")
    try:
        resolved=sender.client.request("GET",path)
    except Exception:
        return False,"resource scope lookup failed"
    if resolved.status!=200 or not isinstance(resolved.body,dict):
        return False,"resource currently inaccessible"
    if (resolved.body.get("type")!=resource["type"] or
            resolved.body.get("id")!=resource["id"] or
            resolved.body.get("projectId") not in sender.config.allowed_project_action_ids):
        return False,"resource outside explicit project write scope"
    return True,None


def verify_execution(sender,row):
    # Exact requestId resolves crashed/ambiguous POST even if no response or
    # remote executionId was ever written to SQLite.
    command=row["payload"]
    allowed,_=can_execute(sender,row)
    if not allowed:
        return None
    path="/api/v1/agent/executions/by-request/"+quote(command["requestId"],safe="")
    receipt=sender.client.request("GET",path)
    if receipt.status!=200 or not isinstance(receipt.body,dict):
        return None
    detail=receipt.body
    if (detail.get("protocolVersion")!="1" or
            detail.get("status")!="confirmed" or
            detail.get("actorId")!=row["actor"] or
            detail.get("action")!=command["action"] or
            detail.get("version")!=command["version"] or
            detail.get("resource")!=command["resource"] or
            detail.get("requestId")!=command["requestId"] or
            detail.get("requestHash")!=hashlib.sha256(
                canonical(command).encode("utf-8")).hexdigest()):
        return None
    execution_id=detail.get("executionId")
    if not isinstance(execution_id,str) or not execution_id.startswith("exec_"):
        return None
    # Server proves commit in its own transaction; persist that receipt as
    # evidence, never trust an HTTP 2xx without this readback.
    return {"id":execution_id,
            "source":"CrewQuay durable Agent execution receipt",
            "sha256":hashlib.sha256(canonical(detail).encode("utf-8")).hexdigest()}
