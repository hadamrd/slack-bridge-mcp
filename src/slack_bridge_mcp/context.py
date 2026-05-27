"""Shared singletons. See dev guide §Dependency-DAG."""

from __future__ import annotations

import logging
import os

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)

log = logging.getLogger(__name__.split(".")[0])

# When you add an HTTP client (client.py), instantiate it here as a module-level
# singleton so every tool module imports the same instance:
#
# from .client import MyClient
# client = MyClient()
