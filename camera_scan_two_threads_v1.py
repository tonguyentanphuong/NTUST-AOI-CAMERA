# -*- coding: utf-8 -*-
"""
Created on Tue Feb  3 10:31:56 2026

@author: user
"""

# -*- coding: utf-8 -*-
"""
Created on Thu Jan  8 12:12:10 2026

@author: user
"""
import serial
import time
import ids_peak_ipl
import re
import logging
import threading
from pathlib import Path
from datetime import datetime
from ids_peak import ids_peak
from ids_peak_common import CommonException
from ids_peak_icv.pipeline import DefaultPipeline
from ids_peak_afl.pipeline import BasicAutoFeatures
from ids_peak_afl import ids_peak_afl


# -----------------------------
# Logging helpers
# -----------------------------
def setup_logger(log_dir: Path, log_name_prefix: str = "run") -> logging.Logger:
    log_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_path = log_dir / f"{log_name_prefix}_{ts}.log"

    logger = logging.getLogger("capture_logger")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    logger.propagate = False

    fmt = logging.Formatter("%(asctime)s | %(levelname)s | %(message)s")

    fh = logging.FileHandler(log_path, encoding="utf-8")
    fh.setLevel(logging.INFO)
    fh.setFormatter(fmt)
    logger.addHandler(fh)

    ch = logging.StreamHandler()
    ch.setLevel(logging.INFO)
    ch.setFormatter(fmt)
    logger.addHandler(ch)

    logger.info(f"[LOG] Logging to: {log_path}")
    return logger


def log_print(logger: logging.Logger, msg: str, level: str = "info"):
    level = level.lower()
    if level == "error":
        logger.error(msg)
    elif level == "warning":
        logger.warning(msg)
    else:
        logger.info(msg)


# -----------------------------
# Camera helpers
# -----------------------------
def open_first_camera():
    mgr = ids_peak.DeviceManager.Instance()
    mgr.Update()

    if mgr.Devices().empty():
        raise RuntimeError("No device found")

    dev = mgr.Devices()[0].OpenDevice(ids_peak.DeviceAccessType_Control)
    remote = dev.RemoteDevice().NodeMaps()[0]
    stream = dev.DataStreams()[0].OpenDataStream()
    return dev, remote, stream


def load_camera_default_userset(remote_nodemap):
    remote_nodemap.FindNode("UserSetSelector").SetCurrentEntry("Default")
    remote_nodemap.FindNode("UserSetLoad").Execute()
    remote_nodemap.FindNode("UserSetLoad").WaitUntilDone()


def allocate_and_queue_buffers(remote_nodemap, data_stream):
    payload_size = remote_nodemap.FindNode("PayloadSize").Value()
    count = data_stream.NumBuffersAnnouncedMinRequired()

    for _ in range(count):
        buf = data_stream.AllocAndAnnounceBuffer(payload_size)
        data_stream.QueueBuffer(buf)


def start_acquisition(remote_nodemap, data_stream):
    remote_nodemap.FindNode("TLParamsLocked").SetValue(1)

    data_stream.StartAcquisition()
    remote_nodemap.FindNode("AcquisitionStart").Execute()
    remote_nodemap.FindNode("AcquisitionStart").WaitUntilDone()


def stop_acquisition(remote_nodemap, data_stream):
    try:
        remote_nodemap.FindNode("AcquisitionStop").Execute()
        remote_nodemap.FindNode("AcquisitionStop").WaitUntilDone()
    except Exception:
        pass

    if data_stream.IsGrabbing():
        data_stream.StopAcquisition(ids_peak.AcquisitionStopMode_Default)

    data_stream.Flush(ids_peak.DataStreamFlushMode_DiscardAll)

    for buf in data_stream.AnnouncedBuffers():
        data_stream.RevokeBuffer(buf)

    try:
        remote_nodemap.FindNode("TLParamsLocked").SetValue(0)
    except Exception:
        pass


# -----------------------------
# Continuous capture thread
# -----------------------------
class ContinuousCaptureWorker(threading.Thread):
    """
    Continuously grabs frames from the IDS stream and processes them through `pipeline`.

    - Keeps ONLY the most recent processed frame in memory.
    - When `save_latest()` is called, it saves the latest processed frame to disk.
    - You can request discarding N frames (useful for stabilization) via `request_discard(n)`.
    """

    def __init__(
        self,
        *,
        data_stream,
        pipeline,
        logger: logging.Logger,
        timeout_ms: int = 1000,
        name: str = "ContinuousCaptureWorker",
    ):
        super().__init__(name=name, daemon=True)
        self.data_stream = data_stream
        self.pipeline = pipeline
        self.logger = logger
        self.timeout_ms = int(timeout_ms)

        self._stop_evt = threading.Event()
        self._cond = threading.Condition()

        self._latest_img = None
        self._latest_ts = 0.0

        self._discard_remaining = 0

    def request_discard(self, n: int):
        n = max(0, int(n))
        with self._cond:
            self._discard_remaining = max(self._discard_remaining, n)
            self._cond.notify_all()
        log_print(self.logger, f"[CAM] Discard requested: {n} frames")

    def save_latest(self, out_path: Path, *, wait_for_new: bool = True, max_wait_sec: float = 2.0) -> bool:
        """
        Save the latest processed frame.

        If wait_for_new=True, it will wait until at least one frame newer than the call time is available
        (or until max_wait_sec expires), which is helpful if you trigger immediately after motion.
        """
        out_path = Path(out_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)

        start_ts = time.time()
        deadline = start_ts + float(max_wait_sec)
        want_newer_than = start_ts if wait_for_new else 0.0

        with self._cond:
            while not self._stop_evt.is_set():
                img = self._latest_img
                ts = self._latest_ts

                if img is not None and ts >= want_newer_than:
                    try:
                        img.save(str(out_path))
                        log_print(self.logger, f"[CAM] Saved -> {out_path}")
                        return True
                    except Exception as e:
                        log_print(self.logger, f"[CAM] Failed to save {out_path}: {e}", "error")
                        return False

                if time.time() >= deadline:
                    log_print(self.logger, "[CAM] Timeout waiting for a fresh frame to save.", "error")
                    return False

                self._cond.wait(timeout=0.05)

        log_print(self.logger, "[CAM] Worker stopped before save could complete.", "error")
        return False

    def stop(self):
        self._stop_evt.set()
        with self._cond:
            self._cond.notify_all()

    def run(self):
        log_print(self.logger, "[CAM] Continuous capture thread started.")
        while not self._stop_evt.is_set():
            try:
                buf = self.data_stream.WaitForFinishedBuffer(self.timeout_ms)
            except Exception as e:
                log_print(self.logger, f"[CAM] WaitForFinishedBuffer timeout/error: {e}", "warning")
                continue

            try:
                if buf.IsIncomplete():
                    self.data_stream.QueueBuffer(buf)
                    continue

                view = buf.ToImageView()
                processed = self.pipeline.process(view)

                # requeue ASAP
                self.data_stream.QueueBuffer(buf)

                with self._cond:
                    if self._discard_remaining > 0:
                        self._discard_remaining -= 1
                        # don't publish as latest during discard
                    else:
                        self._latest_img = processed
                        self._latest_ts = time.time()
                        self._cond.notify_all()

            except CommonException as e:
                try:
                    self.data_stream.QueueBuffer(buf)
                except Exception:
                    pass
                log_print(self.logger, f"[CAM] IDS error in worker: {e}", "error")
                continue
            except Exception as e:
                try:
                    self.data_stream.QueueBuffer(buf)
                except Exception:
                    pass
                log_print(self.logger, f"[CAM] Unexpected worker error: {e}", "error")
                continue

        log_print(self.logger, "[CAM] Continuous capture thread exiting.")


# -----------------------------
# Serial / G-code helpers
# -----------------------------
OK_RE = re.compile(r"^\s*ok\b", re.IGNORECASE)
ERR_RE = re.compile(r"\berror\b", re.IGNORECASE)

X_RE = re.compile(r"\bX(-?\d+(?:\.\d+)?)\b", re.IGNORECASE)

CAPTURE_CMD_RE = re.compile(
    r"^\s*M118\s+@CAPTURE\b(?:\s+X(?P<x>-?\d+(?:\.\d+)?))?(?:\s+Y(?P<y>-?\d+(?:\.\d+)?))?\s*$",
    re.IGNORECASE
)

POS_START_RE = re.compile(r"^\s*M118\s+@POS\s+START\b", re.IGNORECASE)

# Example: "X:105.00 Y:0.00 Z:0.00 E:0.00 Count X: 8400 Y:0 Z:0"
M114_POS_RE = re.compile(
    r"\bX:(?P<x>-?\d+(?:\.\d+)?)\s+Y:(?P<y>-?\d+(?:\.\d+)?)\s+Z:(?P<z>-?\d+(?:\.\d+)?)\s+E:(?P<e>-?\d+(?:\.\d+)?)",
    re.IGNORECASE,
)


def load_cset_if_provided(device, cset_path: str | None, logger: logging.Logger):
    if not cset_path:
        return
    p = Path(cset_path).expanduser().resolve()
    if not p.exists():
        raise FileNotFoundError(f".cset file not found: {p}")
    remote_nodemap = device.RemoteDevice().NodeMaps()[0]
    remote_nodemap.LoadFromFile(str(p))
    log_print(logger, f"Loaded camera settings: {p}")


def is_motion_cmd(cmd: str) -> bool:
    return cmd.startswith(("G0", "G1"))


def extract_x_value(cmd: str):
    m = X_RE.search(cmd)
    if not m:
        return None
    return float(m.group(1))


def send_cmd_and_wait_ok(ser: serial.Serial, cmd: str, logger: logging.Logger, *, timeout=None) -> bool:
    log_print(logger, f"Sending: {cmd}")
    ser.write((cmd + "\n").encode("ascii", errors="ignore"))

    start = time.time()
    while True:
        if timeout is not None and (time.time() - start) > timeout:
            log_print(logger, "ERROR: Timeout waiting for firmware response.", "error")
            return False

        line = ser.readline()
        if not line:
            continue

        resp = line.decode("ascii", errors="ignore").strip()
        if not resp:
            continue

        log_print(logger, f"  <- {resp}")

        if ERR_RE.search(resp):
            log_print(logger, "ERROR received from firmware.", "error")
            return False

        if OK_RE.match(resp) or resp.lower() == "ok" or " ok" in resp.lower():
            return True


def query_m114_position_once(
    ser: serial.Serial,
    logger: logging.Logger,
    *,
    timeout: float = 3.0,
):
    """
    Send M114, read until 'ok' (or timeout). Return dict with x,y,z,e if parsed, else None.
    """
    ser.write(b"M114\n")

    start = time.time()
    pos = None

    while True:
        if (time.time() - start) > timeout:
            log_print(logger, "[M114] Timeout waiting for response.", "warning")
            return pos

        line = ser.readline()
        if not line:
            continue

        resp = line.decode("ascii", errors="ignore").strip()
        if not resp:
            continue

        log_print(logger, f"[M114] <- {resp}")

        if ERR_RE.search(resp):
            log_print(logger, "[M114] ERROR received from firmware.", "error")
            return None

        m = M114_POS_RE.search(resp)
        if m:
            try:
                pos = {
                    "x": float(m.group("x")),
                    "y": float(m.group("y")),
                    "z": float(m.group("z")),
                    "e": float(m.group("e")),
                    "raw": resp,
                }
            except Exception:
                pos = None

        if OK_RE.match(resp) or resp.lower() == "ok" or " ok" in resp.lower():
            return pos


def wait_until_printer_at_capture_point(
    ser: serial.Serial,
    logger: logging.Logger,
    target_x: float | None,
    target_y: float | None,
    *,
    samples: int = 3,
    interval_sec: float = 0.050,
    tol_mm: float = 0.20,
    max_wait_sec: float = 10.0,
):
    """
    Poll M114 multiple times spaced by interval_sec.
    Continue only when the last `samples` polls are within tol_mm of target.
    """
    if target_x is None and target_y is None:
        return True

    def within(pos):
        if not pos:
            return False
        if target_x is not None and abs(pos["x"] - target_x) > tol_mm:
            return False
        if target_y is not None and abs(pos["y"] - target_y) > tol_mm:
            return False
        return True

    start = time.time()
    recent_ok = []

    log_print(
        logger,
        f"[M114] Waiting for capture point "
        f"(X={target_x}, Y={target_y}), tol={tol_mm}mm, samples={samples}, interval={interval_sec*1000:.0f}ms",
    )

    while (time.time() - start) <= max_wait_sec:
        pos = query_m114_position_once(ser, logger, timeout=1.0)
        ok = within(pos)

        recent_ok.append(ok)
        if len(recent_ok) > samples:
            recent_ok.pop(0)

        if len(recent_ok) == samples and all(recent_ok):
            if pos:
                log_print(logger, f"[M114] Position OK: X={pos['x']:.2f} Y={pos['y']:.2f}")
            return True

        time.sleep(interval_sec)

    log_print(logger, "[M114] Did not reach capture point within max_wait_sec.", "error")
    return False


def wait_at_start_position_and_optionally_test_shot(
    logger: logging.Logger,
    *,
    ser: serial.Serial,
    capture_worker: ContinuousCaptureWorker,
    out_dir: Path,
    m114_interval_sec: float = 0.050,
):
    """
    At M118 @POS START: wait for user commands.
      - 't' : take ONE test picture (after quick M114 burst) and keep waiting
      - 's' : start/resume the run
      - 'q' : abort
    """
    test_dir = out_dir / "start_tests"
    test_dir.mkdir(parents=True, exist_ok=True)

    log_print(
        logger,
        "[PAUSE] At start position. Type:\n"
        "  't' + ENTER  -> take a test picture\n"
        "  's' + ENTER  -> start the run\n"
        "  'q' + ENTER  -> abort"
    )

    while True:
        try:
            user = input("Command [t/s/q]: ").strip().lower()
        except (KeyboardInterrupt, EOFError):
            log_print(logger, "[PAUSE] Aborted by user.", "error")
            raise

        if user == "s":
            log_print(logger, "[PAUSE] Starting run.")
            return

        if user == "t":
            # quick M114 burst (3 samples @ 50ms) just to confirm comms / stability
            _ = wait_until_printer_at_capture_point(
                ser,
                logger,
                target_x=None,
                target_y=None,
                samples=3,
                interval_sec=m114_interval_sec,
                tol_mm=0.20,
                max_wait_sec=1.0,
            )

            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            out_path = test_dir / f"test_{ts}.png"

            ok = capture_worker.save_latest(out_path, wait_for_new=True, max_wait_sec=2.0)
            if ok:
                log_print(logger, "[PAUSE] Test shot captured. Waiting for 's' to start...")
            else:
                log_print(logger, "[PAUSE] Test shot failed. Waiting for command...", "warning")
            continue

        if user == "q":
            log_print(logger, "[PAUSE] User requested abort.", "error")
            raise KeyboardInterrupt("User aborted at start position.")

        log_print(logger, f"[PAUSE] Unknown command: {user!r} (use t/s/q).", "warning")


def main():
    # ---- CONFIG ----
    out_dir = Path("captures")
    out_dir.mkdir(parents=True, exist_ok=True)

    log_dir = out_dir / "logs"
    logger = setup_logger(log_dir, log_name_prefix="capture_run")

    pipeline_settings_json = Path("pipeline_config_3.json")
    cset_path = "camera_config_3.cset"

    # Optional settle delay after position confirmed (firmware already does M400/G4 in your G-code)
    CAPTURE_DELAY_SEC = 0.5

    # Stabilize after first M118 @POS START
    STABILIZE_DISCARD_FRAMES = 20
    STABILIZE_POST_DELAY_SEC = 0.5

    # M114 gating before capture
    M114_SAMPLES = 3
    M114_INTERVAL_SEC = 0.050
    M114_TOL_MM = 0.20
    M114_MAX_WAIT_SEC = 10.0

    # Capture worker settings
    timeout_ms = 1000

    num_frames = 1
    row = 0
    index = 0
    last_x = None
    pos_start_seen = False

    ids_peak.Library.Initialize()
    ids_peak_afl.Library.Init()

    ser = serial.Serial("COM5", 250000, timeout=0.5, write_timeout=2)
    ser.write(b"\r\n\r\n")
    time.sleep(2)
    ser.reset_input_buffer()

    dev = None
    remote = None
    stream = None
    worker = None

    try:
        dev, remote, stream = open_first_camera()

        load_camera_default_userset(remote)
        load_cset_if_provided(dev, cset_path, logger)

        autofeatures = BasicAutoFeatures(dev)
        pipeline = DefaultPipeline()
        pipeline.autofeature_module = autofeatures

        if pipeline_settings_json.exists():
            pipeline.import_settings_from_file(str(pipeline_settings_json))
            log_print(logger, f"Loaded pipeline settings: {pipeline_settings_json}")
        else:
            log_print(logger, f"Pipeline settings not found: {pipeline_settings_json} (using defaults)", "warning")

        allocate_and_queue_buffers(remote, stream)
        start_acquisition(remote, stream)

        # Start continuous capture thread (ONLY this thread touches stream buffers)
        worker = ContinuousCaptureWorker(
            data_stream=stream,
            pipeline=pipeline,
            logger=logger,
            timeout_ms=timeout_ms,
        )
        worker.start()

        # ---- ONE-TIME HOME ----
        print("\n" + "!"*40)
        print("FIRST RUN: Homing printer... Please ensure bed is clear.")
        if not send_cmd_and_wait_ok(ser, "G28", logger, timeout=60):
            log_print(logger, "Initial homing failed. Exiting.", "error")
            return
        print("!"*40 + "\n")

        while True:
            # ---- Metadata Input ----
            print("\n" + "="*40)
            board_name = input("Enter Board Name (e.g. 1-1) or 'q' to exit: ").strip()
            if board_name.lower() == 'q':
                break
            
            board_side = input("Enter Side (T/B): ").strip().upper()
            if not board_side:
                board_side = "Unknown"

            # Setup directory structure: captures/name/side
            current_board_dir = out_dir / board_name / board_side
            current_board_dir.mkdir(parents=True, exist_ok=True)
            
            # Reset counters for new board
            row = 0
            index = 0
            last_x = None
            pos_start_seen = False

            log_print(logger, f"Starting scan for Board: {board_name}, Side: {board_side}")
            log_print(logger, f"Running G-code...")

            gcode_path = Path("S3.gcode")
            with gcode_path.open("r", encoding="utf-8", errors="ignore") as f:
                for raw in f:
                    cmd = raw.split(";", 1)[0].strip()
                    if not cmd:
                        continue

                    # Row logic
                    if is_motion_cmd(cmd):
                        x = extract_x_value(cmd)
                        if x is not None:
                            if x == 0.0 and (last_x is None or last_x != 0.0):
                                row += 1
                                log_print(logger, f"[ROW] -> {row} (X reset to 0)")
                            last_x = x

                    # Wait at first M118 @POS START: allow 't' test shots until 's'
                    if (not pos_start_seen) and POS_START_RE.match(cmd):
                        pos_start_seen = True

                        wait_at_start_position_and_optionally_test_shot(
                            logger,
                            ser=ser,
                            capture_worker=worker,
                            out_dir=current_board_dir,
                            m114_interval_sec=M114_INTERVAL_SEC,
                        )

                        log_print(logger, "[CAM] Stabilizing auto-brightness...")
                        worker.request_discard(STABILIZE_DISCARD_FRAMES)
                        time.sleep(max(STABILIZE_POST_DELAY_SEC, 0.1))
                        log_print(logger, "[CAM] Resuming G-code.")

                        if not send_cmd_and_wait_ok(ser, cmd, logger, timeout=None):
                            break
                        continue

                    # Host-side capture trigger
                    mcap_cmd = CAPTURE_CMD_RE.match(cmd)
                    if mcap_cmd:
                        x_str = mcap_cmd.group("x")
                        y_str = mcap_cmd.group("y")
                        target_x = float(x_str) if x_str is not None else None
                        target_y = float(y_str) if y_str is not None else None

                        log_print(logger, f"[HOST TRIGGER] Capture: row={row}, idx={index} at (X={target_x}, Y={target_y})")

                        at_pos = wait_until_printer_at_capture_point(
                            ser, logger, target_x, target_y,
                            samples=M114_SAMPLES, interval_sec=M114_INTERVAL_SEC,
                            tol_mm=M114_TOL_MM, max_wait_sec=M114_MAX_WAIT_SEC,
                        )
                        if not at_pos:
                            log_print(logger, "Printer position check failed. Skipping capture.", "error")
                        else:
                            if CAPTURE_DELAY_SEC > 0: time.sleep(CAPTURE_DELAY_SEC)
                            
                            row_dir = current_board_dir / f"row_{row}"
                            out_path = row_dir / f"img_{index}.png"
                            ok = worker.save_latest(out_path, wait_for_new=True, max_wait_sec=2.0)
                            if not ok:
                                log_print(logger, f"Failed to save image {index}", "error")
                            index += 1

                        if not send_cmd_and_wait_ok(ser, cmd, logger, timeout=None):
                            break
                        continue

                    if not send_cmd_and_wait_ok(ser, cmd, logger, timeout=None):
                        break

            log_print(logger, f"Finished board: {board_name}")
            print("="*40)
            choice = input("Press ENTER to run next board, or type 'q' to exit: ").strip().lower()
            if choice == 'q':
                break

        log_print(logger, "Done.")

    finally:
        try:
            if worker is not None:
                worker.stop()
                worker.join(timeout=2.0)
        except Exception:
            pass

        try:
            ser.close()
            log_print(logger, "Serial port closed.")
        except Exception:
            pass

        try:
            if remote is not None and stream is not None:
                stop_acquisition(remote, stream)
        except Exception:
            pass

        ids_peak.Library.Close()
        ids_peak_afl.Library.Exit()


if __name__ == "__main__":
    main()
