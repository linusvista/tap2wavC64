#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
TAP <-> WAV per Commodore 64  (con interfaccia grafica)

Funzioni
  1. TAP -> WAV / uscita audio  (per registrare su cassetta reale)
  2. WAV -> TAP                 (per recuperare nastri o controllare cosa e' stato scritto)
  3. Verifica                   (confronta una registrazione WAV con il TAP sorgente)

Dipendenze:   pip install numpy sounddevice      (sounddevice serve solo per la riproduzione)
Linux:        sudo apt install python3-tk libportaudio2
Test rapido:  python3 tap2wav_gui.py --selftest   (senza interfaccia grafica)
"""
import json
import os
import queue
import struct
import sys
import threading
import time
import traceback

import numpy as np

try:
    import tkinter as tk
    from tkinter import ttk, filedialog, messagebox, simpledialog
    from tkinter.scrolledtext import ScrolledText
except Exception:  # senza tkinter restano utilizzabili le funzioni di base e --selftest
    tk = None

CLOCKS = {"PAL": 985248, "NTSC": 1022727}
SIG = b"C64-TAPE-RAW"
PILOT = 0x30 * 8                     # impulso del tono pilota standard (cicli)
CONFIG_PATH = os.path.join(os.path.expanduser("~"), ".tap2wav_gui.json")


# ============================================================================
#  NUCLEO: TAP
# ============================================================================
def parse_cycles(data, version):
    """Durata in cicli CPU di ogni impulso (o semionda per TAP v2)."""
    arr = np.frombuffer(data, dtype=np.uint8)
    if not (arr == 0).any():
        return arr.astype(np.int64) * 8
    out, i, n = [], 0, len(data)
    while i < n:
        b = data[i]
        if b:
            out.append(b * 8)
            i += 1
        elif version == 0:
            out.append(256 * 8)
            i += 1
        else:
            if i + 3 >= n:
                break
            out.append(int.from_bytes(data[i + 1:i + 4], "little"))
            i += 4
    return np.array(out, dtype=np.int64)


def read_tap(path):
    with open(path, "rb") as f:
        raw = f.read()
    if raw[:12] != SIG:
        raise ValueError("Non è un file TAP valido (manca la firma C64-TAPE-RAW).")
    if len(raw) < 20:
        raise ValueError("File TAP troncato.")
    version = raw[12]
    if version > 2:
        raise ValueError(f"Versione TAP {version} non supportata.")
    video = raw[14]
    size = struct.unpack("<I", raw[16:20])[0]
    cycles = parse_cycles(raw[20:20 + size], version)
    return {"version": version, "cycles": cycles, "clock": "NTSC" if video == 1 else "PAL"}


def encode_tap(cycles, version=1, video=0):
    """Crea i byte di un file TAP a partire dalle durate in cicli."""
    cyc = np.maximum(np.rint(np.asarray(cycles, dtype=np.float64)).astype(np.int64), 8)
    b = np.maximum(np.rint(cyc / 8).astype(np.int64), 1)
    small = b <= 255
    if version == 0:
        data = np.where(small, b, 0).astype(np.uint8)
    else:
        nbytes = np.where(small, 1, 4)
        starts = np.cumsum(nbytes) - nbytes
        data = np.zeros(int(nbytes.sum()), dtype=np.uint8)
        data[starts[small]] = b[small]
        big = ~small
        idx = starts[big]
        c = np.minimum(cyc[big], 0xFFFFFF)
        data[idx + 1] = c & 0xFF
        data[idx + 2] = (c >> 8) & 0xFF
        data[idx + 3] = (c >> 16) & 0xFF
    header = SIG + bytes([version, 0, video, 0]) + struct.pack("<I", len(data))
    return header + data.tobytes()


def clock_hz(choice, tap_clock="PAL"):
    name = tap_clock if choice == "auto" else choice
    return CLOCKS[name], name


# ============================================================================
#  NUCLEO: generazione audio  (TAP -> segnale)
# ============================================================================
def render(cycles, version, clock, sr, shape="quadra", lead=2.0, tail=2.0, invert=False):
    """Segnale mono float32 in [-1, 1].

    Ogni impulso inizia con un fronte di DISCESA (il C64 misura la distanza tra
    fronti di discesa), resta basso per meta' impulso e poi risale.
    """
    if len(cycles) == 0:
        raise ValueError("Il TAP non contiene impulsi.")
    dur = cycles.astype(np.float64) / clock
    t_end = np.cumsum(dur)
    if version == 2:                                   # ogni valore e' una semionda
        toggles = np.concatenate(([0.0], t_end))        # un fronte anche all'inizio: prima semionda misurabile
    else:
        t_start = t_end - dur
        toggles = np.empty(2 * len(dur))
        toggles[0::2] = t_start
        toggles[1::2] = t_start + dur / 2
    pre = max(4, int(0.001 * sr))                      # 1 ms di livello alto prima del primo fronte di discesa
    pos = np.rint(toggles * sr).astype(np.int64) + pre
    end = max(int(np.rint(t_end[-1] * sr)) + pre, int(pos[-1]))
    lengths = np.diff(np.concatenate(([0], pos, [end])))
    levels = np.where(np.arange(len(lengths)) % 2 == 0, 1.0, -1.0).astype(np.float32)
    total = int(lengths.sum())
    ln, tn = int(lead * sr), int(tail * sr)
    out = np.zeros(ln + total + tn, dtype=np.float32)
    body = out[ln:ln + total]
    if shape == "quadra":
        body[:] = np.repeat(levels, lengths)
    else:
        starts = np.cumsum(lengths) - lengths
        CH = 200000
        lmax, ramp = int(0.004 * sr) + 1, max(2.0, 0.0002 * sr)
        for a in range(0, len(lengths), CH):
            L = lengths[a:a + CH]
            tot = int(L.sum())
            if tot == 0:
                continue
            off = np.arange(tot) - np.repeat(np.cumsum(L) - L, L)
            Lr = np.repeat(L, L)
            ph = (off + 0.5) / Lr
            w = np.sin(np.pi * ph) if shape == "seno" else 1.0 - np.abs(2.0 * ph - 1.0)
            longseg = Lr > lmax                     # pause lunghe: trapezio con rampe brevi
            if longseg.any():
                trap = np.minimum(1.0, np.minimum(off + 0.5, Lr - off - 0.5) / ramp)
                w = np.where(longseg, trap, w)
            body[starts[a]:starts[a] + tot] = np.repeat(levels[a:a + CH], L) * w.astype(np.float32)
    if invert:
        out = -out
    return out


def to_channels(sig, mode):
    z = np.zeros_like(sig)
    if mode == "stereo":
        return np.column_stack((sig, sig))
    if mode == "solo sinistro":
        return np.column_stack((sig, z))
    if mode == "solo destro":
        return np.column_stack((z, sig))
    return sig[:, None]


def to_pcm(data, volume, bits):
    x = np.clip(data * np.float32(volume), -1.0, 1.0)
    if bits == 8:
        return (np.rint(x * 127) + 128).astype(np.uint8)
    if bits == 16:
        return np.rint(x * 32767).astype(np.int16)
    return x.astype(np.float32)


def write_wav(path, pcm, sr):
    n, ch = pcm.shape
    bits = pcm.dtype.itemsize * 8
    fmt = 3 if pcm.dtype == np.float32 else 1
    data = pcm.astype(pcm.dtype.newbyteorder("<"), copy=False).tobytes()
    hdr = (b"RIFF" + struct.pack("<I", 36 + len(data)) + b"WAVE" + b"fmt " +
           struct.pack("<IHHIIHH", 16, fmt, ch, sr, sr * ch * bits // 8, ch * bits // 8, bits) +
           b"data" + struct.pack("<I", len(data)))
    with open(path, "wb") as f:
        f.write(hdr)
        f.write(data)


# ============================================================================
#  NUCLEO: lettura WAV e decodifica (segnale -> impulsi)
# ============================================================================
def read_wav(path):
    """Restituisce (campioni float32 [n, canali], sample rate, bit)."""
    with open(path, "rb") as f:
        raw = f.read()
    if raw[:4] not in (b"RIFF", b"RF64") or raw[8:12] != b"WAVE":
        raise ValueError("Non è un file WAV valido.")
    pos, fmt, data = 12, None, None
    while pos + 8 <= len(raw):
        cid = raw[pos:pos + 4]
        size = struct.unpack("<I", raw[pos + 4:pos + 8])[0]
        body = pos + 8
        if cid == b"fmt ":
            tag, ch, sr, _, _, bits = struct.unpack("<HHIIHH", raw[body:body + 16])
            if tag == 0xFFFE and size >= 26:
                tag = struct.unpack("<H", raw[body + 24:body + 26])[0]
            fmt = (tag, ch, sr, bits)
        elif cid == b"data":
            end = body + size if (size != 0xFFFFFFFF and body + size <= len(raw)) else len(raw)
            data = raw[body:end]
            break
        pos = body + size + (size & 1)
    if fmt is None or data is None:
        raise ValueError("WAV non leggibile (mancano i blocchi fmt/data).")
    tag, ch, sr, bits = fmt
    if tag == 1 and bits == 8:
        x = (np.frombuffer(data, dtype=np.uint8).astype(np.float32) - 128.0) / 128.0
    elif tag == 1 and bits == 16:
        x = np.frombuffer(data, dtype="<i2", count=len(data) // 2).astype(np.float32) / 32768.0
    elif tag == 1 and bits == 24:
        n = len(data) // 3
        b3 = np.frombuffer(data, dtype=np.uint8, count=n * 3).reshape(n, 3).astype(np.int32)
        v = b3[:, 0] | (b3[:, 1] << 8) | (b3[:, 2] << 16)
        x = ((v ^ 0x800000) - 0x800000).astype(np.float32) / 8388608.0
    elif tag == 1 and bits == 32:
        x = np.frombuffer(data, dtype="<i4", count=len(data) // 4).astype(np.float32) / 2147483648.0
    elif tag == 3 and bits in (32, 64):
        dt = "<f4" if bits == 32 else "<f8"
        x = np.frombuffer(data, dtype=dt, count=len(data) // (bits // 8)).astype(np.float32)
    else:
        raise ValueError(f"Formato WAV non supportato (tag {tag}, {bits} bit). Usa PCM o float.")
    frames = len(x) // ch
    return x[:frames * ch].reshape(frames, ch), sr, bits


def select_channel(x, mode):
    if x.shape[1] == 1:
        return np.ascontiguousarray(x[:, 0])
    if mode == "sinistro":
        return np.ascontiguousarray(x[:, 0])
    if mode == "destro":
        return np.ascontiguousarray(x[:, 1])
    if mode == "somma":
        return np.ascontiguousarray(x.mean(axis=1, dtype=np.float32))
    e = [float(np.sqrt(np.mean(x[::17, c].astype(np.float64) ** 2))) for c in range(x.shape[1])]
    return np.ascontiguousarray(x[:, int(np.argmax(e))])


def moving_average(x, w):
    """Media mobile centrata (finestra w campioni), a blocchi per risparmiare memoria."""
    n = len(x)
    out = np.empty(n, dtype=np.float32)
    B = 1 << 21
    h = w // 2
    for a in range(0, n, B):
        b = min(n, a + B)
        lo, hi = max(0, a - w), min(n, b + w)
        seg = x[lo:hi].astype(np.float64)
        c = np.concatenate(([0.0], np.cumsum(seg)))
        i = np.arange(a, b) - lo
        l = np.maximum(i - h, 0)
        r = np.minimum(i + h + 1, len(seg))
        out[a:b] = ((c[r] - c[l]) / (r - l)).astype(np.float32)
    return out


def decode_audio(x, sr, clock, edge="discesa", hyst=0.25, dc_ms=0.0, invert=False):
    """Segnale mono -> durate degli impulsi in cicli CPU (float64)."""
    x = np.asarray(x, dtype=np.float32)
    if invert:
        x = -x
    if dc_ms > 0:
        x = x - moving_average(x, max(3, int(sr * dc_ms / 1000.0)))
    else:
        x = x - np.float32(np.mean(x[::13], dtype=np.float64))
    sub = np.abs(x[::max(1, len(x) // 2000000)])
    peak = float(np.percentile(sub, 99.9)) if len(sub) else 0.0
    if peak < 1e-4:
        raise ValueError("Segnale troppo debole o assente nel file WAV.")
    th = np.float32(hyst * peak)
    hi = x > th
    lo = x < -th
    idx = np.flatnonzero(hi | lo)
    if len(idx) < 3:
        raise ValueError("Nessun impulso rilevato: prova a ridurre la soglia di isteresi.")
    st = hi[idx]
    chg = np.flatnonzero(st[1:] != st[:-1]) + 1
    tr, rising = idx[chg], st[chg]
    s = x >= 0
    zc = np.flatnonzero(s[1:] != s[:-1])
    k = np.searchsorted(zc, tr, side="left") - 1
    ok = k >= 0
    rising, k = rising[ok], k[ok]
    i = zc[k]
    x0 = x[i].astype(np.float64)
    x1 = x[i + 1].astype(np.float64)
    t = i + x0 / (x0 - x1)                               # istante dello zero, in campioni
    tc = t / sr * clock                                  # in cicli CPU
    if edge == "entrambi":
        pts = tc
    elif edge == "discesa":
        pts = tc[~rising]
    else:
        pts = tc[rising]
    info = {"peak": peak, "edges": int(len(t)), "duration": len(x) / sr}
    if len(pts) < 3:
        raise ValueError("Troppo pochi fronti rilevati per costruire un TAP.")
    return np.diff(pts), info


def pulse_histogram(cyc, top=6):
    b = np.rint(cyc / 8).astype(np.int64)
    b = b[(b > 0) & (b <= 255)]
    if len(b) == 0:
        return []
    cnt = np.bincount(b, minlength=256)
    order = np.argsort(cnt)[::-1][:top]
    return [(int(k), int(cnt[k])) for k in order if cnt[k] > 0]


# ============================================================================
#  NUCLEO: confronto WAV <-> TAP
# ============================================================================
def compare_pulses(ref, dec, clock, tol=0.12, resync=6, adaptive=True, gap=30000,
                   wide=40000, wide_budget=40, max_bad=5000, confirm=6):
    """Allinea gli impulsi decodificati a quelli del TAP sorgente.

    Tiene conto della velocita' del nastro (fattore di scala adattivo), ritrova
    il sincronismo dopo impulsi persi/aggiunti e distingue le differenze di
    lunghezza del tono pilota (informative) dagli errori veri.
    """
    R = [int(v) for v in ref]
    D = [float(v) for v in dec]
    nr, nd = len(R), len(D)
    if nr < 10 or nd < 10:
        raise ValueError("Troppo pochi impulsi per fare un confronto.")

    def ok(r, d, sc, t):
        if r >= gap:
            return d >= gap * 0.5
        e = r * sc
        return abs(d - e) <= max(t * e, 16.0)

    def run(a, b, n, sc, t):
        m = min(n, nr - a, nd - b)
        if m < min(n, 3):
            return False
        for k in range(m):
            if not ok(R[a + k], D[b + k], sc, t):
                return False
        return True

    cands = sorted(((a, b) for a in range(resync + 1) for b in range(resync + 1) if a + b > 0),
                   key=lambda p: (p[0] + p[1], abs(p[0] - p[1])))

    def wide_search(i, j, sc, t, n, zero_ok=False):
        for d in range(0, wide + 1):
            if i + d >= nr and j + d >= nd:
                break
            for e in (0, 1, 2):
                for di, dj in ((d, e), (e, d)):
                    if di == 0 and dj == 0 and not zero_ok:
                        continue
                    a, b = i + di, j + dj
                    if a >= nr or b >= nd:
                        continue
                    if ok(R[a], D[b], sc, t) and run(a, b, n, sc, t):
                        return di, dj
        return None

    sync = wide_search(0, 0, 1.0, max(tol, 0.25), 16, zero_ok=True)
    if sync is None:
        raise ValueError("Impossibile allineare i due segnali: il WAV non sembra contenere "
                         "lo stesso nastro (prova un'altra polarità, soglia o canale).")
    i, j = sync
    start_skip = (i, j)
    m = min(300, nr - i, nd - j)
    ratios = [D[j + k] / R[i + k] for k in range(m) if R[i + k] < gap and D[j + k] < gap]
    scale = float(np.median(ratios)) if ratios else 1.0
    smin = smax = scale

    dev = np.full(nr, np.nan, dtype=np.float32)
    err = np.zeros(nr, dtype=bool)
    events = []
    st = {"ok": 0, "gaps": 0, "sub": 0, "ins": 0, "canc": 0, "pilot_ref": 0, "pilot_dec": 0}
    bad_total, aborted = 0, False

    def pilot_like(seq):
        return len(seq) >= 64 and max(seq) <= 1.25 * min(seq)

    while i < nr and j < nd:
        r, d = R[i], D[j]
        if ok(r, d, scale, tol):
            if r >= gap:
                st["gaps"] += 1
            else:
                st["ok"] += 1
                ratio = d / r
                dev[i] = ratio / scale - 1.0
                if adaptive:
                    scale += 0.02 * (ratio - scale)
                    smin, smax = min(smin, scale), max(smax, scale)
            i += 1
            j += 1
            continue
        found, kind = None, "local"
        for di, dj in cands:
            if run(i + di, j + dj, confirm, scale, tol):
                found = (di, dj)
                break
        if found is None and wide_budget > 0:
            wide_budget -= 1
            found, kind = wide_search(i, j, scale, tol, 12), "wide"
        if found is None:
            st["sub"] += 1
            err[i] = True
            events.append(("sostituzione", i, j, 1, r, d))
            i += 1
            j += 1
            bad_total += 1
            if bad_total > max_bad:
                aborted = True
                break
            continue
        di, dj = found
        sub = min(di, dj)
        canc, ins = di - sub, dj - sub
        if sub:
            st["sub"] += sub
            err[i:i + sub] = True
            events.append(("sostituzione", i, j, sub, R[i], D[j]))
        if canc:
            seg = R[i + sub:i + sub + canc]
            if kind == "wide" and pilot_like(seg):
                st["pilot_ref"] += canc
                events.append(("pilota (TAP piu' lungo)", i + sub, j + sub, canc, R[i + sub], 0))
            else:
                st["canc"] += canc
                err[i + sub:i + sub + canc] = True
                events.append(("mancante nel WAV", i + sub, j + sub, canc, R[i + sub], 0))
        if ins:
            seg = D[j + sub:j + sub + ins]
            if kind == "wide" and pilot_like(seg):
                st["pilot_dec"] += ins
                events.append(("pilota (WAV piu' lungo)", i + sub, j + sub, ins, 0, D[j + sub]))
            else:
                st["ins"] += ins
                err[min(i + sub, nr - 1)] = True
                events.append(("extra nel WAV", i + sub, j + sub, ins, 0, D[j + sub]))
        i += di
        j += dj
        bad_total += canc + ins + sub
        if bad_total > max_bad:
            aborted = True
            break

    tail_ref = 0 if aborted else nr - i
    tail_dec = 0 if aborted else nd - j
    errors = st["sub"] + st["ins"] + st["canc"] + (tail_ref if tail_ref > 2 else 0)
    measured = max(1, st["ok"] + st["sub"] + st["canc"])
    rate = errors / measured
    if aborted:
        verdict = "NON COERENTE - troppi errori, l'allineamento è stato interrotto."
    elif errors == 0:
        verdict = "COERENTE - tutti gli impulsi corrispondono al TAP (entro la tolleranza)."
    elif rate < 0.0005:
        verdict = "QUASI COERENTE - solo errori isolati; un loader con checksum li segnalerebbe."
    else:
        verdict = "NON COERENTE - troppe differenze rispetto al TAP sorgente."

    fin = dev[np.isfinite(dev)]
    ref_arr = np.asarray(R, dtype=np.float64)
    return {
        "nr": nr, "nd": nd, "stats": st, "errors": errors, "rate": rate, "aborted": aborted,
        "events": events, "dev": dev, "err": err, "verdict": verdict,
        "scale": (smin, scale, smax), "start_skip": start_skip,
        "tail_ref": tail_ref, "tail_dec": tail_dec,
        "dev_mean": float(np.mean(np.abs(fin))) if len(fin) else 0.0,
        "dev_p99": float(np.percentile(np.abs(fin), 99)) if len(fin) else 0.0,
        "tstart": (np.cumsum(ref_arr) - ref_arr) / clock,
        "ref_dur": float(ref_arr.sum() / clock),
    }


def fmt_time(s):
    m = int(s // 60)
    return f"{m}:{s - 60 * m:04.1f}"


def format_report(res, tap_name, wav_name, info, tol, max_events):
    st = res["stats"]
    L = [f"TAP sorgente : {tap_name}  ({res['nr']:,} impulsi, durata {fmt_time(res['ref_dur'])})",
         f"WAV          : {wav_name}  ({res['nd']:,} impulsi decodificati, "
         f"durata {fmt_time(info['duration'])})", ""]
    L.append(f"RISULTATO: {res['verdict']}")
    L.append("")
    L.append(f"  Impulsi corrispondenti : {st['ok']:,}")
    L.append(f"  Pause lunghe (gap)     : {st['gaps']:,}")
    L.append(f"  Sostituzioni           : {st['sub']:,}   (durata diversa dal TAP)")
    L.append(f"  Extra nel WAV          : {st['ins']:,}   (impulsi in piu')")
    L.append(f"  Mancanti nel WAV       : {st['canc']:,}   (impulsi persi)")
    if res["tail_ref"] > 2:
        L.append(f"  Coda TAP non trovata   : {res['tail_ref']:,} impulsi (il WAV finisce prima)")
    L.append(f"  Errori totali          : {res['errors']:,}  ({res['rate'] * 100:.4f}% degli impulsi)")
    L.append("")
    smin, sfin, smax = res["scale"]
    L.append(f"  Velocità nastro        : {(sfin - 1) * 100:+.2f}% (oscillazione "
             f"{(smin - 1) * 100:+.2f}% … {(smax - 1) * 100:+.2f}%, wow/flutter)")
    L.append(f"  Scarto residuo         : medio {res['dev_mean'] * 100:.2f}%, 99° percentile "
             f"{res['dev_p99'] * 100:.2f}%  (tolleranza ±{tol * 100:.0f}%)")
    if st["pilot_ref"] or st["pilot_dec"]:
        L.append(f"  Tono pilota diverso    : TAP +{st['pilot_ref']:,} / WAV +{st['pilot_dec']:,} "
                 f"impulsi (informativo, non conta come errore)")
    if res["start_skip"] != (0, 0):
        L.append(f"  Allineamento iniziale  : scartati {res['start_skip'][0]} impulsi TAP e "
                 f"{res['start_skip'][1]} del WAV")
    if res["tail_dec"] > 20:
        L.append(f"  Impulsi extra in coda  : {res['tail_dec']:,} (rumore dopo la fine del nastro?)")
    ev = res["events"]
    if ev:
        L.append("")
        L.append(f"Primi {min(len(ev), max_events)} eventi su {len(ev)} "
                 f"(valori in unita' TAP = cicli/8):")
        for kind, i, j, n, r, d in ev[:max_events]:
            ti = res["tstart"][min(i, res["nr"] - 1)]
            L.append(f"  {kind:<26} impulso {i + 1:>8,}  t={fmt_time(ti):>8}  x{n:<5} "
                     f"TAP={r / 8:6.1f}  WAV={d / 8:6.1f}")
    return "\n".join(L)


# ============================================================================
#  AUTOTEST (senza GUI)
# ============================================================================
def _synthetic_tap(rng, n_data=12000, n_pilot=3000):
    pilot = np.full(n_pilot, 0x30 * 8)
    data = rng.choice([0x30, 0x42, 0x56], size=n_data) * 8
    gap = np.array([200000])
    return np.concatenate((pilot, [0x56 * 8, 0x42 * 8], data, gap, pilot[:800], data[:3000])).astype(np.int64)


def selftest():
    rng = np.random.default_rng(7)
    cyc = _synthetic_tap(rng)
    clock = CLOCKS["PAL"]
    fails = 0

    def check(name, cond, extra=""):
        nonlocal fails
        print(("  OK   " if cond else "  FAIL ") + name + (" " + extra if extra else ""))
        fails += 0 if cond else 1

    raw = encode_tap(cyc, 1)
    tmp = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_selftest.tap")
    open(tmp, "wb").write(raw)
    back = read_tap(tmp)["cycles"]
    os.remove(tmp)
    check("TAP encode/decode", len(back) == len(cyc) and np.all(np.abs(back - cyc) <= 4))

    for sr, shape in ((96000, "quadra"), (44100, "seno"), (48000, "triangolare")):
        sig = render(cyc, 1, clock, sr, shape)
        dec, info = decode_audio(sig, sr, clock, "discesa", 0.25, 0)
        res = compare_pulses(cyc, dec, clock)
        check(f"andata e ritorno {sr} Hz {shape}", res["errors"] == 0, f"({res['verdict'][:12]})")
        wav = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_selftest.wav")
        for bits in (8, 16, 32):
            write_wav(wav, to_pcm(to_channels(sig, "mono"), 0.9, bits), sr)
            x, sr2, _ = read_wav(wav)
            check(f"  WAV {bits} bit scrittura/lettura", sr2 == sr and len(x) == len(sig))
        os.remove(wav)

    # nastro "realistico": velocita' +4%, rumore, inizio tagliato (dentro il pilota), 2 buchi
    sr = 96000
    sig = render(cyc, 1, clock, sr, "quadra")
    n = len(sig)
    sig = np.interp(np.arange(0, n - 1, 1.04), np.arange(n), sig).astype(np.float32)
    sig = sig[int(1.0 * sr):]                                   # perde ~1 s di pilota
    sig[int(3.0 * sr):int(3.0 * sr) + 700] = 0                  # dropout
    sig[int(5.0 * sr):int(5.0 * sr) + 400] = 0
    sig += rng.normal(0, 0.04, len(sig)).astype(np.float32)
    dec, _ = decode_audio(sig, sr, clock, "discesa", 0.3, 0)
    res = compare_pulses(cyc, dec, clock)
    print("  nastro simulato:", res["verdict"], f"errori={res['errors']}",
          f"velocità={(res['scale'][1] - 1) * 100:+.1f}%")
    check("nastro con velocità +4%, rumore e dropout riconosciuto", res["errors"] < 40 and not res["aborted"])

    # polarita' invertita: con 'salita' deve tornare coerente
    sig = -render(cyc, 1, clock, sr, "quadra")
    dec, _ = decode_audio(sig, sr, clock, "salita", 0.25, 0)
    check("polarità invertita + fronte di salita", compare_pulses(cyc, dec, clock)["errors"] == 0)

    # contenuto diverso
    other = _synthetic_tap(np.random.default_rng(99))
    sig = render(other, 1, clock, sr, "quadra")
    dec, _ = decode_audio(sig, sr, clock, "discesa", 0.25, 0)
    try:
        res = compare_pulses(cyc, dec, clock)
        check("contenuto diverso rilevato", res["errors"] > 100 or res["aborted"])
    except ValueError:
        check("contenuto diverso rilevato", True)

    print("TUTTO OK" if not fails else f"{fails} TEST FALLITI")
    return 1 if fails else 0


# ============================================================================
#  CONFIGURAZIONE / PRESET
# ============================================================================
def load_store():
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            s = json.load(f)
    except Exception:
        s = {}
    s.setdefault("presets", {})
    s.setdefault("last", {})
    return s


def save_store(s):
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(s, f, indent=1, ensure_ascii=False)
    except Exception:
        pass


ENC_FIELDS = [
    ("sr", "Frequenza di campionamento (Hz)", "combo_edit", ["22050", "44100", "48000", "96000", "192000"], "96000"),
    ("clock", "Clock del C64", "combo", ["auto", "PAL", "NTSC"], "auto"),
    ("shape", "Forma d'onda", "combo", ["quadra", "seno", "triangolare"], "quadra"),
    ("volume", "Volume (%)", "scale", (10, 100), 90),
    ("invert", "Inverti polarità", "bool", None, False),
    ("channels", "Canali", "combo", ["mono", "stereo", "solo sinistro", "solo destro"], "mono"),
    ("bits", "Formato WAV", "combo", ["16 bit", "8 bit", "32 bit float"], "16 bit"),
    ("lead", "Silenzio iniziale (s)", "float", (0, 120, 0.5), 2.0),
    ("tail", "Silenzio finale (s)", "float", (0, 120, 0.5), 2.0),
]

ENC_PRESETS = {
    "Consigliato (96 kHz, onda quadra)": {},
    "Standard Kernal (44,1 kHz)": {"sr": "44100"},
    "Alta precisione (192 kHz)": {"sr": "192000"},
    "Registratore a cassette (onda sinusoidale)": {"sr": "48000", "shape": "seno", "volume": 80},
    "Registratore a cassette (onda triangolare)": {"sr": "48000", "shape": "triangolare", "volume": 80},
    "Solo canale sinistro (mono su registratore stereo)": {"channels": "solo sinistro"},
    "File leggero (44,1 kHz, 8 bit)": {"sr": "44100", "bits": "8 bit"},
}

DEC_FIELDS = [
    ("channel", "Canale audio", "combo", ["auto", "sinistro", "destro", "somma"], "auto"),
    ("edge", "Fronte di misura", "combo", ["discesa", "salita", "entrambi (TAP v2)"], "discesa"),
    ("hyst", "Soglia / isteresi (% del picco)", "scale", (5, 60), 25),
    ("dc", "Offset DC: finestra mobile in ms (0 = solo media globale)", "float", (0, 500, 5), 0.0),
    ("invert", "Inverti polarità", "bool", None, False),
    ("clock", "Clock del C64", "combo", ["auto", "PAL", "NTSC"], "auto"),
    ("version", "Versione TAP in uscita", "combo", ["1", "0"], "1"),
]

DEC_PRESETS = {
    "Registrazione pulita": {},
    "Registrazione rumorosa / fruscio": {"hyst": 40},
    "Segnale debole": {"hyst": 12},
    "Livello instabile / offset variabile": {"hyst": 30, "dc": 100.0},
    "Segnale invertito": {"invert": True},
    "Misura sul fronte di salita": {"edge": "salita"},
    "Mezze onde (TAP v2)": {"edge": "entrambi (TAP v2)"},
}

VER_FIELDS = [
    ("tol", "Tolleranza sulla durata (%)", "scale", (3, 40), 12),
    ("resync", "Impulsi massimi per risincronizzare", "int", (1, 12, 1), 6),
    ("adaptive", "Compensa la velocità del nastro", "bool", None, True),
    ("autopol", "Rileva la polarità automaticamente", "bool", None, True),
    ("events", "Eventi mostrati nel rapporto", "int", (5, 500, 5), 40),
]

VER_PRESETS = {
    "Normale (±12%)": {},
    "Severa (±6%)": {"tol": 6},
    "Tollerante (±20%)": {"tol": 20},
}


def _merge(fields, preset):
    d = {f[0]: f[4] for f in fields}
    d.update(preset)
    return d


# ============================================================================
#  INTERFACCIA GRAFICA
# ============================================================================
if tk is not None:

    class ParamPanel(ttk.LabelFrame):
        """Pannello di parametri con preset predefiniti e personalizzabili."""

        def __init__(self, master, title, kind, fields, builtin, store):
            super().__init__(master, text=title, padding=6)
            self.kind, self.fields, self.store = kind, fields, store
            self.builtin = {n: _merge(fields, p) for n, p in builtin.items()}
            self.vars = {}
            top = ttk.Frame(self)
            top.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 6))
            ttk.Label(top, text="Preset:").pack(side="left")
            self.preset_var = tk.StringVar()
            self.cb = ttk.Combobox(top, textvariable=self.preset_var, state="readonly", width=38)
            self.cb.pack(side="left", padx=4)
            self.cb.bind("<<ComboboxSelected>>", lambda e: self.apply_preset(self.preset_var.get()))
            ttk.Button(top, text="Salva…", width=7, command=self.save_preset).pack(side="left")
            ttk.Button(top, text="Elimina", width=7, command=self.del_preset).pack(side="left", padx=2)

            for r, (key, label, typ, opts, default) in enumerate(fields, start=1):
                ttk.Label(self, text=label).grid(row=r, column=0, sticky="w", padx=(2, 10), pady=2)
                if typ in ("combo", "combo_edit"):
                    var = tk.StringVar(value=str(default))
                    w = ttk.Combobox(self, textvariable=var, values=opts, width=20,
                                     state="normal" if typ == "combo_edit" else "readonly")
                elif typ == "bool":
                    var = tk.BooleanVar(value=bool(default))
                    w = ttk.Checkbutton(self, variable=var)
                elif typ == "scale":
                    var = tk.DoubleVar(value=float(default))
                    w = ttk.Frame(self)
                    lab = ttk.Label(w, width=4, text=str(int(default)))
                    ttk.Scale(w, from_=opts[0], to=opts[1], variable=var, length=170).pack(side="left")
                    lab.pack(side="left", padx=4)
                    var.trace_add("write", lambda *a, v=var, l=lab: l.config(text=str(int(round(v.get())))))
                else:  # int / float
                    var = tk.StringVar(value=str(default))
                    w = ttk.Spinbox(self, textvariable=var, from_=opts[0], to=opts[1],
                                    increment=opts[2], width=10)
                self.vars[key] = var
                w.grid(row=r, column=1, sticky="w", pady=2)
            self.columnconfigure(1, weight=1)
            self.refresh_list()
            last = store["last"].get(kind)
            if last:
                self.set(last)
            else:
                first = next(iter(self.builtin))
                self.preset_var.set(first)
                self.set(self.builtin[first])

        # --- preset ---------------------------------------------------------
        def all_presets(self):
            d = dict(self.builtin)
            for n, v in self.store["presets"].get(self.kind, {}).items():
                d["★ " + n] = _merge(self.fields, v)
            return d

        def refresh_list(self):
            self.cb["values"] = list(self.all_presets().keys())

        def apply_preset(self, name):
            p = self.all_presets().get(name)
            if p:
                self.set(p)

        def save_preset(self):
            try:
                vals = self.get()
            except ValueError as e:
                messagebox.showerror("Errore", str(e))
                return
            name = simpledialog.askstring("Salva preset", "Nome del preset:", parent=self)
            if not name:
                return
            name = name.strip().lstrip("★ ").strip()
            if not name:
                return
            self.store["presets"].setdefault(self.kind, {})[name] = self._raw()
            save_store(self.store)
            self.refresh_list()
            self.preset_var.set("★ " + name)

        def del_preset(self):
            name = self.preset_var.get()
            if not name.startswith("★ "):
                messagebox.showinfo("Preset", "I preset predefiniti non possono essere eliminati.")
                return
            self.store["presets"].get(self.kind, {}).pop(name[2:], None)
            save_store(self.store)
            self.refresh_list()
            self.preset_var.set("")

        # --- valori ---------------------------------------------------------
        def _raw(self):
            out = {}
            for key, label, typ, opts, default in self.fields:
                v = self.vars[key].get()
                out[key] = float(v) if typ == "scale" else v
            return out

        def set(self, values):
            for key, label, typ, opts, default in self.fields:
                if key in values:
                    try:
                        self.vars[key].set(values[key])
                    except Exception:
                        pass

        def get(self):
            out = {}
            for key, label, typ, opts, default in self.fields:
                v = self.vars[key].get()
                try:
                    if typ == "int":
                        out[key] = int(float(str(v).replace(",", ".")))
                    elif typ == "float":
                        out[key] = float(str(v).replace(",", "."))
                    elif typ == "scale":
                        out[key] = float(v)
                    elif typ == "bool":
                        out[key] = bool(v)
                    elif key == "sr":
                        out[key] = int(float(str(v).replace(",", ".")))
                    else:
                        out[key] = str(v)
                except ValueError:
                    raise ValueError(f"Valore non valido per «{label}»: {v!r}")
            return out


    class App:
        def __init__(self, root):
            self.root = root
            root.title("TAP ⇄ WAV per Commodore 64")
            root.geometry("1020x960")
            self.store = load_store()
            self.q = queue.Queue()
            self.busy = False
            self.playing = False
            self.recording = False
            self.rec_stream = None
            self.rec_chunks = []
            self.rec_data = None
            self.rec_sr = 0
            self.rec_peak_now = 0.0
            self.rec_peak_max = 0.0
            self.report_text = ""
            self.status = tk.StringVar(value="Pronto")

            nb = ttk.Notebook(root)
            nb.pack(fill="both", expand=True, padx=6, pady=(6, 0))
            self.build_tab_play(nb)
            self.build_tab_decode(nb)
            self.build_tab_verify(nb)

            bar = ttk.Frame(root)
            bar.pack(fill="x", padx=6, pady=4)
            ttk.Label(bar, textvariable=self.status).pack(side="left")
            self.progress = ttk.Progressbar(bar, mode="indeterminate", length=160)
            self.progress.pack(side="right")
            root.after(100, self.poll)
            root.protocol("WM_DELETE_WINDOW", self.on_close)

        # --- utilita' -------------------------------------------------------
        def on_close(self):
            try:
                if self.rec_stream is not None:
                    self.rec_stream.stop()
                    self.rec_stream.close()
            except Exception:
                pass
            try:
                sd = __import__("sounddevice")
                sd.stop()
            except Exception:
                pass
            for kind, panel in (("encode", self.enc), ("decode", self.dec), ("decode2", self.dec2),
                                ("verify", self.ver)):
                try:
                    self.store["last"][kind] = panel._raw()
                except Exception:
                    pass
            save_store(self.store)
            self.root.destroy()

        def file_row(self, parent, label, var, kind, command=None):
            f = ttk.Frame(parent)
            ttk.Label(f, text=label, width=16).pack(side="left")
            ttk.Entry(f, textvariable=var).pack(side="left", fill="x", expand=True, padx=4)
            ttk.Button(f, text="Sfoglia…", command=lambda: self.browse(var, kind, command)).pack(side="left")
            return f

        def browse(self, var, kind, cb=None):
            types = {"tap": [("File TAP", "*.tap *.TAP"), ("Tutti i file", "*.*")],
                     "wav": [("File WAV", "*.wav *.WAV"), ("Tutti i file", "*.*")]}[kind]
            p = filedialog.askopenfilename(filetypes=types)
            if p:
                var.set(p)
                if cb:
                    cb()

        def log(self, widget, text, clear=False):
            widget.configure(state="normal")
            if clear:
                widget.delete("1.0", "end")
            widget.insert("end", text + "\n")
            widget.see("end")
            widget.configure(state="disabled")

        def run_bg(self, work, done, msg):
            if self.busy:
                messagebox.showinfo("Attendi", "C'è già un'operazione in corso.")
                return
            self.busy = True
            self.status.set(msg)
            self.progress.start(12)

            def target():
                try:
                    self.q.put((done, work(), None))
                except Exception as e:
                    if not isinstance(e, ValueError):
                        traceback.print_exc()
                    self.q.put((done, None, str(e) or e.__class__.__name__))

            threading.Thread(target=target, daemon=True).start()

        def poll(self):
            try:
                while True:
                    done, res, err = self.q.get_nowait()
                    self.busy = False
                    self.progress.stop()
                    self.status.set("Pronto")
                    if err is not None:
                        messagebox.showerror("Errore", err)
                    else:
                        done(res)
            except queue.Empty:
                pass
            self.root.after(100, self.poll)

        # --- scheda 1: TAP -> WAV / audio -----------------------------------
        def build_tab_play(self, nb):
            tab = ttk.Frame(nb, padding=8)
            nb.add(tab, text="  TAP → WAV / Audio  ")
            self.tap_var = tk.StringVar()
            self.file_row(tab, "File TAP:", self.tap_var, "tap", self.refresh_tap_info).pack(fill="x")
            self.tap_info = tk.StringVar(value="")
            ttk.Label(tab, textvariable=self.tap_info, foreground="#555").pack(anchor="w", pady=(2, 8))

            mid = ttk.Frame(tab)
            mid.pack(fill="x")
            self.enc = ParamPanel(mid, "Parametri audio", "encode", ENC_FIELDS, ENC_PRESETS, self.store)
            self.enc.pack(side="left", fill="y")

            right = ttk.LabelFrame(mid, text="Azioni", padding=8)
            right.pack(side="left", fill="both", expand=True, padx=(8, 0))
            ttk.Button(right, text="💾  Salva come WAV…", command=self.do_save_wav).pack(fill="x", pady=2)
            ttk.Separator(right).pack(fill="x", pady=8)
            ttk.Label(right, text="Uscita audio:").pack(anchor="w")
            self.dev_var = tk.StringVar(value="Predefinito")
            row = ttk.Frame(right)
            row.pack(fill="x")
            self.dev_cb = ttk.Combobox(row, textvariable=self.dev_var, state="readonly", width=34)
            self.dev_cb.pack(side="left", fill="x", expand=True)
            ttk.Button(row, text="↻", width=3, command=self.refresh_devices).pack(side="left", padx=2)
            row = ttk.Frame(right)
            row.pack(fill="x", pady=6)
            ttk.Label(row, text="Inizia da (s):").pack(side="left")
            self.start_var = tk.StringVar(value="0")
            ttk.Spinbox(row, textvariable=self.start_var, from_=0, to=7200, increment=1, width=8).pack(side="left", padx=6)
            ttk.Button(right, text="▶  Riproduci sull'uscita audio", command=self.do_play).pack(fill="x", pady=2)
            ttk.Button(right, text="⏹  Stop", command=self.do_stop).pack(fill="x", pady=2)
            ttk.Button(right, text="〰  Tono pilota di calibrazione (30 s)", command=self.do_pilot).pack(fill="x", pady=(10, 2))
            self.play_lbl = tk.StringVar(value="")
            ttk.Label(right, textvariable=self.play_lbl, font=("TkDefaultFont", 11, "bold")).pack(pady=6)
            ttk.Label(right, foreground="#555", wraplength=330, justify="left",
                      text="Consiglio: riproduci il tono pilota e regola il livello di registrazione senza "
                           "saturare; disattiva Dolby, riduzione rumore e AGC/limiter del registratore.").pack(anchor="w")

            self.log1 = ScrolledText(tab, height=9, state="disabled")
            self.log1.pack(fill="both", expand=True, pady=(8, 0))
            self.refresh_devices()

        def refresh_devices(self):
            items = ["Predefinito"]
            try:
                import sounddevice as sd
                for i, d in enumerate(sd.query_devices()):
                    if d["max_output_channels"] > 0:
                        items.append(f"{i}: {d['name']}")
            except Exception:
                pass
            self.dev_cb["values"] = items

        def selected_device(self):
            v = self.dev_var.get()
            return int(v.split(":")[0]) if ":" in v else None

        def refresh_tap_info(self):
            try:
                tap = self.load_tap()
                clock = CLOCKS[tap["clock"]]
                dur = tap["cycles"].sum() / clock
                self.tap_info.set(f"TAP v{tap['version']} · {len(tap['cycles']):,} impulsi · "
                                  f"durata {fmt_time(dur)} · {tap['clock']}")
            except Exception as e:
                self.tap_info.set(str(e))

        def load_tap(self, var=None):
            p = (var or self.tap_var).get().strip()
            if not p:
                raise ValueError("Seleziona un file TAP.")
            if not os.path.isfile(p):
                raise ValueError(f"File non trovato: {p}")
            return read_tap(p)

        def build_signal(self, tap, params, pilot=False):
            clock, _ = clock_hz(params["clock"], tap["clock"])
            if pilot:
                cyc = np.full(int(30 * clock / PILOT), PILOT, dtype=np.int64)
                sig = render(cyc, 1, clock, params["sr"], params["shape"], 0.3, 0.3, params["invert"])
            else:
                sig = render(tap["cycles"], tap["version"], clock, params["sr"], params["shape"],
                             params["lead"], params["tail"], params["invert"])
            return to_channels(sig, params["channels"])

        def do_save_wav(self):
            try:
                tap, params = self.load_tap(), self.enc.get()
            except ValueError as e:
                return messagebox.showerror("Errore", str(e))
            stem = os.path.splitext(os.path.basename(self.tap_var.get()))[0]
            out = filedialog.asksaveasfilename(defaultextension=".wav", initialfile=stem + ".wav",
                                               filetypes=[("File WAV", "*.wav")])
            if not out:
                return
            bits = {"8 bit": 8, "16 bit": 16}.get(params["bits"], 32)

            def work():
                data = self.build_signal(tap, params)
                write_wav(out, to_pcm(data, params["volume"] / 100.0, bits), params["sr"])
                return len(data) / params["sr"], os.path.getsize(out)

            def done(r):
                self.log(self.log1, f"WAV salvato: {out}\n  durata {fmt_time(r[0])}, "
                                    f"{params['sr']} Hz, {params['bits']}, {params['channels']}, "
                                    f"{r[1] / 1e6:.1f} MB")
            self.run_bg(work, done, "Generazione WAV…")

        def _play(self, pilot):
            try:
                tap, params = (None if pilot else self.load_tap()), self.enc.get()
                if pilot:
                    try:
                        tap = self.load_tap()
                    except ValueError:
                        tap = {"clock": "PAL"}
                start = float(self.start_var.get().replace(",", ".") or 0)
                import sounddevice  # noqa: F401
            except (ImportError, OSError):
                return messagebox.showerror("Audio non disponibile",
                                            "Serve il modulo sounddevice con la libreria PortAudio.\n\n"
                                            "Installa:  pip install sounddevice\n"
                                            "Linux:     sudo apt install libportaudio2")
            except ValueError as e:
                return messagebox.showerror("Errore", str(e))
            dev = self.selected_device()

            def work():
                data = self.build_signal(tap, params, pilot) * np.float32(params["volume"] / 100.0)
                return data[int(start * params["sr"]):], params["sr"]

            def done(r):
                import sounddevice as sd
                data, sr = r
                try:
                    sd.stop()
                    sd.play(data, sr, device=dev)
                except Exception as e:
                    return messagebox.showerror("Errore audio", f"{e}\n\nProva un'altra frequenza di "
                                                                f"campionamento (es. 48000) o un altro dispositivo.")
                self.playing = True
                self.play_t0 = time.time()
                self.play_len = len(data) / sr
                self.log(self.log1, f"Riproduzione {'del tono pilota' if pilot else 'del TAP'} a {sr} Hz…")
                self.tick()
            self.run_bg(work, done, "Preparazione audio…")

        def do_play(self):
            self._play(False)

        def do_pilot(self):
            self._play(True)

        def do_stop(self):
            try:
                import sounddevice as sd
                sd.stop()
            except Exception:
                pass
            self.playing = False
            self.play_lbl.set("Fermato")

        def tick(self):
            if not self.playing:
                return
            el = time.time() - self.play_t0
            if el >= self.play_len + 0.3:
                self.playing = False
                self.play_lbl.set("Riproduzione terminata")
                return
            self.play_lbl.set(f"▶ {fmt_time(min(el, self.play_len))} / {fmt_time(self.play_len)}")
            self.root.after(250, self.tick)

        # --- scheda 2: WAV -> TAP -------------------------------------------
        def build_tab_decode(self, nb):
            tab = ttk.Frame(nb, padding=8)
            nb.add(tab, text="  WAV → TAP  ")
            self.wav_var = tk.StringVar()
            self.file_row(tab, "File WAV:", self.wav_var, "wav").pack(fill="x", pady=(0, 8))
            self.dec = ParamPanel(tab, "Parametri di decodifica", "decode", DEC_FIELDS, DEC_PRESETS, self.store)
            self.dec.pack(anchor="w")
            ttk.Button(tab, text="⚙  Decodifica e salva TAP…", command=self.do_decode).pack(anchor="w", pady=8)
            self.log2 = ScrolledText(tab, height=14, state="disabled")
            self.log2.pack(fill="both", expand=True)

        def do_decode(self):
            wav = self.wav_var.get().strip()
            if not os.path.isfile(wav):
                return messagebox.showerror("Errore", "Seleziona un file WAV valido.")
            try:
                p = self.dec.get()
            except ValueError as e:
                return messagebox.showerror("Errore", str(e))
            stem = os.path.splitext(os.path.basename(wav))[0]
            out = filedialog.asksaveasfilename(defaultextension=".tap", initialfile=stem + ".tap",
                                               filetypes=[("File TAP", "*.tap")])
            if not out:
                return
            edge = "entrambi" if p["edge"].startswith("entrambi") else p["edge"]

            def work():
                x, sr, bits = read_wav(wav)
                ch = select_channel(x, p["channel"])
                clock, cname = clock_hz(p["clock"], "PAL")
                cyc, info = decode_audio(ch, sr, clock, edge, p["hyst"] / 100.0, p["dc"], p["invert"])
                version = 2 if edge == "entrambi" else int(p["version"])
                raw = encode_tap(cyc, version, 1 if cname == "NTSC" else 0)
                with open(out, "wb") as f:
                    f.write(raw)
                lines = [f"WAV: {os.path.basename(wav)} - {sr} Hz, {bits} bit, {x.shape[1]} can., "
                         f"durata {fmt_time(info['duration'])}",
                         f"Fronti rilevati: {info['edges']:,} · impulsi: {len(cyc):,} · clock {cname}",
                         f"TAP v{version} salvato: {out} ({len(raw) / 1024:.1f} KB)"]
                hist = pulse_histogram(cyc)
                if hist:
                    lines.append("Valori più frequenti (byte TAP): " +
                                 ", ".join(f"${k:02X}×{c:,}" for k, c in hist))
                if sr < 44100:
                    lines.append("ATTENZIONE: campionamento basso, la precisione sugli impulsi turbo sarà scarsa.")
                lines.append("Suggerimento: usa la scheda «Verifica» per confrontarlo col TAP sorgente.")
                return "\n".join(lines)
            self.run_bg(work, lambda r: self.log(self.log2, r + "\n"), "Decodifica in corso…")

        # --- scheda 3: verifica ---------------------------------------------
        def build_tab_verify(self, nb):
            tab = ttk.Frame(nb, padding=8)
            nb.add(tab, text="  Verifica WAV vs TAP  ")
            self.vtap_var, self.vwav_var = tk.StringVar(), tk.StringVar()
            self.file_row(tab, "TAP sorgente:", self.vtap_var, "tap").pack(fill="x")
            self.file_row(tab, "WAV registrato:", self.vwav_var, "wav").pack(fill="x", pady=(2, 6))

            src = ttk.LabelFrame(tab, text="Sorgente da verificare", padding=6)
            src.pack(fill="x", pady=(0, 6))
            self.vsrc = tk.StringVar(value="file")
            r1 = ttk.Frame(src)
            r1.pack(fill="x")
            ttk.Radiobutton(r1, text="File WAV (qui sopra)", variable=self.vsrc, value="file").pack(side="left")
            ttk.Radiobutton(r1, text="Registrazione dall'ingresso audio del PC", variable=self.vsrc,
                            value="rec").pack(side="left", padx=14)
            r2 = ttk.Frame(src)
            r2.pack(fill="x", pady=4)
            ttk.Label(r2, text="Ingresso:").pack(side="left")
            self.in_dev_var = tk.StringVar(value="Predefinito")
            self.in_dev_cb = ttk.Combobox(r2, textvariable=self.in_dev_var, state="readonly", width=34)
            self.in_dev_cb.pack(side="left", padx=4)
            ttk.Button(r2, text="↻", width=3, command=self.refresh_in_devices).pack(side="left")
            ttk.Label(r2, text="  Hz:").pack(side="left")
            self.rec_sr_var = tk.StringVar(value="96000")
            ttk.Combobox(r2, textvariable=self.rec_sr_var, values=["44100", "48000", "96000", "192000"],
                         width=8).pack(side="left")
            ttk.Label(r2, text="  Canali:").pack(side="left")
            self.rec_ch_var = tk.StringVar(value="1")
            ttk.Combobox(r2, textvariable=self.rec_ch_var, values=["1", "2"], state="readonly",
                         width=3).pack(side="left")
            ttk.Label(r2, text="  Max (min):").pack(side="left")
            self.rec_max_var = tk.StringVar(value="15")
            ttk.Spinbox(r2, textvariable=self.rec_max_var, from_=1, to=60, increment=1, width=4).pack(side="left")
            r3 = ttk.Frame(src)
            r3.pack(fill="x")
            ttk.Button(r3, text="●  Registra", command=self.rec_start).pack(side="left")
            ttk.Button(r3, text="■  Stop", command=self.rec_stop).pack(side="left", padx=4)
            ttk.Button(r3, text="■  Stop e verifica", command=lambda: self.rec_stop(True)).pack(side="left")
            ttk.Button(r3, text="Salva registrazione…", command=self.rec_save).pack(side="left", padx=4)
            self.rec_lbl = tk.StringVar(value="Nessuna registrazione")
            ttk.Label(r3, textvariable=self.rec_lbl).pack(side="left", padx=8)
            r4 = ttk.Frame(src)
            r4.pack(fill="x", pady=(4, 0))
            ttk.Label(r4, text="Livello:").pack(side="left")
            self.rec_bar = ttk.Progressbar(r4, maximum=100, length=260)
            self.rec_bar.pack(side="left", padx=6)
            self.rec_hint = tk.StringVar(value="Avvia la registrazione, poi fai partire la cassetta dall'inizio.")
            ttk.Label(r4, textvariable=self.rec_hint, foreground="#555").pack(side="left")
            self.refresh_in_devices()

            panels = ttk.Frame(tab)
            panels.pack(fill="x")
            self.dec2 = ParamPanel(panels, "Decodifica del WAV", "decode2", DEC_FIELDS, DEC_PRESETS, self.store)
            self.dec2.pack(side="left", fill="y")
            self.ver = ParamPanel(panels, "Confronto", "verify", VER_FIELDS, VER_PRESETS, self.store)
            self.ver.pack(side="left", fill="y", padx=(8, 0))
            btns = ttk.Frame(tab)
            btns.pack(fill="x", pady=6)
            ttk.Button(btns, text="✔  Verifica coerenza", command=self.do_verify).pack(side="left")
            ttk.Button(btns, text="Salva rapporto…", command=self.save_report).pack(side="left", padx=6)
            self.canvas = tk.Canvas(tab, height=120, bg="white", highlightthickness=1, highlightbackground="#bbb")
            self.canvas.pack(fill="x")
            self.log3 = ScrolledText(tab, height=12, state="disabled", font=("Courier", 9))
            self.log3.pack(fill="both", expand=True, pady=(6, 0))

        # --- registrazione dall'ingresso audio ---------------------------
        def refresh_in_devices(self):
            items = ["Predefinito"]
            try:
                import sounddevice as sd
                for i, d in enumerate(sd.query_devices()):
                    if d["max_input_channels"] > 0:
                        items.append(f"{i}: {d['name']}")
            except Exception:
                pass
            self.in_dev_cb["values"] = items

        def _audio_missing(self):
            messagebox.showerror("Audio non disponibile",
                                 "Serve il modulo sounddevice con la libreria PortAudio.\n\n"
                                 "Installa:  pip install sounddevice\n"
                                 "Linux:     sudo apt install libportaudio2")

        def rec_start(self):
            if self.recording:
                return
            try:
                import sounddevice as sd
            except (ImportError, OSError):
                return self._audio_missing()
            try:
                sr = int(float(self.rec_sr_var.get()))
                nch = int(self.rec_ch_var.get())
                limit = float(self.rec_max_var.get().replace(",", ".")) * 60
            except ValueError:
                return messagebox.showerror("Errore", "Parametri di registrazione non validi.")
            v = self.in_dev_var.get()
            dev = int(v.split(":")[0]) if ":" in v else None
            self.rec_chunks = []
            self.rec_peak_now = self.rec_peak_max = 0.0

            def cb(indata, frames, t, status):
                self.rec_chunks.append(indata.copy())
                pk = float(np.max(np.abs(indata))) if frames else 0.0
                self.rec_peak_now = max(self.rec_peak_now, pk)
                self.rec_peak_max = max(self.rec_peak_max, pk)

            try:
                self.rec_stream = sd.InputStream(samplerate=sr, channels=nch, device=dev,
                                                 dtype="float32", callback=cb)
                self.rec_stream.start()
            except Exception as e:
                self.rec_stream = None
                return messagebox.showerror("Errore audio", f"{e}\n\nProva un'altra frequenza di "
                                                           f"campionamento (es. 48000) o un altro ingresso.")
            self.recording = True
            self.rec_sr, self.rec_limit, self.rec_t0 = sr, limit, time.time()
            self.rec_tick()

        def rec_tick(self):
            if not self.recording:
                return
            el = time.time() - self.rec_t0
            pk, self.rec_peak_now = self.rec_peak_now, 0.0
            self.rec_bar["value"] = min(100.0, pk * 100.0)
            self.rec_lbl.set(f"● REC {fmt_time(el)}   picco {pk * 100:3.0f}%")
            if self.rec_peak_max >= 0.98:
                self.rec_hint.set("⚠ Saturazione: abbassa il livello di ingresso del PC o l'uscita del registratore.")
            elif el > 3 and self.rec_peak_max < 0.15:
                self.rec_hint.set("Livello basso: alza l'ingresso (picchi ideali 50-90%). Se è zero, il segnale non arriva.")
            else:
                self.rec_hint.set("Livello OK.")
            if el >= self.rec_limit:
                return self.rec_stop()
            self.root.after(100, self.rec_tick)

        def rec_stop(self, verify=False):
            if not self.recording:
                if verify:
                    self.do_verify()
                return
            self.recording = False
            try:
                self.rec_stream.stop()
                self.rec_stream.close()
            except Exception:
                pass
            self.rec_stream = None
            self.rec_bar["value"] = 0
            if not self.rec_chunks:
                self.rec_lbl.set("Nessun dato registrato")
                return
            self.rec_data = np.concatenate(self.rec_chunks, axis=0)
            self.rec_chunks = []
            self.vsrc.set("rec")
            self.rec_lbl.set(f"Registrati {fmt_time(len(self.rec_data) / self.rec_sr)} "
                             f"@ {self.rec_sr} Hz, {self.rec_data.shape[1]} can.")
            if verify:
                self.do_verify()

        def rec_save(self):
            if self.rec_data is None or self.recording:
                return messagebox.showinfo("Registrazione", "Non c'è nessuna registrazione conclusa da salvare.")
            p = filedialog.asksaveasfilename(defaultextension=".wav", initialfile="registrazione.wav",
                                             filetypes=[("File WAV", "*.wav")])
            if not p:
                return
            data, sr = self.rec_data, self.rec_sr

            def work():
                write_wav(p, to_pcm(data, 1.0, 16), sr)
                return os.path.getsize(p)
            self.run_bg(work, lambda n: self.log(self.log3, f"Registrazione salvata: {p} ({n / 1e6:.1f} MB)"),
                        "Salvataggio WAV…")

        def do_verify(self):
            try:
                tap = self.load_tap(self.vtap_var)
                dp, vp = self.dec2.get(), self.ver.get()
            except ValueError as e:
                return messagebox.showerror("Errore", str(e))
            use_rec = self.vsrc.get() == "rec"
            wav = self.vwav_var.get().strip()
            if use_rec:
                if self.recording:
                    return messagebox.showinfo("Registrazione in corso", "Ferma prima la registrazione.")
                if self.rec_data is None:
                    return messagebox.showerror("Errore", "Nessuna registrazione: premi «Registra» "
                                                          "e fai partire la cassetta.")
                rec_x, rec_sr = self.rec_data, self.rec_sr
                wav_name = "registrazione dall'ingresso audio"
            else:
                if not os.path.isfile(wav):
                    return messagebox.showerror("Errore", "Seleziona un file WAV valido.")
                wav_name = os.path.basename(wav)
            tap_name = os.path.basename(self.vtap_var.get())

            def work():
                if use_rec:
                    x, sr = rec_x, rec_sr
                else:
                    x, sr, _ = read_wav(wav)
                ch = select_channel(x, dp["channel"])
                clock, cname = clock_hz(dp["clock"], tap["clock"])
                tol = vp["tol"] / 100.0
                kw = dict(tol=tol, resync=int(vp["resync"]), adaptive=vp["adaptive"])
                if tap["version"] == 2:
                    edges = ["entrambi"]
                elif vp["autopol"]:
                    edges = ["discesa", "salita"]
                else:
                    edges = ["salita" if dp["edge"].startswith("salita") else "discesa"]
                res, used, info, last_err = None, None, None, None
                decs = {}
                for k, e in enumerate(edges):
                    dec, info = decode_audio(ch, sr, clock, e, dp["hyst"] / 100.0, dp["dc"], dp["invert"])
                    decs[e] = dec
                    try:
                        r = compare_pulses(tap["cycles"], dec, clock,
                                           max_bad=400 if k < len(edges) - 1 else 5000, **kw)
                    except ValueError as ex:
                        last_err = ex
                        continue
                    if res is None or r["errors"] < res["errors"]:
                        res, used = r, e
                    if not r["aborted"]:
                        break
                if res is None:
                    raise last_err
                if res["aborted"] and len(edges) > 1:      # il tentativo rapido era limitato: rifai completo
                    res = compare_pulses(tap["cycles"], decs[used], clock, **kw)
                note = ""
                if len(edges) > 1:
                    note = (f"Polarità/fronte scelto automaticamente: {used}\n")
                return res, note + format_report(res, tap_name, wav_name, info,
                                                 tol, vp["events"]), tol

            def done(r):
                res, text, tol = r
                self.report_text = text
                self.log(self.log3, text, clear=True)
                self.draw_plot(res, tol)
            self.run_bg(work, done, "Verifica in corso…")

        def save_report(self):
            if not self.report_text:
                return messagebox.showinfo("Rapporto", "Esegui prima una verifica.")
            p = filedialog.asksaveasfilename(defaultextension=".txt", initialfile="rapporto_verifica.txt",
                                             filetypes=[("Testo", "*.txt")])
            if p:
                with open(p, "w", encoding="utf-8") as f:
                    f.write(self.report_text + "\n")

        def draw_plot(self, res, tol):
            c = self.canvas
            c.delete("all")
            c.update_idletasks()
            W, H = max(c.winfo_width(), 300), 120
            dev = np.nan_to_num(np.abs(res["dev"]), nan=0.0)
            err = res["err"]
            n = len(dev)
            nb = min(W - 40, n)
            edges = np.linspace(0, n, nb + 1).astype(int)[:-1]
            mx = np.maximum.reduceat(dev, edges)
            ee = np.add.reduceat(err.astype(np.int32), edges) > 0
            ymax = max(tol * 2, 0.05)
            x0, base = 30, H - 14
            for k in range(nb):
                h = min(mx[k] / ymax, 1.0) * (base - 8)
                col = "#d33" if ee[k] else "#4a7fd0"
                x = x0 + k * (W - 40) / nb
                c.create_line(x, base, x, base - max(h, 1 if ee[k] else 0), fill=col)
                if ee[k]:
                    c.create_line(x, 4, x, 12, fill="#d33")
            yt = base - (tol / ymax) * (base - 8)
            c.create_line(x0, yt, W - 10, yt, fill="#e90", dash=(4, 3))
            c.create_text(x0 - 3, yt, text=f"±{tol * 100:.0f}%", anchor="e", font=("TkDefaultFont", 7), fill="#e90")
            c.create_text(x0, H - 3, text=f"scarto di durata per impulso (0 … {ymax * 100:.0f}%) lungo il nastro - "
                                          f"rosso = errori", anchor="w", font=("TkDefaultFont", 7), fill="#555")


def main():
    if "--selftest" in sys.argv:
        sys.exit(selftest())
    if tk is None:
        sys.exit("tkinter non disponibile. Installalo (Linux: sudo apt install python3-tk) "
                 "oppure usa --selftest per provare le funzioni di base.")
    root = tk.Tk()
    try:
        ttk.Style().theme_use("clam" if sys.platform.startswith("linux") else ttk.Style().theme_use())
    except Exception:
        pass
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
