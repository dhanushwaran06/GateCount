"""Camera grabber: one thread per camera that always holds only the newest frame.

A source is a device index (0), a device path (/dev/v4l/by-id/...), or a video file.
Devices reconnect automatically; video files play at their native fps (like a live camera)
and can loop, so everything can be tested without hardware.

On Linux, devices deliver raw MJPEG bytes and only the frame a consumer actually reads is decoded
(the grabber runs at camera fps, inference at a fraction of it). This also avoids the V4L2 decoder's
"Corrupt JPEG data" stderr spam from Logitech webcams.
"""

import logging
import os
import subprocess
import threading
import time

import cv2

log = logging.getLogger(__name__)

WIDTH, HEIGHT = 1280, 720


def parse_source(source):
    """Return (opencv_source, is_device). Digits -> device index, /dev/... -> device path, else file."""
    if isinstance(source, int) or str(source).isdigit():
        return int(source), True
    return str(source), str(source).startswith("/dev/")


def open_capture(source, width=WIDTH, height=HEIGHT, fps=30):
    """Open a source; devices are asked for MJPG at width x height."""
    src, is_device = parse_source(source)
    if not is_device:
        return cv2.VideoCapture(src)
    cap = cv2.VideoCapture(src, cv2.CAP_DSHOW if os.name == "nt" else cv2.CAP_V4L2)
    cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))  # must precede size on V4L2
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, width)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
    cap.set(cv2.CAP_PROP_FPS, fps)
    cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
    if os.name != "nt":
        cap.set(cv2.CAP_PROP_CONVERT_RGB, 0)  # raw MJPEG; decoded on demand by to_frame()
    return cap


def to_frame(data, width=None, height=None):
    """Raw MJPEG bytes or a decoded image -> BGR image (resized to width x height if given), None if corrupt.
    A camera that reconnects after a USB glitch may not take MJPG and sends raw YUYV (2 channels): converted."""
    if data is None:
        return None
    if data.ndim == 2 and data.shape[0] == 1:
        data = cv2.imdecode(data, cv2.IMREAD_COLOR)
        if data is None:
            return None
    elif data.ndim == 3 and data.shape[2] == 2:
        data = cv2.cvtColor(data, cv2.COLOR_YUV2BGR_YUYV)
    if data.ndim != 3 or data.shape[2] != 3:
        return None  # any other layout would crash YOLO or the live window
    if width and (data.shape[1] != width or data.shape[0] != height):
        data = cv2.resize(data, (width, height), interpolation=cv2.INTER_AREA)
    return data


def quiet_libjpeg_warnings():
    """Drop libjpeg's "Corrupt JPEG data: N extraneous bytes" lines (harmless, ~15/s with some Logitech
    webcams) by routing C-level stderr (fd 2) through a filter thread; everything else passes unchanged.
    Returns restore(), which points fd 2 back at the real stderr and flushes what is left in the pipe."""
    real, (r, w) = os.dup(2), os.pipe()
    os.dup2(w, 2)
    os.close(w)
    noted = False

    def pump():
        nonlocal noted
        buf = b""
        try:
            while chunk := os.read(r, 65536):
                *lines, buf = (buf + chunk).split(b"\n")
                keep = [ln for ln in lines if not ln.startswith(b"Corrupt JPEG data")]
                if len(keep) < len(lines) and not noted:
                    noted = True
                    keep.append(b"(suppressing libjpeg 'Corrupt JPEG data' warnings: harmless webcam quirk)")
                if keep:
                    os.write(real, b"\n".join(keep) + b"\n")
                if len(buf) > 65536:  # no newline in sight: pass it through
                    os.write(real, buf)
                    buf = b""
            if buf:
                os.write(real, buf)
        finally:
            os.dup2(real, 2)  # never leave stderr pointing at a dead pipe

    t = threading.Thread(target=pump, daemon=True, name="stderr-filter")
    t.start()

    def restore():
        os.dup2(real, 2)  # closes the pipe's last writer -> pump reads EOF, flushes, exits
        t.join(2)

    return restore


def apply_v4l2_controls(source, controls, name="camera"):
    """Set camera controls with v4l2-ctl (Linux). They reset on replug, so this runs on every (re)open."""
    src, is_device = parse_source(source)
    if not controls or not is_device or os.name == "nt":
        return
    dev = f"/dev/video{src}" if isinstance(src, int) else src
    ctrls = ",".join(f"{k}={v}" for k, v in controls.items())
    try:
        r = subprocess.run(["v4l2-ctl", "-d", dev, "-c", ctrls], capture_output=True, text=True, timeout=5)
        if r.returncode:
            log.warning("%s: v4l2-ctl %s failed: %s", name, ctrls, r.stderr.strip())
    except (OSError, subprocess.TimeoutExpired) as e:
        log.warning("%s: cannot run v4l2-ctl (install v4l-utils): %s", name, e)


class Camera(threading.Thread):
    """Grabber thread. Consumers call read(after_seq) to get the newest frame they have not seen."""

    def __init__(self, source, name=None, width=WIDTH, height=HEIGHT, loop=False, controls=None):
        super().__init__(daemon=True, name=name or f"cam-{source}")
        self.source, self.width, self.height, self.loop = source, width, height, loop
        self.controls = controls or {}  # v4l2 controls, e.g. {"power_line_frequency": 1}
        self.is_device = parse_source(source)[1]
        self.last_frame_at = 0.0  # time.time() of the newest frame; 0 = never
        self.fps = 0.0            # measured capture fps
        self.ended = False        # video file finished (loop=False)
        self._cond = threading.Condition()
        self._data, self._seq = None, 0  # newest raw MJPEG bytes or decoded image
        self._stop_evt = threading.Event()

    def stop(self):
        self._stop_evt.set()

    def read(self, after_seq=0, timeout=1.0):
        """Wait up to `timeout` for a frame newer than `after_seq`. Returns (seq, BGR frame) or None.
        Decoding happens here, in the caller's thread, and only for the frame actually read."""
        deadline = time.monotonic() + timeout
        while True:
            with self._cond:
                self._cond.wait_for(lambda: self._seq > after_seq or self.ended,
                                    max(0.0, deadline - time.monotonic()))
                if self._seq <= after_seq:
                    return None
                seq, data = self._seq, self._data
            frame = to_frame(data, self.width, self.height)
            if frame is not None:
                return seq, frame
            after_seq = seq  # corrupt frame: wait for the next one

    def run(self):
        while not self._stop_evt.is_set():
            cap = open_capture(self.source, self.width, self.height)
            if not cap.isOpened():
                cap.release()
                log.warning("%s: cannot open %s, retrying in 2 s", self.name, self.source)
                self._stop_evt.wait(2)
                continue
            log.info("%s: opened %s", self.name, self.source)
            apply_v4l2_controls(self.source, self.controls, self.name)
            self._grab(cap)
            cap.release()
            self.fps = 0.0
            if self._stop_evt.is_set():
                break
            if not self.is_device and not self.loop:
                log.info("%s: end of video", self.name)
                break
            if self.is_device:
                log.warning("%s: frame read failed, reconnecting", self.name)
                self._stop_evt.wait(1)
        with self._cond:
            self.ended = True
            self._cond.notify_all()

    def _grab(self, cap):
        period = 0.0 if self.is_device else 1.0 / (cap.get(cv2.CAP_PROP_FPS) or 25.0)
        next_at = t0 = time.monotonic()
        n = 0
        while not self._stop_evt.is_set():
            ok, data = cap.read()
            if not ok:
                return
            with self._cond:
                self._data, self._seq, self.last_frame_at = data, self._seq + 1, time.time()
                self._cond.notify_all()
            n += 1
            now = time.monotonic()
            if now - t0 >= 2.0:
                self.fps, n, t0 = n / (now - t0), 0, now
            if period:  # pace video files at real time
                next_at = max(next_at + period, now - period)
                self._stop_evt.wait(max(0.0, next_at - now))
