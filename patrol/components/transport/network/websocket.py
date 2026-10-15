import json
import importlib.metadata
from urllib.parse import urlsplit


def connect_ready(config, secret):
    # Optional dependency; read at use time and never import a vendor directory implicitly.
    if importlib.metadata.version("websocket-client")!="1.9.2":
        raise RuntimeError("install the pinned optional websocket-client 1.9.2")
    import websocket
    host=urlsplit(config.base_url).netloc
    ws=websocket.create_connection("wss://"+host+"/api/v1/events/ws",
        header={"Authorization":"Bearer "+secret()},timeout=config.request_timeout_seconds)
    try:
        frame=json.loads(ws.recv())
        if frame.get("type")!="ready" or not isinstance(frame.get("streamId"),str):
            raise ValueError("invalid ready frame")
        ws.settimeout(0.2)
        return ws,frame
    except BaseException:
        ws.close()
        raise
