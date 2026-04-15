"""
FastAPI app instance shared across all server modules.

This module defines the app first so that ws_handler.py and http_endpoints.py
can import and decorate on it at import time without circular dependency.
"""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from config import load_config

_llm_config, _asr_config, _realtime_vad_config, _server_config, _meeting_config = load_config()

app = FastAPI(title="Meeting Realtime Voice")

app.add_middleware(
    CORSMiddleware,
    allow_origins=_server_config.cors_origins,
    allow_credentials=(
        _server_config.cors_allow_credentials
        and _server_config.cors_origins != ["*"]
    ),
    allow_methods=["*"],
    allow_headers=["*"],
)


def get_app():
    return app


def get_configs():
    return (
        _llm_config,
        _asr_config,
        _realtime_vad_config,
        _server_config,
        _meeting_config,
    )
