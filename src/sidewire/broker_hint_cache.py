"""Persist successful MQTT broker connections per pub_key to bias rendezvous walks.

When a node finishes its router-start broker walk it persists the set
of brokers that successfully became protected_clients along with each
broker's last-success timestamp.  On the next startup these cached
brokers are PREPENDED to the rendezvous candidate list per AF -- they
get a head start in the parallel first-to-complete race that walk_af
runs.  If they're still alive and fast they naturally win; if they're
dead the walk falls through to the rendezvous-derived candidates
behind them, same way it would with no cache.

Net effect: warm startups connect faster (cached brokers usually win),
cold / stale cases pay no extra cost (worst case = same walk_af pass
that would have run anyway).  No TTL gate -- aliveness is determined
by the actual connect attempt at use time, not by a coarse freshness
window.

Cache lives next to ~/aionetiface/servers.json so it inherits the
same on-disk lifecycle (user-deletable, install-script-managed).
Stored as a single JSON file keyed by pub_key_hex so one host can
run multiple nodes without their caches colliding.

Failure modes:
- All cached brokers dead: parallel connect attempts in walk_af still
  fire against the rendezvous tail of the candidate list, so the
  walk completes within broker_walk_cap and returns fresh brokers.
- pub_key change (regenerated keypair): cache miss on the key
  lookup, walk runs normally.
- Disk I/O errors: log + treat as cache miss; cache is purely
  optimisation, never a correctness dependency.
"""
import os
import time

from aionetiface.utility.jsonfile import load_json_or_default, atomic_write_json


def cache_file_path():
    """Absolute path to the broker-hint cache JSON file.

    Lives at ~/aionetiface/broker_hint_cache.json so it sits next to
    servers.json and inherits the same install-script handling.
    """
    return os.path.join(
        os.path.expanduser("~"), "aionetiface", "broker_hint_cache.json",
    )


def load_all():
    """Return the full cache dict (pub_key -> entry) or {} on any error."""
    data = load_json_or_default(cache_file_path(), dict)
    if isinstance(data, dict):
        return data
    return {}


def load_for(pub_key_hex):
    """Return cached broker list for pub_key_hex, or [] on miss.

    Each entry is {"af": int, "host": str, "port": int, "ts": int}
    where ts is the last-success unix timestamp.  Entries are returned
    sorted by recency descending so the caller can prepend them to the
    candidate list in best-first order without needing to know the
    field name.  No TTL gate -- aliveness is determined by the actual
    connect attempt at use time, not by a coarse freshness window.
    """
    data = load_all()
    entry = data.get(pub_key_hex)
    if not isinstance(entry, dict):
        return []
    brokers = entry.get("brokers")
    if not isinstance(brokers, list):
        return []
    out = []
    for b in brokers:
        if (
            isinstance(b, dict)
            and isinstance(b.get("af"), int)
            and isinstance(b.get("host"), str)
            and isinstance(b.get("port"), int)
        ):
            out.append({
                "af": int(b["af"]),
                "host": b["host"],
                "port": int(b["port"]),
                "ts": int(b.get("ts") or 0),
            })
    # Most-recently-successful first.  walk_af prepends in iteration
    # order, so this becomes the parallel try_client launch order;
    # all fire concurrently in practice, but if the gather is limited
    # downstream the freshest entries get launched first.
    out.sort(key=lambda b: b["ts"], reverse=True)
    return out


def store_for(pub_key_hex, brokers, now=None):
    """Replace the cached broker set for pub_key_hex with brokers.

    Each input entry should be a dict with af/host/port; we stamp the
    current time as ts.  Multi-key safe: reads the existing cache,
    updates only this pub_key's entry, writes back atomically.  Best-
    effort -- any OSError is swallowed since the cache is an
    optimisation, never a correctness gate.

    NOTE: This replaces (not merges) the broker list for this pub_key.
    The caller passes exactly what should be cached going forward; a
    broker that was in last run's cache but isn't in this run's
    successful set is correctly evicted.
    """
    if not brokers:
        return
    if now is None:
        now = time.time()
    ts = int(now)
    serialisable = []
    for b in brokers:
        if (
            isinstance(b, dict)
            and isinstance(b.get("af"), int)
            and isinstance(b.get("host"), str)
            and isinstance(b.get("port"), int)
        ):
            serialisable.append({
                "af": int(b["af"]),
                "host": b["host"],
                "port": int(b["port"]),
                "ts": ts,
            })
    if not serialisable:
        return
    data = load_all()
    data[pub_key_hex] = {"brokers": serialisable}
    path = cache_file_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
    except OSError:
        pass
    atomic_write_json(path, data, indent=2, sort_keys=True)


def client_to_hint(client):
    """Convert an MQTTClient to the {af, host, port} hint shape.

    Defensive against partially-constructed clients (e.g. one that
    came back from try_client but whose .dest tuple is malformed).
    Returns None when the client doesn't expose the needed fields.
    """
    af = getattr(client, "af", None)
    dest = getattr(client, "dest", None)
    if af is None or not isinstance(dest, tuple) or len(dest) != 2:
        return None
    host, port = dest
    if not host or not isinstance(port, int):
        return None
    return {"af": int(af), "host": host, "port": int(port)}
