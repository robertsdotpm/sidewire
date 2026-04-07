import hashlib
import math
import random

# ---------------------------
# Config
# ---------------------------

WINDOW_SIZE = 3
MAX_WINDOWS = 4

# ---------------------------
# Hashing + scoring
# ---------------------------

def hash_to_uniform(pubkey: str, server: str) -> float:
    h = hashlib.sha256((pubkey + server).encode()).digest()
    x = int.from_bytes(h, 'big')
    return (x + 1) / (2**256)  # (0,1]


def weighted_score(pubkey: str, server: str, weight: float) -> float:
    U = hash_to_uniform(pubkey, server)
    return -math.log(U) / weight  # smaller = better


# ---------------------------
# Ranking
# ---------------------------

def rendezvous_rank_weighted(pubkey, servers, weights):
    scored = [
        (weighted_score(pubkey, s, weights[s]), s)
        for s in servers
    ]
    scored.sort()
    return [s for _, s in scored]


# ---------------------------
# Routing simulation
# ---------------------------

def attempt_windows(ranked):
    for i in range(MAX_WINDOWS):
        start = i * WINDOW_SIZE
        end = start + WINDOW_SIZE
        yield ranked[start:end]


def route(pubkey, servers_a, servers_b, weights_a, weights_b, verbose=True):
    ranked_a = rendezvous_rank_weighted(pubkey, servers_a, weights_a)
    ranked_b = rendezvous_rank_weighted(pubkey, servers_b, weights_b)

    if verbose:
        print("\n=== Ranked Lists ===")
        print("A:", ranked_a)
        print("B:", ranked_b)

    for i, (win_a, win_b) in enumerate(zip(attempt_windows(ranked_a),
                                           attempt_windows(ranked_b))):

        overlap = set(win_a).intersection(win_b)

        if verbose:
            print(f"\nWindow {i}")
            print(" A:", win_a)
            print(" B:", win_b)
            print(" overlap:", overlap)

        if overlap:
            return True, overlap, i

    return False, set(), None


# ---------------------------
# Test data generation
# ---------------------------

def random_ipv4():
    return f"192.168.1.{random.randint(1,254)}"

def random_ipv6():
    return f"2001:db8::{random.randint(1,1000)}"

def random_host():
    return f"node{random.randint(1,100)}.example.com"


def random_servers(n):
    servers = []
    for _ in range(n):
        t = random.choice(["ipv4", "ipv6", "host"])
        if t == "ipv4":
            servers.append(random_ipv4())
        elif t == "ipv6":
            servers.append(random_ipv6())
        else:
            servers.append(random_host())
    return list(set(servers))


def assign_weights_from_rank(servers):
    """
    Higher-ranked servers get higher weights.
    """
    weights = {}
    total = len(servers)

    for i, s in enumerate(servers):
        # rank-based weight (strong bias toward top)
        weights[s] = 1 / (i + 1)

    return weights


def make_partial_overlap(a, b, overlap_count):
    shared = random.sample(a, min(overlap_count, len(a)))
    b = list(set(b + shared))
    return a, b


# ---------------------------
# Main test
# ---------------------------

def run_test():
    random.seed(42)

    pubkey = "deadbeefcafebabe"

    # Generate server sets
    servers_a = random_servers(10)
    servers_b = random_servers(10)

    # Force partial overlap
    servers_a, servers_b = make_partial_overlap(servers_a, servers_b, overlap_count=3)

    # Sort by "reliability" (simulated)
    # Pretend earlier = more reliable
    servers_a.sort()
    servers_b.sort()

    # Assign weights based on rank
    weights_a = assign_weights_from_rank(servers_a)
    weights_b = assign_weights_from_rank(servers_b)

    print("=== Servers A (reliable first) ===")
    for s in servers_a:
        print(s, "weight=", round(weights_a[s], 3))

    print("\n=== Servers B (reliable first) ===")
    for s in servers_b:
        print(s, "weight=", round(weights_b[s], 3))

    # Run routing simulation
    success, overlap, window = route(
        pubkey,
        servers_a,
        servers_b,
        weights_a,
        weights_b,
        verbose=True
    )

    print("\n=== RESULT ===")
    print("Success:", success)
    print("Matched servers:", overlap)
    print("Window index:", window)


if __name__ == "__main__":
    run_test()