"""Typed platform task actions. Server remains authoritative for every grant.

No arbitrary endpoint/HTTP-method execution. Verified evidence must identify
the exact resulting task revision, submission, review or formal decision.
"""
import hashlib
from urllib.parse import quote
from ...persistence.index import canonical

TASK_ACTIONS=frozenset(("task_claim","task_progress","task_submit",
                        "submission_review","project_decision"))

def task_scope(sender,row):
    action=row["action"]
    if action not in sender.config.allowed_task_actions:
        return False,"action not explicitly approved in allowed_task_actions"
    if action=="project_decision":
        project_id=row["target"]
    else:
        task_id=row["payload"]["taskId"] if action=="submission_review" else row["target"]
        try:
            task=sender.client.request("GET","/api/v1/tasks/"+quote(task_id,safe=""))
        except Exception:
            return False,"task lookup unavailable"
        if task.status!=200 or not isinstance(task.body,dict):
            return False,"task not currently accessible"
        project_id=task.body.get("projectId")
        if action=="submission_review":
            try:
                submissions=sender.client.request(
                    "GET","/api/v1/tasks/"+quote(task_id,safe="")+"/submissions")
            except Exception:
                return False,"submission lookup unavailable"
            if (submissions.status!=200 or not isinstance(submissions.body,list)
                    or not any(isinstance(x,dict) and x.get("id")==row["target"]
                               for x in submissions.body)):
                return False,"submission does not belong to given task"
    if project_id not in sender.config.allowed_project_action_ids:
        return False,"project outside approved action scope"
    return True,None


def task_path(row):
    action=row["action"]
    target=quote(row["target"],safe="")
    if action=="project_decision":
        return "/api/v1/projects/"+target+"/decisions"
    if action=="submission_review":
        return "/api/v1/submissions/"+target+"/reviews"
    suffix={"task_claim":"/claim","task_progress":"/progress",
            "task_submit":"/submissions"}[action]
    return "/api/v1/tasks/"+target+suffix


def task_payload(row):
    if row["action"]=="submission_review":
        return {k:v for k,v in row["payload"].items() if k!="taskId"}
    return row["payload"]


def verify_task(sender,row,response):
    allowed,_=sender.can_send(row)
    if not allowed:
        return None
    action=row["action"]
    payload=row["payload"]
    message=response.get("message",response) if isinstance(response,dict) else None
    remote=message.get("id") if isinstance(message,dict) else None
    remote=remote or row.get("remote_id")
    if action in ("task_claim","task_progress"):
        r=sender.client.request("GET","/api/v1/tasks/"+quote(row["target"],safe=""))
        if r.status!=200 or not isinstance(r.body,dict):
            return None
        t=r.body
        if (t.get("id")!=row["target"] or t.get("assigneeId")!=row["actor"]
                or t.get("version")!=payload["expectedVersion"]+1):
            return None
        if action=="task_claim":
            if t.get("status")!="in_progress" or not t.get("assignmentId"):
                return None
        elif t.get("progress")!=payload["progress"]:
            return None
        if remote is not None and remote!=row["target"]:
            return None
        return {"id":row["target"],"source":"REST exact task version",
                "sha256":hashlib.sha256(canonical(t).encode()).hexdigest()}
    if action=="project_decision":
        if not remote:
            return None
        r=sender.client.request("GET","/api/v1/projects/"+
                                quote(row["target"],safe="")+"/decisions")
        data=r.body if r.status==200 and isinstance(r.body,list) else []
        for item in data:
            if (isinstance(item,dict) and item.get("id")==remote
                    and item.get("actorId")==row["actor"]
                    and item.get("content")==payload["content"]):
                return {"id":remote,"source":"REST formal decision",
                        "sha256":hashlib.sha256(canonical(item).encode()).hexdigest()}
        return None
    task_id=payload["taskId"] if action=="submission_review" else row["target"]
    if not remote:
        # Similar summaries/reviews do not prove the same POST occurred.
        return None
    suffix="/reviews" if action=="submission_review" else "/submissions"
    r=sender.client.request("GET","/api/v1/tasks/"+quote(task_id,safe="")+suffix)
    data=r.body if r.status==200 and isinstance(r.body,list) else []
    for item in data:
        if not isinstance(item,dict) or item.get("id")!=remote:
            continue
        if action=="task_submit":
            if (item.get("taskId")!=task_id or item.get("agentId")!=row["actor"]
                    or item.get("summary")!=payload["summary"]
                    or item.get("notes")!=payload["notes"]
                    or item.get("artifacts")!=payload["artifacts"]):
                continue
        elif (item.get("submissionId")!=row["target"]
              or item.get("reviewerId")!=row["actor"]
              or item.get("decision")!=payload["decision"]
              or item.get("feedback")!=payload["feedback"]
              or item.get("requiredChanges")!=payload["requiredChanges"]):
            continue
        return {"id":remote,"source":"REST exact submission/review ID",
                "sha256":hashlib.sha256(canonical(item).encode()).hexdigest()}
    return None
