package com.hermes.show5;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.Service;
import android.content.Intent;
import android.media.AudioFormat;
import android.media.AudioManager;
import android.media.AudioDeviceInfo;
import android.media.AudioRecord;
import android.media.audiofx.AcousticEchoCanceler;
import android.media.AudioTrack;
import android.media.MediaRecorder;
import android.os.IBinder;
import android.util.Base64;
import android.util.Log;

import org.json.JSONObject;

import java.io.BufferedInputStream;
import java.io.BufferedOutputStream;
import java.io.ByteArrayOutputStream;
import java.io.DataInputStream;
import java.io.DataOutputStream;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.Socket;
import java.nio.ByteBuffer;
import java.nio.ByteOrder;
import java.security.SecureRandom;

/**
 * Always-on ears: streams 16kHz PCM16 to the show5 server over a raw WebSocket.
 * The server does VAD + "hermes" wake + agent + TTS and pushes state events:
 *   {"t":"state","s":"listen"|"think"|"speak"|"done"}
 *   {"t":"audio","b64":"<wav base64>"}
 * Drives the OverlayService bar exactly like Alexa: bar appears on wake (blue),
 * turns amber while thinking, orange while speaking, disappears when done.
 */
public class MicService extends Service {
    private static final String TAG = "HermesOS";
    private static final String WS_URL = "ws://192.168.1.231:8794/ws";

    private volatile boolean running = true;
    private volatile long lastFrame = 0;      // watchdog: last WS data received
    private volatile boolean recResetRequested = false;   // playback done -> fresh capture
    private volatile long lastPlaybackEnd = 0;             // for the post-TTS recovery window
    private volatile boolean playing = false;   // true while TTS plays -> send silence to server
    private Thread micThread, wsThread, audioThread;
    private volatile long lastRead = 0;       // read-watchdog: last successful mic read
    private volatile int ambientLevel = 500;  // room noise (raw, quiet-frame rolling avg)
    private Socket sock;
    private OutputStream out;
    private AudioRecord rec;
    private final SecureRandom rng = new SecureRandom();

    @Override
    public void onCreate() {
        super.onCreate();
        startForeground(1, buildNotification());
        startService(new Intent(this, OverlayService.class));
    }

    @Override
    public int onStartCommand(Intent i, int flags, int startId) {
        if (micThread == null || !micThread.isAlive()) {
            micThread = new Thread(this::micLoop, "hermes-mic");
            micThread.start();
            // read-watchdog: if the mic read freezes inside the HAL (no timeout exists),
            // force-release the recorder from here — the read throws and the mic loop
            // re-opens. This is the on-device recovery for the "dead after response" bug.
            new Thread(() -> {
                while (running) {
                    sleep(5000);
                    if (running && lastRead > 0
                            && System.currentTimeMillis() - lastRead > 10000) {
                        Log.w(TAG, "mic read frozen 10s - force re-opening recorder");
                        try { if (rec != null) rec.stop(); } catch (Exception ignored) {}
                        try { if (rec != null) rec.release(); } catch (Exception ignored) {}
                        rec = null;
                        lastRead = System.currentTimeMillis();   // avoid a tight loop
                    }
                }
            }, "hermes-mic-watchdog").start();
        }
        if (wsThread == null || !wsThread.isAlive()) {
            wsThread = new Thread(this::wsLoop, "hermes-ws");
            wsThread.start();
        }
        return START_STICKY;
    }

    // ---------------- websocket ----------------
    private void wsLoop() {
        while (running) {
            try {
                sock = new Socket("192.168.1.231", 8794);
                sock.setTcpNoDelay(true);
                out = new BufferedOutputStream(sock.getOutputStream());
                InputStream in = new BufferedInputStream(sock.getInputStream());
                handshake(in, out);
                Log.i(TAG, "ws connected");
                lastFrame = System.currentTimeMillis();
                wsReadLoop(in);
            } catch (Exception e) {
                Log.w(TAG, "ws: " + e);
            }
            closeQuietly();
            if (OverlayService.instance != null) {
                OverlayService.instance.setState(OverlayService.STATE_IDLE);
            }
            sleep(5000);   // reconnect every 5s
        }
    }

    private void handshake(InputStream in, OutputStream out) throws Exception {
        byte[] keyBytes = new byte[16];
        rng.nextBytes(keyBytes);
        String key = Base64.encodeToString(keyBytes, Base64.NO_WRAP);
        String req = "GET /ws HTTP/1.1\r\n"
                + "Host: 192.168.1.231:8794\r\n"
                + "Upgrade: websocket\r\n"
                + "Connection: Upgrade\r\n"
                + "Sec-WebSocket-Key: " + key + "\r\n"
                + "Sec-WebSocket-Version: 13\r\n\r\n";
        out.write(req.getBytes());
        out.flush();
        ByteArrayOutputStream hdr = new ByteArrayOutputStream();
        int b;
        while (hdr.size() < 16384 && (b = in.read()) != -1) {
            hdr.write(b);
            if (hdr.toString("ISO-8859-1").contains("\r\n\r\n")) break;
        }
        String resp = hdr.toString("ISO-8859-1");
        if (!resp.startsWith("HTTP/1.1 101")) {
            throw new IllegalStateException("handshake failed: " + resp.split("\r\n")[0]);
        }
    }

    private void wsReadLoop(InputStream in) throws Exception {
        DataInputStream din = new DataInputStream(in);
        sock.setSoTimeout(40000);            // > server's 30s ping, so pings win the race
        while (running) {
            int b0, b1;
            try {
                b0 = din.read();                    // fin + opcode
                b1 = din.read();
            } catch (java.net.SocketTimeoutException ste) {
                if (System.currentTimeMillis() - lastFrame > 90000) {
                    Log.w(TAG, "ws watchdog: no data for 90s - reconnecting");
                    break;                          // dead link -> reconnect
                }
                continue;                           // server pings arrive ~35s
            }
            if (b0 < 0 || b1 < 0) break;
            lastFrame = System.currentTimeMillis();
            int opcode = b0 & 0x0F;
            long len = b1 & 0x7F;
            if (len == 126) len = din.readUnsignedShort();
            else if (len == 127) len = din.readLong();
            boolean masked = (b1 & 0x80) != 0;
            byte[] mask = new byte[4];
            if (masked) din.readFully(mask);
            byte[] payload = new byte[(int) len];
            din.readFully(payload);
            if (masked) {
                for (int i = 0; i < payload.length; i++) payload[i] ^= mask[i % 4];
            }
            if (opcode == 8) break;                 // close
            if (opcode == 9) {                      // ping -> pong
                sendFrame(10, payload);
                continue;
            }
            if (opcode == 1) handleEvent(new String(payload, "UTF-8"));
        }
    }

    private void handleEvent(String json) {
        try {
            JSONObject o = new JSONObject(json);
            String t = o.optString("t");
            if ("state".equals(t)) {
                String s = o.optString("s");
                int st = OverlayService.STATE_IDLE;
                if ("listen".equals(s)) {
                    st = OverlayService.STATE_LISTEN;
                    if (OverlayService.instance != null) {
                        OverlayService.instance.clearInfo();   // fresh exchange
                    }
                } else if ("think".equals(s)) st = OverlayService.STATE_THINK;
                else if ("speak".equals(s)) st = OverlayService.STATE_SPEAK;
                if (OverlayService.instance != null) {
                    OverlayService.instance.setState(st);
                }
            } else if ("audio".equals(t)) {
                final byte[] wav = Base64.decode(o.optString("b64"), Base64.DEFAULT);
                playWav(wav);
            } else if ("log".equals(t)) {
                if (OverlayService.instance != null) {
                    OverlayService.instance.addLog(o.optString("m"));
                }
            } else if ("think".equals(t)) {
                if (OverlayService.instance != null) {
                    OverlayService.instance.setThinking(o.optString("m"));
                }
            } else if ("tool".equals(t)) {
                if (OverlayService.instance != null) {
                    JSONObject te = o.optJSONObject("m");
                    String name = te != null ? te.optString("name", "?") : "?";
                    String status = te != null ? te.optString("status", "") : "";
                    OverlayService.instance.addTool("▶ " + name + (status.isEmpty() ? "" : " " + status));
                }
            } else if ("err".equals(t)) {
                Log.w(TAG, "server err: " + o.optString("m"));
            } else if ("bt".equals(t)) {
                handleBtState(o.optBoolean("on", false));
            } else if ("duck".equals(t)) {
                handleDuck(o.optBoolean("on", false));
            } else if ("cfg".equals(t)) {
                try {
                    if (o.has("duck_ratio")) duckRatio = Math.max(2, o.optInt("duck_ratio", 4));
                    if (o.has("mic_gain")) micGain = Math.max(4, o.optInt("mic_gain", 32));
                    Log.i(TAG, "CFG: duckRatio=" + duckRatio + " micGain=" + micGain);
                } catch (Exception ignored) {}
            }
        } catch (Exception e) {
            Log.w(TAG, "event: " + e);
        }
    }

    /** True if any A2DP sink device is connected (BT speaker mode active).
     *  Permission-free fallback: the SERVER pushes {"t":"bt","on":bool} control
     *  frames (it knows via adb dumpsys); we also check music activity locally. */
    private volatile boolean serverBtConnected = false;

    private boolean btDeviceConnected() {
        try {
            // 1) server-known state (authoritative — server reads dumpsys via adb)
            if (serverBtConnected) return true;
            // 2) local: A2DP sink device in the output device list (permission-free)
            AudioManager am = (AudioManager) getSystemService(AUDIO_SERVICE);
            if (am == null) return false;
            android.media.AudioDeviceInfo[] devs = am.getDevices(
                    AudioManager.GET_DEVICES_OUTPUTS);
            if (devs != null) {
                for (android.media.AudioDeviceInfo d : devs) {
                    int t = d.getType();
                    // TYPE_BLUETOOTH_A2DP=8, TYPE_BLUETOOTH_SCO=7 — the sink's
                    // incoming stream registers as an A2DP output device
                    if (t == 8 || t == 7) return true;
                }
            }
            // 3) music activity fallback
            return am.isMusicActive();
        } catch (Throwable t) {
            return false;
        }
    }

    /** Handle the server's BT-state control message. */
    private void handleBtState(boolean on) {
        serverBtConnected = on;
        Log.i(TAG, "server BT state: " + (on ? "connected" : "disconnected"));
    }

    /** Amazon-style barge-in: duck the music (BT stream) so the wake word hears the user. */
    private int duckPrevVolume = -1;
    private int duckRatio = 4;      // the admin slider (media vol / this)
    private int micGain = 32;       // the admin slider (the amplification x)
    private void handleDuck(boolean on) {
        try {
            AudioManager dm = (AudioManager) getSystemService(AUDIO_SERVICE);
            if (dm != null) {
                if (on) {
                    int cur = dm.getStreamVolume(AudioManager.STREAM_MUSIC);
                    duckPrevVolume = cur;
                    dm.setStreamVolume(AudioManager.STREAM_MUSIC, Math.max(1, cur / duckRatio), 0);
                    Log.i(TAG, "DUCK: music volume " + cur + " -> " + Math.max(1, cur / duckRatio));
                } else if (duckPrevVolume >= 0) {
                    dm.setStreamVolume(AudioManager.STREAM_MUSIC, duckPrevVolume, 0);
                    Log.i(TAG, "DUCK: music volume restored " + duckPrevVolume);
                    duckPrevVolume = -1;
                }
            }
        } catch (Exception e) {
            Log.w(TAG, "duck: " + e);
        }
    }

    /** Send a masked binary frame (client->server MUST be masked). */
    private void sendFrame(int opcode, byte[] payload) {
        try {
            OutputStream o = out;
            if (o == null) return;
            ByteArrayOutputStream f = new ByteArrayOutputStream();
            f.write(0x80 | opcode);
            int len = payload.length;
            if (len < 126) {
                f.write(0x80 | len);
            } else if (len < 65536) {
                f.write(0x80 | 126);
                f.write((len >> 8) & 0xFF);
                f.write(len & 0xFF);
            } else {
                f.write(0x80 | 127);
                for (int i = 7; i >= 0; i--) f.write((int) ((len >> (8 * i)) & 0xFF));
            }
            byte[] mask = new byte[4];
            rng.nextBytes(mask);
            f.write(mask);
            for (int i = 0; i < len; i++) f.write(payload[i] ^ mask[i % 4]);
            o.write(f.toByteArray());
            o.flush();
        } catch (Exception e) {
            Log.w(TAG, "send: " + e);
        }
    }

    // ---------------- mic ----------------
    private void micLoop() {
        short[] buf = new short[1600];   // 100ms @ 16k
        byte[] bytes = new byte[buf.length * 2];
        int deadCycles = 0;              // consecutive HAL-wedge re-open attempts
        while (running) {
            boolean resetRequested = false;   // post-playback fast path (per capture attempt)
            // BT speaker gate (outer): if the A2DP sink is CONNECTED (iPad/phone
            // paired as a BT speaker), the HAL refuses mic init / wedges on
            // concurrent play+record. Key on the SINK CONNECTION, not
            // isMusicActive() — the track can be idle-but-connected and still
            // wedge the mic. Stream silence keepalives until the sink disconnects.
            AudioManager obam = (AudioManager) getSystemService(AUDIO_SERVICE);
            boolean btSinkConnected = false;
            try {
                btSinkConnected = obam != null &&
                        obam.getDevices(AudioManager.GET_DEVICES_OUTPUTS) != null &&
                        btDeviceConnected();
            } catch (Exception ignored) {}
            if (obam != null && obam.isMusicActive() || btSinkConnected) {
                // 2026-09-01: announce the BT state (the server needs it) but KEEP
                // the capture running — the kernel handles play+record simultaneously
                // now (the old wedge is fixed; the gate was a broken-amp workaround).
                try {
                    if (sock != null && sock.isConnected())
                        sendFrame(1, "{\"t\":\"bt\",\"on\":true}".getBytes("UTF-8"));
                } catch (Exception ignored) {}
                Log.i(TAG, "BT sink connected - mic stays LIVE (play+record OK)");
            }
            // post-playback grace: the server resets the audio HAL ~1s after the
            // playend signal — wait ~2s so the fresh capture opens on a CLEAN HAL
            // (opening early = stalled reads + the 10s frozen-watchdog re-open)
            long sincePlay = System.currentTimeMillis() - lastPlaybackEnd;
            if (sincePlay > 0 && sincePlay < 2000) {
                sleep(2000 - sincePlay);
            }
            try {
                // 2026-09-01: enable the AMAZON HAL's AEC (the audio.primary_amazon.mt8163
                // HAL has the full ASR AEC + the EchoRef data provider — gated by these
                // audio parameters). Reverse-engineered from the Fire OS's HAL: "AECOn"
                // + SetForceAECRec() engage the AudioALSACaptureHandlerAEC which cancels
                // the device's own output (the BT music!) from the mic.
                try {
                    AudioManager aam = (AudioManager) getSystemService(AUDIO_SERVICE);
                    if (aam != null) {
                        // The MTK/Amazon HAL's keys are BARE flags (SpeechOn/AECOn style) —
                        // the "=1" KV form is left as "remain" and never consumed.
                        aam.setParameters("AECOn");
                        aam.setParameters("ForceAECRec");
                        aam.setParameters("EnableBesRecord");
                        aam.setParameters("SpeechOn");
                        // the speech mode engages the HAL's speech-enhance (the BES:
                        // the AEC + the NS + the reference engine) on the capture
                        try { aam.setMode(AudioManager.MODE_IN_COMMUNICATION); } catch (Exception ignored) {}
                        Log.i(TAG, "Amazon HAL AEC requested (bare flags: AECOn/ForceAECRec/BesRecord/SpeechOn)");
                    }
                } catch (Exception e) {
                    Log.w(TAG, "AEC param failed: " + e);
                }
                int min = AudioRecord.getMinBufferSize(16000, AudioFormat.CHANNEL_IN_MONO,
                        AudioFormat.ENCODING_PCM_16BIT);
                // 2026-09-01: PIN the built-in mic explicitly — when a BT A2DP sink is
                // connected the audio policy picks NO input device (AUDIO_DEVICE_NONE)
                // and the capture opens against nothing ("mic gated" while the BT is on).
                AudioRecord.Builder arb = new AudioRecord.Builder()
                        .setAudioSource(MediaRecorder.AudioSource.VOICE_RECOGNITION)  // HAL AEC/NS path
                        .setAudioFormat(new AudioFormat.Builder()
                                .setEncoding(AudioFormat.ENCODING_PCM_16BIT)
                                .setSampleRate(16000)
                                .setChannelMask(AudioFormat.CHANNEL_IN_MONO)
                                .build())
                        .setBufferSizeInBytes(Math.max(min, 3200) * 2);
                AudioDeviceInfo micDev = null;
                AudioManager dvm = (AudioManager) getSystemService(AUDIO_SERVICE);
                if (dvm != null) {
                    for (AudioDeviceInfo di : dvm.getDevices(AudioManager.GET_DEVICES_INPUTS)) {
                        if (di.getType() == AudioDeviceInfo.TYPE_BUILTIN_MIC) {
                            micDev = di;
                            break;
                        }
                    }
                }
                rec = arb.build();
                // setPreferredDevice is API 23+ (Builder.setAudioDevice is API 31)
                if (micDev != null) {
                    try { rec.setPreferredDevice(micDev); }
                    catch (Exception e) { Log.w(TAG, "preferred device failed: " + e); }
                }
                // 2026-09-01: enable the Acoustic Echo Canceller — the BT stream plays
                // through the Show's own speaker, so the AEC cancels that echo from the
                // capture and the wake word can hear the user OVER the music.
                try {
                    if (AcousticEchoCanceler.isAvailable()) {
                        AcousticEchoCanceler aec = AcousticEchoCanceler.create(rec.getAudioSessionId());
                        if (aec != null) {
                            aec.setEnabled(true);
                            Log.i(TAG, "AEC enabled - wake word works over music");
                        }
                    } else {
                        Log.w(TAG, "AEC unavailable on this HAL");
                    }
                } catch (Exception e) {
                    Log.w(TAG, "AEC enable failed: " + e);
                }
                if (rec.getState() != AudioRecord.STATE_INITIALIZED) {
                    Log.w(TAG, "AudioRecord init failed - retrying");
                    // true-wedge guard: init failures right after a playback are the
                    // EXPECTED HAL recovery (the server resets the mediaserver ~8s in) —
                    // only count failures >20s after the last playback as real wedges
                    if (System.currentTimeMillis() - lastPlaybackEnd > 20000) {
                        deadCycles++;
                        Log.w(TAG, "mic wedge count: " + deadCycles);
                    }
                } else {
                    rec.startRecording();
                    int zeros = 0;
                    boolean btGateActive = false;
                    while (running) {
                        if (recResetRequested) {          // playback finished -> fresh capture
                            recResetRequested = false;
                            resetRequested = true;
                            break;
                        }
                                                                                                // 2026-09-01: music started MID-capture - announce the state but
                        // KEEP capturing (the kernel handles play+record simultaneously
                        // since the fixes; the old wedge is gone).
                        AudioManager bam = (AudioManager) getSystemService(AUDIO_SERVICE);
                        boolean btMusic = false;
                        try {
                            btMusic = (bam != null && bam.isMusicActive()) || btDeviceConnected();
                        } catch (Exception ignored) {}
                        if (btMusic && !btGateActive) {
                            btGateActive = true;
                            try {
                                if (sock != null && sock.isConnected())
                                    sendFrame(1, "{\"t\":\"bt\",\"on\":true}".getBytes("UTF-8"));
                            } catch (Exception ignored) {}
                            // 2026-09-01 MUSIC MODE: ride the media volume LOW (~25%) so the
                            // software wake word ALWAYS hears the user over the stream. The
                            // user raises it for full volume (the wake still works - just
                            // quieter music). This is the sure-fire wake-over-music.
                            try {
                                AudioManager mm = (AudioManager) getSystemService(AUDIO_SERVICE);
                                if (mm != null) {
                                    int cur = mm.getStreamVolume(AudioManager.STREAM_MUSIC);
                                    if (cur > 4) {
                                        duckPrevVolume = cur;
                                        mm.setStreamVolume(AudioManager.STREAM_MUSIC, Math.max(3, cur / duckRatio), 0);
                                        Log.i(TAG, "MUSIC MODE: media " + cur + " -> " + Math.max(3, cur / duckRatio));
                                    }
                                }
                            } catch (Exception ignored) {}
                            Log.i(TAG, "BT streaming - mic stays LIVE");
                        }
                        if (!btMusic && btGateActive) {
                            btGateActive = false;
                            try {
                                if (sock != null && sock.isConnected())
                                    sendFrame(1, "{\"t\":\"bt\",\"on\":false}".getBytes("UTF-8"));
                            } catch (Exception ignored) {}
                            // restore the pre-duck volume when the stream ends
                            try {
                                AudioManager mm = (AudioManager) getSystemService(AUDIO_SERVICE);
                                if (mm != null && duckPrevVolume >= 0) {
                                    mm.setStreamVolume(AudioManager.STREAM_MUSIC, duckPrevVolume, 0);
                                    duckPrevVolume = -1;
                                }
                            } catch (Exception ignored) {}
                        }
                        // NON-BLOCKING read: the HAL can wedge and never return data
                        // (a blocking read would hang this thread forever)
                        int n = rec.read(buf, 0, buf.length, AudioRecord.READ_NON_BLOCKING);
                        if (n <= 0) {
                            zeros++;                 // HAL wedge: no data -> count toward re-open
                            if (zeros > 150) {       // ~3s of nothing
                                Log.w(TAG, "mic stream silent - re-opening (" + (deadCycles + 1) + ")");
                                if (System.currentTimeMillis() - lastPlaybackEnd > 20000) deadCycles++;
                                break;
                            }
                            sleep(20);
                            continue;
                        }
boolean allZero = true;
                        for (int i = 0; i < n; i++) {
                            if (buf[i] != 0) { allZero = false; break; }
                        }
                        if (allZero) {              // capture died (e.g. after playback) -> re-open
                            zeros++;
                            if (zeros > 150) {       // ~15s of pure silence
                                Log.w(TAG, "mic stream silent - re-opening (" + (deadCycles + 1) + ")");
                                if (System.currentTimeMillis() - lastPlaybackEnd > 20000) deadCycles++;
                                break;
                            }
                            sleep(20);
                            continue;
                        }
                        zeros = 0;
                        deadCycles = 0;             // healthy audio -> reset the wedge counter
                        lastRead = System.currentTimeMillis();   // read-watchdog heartbeat
                        // Amplify ~32x (30dB): the kernel PGA (+15.5dB) now does the heavy
                        // lifting; x512 + PGA clipped hard. Server AGC normalizes level.
                        if (playing) {                 // own TTS on the speaker -> send silence
                            for (int i = 0; i < n; i++) { bytes[i*2] = 0; bytes[i*2+1] = 0; }
                        } else {
                            for (int i = 0; i < n; i++) {
                                int v = buf[i] * micGain;
                                if (v > 32767) v = 32767;
                                else if (v < -32768) v = -32768;
                                bytes[i * 2] = (byte) (v & 0xFF);
                                bytes[i * 2 + 1] = (byte) ((v >> 8) & 0xFF);
                            }
                        }
                        if (sock != null && sock.isConnected()) {
                            byte[] frame = new byte[n * 2];
                            System.arraycopy(bytes, 0, frame, 0, n * 2);
                            sendFrame(2, frame);   // binary PCM
                        }
                        // voice flash: drive the overlay bar with the mic level (x32 gain)
                        long acc = 0;
                        for (int i = 0; i < n; i++) acc += (long) buf[i] * buf[i];
                        int rms = (int) Math.sqrt(acc / Math.max(1, n)) * 32;
                        // ambient room level (raw): rolling average of quiet frames —
                        // the TTS volume adapts to it so she speaks at room volume
                        if (rms < 80000) {   // raw < 2500: the room's noise floor (excludes speech)
                            int raw = (int) Math.sqrt(acc / Math.max(1, n));
                            ambientLevel = (ambientLevel * 7 + raw) / 8;
                        }
                        if (OverlayService.instance != null) {
                            OverlayService.instance.setMicLevel(rms);
                        }
                    }
                }
            } catch (Exception e) {
                Log.w(TAG, "mic err: " + e);
            }
            try { if (rec != null) { rec.stop(); rec.release(); } } catch (Exception ignored) {}
            rec = null;
            if (resetRequested) continue;      // post-playback: fresh capture NOW (no 5s wait)
            if (playing) {
                // the TTS is still playing — WAIT it out. Re-opening the capture mid-playback
                // re-creates the concurrent play+record wedge (the "kills the admin panel" bug).
                while (playing && running) { sleep(100); }
                if (recResetRequested) {   // playback just ended -> fresh capture immediately
                    recResetRequested = false;
                    continue;
                }
            }
            if (deadCycles >= 3) {
                // the HAL wedge is permanent until the process dies — restart it fast
                // (3 wedges ~= 25s of silence; the old 8 took a full minute)
                Log.e(TAG, "mic HAL unrecoverable (" + deadCycles + " wedges) - restarting app");
                try { Thread.sleep(1500); } catch (InterruptedException ignored) {}
                android.os.Process.killProcess(android.os.Process.myPid());
                return;
            }
            try {
                // right after a playback the HAL is settling (server resets it ~1s after
                // playend) — retry FAST so the mic returns in ~2-3s, not 30s
                Thread.sleep(System.currentTimeMillis() - lastPlaybackEnd < 20000 ? 2000 : 5000);
            } catch (InterruptedException ignored) {}
        }
    }

    // ---------------- tts playback ----------------
    private void playWav(byte[] wav) {
        if (audioThread != null && audioThread.isAlive()) return;  // one at a time
        playing = true;   // mute mic stream while TTS plays (stop the echo re-wake)
        // stop the recorder NOW, before the playback: the concurrent play+record
        // wedges this MTK HAL (the read freezes -> "mic stream silent" -> recovery
        // lag). A clean stop means the post-play re-open is instant.
        try { if (rec != null) { rec.stop(); rec.release(); } } catch (Exception ignored) {}
        rec = null;
        audioThread = new Thread(() -> {
            AudioManager duckAm = null;
            int savedMusicVol = -1;
            try {
                try {
                    duckAm = (AudioManager) getSystemService(AUDIO_SERVICE);
                    // TTS ducking: save the media (BT music) volume, duck it so the
                    // announcement cuts through, restore after playback.
                    savedMusicVol = duckAm.getStreamVolume(AudioManager.STREAM_MUSIC);
                    int duck = Math.max(1, duckAm.getStreamMaxVolume(AudioManager.STREAM_MUSIC) / 5);
                    duckAm.setStreamVolume(AudioManager.STREAM_MUSIC, duck, 0);
                    // TTS rides the ALARM stream at max (own volume, not AVRCP-slaved)
                    duckAm.setStreamVolume(AudioManager.STREAM_ALARM,
                            duckAm.getStreamMaxVolume(AudioManager.STREAM_ALARM), 0);
                } catch (Exception ignored) {}
                // pause the capture while we play: the HAL wedges on concurrent play+record
                // (the "works once then silence" bug). Stop -> play -> restart.
                try {
                    if (rec != null && rec.getRecordingState() == AudioRecord.RECORDSTATE_RECORDING)
                        rec.stop();
                } catch (Exception ignored) {}
                // parse wav: find fmt + data chunks
                int rate = 22050, dataStart = 44;
                ByteBuffer bb = ByteBuffer.wrap(wav).order(ByteOrder.LITTLE_ENDIAN);
                int pos = 12;
                while (pos + 8 <= wav.length) {
                    String id = new String(wav, pos, 4, "ISO-8859-1");
                    int sz = bb.getInt(pos + 4);
                    if ("fmt ".equals(id)) {
                        rate = bb.getInt(pos + 12);
                    } else if ("data".equals(id)) {
                        dataStart = pos + 8;
                        break;
                    }
                    pos += 8 + sz;
                }
                int pcmLen = wav.length - dataStart;
                AudioTrack track = new AudioTrack(AudioManager.STREAM_ALARM, rate,
                        AudioFormat.CHANNEL_OUT_MONO, AudioFormat.ENCODING_PCM_16BIT,
                        pcmLen, AudioTrack.MODE_STATIC);
                track.write(wav, dataStart, pcmLen);
                // adaptive volume: match the room's ambient noise (quiet room = soft,
                // noisy room = loud). NOTE: the MTK HAL seems to mute below ~0.85 on
                // this device, so the floor is high — tune 0.85..1.0 once audible again
                // 2026-09-01: amp is FIXED (kernel 6e7d3e33) — the old 0.85 floor was a
                // workaround for the silent amp. Now the TTS matches the room: quiet
                // room (~500 RMS) = ~15%, normal (~1000) = ~25%, noisy (4000+) = 100%.
                float vol = Math.min(1f, Math.max(0.05f, ambientLevel / 11400f));
                track.setVolume(vol);
                Log.i(TAG, "TTS volume " + (int) (vol * 100) + "% (ambient " + ambientLevel + ")");
                track.play();
                long playStart = System.currentTimeMillis();
                // the HAL can wedge the AudioTrack mid-play: playState gets STUCK at
                // PLAYING after the buffer finishes and the head position is
                // unreliable on this MTK HAL (measured: 30s stall). Wait the WAV's
                // ACTUAL duration (rate x bytes) + a small slack instead — the
                // finally (and the mic recovery) then run right after the sound.
                int playMs = (int) (pcmLen * 1000L / (rate * 2)) + 800;
                while (track.getPlayState() == AudioTrack.PLAYSTATE_PLAYING
                        && System.currentTimeMillis() - playStart < playMs) {
                    sleep(50);
                }
                track.release();
            } catch (Exception e) {
                Log.w(TAG, "play: " + e);
            } finally {
                playing = false;
                // restore the media volume we ducked (BT music returns to its level)
                try {
                    if (duckAm != null && savedMusicVol >= 0)
                        duckAm.setStreamVolume(AudioManager.STREAM_MUSIC, savedMusicVol, 0);
                } catch (Exception ignored) {}
                // release the capture and ask the mic thread for a FRESH AudioRecord.
                // (No process self-kill here anymore: the server resets the audio HAL
                // (mediaserver) ~8s later, and our mic retry picks up the fresh HAL —
                // this way the overlay + the app NEVER visibly restart.)
                try { if (rec != null) { rec.stop(); rec.release(); } } catch (Exception ignored) {}
                rec = null;
                recResetRequested = true;
                lastPlaybackEnd = System.currentTimeMillis();   // recovery window starts
                // tell the server the playback ended -> it resets the audio HAL in ~1s
                // (was: fixed 8s after the push — the mic gap was ~30s)
                try {
                    if (sock != null && sock.isConnected())
                        sendFrame(1, "{\"t\":\"playend\"}".getBytes("UTF-8"));
                } catch (Exception ignored) {}
            }
        }, "hermes-audio");
        audioThread.start();
    }

    // ---------------- plumbing ----------------
    private Notification buildNotification() {
        NotificationManager nm = getSystemService(NotificationManager.class);
        if (android.os.Build.VERSION.SDK_INT >= 26) {
            NotificationChannel ch = new NotificationChannel("hermes",
                    "Hermes", NotificationManager.IMPORTANCE_MIN);
            nm.createNotificationChannel(ch);
            return new Notification.Builder(this, "hermes")
                    .setContentTitle("Hermes")
                    .setContentText("listening for 'hermes'")
                    .setSmallIcon(android.R.drawable.ic_btn_speak_now)
                    .build();
        }
        return new Notification.Builder(this)
                .setContentTitle("Hermes")
                .setContentText("listening")
                .setSmallIcon(android.R.drawable.ic_btn_speak_now)
                .build();
    }

    private void closeQuietly() {
        try { if (sock != null) sock.close(); } catch (Exception ignored) {}
        sock = null;
        out = null;
    }

    private void sleep(long ms) {
        try { Thread.sleep(ms); } catch (InterruptedException ignored) {}
    }

    @Override
    public void onDestroy() {
        running = false;
        closeQuietly();
        super.onDestroy();
    }

    @Override
    public IBinder onBind(Intent i) { return null; }
}
