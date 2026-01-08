
import sys


"""
This is a hack to avoid double-imports of a module when using
the -m switch to run a module directly. 
"""
if not '-m' in sys.argv:
    from .signal_defs import *
    from .utils import *
    from .mqtt_client import *
    from .signal_router import *