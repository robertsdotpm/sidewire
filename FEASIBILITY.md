# sidewire on runloom — sync-port feasibility

sidewire (the MQTT signalling layer, including a full pure-Python MQTT client)
ported to synchronous code on runloom (free-threaded 3.13t, M:N), on top of the
ported aionetiface base layer and its shims.  Part of the 4-repo stack port —
see warpgate's `FEASIBILITY.md` for the stack verdict and aionetiface's for the
technique.

## What was done
- AST-stripped all async/await (12 files; **0 executable async nodes**).
- One hand-fix: the MQTT dispatcher (a forever republish loop) was
  `asyncio.create_task(dispatcher(...))`; after the strip that runs eagerly and
  never returns, so it is now spawned as a `runloom.go` goroutine, with teardown
  riding the existing `is_closed` flag.

## Validated against the public broker `test.mosquitto.org:1883` (no infra)
| check | run(1) cooperative | run(8) M:N |
| --- | --- | --- |
| MQTT wire codec build/parse | PASS | PASS |
| real MQTT CONNECT/CONNACK + subscribe | PASS | PASS |
| real signed/sequenced **pub/sub** (alice → bob) | **PASS** | **FAIL** |

So **3/3 under `run(1)`** (the asyncio-equivalent) including a real signed
message delivered through a real broker, and **2/3 under `run(8)`**.

## The M:N finding (the interesting one)
The pub/sub failure under `run(8)` is **deterministic** (3/3 timeouts across
runs — not broker flakiness; `run(1)` is 3/3).  sidewire's application layer
(shared `msg_queues`, ack futures, sequence counters) was written for a
single-threaded asyncio loop.  Under `run(1)` no two goroutines execute Python
at once, so that state is safe.  Under `run(8)` eight hubs run its goroutines in
parallel and race it.  This is the predicted "true M:N runs Python in parallel,
so a single-threaded-loop library can race" axis, demonstrated cleanly:
`run(1)` is the drop-in equivalent; `run(8)` additionally requires locking the
shared mutable state.  (The other checks, and the rest of the stack, are fine
under `run(8)`.)

Run: `PYTHON_GIL=0 PYTHONPATH=src:<aionetiface>/src:<runloom>/src python3.13t tests_runloom/test_sidewire_port.py 1`
