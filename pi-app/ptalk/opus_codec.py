"""Opus encoder via ctypes over the system libopus (libopus0).

Only an encoder is needed: the PTalk voice server returns raw PCM16 on the
downlink (START_PCM_OUT mode), so no Opus decoding happens on the client.
"""
import ctypes
import ctypes.util

_APPLICATION_VOIP = 2048
_OPUS_SET_BITRATE_REQUEST = 4002
_MAX_PACKET = 4000


def _load_libopus():
    name = ctypes.util.find_library("opus") or "libopus.so.0"
    return ctypes.CDLL(name)


class OpusEncoder:
    def __init__(self, sample_rate=48000, channels=1, bitrate=24000):
        lib = _load_libopus()
        self._lib = lib
        lib.opus_encoder_create.restype = ctypes.c_void_p
        lib.opus_encoder_create.argtypes = [
            ctypes.c_int32, ctypes.c_int, ctypes.c_int, ctypes.POINTER(ctypes.c_int)
        ]
        lib.opus_encode.restype = ctypes.c_int32
        lib.opus_encode.argtypes = [
            ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int,
            ctypes.POINTER(ctypes.c_ubyte), ctypes.c_int32,
        ]
        err = ctypes.c_int()
        self._enc = lib.opus_encoder_create(sample_rate, channels, _APPLICATION_VOIP,
                                            ctypes.byref(err))
        if not self._enc or err.value != 0:
            raise RuntimeError(f"opus_encoder_create failed (err={err.value})")
        try:
            lib.opus_encoder_ctl(self._enc, _OPUS_SET_BITRATE_REQUEST,
                                 ctypes.c_int32(bitrate))
        except Exception:
            pass  # bitrate tuning is best-effort
        self._channels = channels
        self._out = (ctypes.c_ubyte * _MAX_PACKET)()

    def encode(self, pcm_bytes):
        """pcm_bytes: PCM16-LE, one frame (frame_size samples per channel)."""
        frame_size = (len(pcm_bytes) // 2) // self._channels
        n = self._lib.opus_encode(self._enc, pcm_bytes, frame_size,
                                  self._out, _MAX_PACKET)
        if n < 0:
            raise RuntimeError(f"opus_encode failed ({n})")
        return bytes(self._out[:n])
