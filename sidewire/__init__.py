
import sys


"""
This is a hack to avoid double-imports of a module when using
the -m switch to run a module directly. 
"""
if not '-m' in sys.argv:
    from .node_addr import *
    from .signal_defs import *
    from .signal_msgs import *
    from .signal_utils import *
    from .signal_client import *
    from .signal_router import *