from aionetiface import *
from .mqtt_client import *

async def workspace_two():
    servers = get_infra(IP4, TCP, "MQTT")
    mqtt_iter = seed_iter(servers, "some node id")

    def select_servers(n, kv):
        return [next(mqtt_iter) for _ in range(n)]

    c = ObjCollection(
        lambda kparams, dest=None: MQTT(**kparams, dest=dest),
        select_servers=select_servers
    )

    out = await c.get_n(1, kv={
            "af": IP4,
            "proto": UDP,
        }
    )

    print(out)

async_run(workspace_two())