"""Discover approved public social candidates and atomically admit Scheduler work.

The server's descriptions, titles and excerpts are deliberately NOT persisted.
No untrusted content is interpreted as tool instructions, and no external
mutation is issued by this component.
"""
import hashlib
import math
import re

from ...persistence.index import new_id

ACTIONS={
    ("post","community.post.comment"),
    ("circle","community.circle.join"),
    ("circle","community.circle.message"),
}
ID=re.compile(r"^(post|cir)_[A-Za-z0-9_-]{1,112}$")
WINDOW=86400
MIN_TARGET_SPACING=900
MAX_PER_TARGET_DAY=2
MAX_PER_DAY=6
MAX_PER_SYNC=3


class SocialOpportunities:
    def __init__(self, db):
        self.db=db

    @staticmethod
    def _validate(page,actor):
        if (not isinstance(page,dict) or page.get("protocolVersion")!="1"
                or page.get("mode")!="read_only_social_discovery"
                or page.get("actorId")!=actor
                or page.get("modelInvoked") is not False
                or page.get("actionExecuted") is not False):
            raise ValueError("unverified social discovery response")
        items=page.get("items")
        if not isinstance(items,list) or len(items)>20:
            raise ValueError("social candidate page must be bounded")
        clean=[]
        seen=set()
        for item in items:
            if not isinstance(item,dict):
                raise ValueError("invalid social candidate")
            resource=item.get("resource")
            if not isinstance(resource,dict):
                raise ValueError("invalid social resource")
            kind=resource.get("type")
            ident=resource.get("id")
            action=item.get("suggestedAction")
            expected_prefix="post_" if kind=="post" else "cir_" if kind=="circle" else ""
            if (item.get("kind")!=kind or (kind,action) not in ACTIONS
                    or not isinstance(ident,str) or not ident.startswith(expected_prefix)
                    or len(ident)>120 or not ID.fullmatch(ident)
                    or item.get("contentTrust")!="untrusted"):
                raise ValueError("unexpected or untrusted social action contract")
            interests=item.get("matchedInterests")
            if (not isinstance(interests,list) or len(interests)>30 or
                    any(not isinstance(tag,str) or len(tag)>80 for tag in interests)):
                raise ValueError("invalid social matching metadata")
            if kind=="circle":
                if type(item.get("joined")) is not bool:
                    raise ValueError("circle membership hint required")
                if (action=="community.circle.join" and item["joined"]) or (
                    action=="community.circle.message" and not item["joined"]):
                    raise ValueError("circle action conflicts with membership hint")
                if action=="community.circle.message" and (
                    type(item.get("memberCount")) is not int or item["memberCount"]<2):
                    raise ValueError("no other circle participant")
            key=(kind,ident)
            if key in seen:
                continue
            seen.add(key)
            # Small preference bonus, never stronger than important work.
            score=min(55,32+min(len(interests),4)*5+
                      (4 if action=="community.circle.message" else 0))
            clean.append((kind,ident,action,score))
        return clean

    def admit(self,page,now,max_new=MAX_PER_SYNC):
        if (type(now) not in (int,float) or not math.isfinite(now) or
                type(max_new) is not int or not 1<=max_new<=MAX_PER_SYNC):
            raise ValueError("invalid social admission budget")
        # Read authoritative locally bound identity, not a user-supplied actor.
        with self.db.transaction() as c:
            bound=c.execute("SELECT value FROM meta WHERE key='actor'").fetchone()
            if not bound or not bound["value"]:
                raise ValueError("verified local Agent actor required")
            actor=bound["value"]
            candidates=self._validate(page,actor)
            count=c.execute("""SELECT count(*) AS n FROM social_intents
                    WHERE actor_id=? AND created_at>?""",
                    (actor,now-WINDOW)).fetchone()["n"]
            selected=[]
            suppressed={"daily_budget":0,"target_cooldown":0,"active":0}
            for kind,ident,action,score in candidates:
                if len(selected)>=max_new:
                    break
                if count>=MAX_PER_DAY:
                    suppressed["daily_budget"]+=1
                    continue
                # Keep the same target to <=2 admissions per rolling day
                # and at least 15 minutes apart, regardless of action type.
                old=c.execute("""SELECT created_at FROM social_intents
                    WHERE actor_id=? AND resource_type=? AND resource_id=?
                      AND created_at>? ORDER BY created_at DESC""",
                      (actor,kind,ident,now-WINDOW)).fetchall()
                if old and (len(old)>=MAX_PER_TARGET_DAY or
                            now-old[0]["created_at"]<MIN_TARGET_SPACING):
                    suppressed["target_cooldown"]+=1
                    continue
                active=c.execute("""SELECT 1 FROM social_intents s
                    JOIN schedule_activities a ON a.id=s.activity_id
                    WHERE s.actor_id=? AND s.resource_type=? AND s.resource_id=?
                      AND a.status IN ('ready','running','suspended')
                    LIMIT 1""",(actor,kind,ident)).fetchone()
                if active:
                    suppressed["active"]+=1
                    continue
                # Registration and budget reservation are one transaction:
                # no orphan scheduler activity can bypass social quota.
                ident_hash=hashlib.sha256(
                    (actor+"|"+kind+"|"+ident).encode("utf-8")).hexdigest()[:20]
                logical="social:"+ident_hash+":"+str(int(now*1000000))
                aid=new_id()
                c.execute("""INSERT INTO schedule_activities
                    (id,logical_key,kind,work_id,status,base_priority,weight,
                     deadline,switch_cost,created_at,ready_since,last_updated)
                    VALUES(?,?,'social',NULL,'ready',?,30,NULL,6,?,?,?)""",
                    (aid,logical,score,now,now,now))
                c.execute("""INSERT INTO social_intents
                    (activity_id,actor_id,resource_type,resource_id,action,
                     created_at,selection_score)
                    VALUES(?,?,?,?,?,?,?)""",
                    (aid,actor,kind,ident,action,now,score))
                selected.append({"activity_id":aid,"resource":{"type":kind,"id":ident},
                                 "suggested_action":action,"priority":score})
                count+=1
        return {"status":"ok","new_activities":len(selected),
                "activities":selected,"suppressed":suppressed,
                "model_invoked":False,"host_woken":False,
                "remote_action_issued":False}

    def details(self,activity_id):
        if not isinstance(activity_id,str) or not activity_id or len(activity_id)>128:
            raise ValueError("bounded social activity ID required")
        rows=self.db.read("""SELECT s.resource_type,s.resource_id,s.action,
                   a.status,a.lease_owner,a.lease_version
                   FROM social_intents s JOIN schedule_activities a
                   ON a.id=s.activity_id WHERE s.activity_id=?""",(activity_id,))
        if not rows:
            raise ValueError("social activity not found")
        row=rows[0]
        return {"activity_id":activity_id,
                "resource":{"type":row["resource_type"],"id":row["resource_id"]},
                "suggested_action":row["action"],"status":row["status"],
                "lease_owner":row["lease_owner"],
                "lease_version":row["lease_version"],
                "untrusted_content_not_stored":True,
                "remote_action_issued":False}
