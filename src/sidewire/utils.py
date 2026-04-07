from aionetiface import *
import hashlib
import math
import copy

def rendezvous_hash(nic, pub_key_hex, servers):
    process_list = []
    for af in nic.supported():
        for host in servers[af]:
            # Record server to score.
            server = copy.deepcopy(servers[af][host])
            process_list.append(server)

            # Starting value to run through scoring func.
            h = hashlib.sha256(
                bytes([int(af)]) + 
                h_to_b(pub_key_hex) + 
                to_b(host)
            ).digest()

            # Convert hex to an integer.
            int_hash = int.from_bytes(h, 'big')

            """
            Converts massive int 256 bit value into range from 1 to < 1
            as a decimal. This is used for the next trick with log.
            """
            one_or_less = (int_hash + 1) / (2 ** 256)

            """
            When a number is less than one: math.log expands differences.
            They all fit on the same line so large numbers don't adversely
            effect clustering of final values. The field ends up being fair.
            """
            even_playing_field = -math.log(one_or_less)

            # optional weighting:
            # score /= server.get("weight", 1)

            # Record score for server.
            server["score"] = even_playing_field

    # Now sort process list by smallest scores first.
    return sorted(process_list, key=lambda v: v["score"])