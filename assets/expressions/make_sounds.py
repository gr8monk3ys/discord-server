"""Soundboard pack for the server, synthesized from scratch with numpy.

    python make_sounds.py       # writes sounds/<name>.mp3 (needs ffmpeg on PATH)

No samples or recordings: every sound is additive synthesis, filtered noise
or an 8-bit square wave built below. Each one is peak-normalized below 0 dBFS
and kept under 3 s, well inside Discord's soundboard limits (MP3/OGG,
<= 512 KB, <= 5.2 s).

MP3 rather than OGG because discord.py 2.7's create_soundboard_sound only
recognises MP3 data (utils._get_mime_type_for_audio).
"""
import shutil
import subprocess
import tempfile
import wave
from pathlib import Path

import numpy as np

OUT = Path(__file__).with_name("sounds")
SR = 44100
PEAK = 0.84  # leaves headroom for MP3 encoder overshoot
rng = np.random.default_rng(7)


def t_axis(seconds):
    return np.arange(int(seconds * SR)) / SR


def note_hz(n):
    """MIDI note number -> Hz."""
    return 440.0 * 2 ** ((n - 69) / 12)


def env(n, attack=0.005, release=0.05, decay=None):
    """Attack/release envelope over n samples, optional exponential decay (seconds)."""
    t = np.arange(n) / SR
    e = np.ones(n)
    a = max(1, int(attack * SR))
    r = max(1, int(release * SR))
    e[:a] *= np.linspace(0, 1, a)
    e[-r:] *= np.linspace(1, 0, r)
    if decay:
        e *= np.exp(-t / decay)
    return e


def phase(freq):
    """Integrate an instantaneous-frequency array into phase."""
    return 2 * np.pi * np.cumsum(freq) / SR


def additive(freq, partials):
    """Sum of harmonics: partials = [(ratio, amp), ...]; freq may be an array (glides)."""
    ph = phase(freq)
    return sum(amp * np.sin(ratio * ph) for ratio, amp in partials)


def brass(freq, bright=1.0, n=14):
    return additive(freq, [(k, (1 / k) * min(1.0, bright * 3 / k)) for k in range(1, n + 1)])


def bell(freq, seconds, decay=0.6):
    n = int(seconds * SR)
    f = np.full(n, freq)
    tone = additive(f, [(1, 1.0), (2.0, 0.5), (2.76, 0.35), (5.4, 0.18), (8.93, 0.08)])
    return tone * env(n, 0.002, 0.05, decay)


def place(buf, sound, at):
    i = int(at * SR)
    end = min(len(buf), i + len(sound))
    buf[i:end] += sound[: end - i]


def noise(n):
    return rng.uniform(-1, 1, n)


def highpass(x, amount=0.95):
    """One-pole high-pass: crude, but enough to make noise hiss like a cymbal."""
    y = np.zeros_like(x)
    prev_x = prev_y = 0.0
    for i, v in enumerate(x):
        prev_y = amount * (prev_y + v - prev_x)
        prev_x = v
        y[i] = prev_y
    return y


def lowpass(x, amount=0.2):
    y = np.zeros_like(x)
    acc = 0.0
    for i, v in enumerate(x):
        acc += amount * (v - acc)
        y[i] = acc
    return y


# ---------------------------------------------------------------- sounds
def gg_chime():
    """Victory arpeggio: C major up an octave, then the whole chord rings."""
    buf = np.zeros(int(2.0 * SR))
    for i, n in enumerate((72, 76, 79, 84)):
        place(buf, bell(note_hz(n), 1.2, 0.35), i * 0.11)
    for n in (72, 76, 79, 84, 88):
        place(buf, 0.6 * bell(note_hz(n), 1.5, 0.5), 0.5)
    return buf


def oof():
    """A short 'oo' vowel that drops in pitch."""
    n = int(0.55 * SR)
    t = np.arange(n) / SR
    f0 = 210 * np.exp(-t * 2.2) + 70
    ph = phase(f0)
    out = np.zeros(n)
    for k in range(1, 30):
        fk = k * f0
        # vowel 'u' formants around 320 Hz and 800 Hz
        amp = np.exp(-((fk - 320) / 140) ** 2) + 0.4 * np.exp(-((fk - 800) / 200) ** 2) + 0.02
        out += amp * np.sin(k * ph) / k ** 0.5
    return out * env(n, 0.02, 0.12)


def airhorn_ish():
    """Three brassy synth stabs (short, short, long) with a pitch scoop."""
    buf = np.zeros(int(1.7 * SR))
    for start, length in ((0.0, 0.16), (0.22, 0.16), (0.44, 1.0)):
        n = int(length * SR)
        t = np.arange(n) / SR
        scoop = 1 - 0.06 * np.exp(-t * 40)
        stab = sum(brass(f * scoop * (1 + d), 1.6) for f in (466.16, 698.46) for d in (-0.004, 0.004))
        place(buf, stab * env(n, 0.008, 0.06), start)
    return buf


def snare(length=0.12, tone=190):
    n = int(length * SR)
    hit = 0.8 * highpass(noise(n), 0.9) + 0.4 * np.sin(phase(np.full(n, tone)))
    return hit * env(n, 0.001, 0.01, length / 4)


def cymbal(length=1.2):
    n = int(length * SR)
    return 0.6 * highpass(noise(n), 0.98) * env(n, 0.001, 0.2, length / 4)


def drumroll():
    """Snare roll that speeds up and swells, ending on a crash."""
    buf = np.zeros(int(2.8 * SR))
    at, gap = 0.0, 0.07
    while at < 1.7:
        place(buf, (0.35 + 0.4 * at / 1.7) * snare(0.09), at)
        at += gap
        gap = max(0.035, gap * 0.97)
    place(buf, snare(0.25), 1.75)
    place(buf, cymbal(1.0), 1.75)
    return buf


def tom(freq, length=0.25):
    n = int(length * SR)
    t = np.arange(n) / SR
    f = freq * (1 + 0.6 * np.exp(-t * 30))
    return np.sin(phase(f)) * env(n, 0.001, 0.03, length / 3)


def rimshot():
    """Two tom hits and a cymbal: the joke-landing fill."""
    buf = np.zeros(int(1.7 * SR))
    place(buf, tom(150), 0.0)
    place(buf, tom(105), 0.18)
    place(buf, 0.5 * snare(0.1), 0.42)
    place(buf, cymbal(1.2), 0.42)
    return buf


def square(freq, n, duty=0.5):
    ph = (np.cumsum(np.full(n, freq) if np.isscalar(freq) else freq) / SR) % 1.0
    return np.where(ph < duty, 1.0, -1.0)


def level_up():
    """8-bit rising arpeggio and a held note with vibrato."""
    buf = np.zeros(int(1.3 * SR))
    for i, n in enumerate((60, 64, 67, 72, 76, 79)):
        k = int(0.06 * SR)
        place(buf, 0.5 * square(note_hz(n), k, 0.25) * env(k, 0.002, 0.01), i * 0.06)
    k = int(0.8 * SR)
    t = np.arange(k) / SR
    f = note_hz(84) * (1 + 0.012 * np.sin(2 * np.pi * 7 * t))
    place(buf, 0.5 * square(f, k, 0.5) * env(k, 0.002, 0.2), 0.38)
    # soften the square edges a touch: less harsh, and no MP3 overshoot past 0 dBFS
    return lowpass(buf, 0.45)


def sad_trombone_ish():
    """Descending brassy synth that sags into a wobbly last note."""
    buf = np.zeros(int(2.6 * SR))
    notes = ((62, 0.0, 0.42), (61, 0.45, 0.42), (60, 0.9, 0.42), (59, 1.35, 1.15))
    for i, (n, at, length) in enumerate(notes):
        k = int(length * SR)
        t = np.arange(k) / SR
        sag = 1 - 0.015 * t / length  # each note droops a little
        wobble = 1 + (0.02 * np.sin(2 * np.pi * 5 * t) * np.minimum(1, t / 0.3) if i == 3 else 0)
        tone = brass(note_hz(n) * sag * wobble, 0.8, 10)
        place(buf, tone * env(k, 0.04, 0.12), at)
    return buf


def ding():
    """A single bright bell."""
    return bell(note_hz(88), 1.6, 0.45)


SOUNDS = {
    "gg_chime": gg_chime,
    "oof": oof,
    "airhorn_ish": airhorn_ish,
    "drumroll": drumroll,
    "rimshot": rimshot,
    "level_up": level_up,
    "sad_trombone_ish": sad_trombone_ish,
    "ding": ding,
}


def finish(x):
    x = x - np.mean(x)
    fade = int(0.004 * SR)
    x[:fade] *= np.linspace(0, 1, fade)
    x[-fade:] *= np.linspace(1, 0, fade)
    return x * (PEAK / np.max(np.abs(x)))


def write_wav(path, x):
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes((x * 32767).astype("<i2").tobytes())


def main():
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise SystemExit("ffmpeg not found on PATH; it's needed to encode MP3 (winget install ffmpeg).")
    OUT.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        for name, fn in SOUNDS.items():
            x = finish(fn())
            wav = Path(tmp) / f"{name}.wav"
            write_wav(wav, x)
            mp3 = OUT / f"{name}.mp3"
            subprocess.run(
                [ffmpeg, "-y", "-loglevel", "error", "-i", str(wav), "-ac", "1", "-ar", str(SR),
                 "-c:a", "libmp3lame", "-b:a", "128k", str(mp3)],
                check=True,
            )
            print(f"{mp3.name:<22} {len(x) / SR:4.2f}s  {mp3.stat().st_size:>6} bytes")


if __name__ == "__main__":
    main()
