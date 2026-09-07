#!/usr/bin/env python3
"""Fetch Instagram / X / Threads via Bright Data — MCP HTTP first; Datasets REST IG-only fallback."""
from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

JOBS = Path("/workspace/video-timeline-pipeline/jobs")
DATASET_ID = "gd_lyclm20il4r5helnj"
API_ROOT = "https://api.brightdata.com/datasets/v3"
MCP_HOST = "https://mcp.brightdata.com/mcp"
SECRETS = (
    Path("/home/box/agent-data/box-secrets.json"),
    Path("/home/box/sand-data/box-secrets.json"),
)
SNAPSHOT_RE = re.compile(r"^s[a-z]*_[A-Za-z0-9_-]+$")


def redact(tok: str, text: str) -> str:
    if not tok:
        return text
    return text.replace(tok, tok[:4] + "…")
