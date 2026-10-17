"""Generic Phase 5 Agent Action Protocol adapter.

No client-known business endpoint table. The authenticated platform supplies
the action catalog, authoritative project scope and exact durable receipts.
Formal project actions retain local allow-lists. Community actions use the
existing global remote-write permission plus an actor-bound server registry
and authoritative resource checks. The server always makes the final decision.
"""
import hashlib
from urllib.parse import quote

from ...persistence.index import canonical


def can_execute(sender,row):
    if row["action"]!="platform_action":
        return False,"not an Agent Action envelope"
    command=row["payload"]
    resource=command["resource"]
    is_community=resource["type"] in ("community","circle","post")
    if is_community:
        if not sender.config.allow_writes:
            return False,"outbound writes not enabled"
        # The server remains authoritative. Owner denial is the only local
        # per-social-action policy; no allowlist of permissible actions.
        if not sender.config.community_social_enabled:
            return False,"community social activity disabled by Owner"
        if command["action"] in sender.config.blocked_social_actions:
            return False,"community action explicitly blocked by Owner"
        if (resource["type"]+":"+resource["id"]
                in sender.config.blocked_social_resources):
            return False,"community resource explicitly blocked by Owner"
        if not command["action"].startswith("community."):
            return False,"non-community action cannot target community resource"
    elif command["action"] not in sender.config.allowed_platform_actions:
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
    path="/api/v1/agent/resources/"+quote(resource["type"],safe="")+"/"+quote(resource["id"],safe="")
    try:
        resolved=sender.client.request("GET",path)
    except Exception:
        return False,"resource scope lookup failed"
    if resolved.status!=200 or not isinstance(resolved.body,dict):
        return False,"resource currently inaccessible"
    if (resolved.body.get("type")!=resource["type"] or
            resolved.body.get("id")!=resource["id"]):
        return False,"resource scope identity mismatch"
    if resource["type"] in ("community","circle","post"):
        # A circle action may post into the same underlying group chat; do
        # not allow it to bypass an Owner prohibition on that chat ID.
        if resource["type"]=="circle":
            linked_chat=resolved.body.get("chatId")
            if (not isinstance(linked_chat,str) or not linked_chat):
                return False,"circle linked chat identity unavailable"
            if linked_chat in sender.config.blocked_chat_ids:
                return False,"linked chat explicitly blocked by Owner"
        # A public social directory entry is not permission to write.
        # Identity, current membership and author checks remain enforced
        # by the platform action handler at execution.
        if resource["type"]=="community" and resource["id"]!="community":
            return False,"unknown virtual community resource"
        if (resource["type"]=="circle" and
                command["action"]=="community.circle.message" and
                resolved.body.get("joined") is not True):
            return False,"circle message requires current membership"
    elif resolved.body.get("projectId") not in sender.config.allowed_project_action_ids:
        return False,"resource outside explicit project write scope"
    return True,None


def verify_execution(sender,row):
    # Exact requestId resolves crashed/ambiguous POST even if no response or
    # remote executionId was ever written to SQLite.
    command=row["payload"]
    # A read-only receipt lookup must remain possible even after the Owner
    # bans future writes or a resource leaves the registry. Never POST here.
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
            detail.get("requestId")!=command["requestId"]):
        return None
    # The authoritative handler may insert schema defaults. Compare every
    # caller-supplied key, avoiding cross-language numeric JSON encoding
    # discrepancies (e.g. 1.0 vs 1) while binding exact request identity.
    persisted=detail.get("input")
    if (not isinstance(persisted,dict) or
            any(k not in persisted or persisted[k]!=v
                for k,v in command["input"].items())):
        return None
    execution_id=detail.get("executionId")
    if not isinstance(execution_id,str) or not execution_id.startswith("exec_"):
        return None
    # Server proves commit in its own transaction; persist that receipt as
    # evidence, never trust an HTTP 2xx without this readback.
    return {"id":execution_id,
            "source":"CrewQuay durable Agent execution receipt",
            "sha256":hashlib.sha256(canonical(detail).encode("utf-8")).hexdigest()}
