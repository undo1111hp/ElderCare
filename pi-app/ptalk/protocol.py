"""Wire framing shared with the server / Flutter app.

Frame = [uint16 LE length][payload]; multiple frames may be concatenated in a
single WebSocket message. Uplink payload is Opus; downlink payload is raw PCM16.
"""
import struct

_MAX_FRAME = 4096


def pack_frame(payload: bytes) -> bytes:
    if not payload:
        raise ValueError("empty frame")
    if len(payload) > 0xFFFF:
        raise ValueError("frame too large")
    return struct.pack("<H", len(payload)) + payload


def unpack_frames(packet: bytes):
    frames = []
    pos = 0
    n = len(packet)
    while n - pos >= 2:
        (length,) = struct.unpack_from("<H", packet, pos)
        pos += 2
        if length <= 0 or length > _MAX_FRAME or length > n - pos:
            raise ValueError(f"invalid frame length {length}")
        frames.append(packet[pos:pos + length])
        pos += length
    return frames
