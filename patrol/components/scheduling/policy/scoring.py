"""Pure bounded, interpretable scheduler heuristic; no database access."""
import math


def priority(row, now, *, running=False):
    base=float(row["base_priority"])
    deadline=row["deadline"]
    urgent=0.0
    if deadline is not None:
        remaining=float(deadline)-now
        urgent=75.0 if remaining<=0 else max(0.0, 75.0*(1.0-remaining/3600.0))
    ready_since=float(row["ready_since"])
    aging=min(30.0,max(0.0,(now-ready_since)/90.0))
    fair=min(34.0,max(0.0,float(row["virtual_runtime"])/60.0))
    # Do not thrash on small priority changes. Existing work retains some
    # continuity, while new resumptions pay a measured local restore penalty.
    restore=0 if running else min(15.0,float(row["switch_cost"]))
    continuity=8.0 if running else 0.0
    return round(base+urgent+aging-fair-restore+continuity,3)


def quantum(weight):
    """Finite, weight-sensitive cooperative time slice, in seconds."""
    return max(20.0,min(120.0,30.0+float(weight)*0.6))


def preemption(running, challenger, now):
    """Return requested safe-point mode, not permission to kill a model."""
    if challenger is None:
        return None
    active_score=priority(running,now,running=True)
    other_score=priority(challenger,now)
    remaining=(float(challenger["deadline"])-now
               if challenger["deadline"] is not None else math.inf)
    # A large gap supersedes hysteresis; near-deadline work has an
    # urgency-sensitive override; expired slices permit fair rotation.
    if remaining<=30 and other_score>=active_score-8:
        return "urgent_safe_point"
    if other_score>=active_score+12+min(8.0,float(running["switch_cost"])/2):
        return "medium_safe_point"
    if now>=float(running["slice_until"]) and other_score>=active_score-4:
        return "soft_safe_point"
    return None
