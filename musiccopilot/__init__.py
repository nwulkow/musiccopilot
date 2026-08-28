"""MusicCopilot - analyse a song, tab it, and generate solos you can hear.

`Song` is imported lazily so that light modules (tabs, synth, notes) can be
used without pulling in librosa/torch.
"""
__all__ = ["Song"]
__version__ = "0.1.0"


def __getattr__(name):
    """PEP 562 module-level lazy attribute: defers the heavy `Song` import."""
    if name == "Song":
        from .pipeline import Song
        return Song
    raise AttributeError(name)
