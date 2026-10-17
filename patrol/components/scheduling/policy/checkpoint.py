"""Versioned, bounded task-progress envelope; not an LLM state snapshot.

Legacy free-form dict checkpoints are preserved for existing deployments.
A versioned checkpoint is strictly validated before being persisted and
all external references remain untrusted until re-read with current rights.
"""
import math
import re

_REF=re.compile(r"^[A-Za-z0-9._:/-]{1,160}$")
_ALLOWED=frozenset(("schema_version","goal","phase","completed_steps",
                    "next_actions","decisions","resource_refs",
                    "pending_outbox_ids","source_task_version","saved_at"))


def validate(data):
    if not isinstance(data,dict) or not data:
        raise ValueError("nonempty checkpoint object required")
    if "schema_version" not in data:
        return "legacy"
    if type(data["schema_version"]) is not int or data["schema_version"]!=1:
        raise ValueError("unsupported checkpoint schema_version")
    if set(data)!=_ALLOWED:
        raise ValueError("versioned checkpoint fields must match v1 exactly")
    if not isinstance(data["goal"],str) or not 1<=len(data["goal"].strip())<=2000:
        raise ValueError("checkpoint goal must be nonempty <=2000 chars")
    if not isinstance(data["phase"],str) or not 1<=len(data["phase"])<=80:
        raise ValueError("checkpoint phase must be 1..80 chars")
    for name in ("completed_steps","next_actions","decisions"):
        seq=data[name]
        if not isinstance(seq,list) or len(seq)>20 or any(
            not isinstance(s,str) or not 1<=len(s.strip())<=300 for s in seq
        ):
            raise ValueError("invalid bounded checkpoint "+name)
    refs=data["resource_refs"]
    if not isinstance(refs,list) or len(refs)>25:
        raise ValueError("checkpoint resource_refs must be bounded")
    for ref in refs:
        if (not isinstance(ref,dict) or set(ref)!={"type","id"}
                or not isinstance(ref["type"],str)
                or not isinstance(ref["id"],str)
                or not _REF.fullmatch(ref["type"])
                or not _REF.fullmatch(ref["id"])):
            raise ValueError("checkpoint references must be typed IDs, not content")
    ids=data["pending_outbox_ids"]
    if (not isinstance(ids,list) or len(ids)>25 or
            any(not isinstance(ident,str) or not _REF.fullmatch(ident)
                for ident in ids) or len(ids)!=len(set(ids))):
        raise ValueError("checkpoint outbox IDs must be unique bounded references")
    version=data["source_task_version"]
    if version is not None and (type(version) is not int or version<=0):
        raise ValueError("source_task_version must be positive or null")
    t=data["saved_at"]
    if (type(t) not in (int,float) or not math.isfinite(t) or t<0):
        raise ValueError("saved_at must be finite UTC epoch seconds")
    return "v1"
