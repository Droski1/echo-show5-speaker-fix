#!/usr/bin/env python3
"""Show 5 voice client server — self-contained Hermes terminal for the Echo Show 5.

HTTPS :8793 (CA-signed) — browser fallback:
  GET  /show5              -> kiosk page (orange bar + tap-to-talk)
  POST /show5/query        -> WAV -> STT -> agent -> TTS -> {text, audio_b64}

PLAIN :8794 (native app, cleartext):
  WS   /ws                 -> always-on mic channel:
     app streams 16k PCM16 binary frames
     server runs VAD + fuzzy "hermes" wake + STT + agent + piper TTS
     pushes {"t":"state","s":"listen|think|speak|done"} + {"t":"audio","b64":...}
     bar appears ONLY on wake (Alexa-style: blue->amber->orange->gone)

Reuses relay.py (ask_hermes, shared SESSION, Alexa-style system message).
hs2 does ears + brain + mouth. No pop-os / JLab / Show 8 / C2 required.
"""
import asyncio, base64, concurrent.futures, difflib, glob, hashlib, json, os, ssl, struct, subprocess, sys, tempfile, time, urllib.parse
import numpy as np

sys.path.insert(0, os.path.expanduser("~/hermes-relay"))
import relay

HOME = os.path.expanduser("~")
PORT_HTTPS = int(os.environ.get("SHOW5_PORT", "8793"))
PORT_PLAIN = 8794
HTML_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "show5.html")
PIPER_MODEL = f"{HOME}/hermes-relay/piper/en_US-lessac-medium.onnx"
PIPER_CANDIDATES = [f"{HOME}/hermes-relay/piper/piper/piper", f"{HOME}/hermes-relay/piper/piper"]
WHISPER_MODEL = "small.en"
WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
_STT_EXEC = concurrent.futures.ThreadPoolExecutor(max_workers=2)   # dedicated STT workers

ADMIN = set()   # admin-panel ws clients (live mic monitor + pipeline feed)
LOG_FILE = os.path.expanduser("~/show5/show5.log")
ADMIN_FILE = os.path.expanduser("~/show5/admin.html")
# --- tunables (the admin sliders) ---
_WAKE_TH = 0.45        # the ONNX wake-probability gate (higher = stricter wake)
_TALK_GATE = 2300      # the "still talking" RMS (the utterance-end)
_SIL_LO = 1.0          # the silence window without a wake (seconds)
_SIL_WAKE = 2.5        # the silence window after a wake (seconds)
_DUCK_RATIO = 4        # the music duck ratio (media vol / this) - pushed to the app
_MIC_GAIN = 32         # the app's mic gain (x) - pushed to the app
_VAD_ON = 2500         # the speech-start RMS (the wake detector's sensitivity)
_RUNAWAY = 320000      # the utterance length cap in bytes (10s @ 32k B/s)
_PREBUF = 16000        # the wake's pre-roll (the audio before the wake, in samples)
STATS = {"wakes": 0, "utterances": 0, "last_heard": "", "last_answer": "",
         "last_latency": 0.0, "started": time.time(), "utter_start": 0.0,
         "last_mic": 0.0, "heals": 0, "bt_streaming": False, "last_bt": 0.0}
AGENT = {"running": False, "thoughts": [], "tools": [], "stop_event": None}
_device_cache = {"t": 0.0, "data": {}}
_PENDING_TTS_RESET = None

def _admin_send(obj):
    # push a JSON object to every admin panel viewer (best-effort)
    try:
        payload = ws_frame(1, json.dumps(obj).encode())
        for w in list(ADMIN):
            try:
                w.write(payload)
            except Exception:
                ADMIN.discard(w)
    except Exception:
        pass


def log(m):
    line = time.strftime("%H:%M:%S") + " " + m
    print(line, flush=True)
    if ADMIN:
        try:
            payload = ws_frame(1, json.dumps({"t": "log", "m": line}).encode())
            for w in list(ADMIN):
                try:
                    w.write(payload)
                except Exception:
                    ADMIN.discard(w)
        except Exception:
            pass   # ws_frame not defined yet at module init

# optional server-side DSP (high-pass + spectral NS + AGC)
try:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from show5_dsp import VoiceDSP
    _DSP = VoiceDSP(rate=16000)
    log("DSP engine: high-pass + spectral NS + AGC armed")
except Exception as e:
    _DSP = None
    log(f"DSP engine unavailable ({e}) — raw path")

# wake-word engine: vosk keyword spotter (Alexa-style: ONLY "hermes" wakes)
_KW = None
_KW_ARMED = False
_WOKE = False      # set when the wake ENGINE confirms 'hermes' (skip STT re-verification)
_LAST_WAKE = None  # (engine_woke, is_wake) of the latest utterance — for adaptive labeling
TRAIN_DIR = os.path.expanduser("~/show5/hermes_train")
_OW = None        # openWakeWord engine (primary wake word: custom 'hermes' model)
_ow_buf = np.zeros(0, dtype=np.int16)

try:
    from openwakeword.model import Model as OWModel
    _ow_model_path = os.path.expanduser("~/show5/hermes_v0.1.onnx")
    if os.path.exists(_ow_model_path):
        _OW = OWModel(wakeword_model_paths=[_ow_model_path],
                      class_mapping_dicts=[{"0": "not_hermes", "1": "hermes"}],
                      vad_threshold=0.0)      # our RMS gate + STT verification do the gating
        log("wake-word engine: openWakeWord (custom hermes model)")
    else:
        log("openWakeWord model missing — vosk fallback")
except Exception as e:
    log(f"openWakeWord unavailable ({e}) — vosk fallback")

if _OW is None:
    try:
        from vosk import Model as _VoskModel, KaldiRecognizer
        _KW_MODEL = _VoskModel(os.path.expanduser("~/vosk-model-small-en-us-0.15"))
        _KW = KaldiRecognizer(_KW_MODEL, 16000, '["hermes [unk]", "[unk]"]')
        log("wake-word engine: vosk (listening for 'hermes')")
    except Exception as e:
        log(f"vosk unavailable ({e}) — no wake word")

def _ow_predict(model, frame):
    """Run openWakeWord in a worker thread (its embedding model stalls the loop otherwise)."""
    try:
        p = model.predict(frame)
        return float(p.get("hermes", p.get("1", 0.0)))   # class_mapping keys OR raw labels
    except Exception:
        return 0.0

async def run_utterance(data, writer, wake_audio=None):
    """STT -> agent -> TTS in the BACKGROUND so the voice channel keeps reading the
    mic and the admin panel keeps streaming during the whole chain (~10-25s)."""
    global _LAST_WAKE
    loop = asyncio.get_event_loop()
    try:
        events, audio = await asyncio.wait_for(
            loop.run_in_executor(_STT_EXEC, handle_utterance, data), 90)
    except asyncio.TimeoutError:
        log("STT TIMEOUT (host memory-starved?) — skipping")
        return
    except Exception as e:
        log(f"utterance error: {e}")
        return
    # adaptive training: confirmed wakes -> positives; engine false-triggers -> negatives
    if wake_audio and _LAST_WAKE:
        try:
            ew, iw = _LAST_WAKE
            _LAST_WAKE = None
            if len(wake_audio) > 12000:      # >0.75s (the hermes + a chunk)
                if ew and iw:
                    sub = "auto_pos"
                elif ew and not iw:
                    sub = "auto_neg"
                else:
                    sub = None
                if sub:
                    d = os.path.join(TRAIN_DIR, sub)
                    os.makedirs(d, exist_ok=True)
                    p = os.path.join(d, f"sample_{int(time.time() * 1000)}.wav")
                    with open(p, "wb") as f:
                        f.write(make_wav(wake_audio))
                    log(f"adaptive sample -> {sub} ({len(wake_audio) // 32000}.{len(wake_audio) % 32000 // 3200}s)")
        except Exception as e:
            log(f"adaptive save err: {e}")
    for kind, val in events:
        try:
            if kind == "state":
                writer.write(ws_frame(1, json.dumps({"t": "state", "s": val}).encode()))
            elif kind == "log":
                writer.write(ws_frame(1, json.dumps({"t": "log", "m": val}).encode()))
            elif kind == "think":
                writer.write(ws_frame(1, json.dumps({"t": "think", "m": val}).encode()))
            elif kind == "tool":
                writer.write(ws_frame(1, json.dumps({"t": "tool", "m": val}).encode()))
            elif kind == "audio":
                # the HAL reverts the speaker amp to Off on every playback — force it
                # ON via adb right before the audio goes out so she's actually heard
                for _t in (_TCP, _SERIAL):
                    subprocess.Popen([_ADB, "-s", _t, "shell",
                                      "tinymix Speaker_Amp_Switch 1 2>/dev/null"],
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                writer.write(ws_frame(1, json.dumps({"t": "audio", "b64": val}).encode()))
                _schedule_post_tts_reset(8)   # fallback: reset HAL if no playend arrives
                # the TTS will play (~5-8s), then the app self-restarts and the HAL holds
                # the mic — schedule the full audio reset (mediaserver) right after,
                # UNLESS BT is streaming (the reset would kill the music too)
                def _post_tts_reset():
                    if STATS.get("bt_streaming"):
                        log("post-TTS reset SKIPPED (BT streaming — music protected)")
                        return
                    heal_app("post-TTS audio reset", force=True, media_only=True)
                global _PENDING_TTS_RESET
                _PENDING_TTS_RESET = asyncio.get_event_loop().call_later(8, _post_tts_reset)
            elif kind == "err":
                writer.write(ws_frame(1, json.dumps({"t": "err", "m": val}).encode()))
            await writer.drain()
        except Exception:
            break

# ---------------- device healing (ALWAYS-UP) ----------------
_ADB = os.path.expanduser("~/platform-tools/adb")
_SERIAL = "G6G1MK082254031J"              # USB (when plugged)
_TCP = "100.81.55.108:5555"               # Tailscale adb-over-TCP (unplugged!)
_last_heal = 0.0
_BOOT = time.time()

def heal_app(reason, force=False, media_only=False):
    """Restart the Show 5 app via adb when the mic/voice channel dies.
    Tries BOTH the tailscale TCP + the USB serial. 5-minute cooldown (unless force).
    media_only=True: just reset the audio HAL (mediaserver) — the app survives."""
    global _last_heal
    if not force and time.time() - _last_heal < 300:
        return
    _last_heal = time.time()
    STATS["heals"] += 1
    log(f"HEAL: {reason} — {'media reset' if media_only else 'app restart'} via adb")
    for target in (_TCP, _SERIAL):
        try:
            # killall mediaserver audioserver: the ALSA/audio HAL (hosted in the
            # audioserver on A11+) holds the capture across app deaths — the FULL reset
            cmd = ("killall mediaserver audioserver 2>/dev/null" if media_only else
                   "killall mediaserver audioserver 2>/dev/null; am force-stop com.hermes.show5; "
                   "am start -n com.hermes.show5/.MainActivity")
            subprocess.Popen([_ADB, "-s", target, "shell", cmd],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception as e:
            log(f"HEAL {target} failed: {e}")

async def retrain_and_reload():
    """Rebuild the hermes model from ALL accumulated samples (incl. adaptive) + reload it."""
    global _OW
    log("auto-retrain: rebuilding hermes model from all samples...")
    try:
        script = os.path.expanduser("~/show5/train_hermes.py")
        r = await asyncio.get_event_loop().run_in_executor(
            _STT_EXEC, lambda: subprocess.run(
                ["python3", script], capture_output=True, text=True, timeout=1800))
        if r.returncode == 0 and os.path.exists(os.path.expanduser("~/show5/hermes_v0.1.onnx")):
            _OW = OWModel(wakeword_model_paths=[os.path.expanduser("~/show5/hermes_v0.1.onnx")],
                          class_mapping_dicts=[{"0": "not_hermes", "1": "hermes"}],
                          vad_threshold=0.0)
            log("auto-retrain: model rebuilt + reloaded")
        else:
            log(f"auto-retrain FAILED rc={r.returncode}")
    except Exception as e:
        log(f"auto-retrain err: {e}")

async def watchdog():
    """Periodic liveness check: dead mic or missing voice channel -> heal the app.
    Also: auto-retrain when 10+ new adaptive samples accumulate.
    Also: push the BT sink connection state to the app (it gates the mic on it)."""
    base_pos = len(glob.glob(os.path.join(TRAIN_DIR, "auto_pos", "*.wav")))
    base_neg = len(glob.glob(os.path.join(TRAIN_DIR, "auto_neg", "*.wav")))
    trained_at = 0.0
    while True:
        await asyncio.sleep(30)
        try:
            mic_age = time.time() - STATS.get("last_mic", 0)
            if mic_age > 45 and not STATS.get("bt_streaming"):
                heal_app(f"mic silent {int(mic_age)}s")
            elif time.time() - _BOOT > 120 and not CHANNELS:
                heal_app("no voice channel")
            # push BT sink connection state to the app (mic gate key)
            try:
                r = subprocess.run(
                    [_ADB, "-s", _TCP, "shell",
                     "dumpsys bluetooth_manager | grep -c 'A2DPSinkStateMachine state=Connected'"],
                    capture_output=True, text=True, timeout=8)
                bt_on = (r.stdout or "").strip() not in ("", "0")
                msg = json.dumps({"t": "bt", "on": bool(bt_on)}).encode()
                for w in list(CHANNELS):
                    try:
                        w.write(ws_frame(1, msg))
                    except Exception:
                        pass
                if bt_on != STATS.get("bt_streaming"):
                    STATS["bt_streaming"] = bool(bt_on)
                    STATS["last_bt"] = time.time()
                    log(f"BT sink {'connected' if bt_on else 'disconnected'} (pushed to app)")
            except Exception as e:
                log(f"bt-state push err: {e}")
            # adaptive retrain: 10+ new auto samples since the last train -> rebuild
            np_ = len(glob.glob(os.path.join(TRAIN_DIR, "auto_pos", "*.wav")))
            nn = len(glob.glob(os.path.join(TRAIN_DIR, "auto_neg", "*.wav")))
            if time.time() - trained_at > 600 and (np_ - base_pos) + (nn - base_neg) > 10:
                trained_at = time.time()
                base_pos, base_neg = np_, nn
                asyncio.get_event_loop().create_task(retrain_and_reload())
        except Exception as e:
            log(f"watchdog err: {e}")

# ---------------- piper ----------------
def find_piper():
    for p in PIPER_CANDIDATES:
        if os.path.isfile(p) and os.access(p, os.X_OK):
            return p
    import shutil
    return shutil.which("piper")

def piper_say(text):
    piper = find_piper()
    if not piper:
        raise RuntimeError("piper binary not found")
    out = f"/tmp/show5_{int(time.time()*1000)}.wav"
    subprocess.run([piper, "--model", PIPER_MODEL, "--output_file", out],
                   input=text.encode(), capture_output=True, timeout=60, check=True)
    data = open(out, "rb").read()
    try: os.unlink(out)
    except OSError: pass
    return base64.b64encode(data).decode()

# ---------------- whisper ----------------
_model = None
def stt(wav_path):
    global _model
    if _model is None:
        from faster_whisper import WhisperModel
        log("loading whisper small.en (cpu int8)...")
        _model = WhisperModel(WHISPER_MODEL, device="cpu", compute_type="int8")
        log("whisper ready")
    segments, _ = _model.transcribe(wav_path, beam_size=5)
    return " ".join(s.text.strip() for s in segments).strip()

# ---------------- wake word (adapted from voice.py) ----------------
_WAKE_LIT = {"hermes", "hermies", "ermes", "ernie", "herr mies", "hernias",
             "hermus", "her mess", "hermes", "ermies", "hermes.", "hermes,"}
_FRAG = {"mies", "mes", "miz", "miss", "mrs", "us", "erm", "erms"}
_FUZZY_BLOCK = {"where","what","when","here","there","them","their","this","these",
                "that","how","who","why","did","do","does","is","are","you","your",
                "i","we","they","he","she","it","a","the","an","and","but","so"}
_GREET = {"hey","hi","yo","okay","ok","oh","hello","alexa","echo","computer"}

def parse_wake(words):
    """(is_wake, rest, matched). Literal match anywhere at start; fuzzy only at
    index 0-1 (index 1 needs a greeting first); fragments at start."""
    if not words:
        return (False, "", "")
    joined = " ".join(words)
    for w in _WAKE_LIT:
        if joined == w or joined.startswith(w + " "):
            rest = joined[len(w):].strip().lstrip(" ,.?!")
            return (True, rest, w)
    for i, w in enumerate(words[:2]):
        if w in _FUZZY_BLOCK:
            continue
        if i == 1 and words[0] not in _GREET:
            continue
        if difflib.SequenceMatcher(None, w, "hermes").ratio() >= 0.5:
            rest = " ".join(words[i+1:]).lstrip(" ,.?!")
            return (True, rest, w)
    if words and words[0] in _FRAG:
        rest = " ".join(words[1:]).lstrip(" ,.?!")
        if rest:
            return (True, rest, words[0])
    return (False, "", "")

# ---------------- agent ----------------
def do_agent(text):
    now = time.time()
    if now - relay.SESSION["last"] > relay.SESSION_TIMEOUT:
        relay.SESSION["messages"] = []
        log("new conversation window")
    relay.SESSION["last"] = now
    hist = relay.SESSION["messages"] + [{"role": "user", "content": text}]
    answer = relay.ask_hermes(hist)
    relay.SESSION["messages"] = hist + [{"role": "assistant", "content": answer}]
    if len(relay.SESSION["messages"]) > 20:
        relay.SESSION["messages"] = relay.SESSION["messages"][-20:]
    return answer

def handle_utterance(pcm):
    """Full wake->answer chain for one utterance. Returns (events, audio_b64)."""
    _t0 = time.time()
    fd, path = tempfile.mkstemp(suffix=".wav")
    os.close(fd)
    with open(path, "wb") as f:
        f.write(make_wav(pcm))
    # 2026-09-01: the OpenWakeWord GATE — if the engine didn't already confirm the
    # wake (the streaming path), spot the utterance's audio with the ONNX model and
    # SKIP the STT entirely when there's no "hermes" (video/TV speech gets filtered
    # instead of transcribed word-for-word).
    # ALWAYS gate with the ONNX (the _WOKE sticky-global gets set by false positives
    # in the video's audio — it must NOT bypass the gate!)
    if _OW is not None and len(pcm) > 8000:
        try:
            import numpy as np
            import io as _io
            # the first ~1.5s (the wake sits at the start); mono 16k float32
            head = pcm[:24000]
            frame = np.frombuffer(head, dtype=np.int16).astype(np.float32) / 32768.0
            prob = _STT_EXEC.submit(_ow_predict, _OW, frame).result(timeout=5)
        except Exception as e:
            prob = 0.5
        STATS["last_wake_prob"] = prob
        if prob < _WAKE_TH:
            log(f"skip: no wake (prob {prob:.2f}) - external audio filtered")
            try: os.unlink(path)
            except OSError: pass
            return [], None
    try:
        text = stt(path)
    except Exception as e:
        return [("err", str(e))], None
    finally:
        try: os.unlink(path)
        except OSError: pass
    if not text:
        return [], None
    _t_stt = time.time()
    STATS["utterances"] += 1
    STATS["utter_start"] = time.time()
    STATS["last_heard"] = text
    words = [w.strip(".,!?") for w in text.lower().split()]
    is_wake, rest, matched = parse_wake(words)
    log(f"heard: {text!r} wake={is_wake} ({matched})")
    global _WOKE, _LAST_WAKE
    engine_woke = _WOKE
    _WOKE = False
    _LAST_WAKE = (engine_woke, is_wake)
    if not is_wake and not _KW_ARMED:
        # 2026-09-01 WAKE-ONLY: the engine's _WOKE flag alone is NOT sufficient —
        # the ONNX false-positives on the video's audio (e.g. "hermes"-like sounds)
        # used to answer the video's speech. The STT-verified "hermes" in the text
        # (is_wake) is the ONLY gate to the answer path now.
        return [], None                     # only answer when the text-verified wake was heard
    query = rest if rest else (text if engine_woke else ("hello" if not _KW_ARMED else text))
    events = [("state", "listen"), ("log", "you: " + (query if query else text))]
    think_parts, tool_list = [], []
    now = time.time()
    if now - relay.SESSION["last"] > relay.SESSION_TIMEOUT:
        relay.SESSION["messages"] = []
        log("new conversation window")
    relay.SESSION["last"] = now
    hist = relay.SESSION["messages"] + [{"role": "user", "content": query or text}]
    try:
        import threading
        AGENT["stop_event"] = threading.Event()
        AGENT["thoughts"] = []
        AGENT["tools"] = []
        AGENT["running"] = True

        def _push_think(parts):
            text = "".join(parts)
            AGENT["thoughts"] = parts
            _admin_send({"t": "think", "m": text})

        def _push_tool(ev):
            AGENT["tools"].append(ev)
            _admin_send({"t": "tool", "ev": ev})

        answer = relay.ask_hermes(hist,
                                  on_think=_push_think,
                                  on_tool=_push_tool,
                                  stop_event=AGENT["stop_event"])
        think_parts[:] = AGENT["thoughts"]
        relay.SESSION["messages"] = hist + [{"role": "assistant", "content": answer}]
        if len(relay.SESSION["messages"]) > 20:
            relay.SESSION["messages"] = relay.SESSION["messages"][-20:]
        AGENT["running"] = False
        _admin_send({"t": "answer", "m": answer})
    except Exception as e:
        if AGENT["stop_event"] is not None and AGENT["stop_event"].is_set():
            answer = "(stopped)"
            log("agent run STOPPED by user")
            events.append(("log", "you stopped the agent"))
            _admin_send({"t": "stopped"})
        else:
            events.append(("err", f"agent: {e}"))
        return events, None
    if think_parts:
        events.append(("think", "\n".join(think_parts)))
    for te in tool_list:
        events.append(("tool", te))
    _t_agent = time.time()
    log(f"hermes: {answer}")
    STATS["last_answer"] = answer
    STATS["last_latency"] = round(time.time() - STATS["utter_start"], 2)
    events.append(("state", "think"))
    events.append(("log", "hermes: " + answer))
    try:
        audio = piper_say(answer)
    except Exception as e:
        events.append(("err", f"tts: {e}"))
        return events, None
    _t_tts = time.time()
    log(f"times: stt {_t_stt - _t0:.1f}s agent {_t_agent - _t_stt:.1f}s tts {_t_tts - _t_agent:.1f}s "
        f"total {_t_tts - _t0:.1f}s (capture {STATS['last_latency']}s end-to-end)")
    events.append(("state", "speak"))
    events.append(("audio", audio))
    events.append(("state", "done"))
    events.append(("log", "done"))
    return events, audio

def make_wav(pcm):
    hdr = bytearray(44)
    hdr[0:4] = b"RIFF"
    struct.pack_into("<I", hdr, 4, 36 + len(pcm))
    hdr[8:12] = b"WAVE"
    hdr[12:16] = b"fmt "
    struct.pack_into("<IHHIIHH", hdr, 16, 16, 1, 1, 16000, 32000, 2, 16)
    hdr[36:40] = b"data"
    struct.pack_into("<I", hdr, 40, len(pcm))
    return bytes(hdr) + pcm

# ================= websocket (server side, adapted from c2.py) =================
async def ws_read_frame(reader):
    h = await reader.readexactly(2)
    opcode = h[0] & 0x0F
    masked = h[1] & 0x80
    ln = h[1] & 0x7F
    if ln == 126:
        ln = int.from_bytes(await reader.readexactly(2), "big")
    elif ln == 127:
        ln = int.from_bytes(await reader.readexactly(8), "big")
    mask = await reader.readexactly(4) if masked else b""
    payload = await reader.readexactly(ln)
    if masked:
        payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    return opcode, payload

def ws_frame(opcode, payload):
    hdr = bytes([0x80 | opcode])
    ln = len(payload)
    if ln < 126:
        hdr += bytes([ln])
    elif ln < 65536:
        hdr += bytes([126]) + ln.to_bytes(2, "big")
    else:
        hdr += bytes([127]) + ln.to_bytes(8, "big")
    return hdr + payload

def ws_accept(key):
    import hashlib
    accept = base64.b64encode(hashlib.sha1((key + WS_GUID).encode()).digest()).decode()
    return (b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n"
            b"Connection: Upgrade\r\nSec-WebSocket-Accept: " + accept.encode() + b"\r\n\r\n")

# ---------------- voice channel ----------------
CHANNELS = set()   # active ws writers for test/play broadcast
RECORD = None      # {buf: bytearray, until: float} — server-side mic capture for testing

async def voice_channel(reader, writer):
    """Continuously VAD the incoming PCM; on an utterance, run wake->agent->tts."""
    global _KW, _ow_buf
    pcm = bytearray()
    prebuf = bytearray()       # last ~2s of audio BEFORE the wake (the "hermes" itself)
    wake_audio = None          # the hermes + first chunk, saved for adaptive training
    speech = False
    silence_since = 0.0
    last_wake = 0.0
    _kw_reset_at = 0.0
    vad_on = _VAD_ON
    peak = 0
    last_log = 0.0
    CHANNELS.add(writer)
    log("voice channel up")
    last_ping = 0.0
    try:
        while True:
            try:
                opcode, payload = await asyncio.wait_for(ws_read_frame(reader), 2.0)
            except asyncio.TimeoutError:
                opcode = None
            if time.time() - last_ping > 30:     # ping EVERY iteration, mic traffic or not
                writer.write(ws_frame(9, b""))     # keepalive ping (app watchdog relies on it)
                await writer.drain()
                last_ping = time.time()
            if opcode is None:
                continue
            if opcode == 8:
                break
            if opcode == 9:                      # ping -> pong
                writer.write(ws_frame(10, payload)); await writer.drain()
                continue
            if opcode == 10:                     # pong -> ignore
                continue
            if opcode == 1:                      # app control message (JSON)
                try:
                    ctl = json.loads(payload.decode("utf-8", "replace"))
                    if ctl.get("t") == "playend":
                        # the app (pre-stop build) recovers cleanly without a HAL
                        # kill — the reset was the ~25s recovery delay. Cancel the
                        # 8s fallback entirely for this app.
                        log("app playback ended - clean recovery (no HAL reset)")
                        global _PENDING_TTS_RESET
                        try:
                            if _PENDING_TTS_RESET is not None:
                                _PENDING_TTS_RESET.cancel()
                        except Exception:
                            pass
                        _PENDING_TTS_RESET = None
                    if ctl.get("t") == "bt":
                        STATS["bt_streaming"] = bool(ctl.get("on"))
                        STATS["last_bt"] = time.time()
                        log(f"BT streaming {'ON' if STATS['bt_streaming'] else 'OFF'} (mic gated)")
                except Exception:
                    pass
                continue
            if opcode != 2:
                continue
            STATS["last_mic"] = time.time()      # mic liveness for the admin console
            if _DSP is not None:                    # HP -> spectral NS (VAD path) ; +AGC (STT path)
                try:
                    vad_pcm, payload = _DSP.process(payload)
                    if RECORD is not None and time.time() < RECORD["until"]:
                        RECORD["buf"] += vad_pcm
                        RECORD["agc"] += payload
                except Exception:
                    vad_pcm = payload
            else:
                vad_pcm = payload
            prebuf += vad_pcm
            prebuf = prebuf[-32000:]             # keep the last 2s (the pre-wake "hermes")
            # live mic monitor: stream the VAD-path PCM + RMS to admin clients
            if ADMIN:
                try:
                    pcmf = ws_frame(2, vad_pcm[:6400])
                    for w in list(ADMIN):
                        try:
                            w.write(pcmf)
                        except Exception:
                            ADMIN.discard(w)
                except Exception:
                    pass
            # RMS over the VAD chunk (16k mono 16-bit)
            n = len(vad_pcm) // 2
            if n == 0:
                continue
            s = 0
            for i in range(0, len(vad_pcm) - 1, 2):
                v = vad_pcm[i] | (vad_pcm[i+1] << 8)
                if v >= 32768:
                    v -= 65536
                s += v * v
            rms = int((s / n) ** 0.5)
            now = time.time()
            if ADMIN:
                try:
                    rf = ws_frame(1, json.dumps({"t": "rms", "v": rms}).encode())
                    for w in list(ADMIN):
                        try:
                            w.write(rf)
                        except Exception:
                            ADMIN.discard(w)
                except Exception:
                    pass
            STATS["last_rms"] = rms   # live out-RMS for the admin (per-chunk!)
            if rms > peak:
                peak = rms
            if now - last_log > 2.0 and peak > 0:      # mic-level debug
                STATS["last_rms"] = peak
                log(f"mic peak {peak}")
                peak = 0
                last_log = now
            if not speech:
                wake = False
                if _OW is not None:
                    # openWakeWord custom 'hermes' detector (executor — keeps the loop free)
                    try:
                        if now - last_wake < 10.0:     # cooldown covers TTS playback (echo re-wake)
                            pass
                        else:
                            _ow_buf = np.concatenate(
                                [_ow_buf, np.frombuffer(vad_pcm, dtype=np.int16)])
                            if len(_ow_buf) >= 10240:   # predict per 8 frames (640ms), off the loop
                                frame = _ow_buf[:10240]
                                _ow_buf = _ow_buf[10240:]
                                score = await asyncio.get_event_loop().run_in_executor(
                                    _STT_EXEC, _ow_predict, _OW, frame)
                                if score > 0.75 and rms > 1800:   # tightened: confident hermes only
                                    wake = True
                    except Exception:
                        pass
                if _KW is not None and not wake:
                    # vosk backup: keyword gate (ONLY "hermes" wakes, Alexa-style)
                    try:
                        if now - last_wake < 10.0:     # cooldown covers TTS playback (echo re-wake)
                            pass
                        elif now - _kw_reset_at > 45.0:   # periodic re-arm (state pollution)
                            _KW = KaldiRecognizer(_KW_MODEL, 16000, '["hermes [unk]", "[unk]"]')
                            _kw_reset_at = now
                        elif _KW.AcceptWaveform(vad_pcm):   # HP-only (real levels) — best for vosk
                            pass
                        else:
                            p = json.loads(_KW.PartialResult())
                            if 'hermes' in p.get('partial', '').lower() and rms > 1500:
                                # vosk keyword + speech-energy confirmation (quiet room stays quiet)
                                wake = True
                    except Exception:
                        pass
                if wake:
                    global _WOKE
                    _WOKE = True             # engine confirmed 'hermes' — skip STT re-check
                    wake_audio = bytes(prebuf) + payload   # the hermes itself, for training
                    speech = True
                    silence_since = now
                    last_wake = now
                    STATS["wakes"] += 1
                    STATS["duck_active"] = True
                    log("WAKE WORD: hermes")
                    # Amazon-style: the WAKE ducks the music (not the speech-start — the
                    # video's speech must NOT trigger the duck or the STT)
                    try:
                        writer.write(ws_frame(1, json.dumps({"t": "duck", "on": True}).encode()))
                        await writer.drain()
                    except Exception as e:
                        log(f"duck push failed: {e}")
                    writer.write(ws_frame(1, json.dumps({"t": "state", "s": "listen"}).encode()))
                    writer.write(ws_frame(1, json.dumps({"t": "log", "m": "wake: hermes"}).encode()))
                    await writer.drain()
                    pcm += payload
                else:
                    if rms > vad_on:
                        # RMS speech-start -> the STT verifies the wake word downstream
                        speech = True
                        log(f"speech start (rms {rms})")
                        pcm += payload
                        silence_since = now
            else:
                pcm += payload                     # keep trailing silence for word tails
                if rms > _TALK_GATE:                 # still talking — the admin slider BT music's
                    # continuous RMS sits at ~1000-2000 and kept the utterance from EVER
                    # ending (the processing waited for the music to stop!). Speech over
                    # the music bursts past 2500.
                    silence_since = now
                elif now - silence_since > (_SIL_WAKE if globals().get("_WOKE", False) else _SIL_LO) \
                        or len(pcm) > _RUNAWAY:   # quiet OR runaway — 2.5s after a WAKE so
                    # the user's full request survives their natural pauses (the 1s window
                    # was ending the utterance at the pause right after "Hermes"!)
                    try:
                        writer.write(ws_frame(1, json.dumps({"t": "duck", "on": False}).encode()))
                        await writer.drain()
                    except Exception as e:
                        log(f"un-duck push failed: {e}")
                    STATS["duck_active"] = False
                    STATS["last_utt_len"] = len(pcm)
                    log(f"utterance end ({len(pcm)} bytes, rms {rms})")
                    data = bytes(pcm)
                    pcm = bytearray()
                    speech = False
                    if _KW is not None:            # re-arm the keyword spotter
                        _KW = KaldiRecognizer(_KW_MODEL, 16000, '["hermes [unk]", "[unk]"]')
                    if len(data) > 3200:           # >100ms of audio -> background chain
                        asyncio.create_task(run_utterance(data, writer, wake_audio))
                    wake_audio = None
    except (asyncio.IncompleteReadError, ConnectionError):
        pass
    except Exception as e:
        log(f"voice channel err: {e}")
    finally:
        CHANNELS.discard(writer)
        try: writer.close()
        except Exception: pass
    log("voice channel down")

# ---------------- admin console ----------------
async def admin_channel(reader, writer):
    """Admin panel WS: streams every log line + the live mic PCM + RMS.
    On connect: sends the recent log tail + current stats."""
    ADMIN.add(writer)
    log("admin connected")
    try:
        try:
            lines = open(LOG_FILE).read().splitlines()[-100:]
            tail = json.dumps({"t": "tail", "lines": lines}).encode()
            writer.write(ws_frame(1, tail))
            writer.write(ws_frame(1, json.dumps({"t": "stats", "s": STATS}).encode()))
            await writer.drain()
            log(f"admin tail sent ({len(lines)} lines)")
        except Exception as e:
            log(f"admin init: {e}")
        while True:
            try:
                opcode, payload = await asyncio.wait_for(ws_read_frame(reader), 1.0)
            except asyncio.TimeoutError:
                await writer.drain()     # flush the voice-channel broadcasts to the console
                continue
            if opcode == 8:
                break
            if opcode == 1:
                try:
                    msg = json.loads(payload)
                    if msg.get("t") == "stats":
                        writer.write(ws_frame(1, json.dumps({"t": "stats", "s": STATS}).encode()))
                        await writer.drain()
                except Exception:
                    pass
    except Exception:
        pass
    finally:
        ADMIN.discard(writer)
        try:
            writer.close()
        except Exception:
            pass
    log("admin channel down")

def device_info():
    """Queries live device state via adb (cached ~8s — adb round-trips are slow)."""
    now = time.time()
    if now - _device_cache["t"] < 8 and _device_cache["data"]:
        return _device_cache["data"]
    adb = os.path.expanduser("~/platform-tools/adb -s G6G1MK082254031J")

    def sh(c):
        try:
            r = subprocess.run(f"{adb} shell {c}", shell=True, capture_output=True,
                               text=True, timeout=8)
            return r.stdout.strip()
        except Exception:
            return "?"

    info = {
        "kernel": sh("uname -r"),
        "android": sh("getprop ro.build.version.release"),
        "app": sh("dumpsys package com.hermes.show5 | grep versionName | head -1"),
        "wifi": sh("dumpsys wifi | grep -E 'Wi-Fi is' | head -1"),
        "volume": sh("dumpsys audio | grep -E 'STREAM_MUSIC:' | head -1"),
        "battery": sh("dumpsys battery | grep -E 'level|status:' | tr -d ' ' | head -2"),
        "screen": sh("dumpsys power | grep mWakefulness | head -1"),
        "uptime_s": sh("cat /proc/uptime | cut -d. -f1"),
        "app_connected": len(CHANNELS) > 0,
        "vad_on": 2500, "talk_gate": 2300,
        "dsp": "HP+AGC (NS parked)" if _DSP is not None else "OFF",
        "wake_word": "vosk 'hermes'",
    }
    _device_cache["t"] = now
    _device_cache["data"] = info
    return info

# ---------------- HTTP handling (shared by TLS + plain listeners) ----------------
def resp(writer, status, ctype, body):
    return writer.write(
        f"HTTP/1.1 {status}\r\nContent-Type: {ctype}\r\n"
        f"Access-Control-Allow-Origin: *\r\n"
        f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode()
        + body)

async def send_resp(writer, status, ctype, body):
    """Safe async response: write + drain."""
    writer.write(f"HTTP/1.1 {status}\r\nContent-Type: {ctype}\r\n"
                 f"Access-Control-Allow-Origin: *\r\n"
                 f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode() + body)
    try:
        await writer.drain()
    except Exception:
        pass

async def handle(reader, writer):
    global _WOKE, _WAKE_TH, _TALK_GATE, _SIL_LO, _SIL_WAKE, _DUCK_RATIO, _MIC_GAIN, _VAD_ON, _RUNAWAY, _PREBUF
    try:
        head = await asyncio.wait_for(reader.read(65536), 30)
        if not head:
            writer.close(); return
        idx = head.find(b"\r\n\r\n")
        if idx < 0:
            await send_resp(writer, 400, "text/plain", b"bad request")
            writer.close(); return
        lines = head[:idx].decode("latin-1").split("\r\n")
        parts = lines[0].split(" ")
        if len(parts) < 2:
            writer.close(); return
        method, target = parts[0], parts[1]
        path = urllib.parse.urlparse(target).path
        headers = {}
        for l in lines[1:]:
            if ":" in l:
                k, v = l.split(":", 1)
                headers[k.strip().lower()] = v.strip()

        # WebSocket upgrade
        if path == "/ws" and headers.get("upgrade", "").lower() == "websocket":
            key = headers.get("sec-websocket-key", "")
            writer.write(ws_accept(key))
            await writer.drain()
            await voice_channel(reader, writer)
            return
        if path == "/admin/ws" and headers.get("upgrade", "").lower() == "websocket":
            key = headers.get("sec-websocket-key", "")
            writer.write(ws_accept(key))
            await writer.drain()
            # send the current tunables on connect so the sliders sync with the server
            try:
                writer.write(ws_frame(1, json.dumps({"t": "tune", "v": {
                    "wake_th": _WAKE_TH, "talk_gate": _TALK_GATE, "vad_on": _VAD_ON,
                    "sil_lo": _SIL_LO, "sil_wake": _SIL_WAKE, "runaway": _RUNAWAY,
                    "prebuf": _PREBUF, "duck_ratio": _DUCK_RATIO, "mic_gain": _MIC_GAIN
                }}).encode()))
                await writer.drain()
            except Exception:
                pass
            await admin_channel(reader, writer)
            return

        clen = int(headers.get("content-length", 0))
        body = head[idx + 4:]
        while len(body) < clen:
            more = await asyncio.wait_for(reader.read(clen - len(body)), 30)
            if not more:
                break
            body += more
        body = body[:clen]

        if method == "GET" and path in ("/show5", "/show5/"):
            html = open(HTML_FILE, "rb").read()
            await send_resp(writer, 200, "text/html; charset=utf-8", html)
        elif method == "GET" and path in ("/admin", "/admin/"):
            try:
                html = open(ADMIN_FILE, "rb").read()
            except FileNotFoundError:
                html = b"<html><body><h1>admin.html missing on ~/show5/</h1></body></html>"
            await send_resp(writer, 200, "text/html; charset=utf-8", html)
        elif method == "GET" and path == "/admin/device":
            await send_resp(writer, 200, "application/json", json.dumps(device_info()).encode())
        elif method == "GET" and path == "/admin/stats":
            await send_resp(writer, 200, "application/json", json.dumps(STATS).encode())
        elif method == "POST" and path == "/admin/set":
            try:
                body = json.loads(body)
                key, val = body.get("key"), body.get("value")
                if key == "wake_th": _WAKE_TH = float(val)
                elif key == "talk_gate": _TALK_GATE = float(val)
                elif key == "sil_lo": _SIL_LO = float(val)
                elif key == "sil_wake": _SIL_WAKE = float(val)
                elif key == "duck_ratio": _DUCK_RATIO = float(val)
                elif key == "mic_gain": _MIC_GAIN = float(val)
                elif key == "vad_on": _VAD_ON = float(val)
                elif key == "runaway": _RUNAWAY = float(val)
                elif key == "prebuf": _PREBUF = int(val)
                # push the app-side tunables (the duck + the gain) to the app
                try:
                    payload = ws_frame(1, json.dumps(
                        {"t": "cfg", "duck_ratio": _DUCK_RATIO, "mic_gain": _MIC_GAIN}).encode())
                    for w in list(APP):
                        try: w.write(payload)
                        except Exception: pass
                except Exception: pass
                log(f"admin set {key}={val}")
                await send_resp(writer, 200, "application/json", b'{"ok":true}')
            except Exception as e:
                log(f"admin set err: {e}")
                await send_resp(writer, 200, "application/json", b'{"ok":false}')
        elif method == "POST" and path == "/admin/stop":
            if AGENT["stop_event"] is not None and not AGENT["stop_event"].is_set():
                AGENT["stop_event"].set()
                log("admin STOP requested")
                await send_resp(writer, 200, "application/json", b'{"ok":true}')
            else:
                await send_resp(writer, 200, "application/json", b'{"ok":false,"error":"no active run"}')
        elif method == "GET" and path == "/show5/query/ping":
            await send_resp(writer, 200, "application/json", b'{"ok":true,"service":"show5"}')
        elif method == "POST" and path == "/show5/test/play":
            # broadcast a wav (b64 body) to every connected device channel
            n = 0
            for w in list(CHANNELS):
                try:
                    try:
                        _j = json.loads(body.decode())
                        _b64 = _j.get("b64", body.decode())
                    except Exception:
                        _b64 = body.decode()
                    w.write(ws_frame(1, json.dumps({"t": "audio", "b64": _b64}).encode()))
                    await w.drain()
                    n += 1
                except Exception:
                    pass
            await send_resp(writer, 200, "application/json", json.dumps({"sent": n}).encode())
        elif method == "POST" and path.startswith("/show5/test/state"):
            # broadcast a bar state to every channel: s=listen|think|speak|done
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(target).query)
            st = qs.get("s", ["listen"])[0]
            n = 0
            for w in list(CHANNELS):
                try:
                    w.write(ws_frame(1, json.dumps({"t": "state", "s": st}).encode()))
                    await w.drain()
                    n += 1
                except Exception:
                    pass
            await send_resp(writer, 200, "application/json", json.dumps({"sent": n, "state": st}).encode())
        elif method == "POST" and path.startswith("/show5/test/record"):
            # record the app's live mic stream for N seconds, then analyze RMS
            global RECORD
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(target).query)
            secs = float(qs.get("secs", ["8"])[0])
            RECORD = {"buf": bytearray(), "agc": bytearray(), "until": time.time() + secs}
            await asyncio.sleep(secs + 1.0)
            pcm = bytes(RECORD["buf"])
            agc = bytes(RECORD.get("agc", b""))
            RECORD = None
            if len(pcm) < 16000:
                await send_resp(writer, 200, "application/json",
                                json.dumps({"frames": len(pcm) // 2, "note": "too little audio captured"}).encode())
                return
            import array, math
            s = array.array("h", pcm)
            n = len(s)
            rms_all = int((sum(v * v for v in s) / n) ** 0.5)
            peak = max(abs(v) for v in s)
            per = [int((sum(v * v for v in s[i:i + 16000]) / min(16000, len(s) - i)) ** 0.5)
                   for i in range(0, n - 16000, 16000)]
            open('/tmp/show5-record.wav', 'wb').write(make_wav(pcm))
            open('/tmp/show5-record-agc.wav', 'wb').write(make_wav(agc))
            await send_resp(writer, 200, "application/json", json.dumps({
                "seconds": round(len(pcm) / 32000, 1), "peak": peak, "rms": rms_all,
                "per_second_rms": per, "saved": "/tmp/show5-record.wav"}).encode())
        elif method == "POST" and path == "/show5/query":
            loop = asyncio.get_event_loop()
            result = await loop.run_in_executor(None, do_query, body)
            await send_resp(writer, 200, "application/json", json.dumps(result).encode())
        else:
            await send_resp(writer, 404, "text/plain", b"not found")
    except Exception as e:
        log(f"req err: {e!r} path={path}")
        try:
            await send_resp(writer, 500, "text/plain", str(e).encode())
        except Exception:
            pass
    finally:
        try: writer.close()
        except Exception: pass

def do_query(wav_bytes):
    fd, path = tempfile.mkstemp(suffix=".wav")
    os.close(fd)
    with open(path, "wb") as f:
        f.write(wav_bytes)
    try:
        try:
            text = stt(path)
        except Exception as e:
            return {"error": f"stt: {e}"}
        if not text:
            return {"error": "didn't catch that"}
        log(f"stt: {text}")
        try:
            answer = do_agent(text)
        except Exception as e:
            return {"error": f"agent: {e}"}
        log(f"hermes: {answer}")
        try:
            audio_b64 = piper_say(answer)
        except Exception as e:
            return {"text": answer, "error": f"tts: {e}"}
        return {"text": answer, "audio_b64": audio_b64}
    finally:
        try: os.unlink(path)
        except OSError: pass

async def _aec_sampler():
    """Every 2s: raw-mic RMS (tinycap dev 8 = pre-AEC), the output level (the app's
    stream), and the reference state (BT + duck) -> the admin's AEC view."""
    import struct as _st, math as _m
    while True:
        try:
            raw = 0.0
            r = await asyncio.get_event_loop().run_in_executor(
                None, lambda: subprocess.run(
                    ["bash", "-c",
                     "timeout 2 ~/platform-tools/adb -s G6G1MK082254031J shell "
                     "'tinycap /data/local/tmp/aecraw.wav -D 0 -d 8 -c 1 -r 16000 -b 16 -T 1 2>/dev/null; "
                     "cat /data/local/tmp/aecraw.wav' 2>/dev/null | tail -c 16000"],
                    capture_output=True, timeout=6).stdout)
            if len(r) > 44:
                pcm = r[44:]
                n = len(pcm) // 2
                vals = _st.unpack(f"<{n}h", pcm[: n * 2])
                raw = round(_m.sqrt(sum(v * v for v in vals) / max(1, n)), 1)
        except Exception:
            raw = -1
        out_rms = STATS.get("last_rms", 0)
        try:
            payload = ws_frame(1, json.dumps(
                {"t": "aec", "raw": raw, "out": out_rms,
                 "bt": STATS.get("bt_streaming", False),
                 "duck": STATS.get("duck_active", False),
                 "live": {"rms": out_rms, "wake_prob": STATS.get("last_wake_prob", 0.0),
                          "utt_len": STATS.get("last_utt_len", 0),
                          "vol": STATS.get("last_vol", -1)}}).encode())
            for w in list(ADMIN):
                try:
                    w.write(payload)
                except Exception:
                    pass
        except Exception:
            pass
        await asyncio.sleep(0.5)

async def main():
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(f"{HOME}/show5/certs/server.crt", f"{HOME}/show5/certs/server.key")
    asyncio.get_event_loop().create_task(_aec_sampler())
    srv_tls = await asyncio.start_server(handle, "0.0.0.0", PORT_HTTPS, ssl=ctx)
    srv_plain = await asyncio.start_server(handle, "0.0.0.0", PORT_PLAIN)
    log(f"show5 https :{PORT_HTTPS} + plain ws :{PORT_PLAIN} (piper={find_piper()})")
    loop = asyncio.get_event_loop()

    def preload():
        global _model
        # the adbd loses root on every device reboot — re-root so the amp-forcing
        # tinymix (and the heals) work as root (the shell can't open the 660 nodes)
        for t in (_TCP, _SERIAL):
            try:
                subprocess.Popen([_ADB, "-s", t, "root"],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except Exception:
                pass
        if _model is None:
            from faster_whisper import WhisperModel
            log("preloading whisper small.en (cpu int8)...")
            _model = WhisperModel(WHISPER_MODEL, device="cpu", compute_type="int8")
            log("whisper preloaded")
    loop.run_in_executor(None, preload)
    loop.create_task(watchdog())               # ALWAYS-UP: heal the device when it dies

    async with srv_tls, srv_plain:
        await asyncio.gather(srv_tls.serve_forever(), srv_plain.serve_forever())

if __name__ == "__main__":
    log("show5 server starting")
    asyncio.run(main())
