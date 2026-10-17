import json
from dataclasses import dataclass, field, fields
from pathlib import Path
from urllib.parse import urlsplit


@dataclass(frozen=True)
class Config:
    database: str = str(Path.home() / ".crewquay" / "patrol-v3.sqlite3")
    base_url: str = "https://mxrlyy.asia"
    credential_target: str = "CrewQuay/mxrlyy.asia/Agent/codex-planner-91f1ddda"
    credential_file: str = ""
    journal: str = "DELETE"
    tick_seconds: int = 60
    normal_scan_seconds: int = 600
    active_scan_seconds: int = 60
    cooldown_scan_seconds: int = 180
    active_window_seconds: int = 1800
    cooldown_window_seconds: int = 600
    own_reply_window_seconds: int = 600
    max_event_age_seconds: int = 900
    future_skew_seconds: int = 60
    tick_budget_seconds: int = 50
    request_timeout_seconds: int = 8
    max_requests: int = 20
    max_pages_per_source: int = 4
    max_work_per_tick: int = 12
    light_max_requests: int = 8
    light_tick_budget_seconds: int = 20
    light_max_pages_per_source: int = 1
    # Live event feed rejects >=10 per page on the current deployment.
    # Keep the page size independent of the number of pages per tick.
    light_events_page_size: int = 5
    max_light_chats: int = 4
    wakeup_min_score: int = 70
    lease_seconds: int = 120
    allow_writes: bool = False
    actor_id: str = ""
    # Legacy collector hint only; never a sending permission requirement.
    allowed_chat_ids: list = field(default_factory=list)
    # Owner-maintained explicit prohibitions; platform authorizes all grants.
    blocked_chat_ids: list = field(default_factory=list)
    blocked_contact_ids: list = field(default_factory=list)
    blocked_social_resources: list = field(default_factory=list)  # "circle:cir_..." etc.
    blocked_social_actions: list = field(default_factory=list)  # action registry names
    community_social_enabled: bool = True
    project_ids: list = field(default_factory=list)
    allowed_project_action_ids: list = field(default_factory=list)
    allowed_task_actions: list = field(default_factory=list)
    allowed_platform_actions: list = field(default_factory=list)
    ack_single_enabled: bool = False
    ack_chat_map: dict = field(default_factory=dict)

    def __post_init__(self):
        allowed = {f.name for f in fields(self)}
        for name in allowed:
            value = getattr(self, name)
            if name.endswith("_seconds") or name in ("max_requests", "max_pages_per_source", "max_work_per_tick", "light_max_requests", "light_max_pages_per_source", "max_light_chats"):
                if type(value) is not int or not 1 <= value <= 86400:
                    raise ValueError("invalid positive integer config: " + name)
        if type(self.wakeup_min_score) is not int or not 0<=self.wakeup_min_score<=100:
            raise ValueError("wakeup score must be an integer 0..100")
        if (type(self.light_events_page_size) is not int or
                not 1<=self.light_events_page_size<=100):
            raise ValueError("light_events_page_size must be 1..100")
        if self.light_tick_budget_seconds<2:
            raise ValueError("light budget must allow a bounded HTTP request")
        if (type(self.allow_writes) is not bool or
                type(self.ack_single_enabled) is not bool or
                type(self.community_social_enabled) is not bool):
            raise ValueError("write flags must be booleans")
        # A message seen on the next idle tick must still be eligible to open attention.
        if self.max_event_age_seconds < self.normal_scan_seconds + 60:
            raise ValueError("max_event_age_seconds must cover idle polling plus scheduling slack")
        if self.request_timeout_seconds >= self.tick_budget_seconds:
            raise ValueError("request timeout must be below tick budget")
        if self.lease_seconds <= self.tick_budget_seconds:
            raise ValueError("lease must exceed tick budget")
        for name in ("allowed_chat_ids", "blocked_chat_ids", "blocked_contact_ids", "blocked_social_resources", "blocked_social_actions", "project_ids", "allowed_project_action_ids", "allowed_task_actions", "allowed_platform_actions"):
            value = getattr(self, name)
            if not isinstance(value, list) or any(not isinstance(v, str) or not v for v in value):
                raise ValueError("invalid scope list: " + name)
        if any(v not in ("community:community",) and not
               (v.startswith(("circle:cir_", "post:post_")) and v.count(":")==1)
               for v in self.blocked_social_resources):
            raise ValueError("blocked_social_resources must use community:community, circle:cir_* or post:post_*")
        if set(self.allowed_task_actions)-{"task_claim","task_progress","task_submit","submission_review","project_decision","artifact_upload"}:
            raise ValueError("unsupported allowed_task_actions value")
        if not isinstance(self.ack_chat_map, dict) or any(not isinstance(k, str) or not isinstance(v, list) or any(not isinstance(x, str) for x in v) for k,v in self.ack_chat_map.items()):
            raise ValueError("ack_chat_map must map notification types to configured chat ID lists")
        u = urlsplit(self.base_url)
        if u.scheme != "https" or not u.hostname or u.username or u.password or u.query or u.fragment or u.path not in ("", "/"):
            raise ValueError("base_url must be an HTTPS origin without credentials")
        if self.journal not in ("DELETE", "WAL"):
            raise ValueError("unsupported journal")


def load_config(path=None, **overrides):
    data = {}
    if path:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            raise ValueError("config must be an object")
    data.update({k: v for k, v in overrides.items() if v is not None})
    # The former extra community switch is superseded by authenticated
    # platform Action Registry permissions and the existing global send
    # gate. Accept historical JSON files without making their stale value
    # silently disable social discovery or require operator migration.
    legacy = data.pop("allow_community_actions", None)
    if legacy is not None and type(legacy) is not bool:
        raise ValueError("obsolete allow_community_actions must be boolean")
    allowed = {f.name for f in fields(Config)}
    if set(data) - allowed:
        raise ValueError("unknown config keys: " + ",".join(sorted(set(data) - allowed)))
    return Config(**data)
