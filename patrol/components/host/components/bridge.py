"""Compose existing Wakeup and Scheduler facilities for a host Hook.

This is a durable handoff *reservation*, not proof a Muse was woken,
an activity was dispatched, or platform work was completed.
"""
from ...scheduling.index import Scheduler
from ...wakeup.index import Wakeups


class HostHandoff:
    def __init__(self, db, lease_seconds=120):
        self.scheduler=Scheduler(db,lease_seconds)
        self.wakeups=Wakeups(db,lease_seconds)

    def prepare(self, owner, now):
        """Reserve one due intent and provide bounded, non-content routing hints.

        The Hook invokes wake() only after a successful reservation. Its host
        must subsequently acknowledge actual acceptance using wakeup-ack with
        verifiable evidence; a missing receipt becomes unknown at lease expiry.
        The Muse worker, not the Hook, owns schedule-next and model execution.
        """
        decision=self.scheduler.reconcile(now)["decision"]
        leased=self.wakeups.claim_next(owner,now)
        if leased is None:
            return {"should_wake":False,"host_called":False,
                    "status":"idle","activity_dispatched":False,
                    "scheduler_advice":decision["advice"]}
        candidate=decision["candidate"]
        current=decision["running"]
        return {"should_wake":True,"host_called":False,
                "status":"reserved","activity_dispatched":False,
                "wakeup_id":leased["id"],"source_type":leased["source_type"],
                "source_id":leased["source_id"],"lease_owner":leased["lease_owner"],
                "lease_version":leased["lease_version"],
                "lease_until":leased["lease_until"],
                "scheduler_advice":decision["advice"],
                "candidate_activity_id":candidate["id"] if candidate else None,
                "running_activity_id":current["id"] if current else None,
                "requires_host_receipt":True}
