"""Generate a short original instrumental fallback locally; no external music download."""
import math
import struct
import wave


def write_fallback(path):
    rate = 24000
    notes = [261.63, 329.63, 392.00, 329.63]
    frames = bytearray()
    for i in range(rate * 4):
        t = i / rate
        within = t % 1
        envelope = min(1, within / 0.03) * min(1, (1 - within) / 0.12)
        frequency = notes[int(t)]
        value = (math.sin(2 * math.pi * frequency * t) +
                 0.3 * math.sin(2 * math.pi * frequency * 2 * t)) * envelope
        frames.extend(struct.pack('<h', int(1800 * value)))
    with wave.open(str(path), 'wb') as output:
        output.setparams((1, 2, rate, 0, 'NONE', 'not compressed'))
        output.writeframes(frames)
