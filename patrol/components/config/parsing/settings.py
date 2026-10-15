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
    lease_seconds: int = 120
    allow_writes: bool = False
    actor_id: str = ""
    allowed_chat_ids: list = field(default_factory=list)
    project_ids: list = field(default_factory=list)
    ack_single_enabled: bool = False
    ack_chat_map: dict = field(default_factory=dict)

    def __post_init__(self):
        allowed = {f.name for f in fields(self)}
        for name in allowed:
            value = getattr(self, name)
            if name.endswith("_seconds") or name in ("max_requests", "max_pages_per_source", "max_work_per_tick"):
                if type(value) is not int or not 1 <= value <= 86400:
                    raise ValueError("invalid positive integer config: " + name)
        if type(self.allow_writes) is not bool or type(self.ack_single_enabled) is not bool:
            raise ValueError("write flags must be booleans")
        # A message seen on the next idle tick must still be eligible to open attention.
        if self.max_event_age_seconds < self.normal_scan_seconds + 60:
            raise ValueError("max_event_age_seconds must cover idle polling plus scheduling slack")
        if self.request_timeout_seconds >= self.tick_budget_seconds:
            raise ValueError("request timeout must be below tick budget")
        if self.lease_seconds <= self.tick_budget_seconds:
            raise ValueError("lease must exceed tick budget")
        for name in ("allowed_chat_ids", "project_ids"):
            value = getattr(self, name)
            if not isinstance(value, list) or any(not isinstance(v, str) or not v for v in value):
                raise ValueError("invalid scope list: " + name)
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
    allowed = {f.name for f in fields(Config)}
    if set(data) - allowed:
        raise ValueError("unknown config keys: " + ",".join(sorted(set(data) - allowed)))
    return Config(**data)
