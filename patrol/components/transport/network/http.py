import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass


class BudgetExceeded(RuntimeError):
    pass


class AuthError(RuntimeError):
    pass


@dataclass
class Response:
    status: int
    body: object
    retry_after: float = 0


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class Client:
    def __init__(self, config, secret, clock=time.monotonic, opener=None):
        self.config=config
        self.secret=secret
        self.clock=clock
        self.deadline=clock()+config.tick_budget_seconds
        self.requests=0
        self.stopped=False
        self.open=opener or urllib.request.build_opener(NoRedirect()).open

    def request(self, method, path, payload=None, idem=None):
        if self.stopped:
            raise AuthError("authorization stopped for this tick")
        remaining=self.deadline-self.clock()
        if self.requests>=self.config.max_requests or remaining<1:
            raise BudgetExceeded("bounded HTTP budget exhausted")
        if not path.startswith("/api/v1/") or any(x in path for x in ("\r","\n","#")):
            raise ValueError("only verified API paths are accepted")
        if method!="GET" and (not self.config.allow_writes or not idem):
            raise ValueError("writes require explicit config authorization and a persisted key")
        self.requests+=1
        headers={"Authorization":"Bearer "+self.secret(),"Accept":"application/json"}
        data=None
        if payload is not None:
            data=json.dumps(payload,ensure_ascii=False,allow_nan=False).encode()
            headers["Content-Type"]="application/json"
        if idem:
            headers["Idempotency-Key"]=idem
        req=urllib.request.Request(self.config.base_url.rstrip("/")+path,data=data,headers=headers,method=method)
        try:
            with self.open(req,timeout=min(self.config.request_timeout_seconds,remaining)) as r:
                raw=r.read(2*1024*1024+1)
                if len(raw)>2*1024*1024:
                    return Response(0,{"code":"RESPONSE_TOO_LARGE"})
                return Response(r.status,json.loads(raw) if raw.strip() else {})
        except urllib.error.HTTPError as e:
            body={}
            try:
                body=json.loads(e.read(65536))
            except (ValueError,TypeError):
                pass
            retry=0
            try:
                retry=max(0,min(3600,float(e.headers.get("Retry-After","0"))))
            except (ValueError,TypeError):
                pass
            if e.code in (401,403):
                self.stopped=True
            return Response(e.code,body,retry)
        except (OSError,ValueError):
            return Response(0,{"code":"TRANSPORT_FAILURE"})

