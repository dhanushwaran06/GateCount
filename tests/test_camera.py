"""Frame decoding: whatever a (glitching) camera delivers, to_frame returns a 3-channel BGR image or None."""

import cv2
import numpy as np

from counter.camera import to_frame


def test_mjpeg_bytes_are_decoded_and_resized():
    ok, jpg = cv2.imencode(".jpg", np.zeros((480, 640, 3), np.uint8))
    assert ok
    assert to_frame(jpg.reshape(1, -1), 1280, 720).shape == (720, 1280, 3)


def test_raw_yuyv_after_a_reconnect_becomes_bgr():
    yuyv = np.full((720, 1280, 2), 128, np.uint8)  # what OpenCV returns when the camera did not take MJPG
    assert to_frame(yuyv, 1280, 720).shape == (720, 1280, 3)


def test_unexpected_layouts_are_dropped_not_passed_on():
    assert to_frame(np.zeros((720, 1280), np.uint8)) is None
    assert to_frame(np.zeros((720, 1280, 5), np.uint8)) is None
    assert to_frame(np.frombuffer(b"not a jpeg", np.uint8).reshape(1, -1)) is None
