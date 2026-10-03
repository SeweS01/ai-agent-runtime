"""Versioned local IPC."""

from .protocol import MAX_FRAME_SIZE, PROTOCOL_MAJOR, PROTOCOL_MINOR, read_frame, write_frame

__all__ = ["MAX_FRAME_SIZE", "PROTOCOL_MAJOR", "PROTOCOL_MINOR", "read_frame", "write_frame"]
