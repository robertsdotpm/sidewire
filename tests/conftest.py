"""Test configuration for sidewire test suite."""
import unittest

from aionetiface.testing import AsyncTestCase

if not hasattr(unittest, "IsolatedAsyncioTestCase"):
    unittest.IsolatedAsyncioTestCase = AsyncTestCase
