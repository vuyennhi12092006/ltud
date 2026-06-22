import subprocess
import threading
import time
import wave

import numpy as np

try:
    import imageio_ffmpeg

    FFMPEG_EXE = imageio_ffmpeg.get_ffmpeg_exe()
except Exception:
    FFMPEG_EXE = None

try:
    import sounddevice as sd
except ImportError:
    sd = None

try:
    import pyaudiowpatch as pyaudio
except ImportError:
    pyaudio = None


def ffmpeg_available():
    return FFMPEG_EXE is not None and __import__("os").path.isfile(FFMPEG_EXE)


def mux_video_audio(video_path, audio_path, output_path):
    if not ffmpeg_available():
        raise RuntimeError("Thiếu ffmpeg — cài: pip install imageio-ffmpeg")

    cmd = [
        FFMPEG_EXE,
        "-y",
        "-i",
        video_path,
        "-i",
        audio_path,
        "-c:v",
        "copy",
        "-c:a",
        "aac",
        "-b:a",
        "192k",
        "-shortest",
        output_path,
    ]
    result = subprocess.run(
        cmd, capture_output=True, text=True, creationflags=subprocess.CREATE_NO_WINDOW
        if hasattr(subprocess, "CREATE_NO_WINDOW")
        else 0,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr[-500:] if result.stderr else "Ghép audio thất bại")


def _find_loopback_device(p):
    wasapi = p.get_host_api_info_by_type(pyaudio.paWASAPI)
    default_speakers = p.get_device_info_by_index(wasapi["defaultOutputDevice"])

    if default_speakers.get("isLoopbackDevice"):
        return default_speakers

    for loopback in p.get_loopback_device_info_generator():
        if default_speakers["name"] in loopback["name"]:
            return loopback
    return None


class AudioRecorder:
    """Ghi âm thanh hệ thống (WASAPI loopback) và/hoặc micro."""

    def __init__(self, sample_rate=44100):
        self.sample_rate = sample_rate
        self.actual_rate = sample_rate
        self.recording = False
        self.record_system = True
        self.record_mic = True
        self._system_chunks = []
        self._mic_chunks = []
        self._threads = []
        self._pa = None
        self._mic_stream = None
        self._system_channels = 2

    def start(self, record_system=True, record_mic=True):
        self.recording = True
        self.record_system = record_system
        self.record_mic = record_mic
        self._system_chunks = []
        self._mic_chunks = []
        self._threads = []

        if record_system and pyaudio is not None:
            with pyaudio.PyAudio() as p:
                device = _find_loopback_device(p)
                if device:
                    self.actual_rate = int(device["defaultSampleRate"])
                    self._system_channels = max(1, device["maxInputChannels"])

            t = threading.Thread(target=self._capture_system, daemon=True)
            t.start()
            self._threads.append(t)
        elif record_system:
            raise RuntimeError(
                "Không ghi được âm thanh hệ thống — cài: pip install pyaudiowpatch"
            )

        if record_mic and sd is not None:
            t = threading.Thread(target=self._capture_mic, daemon=True)
            t.start()
            self._threads.append(t)
        elif record_mic:
            raise RuntimeError(
                "Không ghi được micro — cài: pip install sounddevice"
            )

    def _capture_system(self):
        self._pa = pyaudio.PyAudio()
        device = _find_loopback_device(self._pa)
        if device is None:
            self._pa.terminate()
            return

        channels = self._system_channels
        stream = self._pa.open(
            format=pyaudio.paInt16,
            channels=channels,
            rate=self.actual_rate,
            frames_per_buffer=1024,
            input=True,
            input_device_index=device["index"],
        )

        try:
            while self.recording:
                self._system_chunks.append(stream.read(1024, exception_on_overflow=False))
        finally:
            stream.stop_stream()
            stream.close()
            self._pa.terminate()
            self._pa = None

    def _capture_mic(self):
        block = int(self.actual_rate * 0.05)

        def callback(indata, frames, time_info, status):
            if self.recording:
                self._mic_chunks.append(indata.copy())

        self._mic_stream = sd.InputStream(
            samplerate=self.actual_rate,
            channels=1,
            dtype="int16",
            blocksize=block,
            callback=callback,
        )
        self._mic_stream.start()
        while self.recording:
            time.sleep(0.05)
        self._mic_stream.stop()
        self._mic_stream.close()
        self._mic_stream = None

    def stop(self, output_wav_path):
        self.recording = False
        for t in self._threads:
            t.join(timeout=5)

        if not self._system_chunks and not self._mic_chunks:
            return None

        mixed = self._mix_tracks()
        if mixed is None or len(mixed) == 0:
            return None

        with wave.open(output_wav_path, "wb") as wf:
            wf.setnchannels(2)
            wf.setsampwidth(2)
            wf.setframerate(self.actual_rate)
            wf.writeframes(mixed.astype(np.int16).tobytes())

        return output_wav_path

    def _mix_tracks(self):
        system = None
        mic = None

        if self._system_chunks:
            raw = b"".join(self._system_chunks)
            system = np.frombuffer(raw, dtype=np.int16)
            ch = self._system_channels
            if len(system) % ch != 0:
                system = system[: len(system) - len(system) % ch]
            if ch == 1:
                system = np.column_stack([system, system]).astype(np.float32)
            else:
                system = system.reshape(-1, 2).astype(np.float32)

        if self._mic_chunks:
            mic = np.concatenate(self._mic_chunks, axis=0).astype(np.float32)
            if mic.ndim == 2 and mic.shape[1] == 1:
                mic = mic[:, 0]

        if system is None and mic is None:
            return None

        if system is None:
            mic = mic[: len(mic) - len(mic) % 2]
            stereo = np.column_stack([mic, mic])
            return stereo

        if mic is None:
            return system

        length = min(len(system), len(mic))
        system = system[:length]
        mic = mic[:length]
        mic_stereo = np.column_stack([mic, mic])
        mixed = system * 0.55 + mic_stereo * 0.45
        return np.clip(mixed, -32768, 32767)
