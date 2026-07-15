import logging
import os
import signal
import subprocess
import threading

import numpy as np
import sounddevice as sd
import soundfile as sf

log = logging.getLogger("mywhisper")


def input_devices(refresh=False):
    """List (index, name) for every available input device.

    PortAudio caches its device list on first init. When `refresh=True`,
    we ask PortAudio to re-enumerate so newly-plugged mics show up
    without restarting the app. Caller must ensure no stream is active
    when refreshing — re-init kills active streams.
    """
    if refresh:
        try:
            sd._terminate()
            sd._initialize()
        except Exception:
            pass
    devices = []
    try:
        for index, dev in enumerate(sd.query_devices()):
            if dev.get("max_input_channels", 0) > 0:
                devices.append((index, dev["name"]))
    except Exception:
        pass
    return devices


class MicRecorder:
    """Records the chosen input device straight to a WAV file on disk."""

    def __init__(self):
        self._stream = None
        self._file = None
        self._path = None
        self.samplerate = 48000
        self.device = None      # None = system default, else a device name
        self.level = 0.0        # live RMS level, drives the waveform indicator
        self.max_level = 0.0    # loudest RMS this recording — tells "mic had
                                # signal" apart from true silence afterwards

    def _callback(self, indata, frames, time_info, status):
        if self._file is not None:
            self._file.write(indata.copy())
        try:
            self.level = float(np.sqrt(np.mean(indata.astype("float64") ** 2)))
            if self.level > self.max_level:
                self.max_level = self.level
        except Exception:
            pass

    def start(self, out_path):
        self.max_level = 0.0
        try:
            info = sd.query_devices(self.device, kind="input")
            self.samplerate = int(info["default_samplerate"])
        except Exception:
            self.samplerate = 48000
        self._path = out_path
        self._file = sf.SoundFile(
            out_path, mode="w", samplerate=self.samplerate,
            channels=1, subtype="FLOAT")
        self._stream = sd.InputStream(
            samplerate=self.samplerate, channels=1, dtype="float32",
            device=self.device, callback=self._callback)
        self._stream.start()
        # Log the device PortAudio actually opened — if recordings come
        # back garbled, this tells us whether the wrong mic was captured.
        try:
            opened = sd.query_devices(self._stream.device)["name"]
        except Exception:
            opened = self.device or "system default"
        log.info("mic recording: device=%r rate=%dHz", opened, self.samplerate)

    def stop(self):
        self.level = 0.0
        if self._stream is not None:
            self._stream.stop()
            self._stream.close()
            self._stream = None
        if self._file is not None:
            self._file.close()
            self._file = None
        return self._path


class SystemAudioRecorder:
    """Drives the Swift ScreenCaptureKit helper as a subprocess."""

    def __init__(self, helper_path):
        self.helper_path = helper_path
        self._proc = None
        self.out_path = None
        self.error = None

    def start(self, out_path):
        self.out_path = out_path
        self.error = None
        self._proc = None
        if not self.helper_path or not os.path.exists(self.helper_path):
            self.error = "System-audio helper not built (run build_app.sh)."
            return False
        try:
            self._proc = subprocess.Popen(
                [self.helper_path, out_path],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        except Exception as e:
            self.error = str(e)
            return False

        first_line = [None]

        def _read():
            first_line[0] = self._proc.stdout.readline()

        reader = threading.Thread(target=_read, daemon=True)
        reader.start()
        reader.join(timeout=10)

        if self._proc.poll() is not None:
            err = ""
            try:
                err = self._proc.stderr.read() or ""
            except Exception:
                pass
            self.error = err.strip() or "System-audio helper exited unexpectedly."
            return False
        return True

    def stop(self):
        if self._proc is None:
            return None
        if self._proc.poll() is None:
            self._proc.send_signal(signal.SIGINT)
            try:
                self._proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                self._proc.terminate()
                try:
                    self._proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self._proc.kill()
        path = self.out_path
        self._proc = None
        if path and os.path.exists(path) and os.path.getsize(path) > 0:
            return path
        return None
