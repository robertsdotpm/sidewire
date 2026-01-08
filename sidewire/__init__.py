
import sys


"""
This is a hack to avoid double-imports of a module when using
the -m switch to run a module directly. 
"""
if not '-m' in sys.argv:
    from ...aionetiface.src.aionetiface.net.topology import *
    from .signal_defs import *
    from ...p2pd.src.p2pd.protocol.signaling.signal_msgs import *
    from .utils import *
    from .signal_client import *
    from .signal_router import *