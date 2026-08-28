"""Scriptum - the web front end's backend.

A thin FastAPI layer over `musiccopilot`. Everything musical still happens in
that package: this one uploads files, runs the pipeline in a worker thread,
turns dataclasses and tab layouts into JSON, and streams the live mic modes
over WebSockets. No analysis logic lives here.
"""
from __future__ import annotations

__all__ = ["create_app"]


def __getattr__(name):
    """Lazy so `import scriptum` stays cheap and torch-free (see CLAUDE.md)."""
    if name == "create_app":
        from .app import create_app
        return create_app
    raise AttributeError(name)
