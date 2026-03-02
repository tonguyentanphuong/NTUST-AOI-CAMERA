# -*- coding: utf-8 -*-
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

import tkinter as tk
from tkinter import ttk, messagebox, scrolledtext

# -----------------------------
# Logging helpers
# -----------------------------
class TkinterHandler(logging.Handler):
    def __init__(self, text_widget):
        super().__init__()
        self.text_widget = text_widget

    def emit(self, record):
        msg = self.format(record)
        def append():
            self.text_widget.configure(state='normal')
            self.text_widget.insert(tk.END, msg + '\n')
            self.text_widget.see(tk.END)
            self.text_widget.configure(state='disabled')
        # Schedule the append operation on the main GUI thread
        self.text_widget.after(0, append)

def setup_logger(log_dir: Path, log_name_prefix: str = "run", text_widget=None) -> logging.Logger:
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

    if text_widget:
        th = TkinterHandler(text_widget)
        th.setLevel(logging.INFO)
        th.setFormatter(logging.Formatter("%(message)s"))
        logger.addHandler(th)

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

class ContinuousCaptureWorker(threading.Thread):
    def __init__(self, *, data_stream, pipeline, logger: logging.Logger, timeout_ms: int = 1000, name: str = "ContinuousCaptureWorker"):
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
            except Exception:
                continue
            try:
                if buf.IsIncomplete():
                    self.data_stream.QueueBuffer(buf)
                    continue
                view = buf.ToImageView()
                processed = self.pipeline.process(view)
                self.data_stream.QueueBuffer(buf)
                with self._cond:
                    if self._discard_remaining > 0:
                        self._discard_remaining -= 1
                    else:
                        self._latest_img = processed
                        self._latest_ts = time.time()
                        self._cond.notify_all()
            except Exception as e:
                try: self.data_stream.QueueBuffer(buf)
                except Exception: pass
                log_print(self.logger, f"[CAM] Unexpected worker error: {e}", "error")
                continue
        log_print(self.logger, "[CAM] Continuous capture thread exiting.")

# -----------------------------
# Serial / G-code helpers
# -----------------------------
OK_RE = re.compile(r"^\s*ok\b", re.IGNORECASE)
ERR_RE = re.compile(r"\berror\b", re.IGNORECASE)
X_RE = re.compile(r"\bX(-?\d+(?:\.\d+)?)\b", re.IGNORECASE)
CAPTURE_CMD_RE = re.compile(r"^\s*M118\s+@CAPTURE\b(?:\s+X(?P<x>-?\d+(?:\.\d+)?))?(?:\s+Y(?P<y>-?\d+(?:\.\d+)?))?\s*$", re.IGNORECASE)
POS_START_RE = re.compile(r"^\s*M118\s+@POS\s+START\b", re.IGNORECASE)
M114_POS_RE = re.compile(r"\bX:(?P<x>-?\d+(?:\.\d+)?)\s+Y:(?P<y>-?\d+(?:\.\d+)?)\s+Z:(?P<z>-?\d+(?:\.\d+)?)\s+E:(?P<e>-?\d+(?:\.\d+)?)", re.IGNORECASE)

def load_cset_if_provided(device, cset_path: str, logger: logging.Logger):
    if not cset_path: return
    p = Path(cset_path).expanduser().resolve()
    if not p.exists(): return
    remote_nodemap = device.RemoteDevice().NodeMaps()[0]
    remote_nodemap.LoadFromFile(str(p))
    log_print(logger, f"Loaded camera settings: {p}")

def is_motion_cmd(cmd: str) -> bool:
    return cmd.startswith(("G0", "G1"))

def extract_x_value(cmd: str):
    m = X_RE.search(cmd)
    return float(m.group(1)) if m else None

def send_cmd_and_wait_ok(ser: serial.Serial, cmd: str, logger: logging.Logger, check_abort, *, timeout=None) -> bool:
    if check_abort(): return False
    log_print(logger, f"Sending: {cmd}")
    ser.write((cmd + "\n").encode("ascii", errors="ignore"))
    start = time.time()
    while True:
        if check_abort(): return False
        if timeout is not None and (time.time() - start) > timeout:
            log_print(logger, "ERROR: Timeout waiting for firmware response.", "error")
            return False
        if ser.in_waiting > 0:
            line = ser.readline()
            if not line: continue
            resp = line.decode("ascii", errors="ignore").strip()
            if not resp: continue
            if ERR_RE.search(resp):
                log_print(logger, "ERROR received from firmware.", "error")
                return False
            if OK_RE.match(resp) or resp.lower() == "ok" or " ok" in resp.lower():
                return True
        else:
            time.sleep(0.01)

def query_m114_position_once(ser: serial.Serial, logger: logging.Logger, check_abort, *, timeout: float = 3.0):
    if check_abort(): return None
    ser.write(b"M114\n")
    start = time.time()
    pos = None
    while True:
        if check_abort(): return None
        if (time.time() - start) > timeout:
            return pos
        if ser.in_waiting > 0:
            line = ser.readline()
            if not line: continue
            resp = line.decode("ascii", errors="ignore").strip()
            if not resp: continue
            if ERR_RE.search(resp): return None
            m = M114_POS_RE.search(resp)
            if m:
                try: pos = {"x": float(m.group("x")), "y": float(m.group("y"))}
                except Exception: pass
            if OK_RE.match(resp) or resp.lower() == "ok" or " ok" in resp.lower():
                return pos
        else:
            time.sleep(0.01)

def wait_until_printer_at_capture_point(ser: serial.Serial, logger: logging.Logger, check_abort, target_x: float, target_y: float, *, samples: int = 3, interval_sec: float = 0.050, tol_mm: float = 0.20, max_wait_sec: float = 10.0):
    if target_x is None and target_y is None: return True
    def within(pos):
        if not pos: return False
        if target_x is not None and abs(pos["x"] - target_x) > tol_mm: return False
        if target_y is not None and abs(pos["y"] - target_y) > tol_mm: return False
        return True

    start = time.time()
    recent_ok = []
    log_print(logger, f"[M114] Waiting: X={target_x}, Y={target_y}")
    while (time.time() - start) <= max_wait_sec:
        if check_abort(): return False
        pos = query_m114_position_once(ser, logger, check_abort, timeout=1.0)
        ok = within(pos)
        recent_ok.append(ok)
        if len(recent_ok) > samples: recent_ok.pop(0)
        if len(recent_ok) == samples and all(recent_ok):
            if pos: log_print(logger, f"[M114] Position OK: X={pos['x']:.2f} Y={pos['y']:.2f}")
            return True
        time.sleep(interval_sec)
    
    log_print(logger, "[M114] Did not reach capture point.", "error")
    return False

# -----------------------------
# GUI App
# -----------------------------
class CameraScannerApp:
    def __init__(self, root):
        self.root = root
        self.root.title("AOI Camera Scanner UI")
        self.root.geometry("800x750")
        
        # State variables
        self.scan_state = "IDLE"  # IDLE, RUNNING, PAUSED
        self.abort_flag = False
        self.worker_thread = None
        
        # Hardware Handles
        self.ser = None
        self.dev = None
        self.remote = None
        self.stream = None
        self.camera_worker = None
        self.hw_initialized = False

        self.out_dir = Path("captures")
        self.out_dir.mkdir(parents=True, exist_ok=True)
        
        self._build_ui()
        self.logger = setup_logger(self.out_dir / "logs", "capture_run_ui", self.log_text)
        
        # Connect to hardware on startup
        threading.Thread(target=self.init_hardware, daemon=True).start()

    def _build_ui(self):
        # Top Config Frame
        config_frame = ttk.LabelFrame(self.root, text="Configuration", padding=10)
        config_frame.pack(fill="x", padx=10, pady=5)

        ttk.Label(config_frame, text="Board Model:").grid(row=0, column=0, sticky="w", pady=2)
        self.model_var = tk.StringVar(value="A0")
        model_cb = ttk.Combobox(config_frame, textvariable=self.model_var, values=["A0", "A1", "NEWLED BOARD A0"], state="readonly")
        model_cb.grid(row=0, column=1, sticky="ew", pady=2)

        ttk.Label(config_frame, text="Board Name (e.g. 1-1):").grid(row=1, column=0, sticky="w", pady=2)
        self.name_var = tk.StringVar(value="1-1")
        ttk.Entry(config_frame, textvariable=self.name_var).grid(row=1, column=1, sticky="ew", pady=2)

        ttk.Label(config_frame, text="Side:").grid(row=2, column=0, sticky="w", pady=2)
        self.side_var = tk.StringVar(value="T")
        side_cb = ttk.Combobox(config_frame, textvariable=self.side_var, values=["T", "B"], state="readonly")
        side_cb.grid(row=2, column=1, sticky="ew", pady=2)

        config_frame.columnconfigure(1, weight=1)

        # Manual Jog Frame
        jog_frame = ttk.LabelFrame(self.root, text="Manual Jog (XY)", padding=10)
        jog_frame.pack(fill="x", padx=10, pady=5)

        ttk.Label(jog_frame, text="Step (mm):").pack(side="left", padx=5)
        self.jog_step_var = tk.StringVar(value="10")
        step_cb = ttk.Combobox(jog_frame, textvariable=self.jog_step_var, values=["0.1", "1", "5", "10", "50"], width=5, state="readonly")
        step_cb.pack(side="left", padx=5)

        self.btn_x_neg = ttk.Button(jog_frame, text="X -", command=lambda: self.gui_jog("X", -1), width=5)
        self.btn_x_neg.pack(side="left", padx=10)

        self.btn_x_pos = ttk.Button(jog_frame, text="X +", command=lambda: self.gui_jog("X", 1), width=5)
        self.btn_x_pos.pack(side="left", padx=2)

        self.btn_y_pos = ttk.Button(jog_frame, text="Y +", command=lambda: self.gui_jog("Y", 1), width=5)
        self.btn_y_pos.pack(side="left", padx=20)

        self.btn_y_neg = ttk.Button(jog_frame, text="Y -", command=lambda: self.gui_jog("Y", -1), width=5)
        self.btn_y_neg.pack(side="left", padx=2)

        # Control Panel Frame
        ctrl_frame = ttk.LabelFrame(self.root, text="Controls", padding=10)
        ctrl_frame.pack(fill="x", padx=10, pady=5)

        self.home_btn = ttk.Button(ctrl_frame, text="Home Printer (G28)", command=self.gui_home_printer)
        self.home_btn.pack(side="left", expand=True, fill="x", padx=5)

        self.start_btn = ttk.Button(ctrl_frame, text="Start Scan", command=self.gui_start_pause_continue)
        self.start_btn.pack(side="left", expand=True, fill="x", padx=5)

        self.stop_btn = ttk.Button(ctrl_frame, text="STOP", command=self.gui_emergency_stop)
        self.stop_btn.pack(side="left", expand=True, fill="x", padx=5)
        
        # Styling for buttons
        style = ttk.Style()
        style.configure("EStop.TButton", foreground="red", font=("Helvetica", 10, "bold"))
        self.stop_btn.configure(style="EStop.TButton")
        self.home_btn.configure(state="disabled")
        self.start_btn.configure(state="disabled")

        # Logs Frame
        log_frame = ttk.LabelFrame(self.root, text="Logs & Output", padding=10)
        log_frame.pack(fill="both", expand=True, padx=10, pady=5)

        self.log_text = scrolledtext.ScrolledText(log_frame, state='disabled', height=15)
        self.log_text.pack(fill="both", expand=True)

    def init_hardware(self):
        """Initializes the Serial Port and Camera exactly once at application startup."""
        try:
            log_print(self.logger, "Initializing Serial COM5...")
            self.ser = serial.Serial("COM5", 250000, timeout=0.5, write_timeout=2)
            self.ser.write(b"\r\n\r\n")
            time.sleep(2)
            self.ser.reset_input_buffer()

            log_print(self.logger, "Initializing Camera...")
            ids_peak.Library.Initialize()
            ids_peak_afl.Library.Init()
            
            self.dev, self.remote, self.stream = open_first_camera()
            load_camera_default_userset(self.remote)
            load_cset_if_provided(self.dev, "camera_config_3.cset", self.logger)

            pipeline = DefaultPipeline()
            pipeline.autofeature_module = BasicAutoFeatures(self.dev)
            if Path("pipeline_config_3.json").exists():
                pipeline.import_settings_from_file("pipeline_config_3.json")

            allocate_and_queue_buffers(self.remote, self.stream)
            start_acquisition(self.remote, self.stream)

            self.camera_worker = ContinuousCaptureWorker(data_stream=self.stream, pipeline=pipeline, logger=self.logger)
            self.camera_worker.start()

            self.hw_initialized = True
            log_print(self.logger, "Hardware Initialization Complete.")
            
            # Enable buttons via main thread
            self.root.after(0, lambda: self.home_btn.configure(state="normal"))
            self.root.after(0, lambda: self.start_btn.configure(state="normal"))

        except Exception as e:
            log_print(self.logger, f"Failed to initialize hardware: {e}", "error")

    def check_abort(self):
        return self.abort_flag

    def gui_home_printer(self):
        if not self.hw_initialized or self.scan_state != "IDLE":
            log_print(self.logger, "Cannot home while scan is running or hardware is offline.", "warning")
            return
            
        self.abort_flag = False
            
        def _home():
            self.root.after(0, lambda: self.home_btn.configure(state="disabled"))
            self.root.after(0, lambda: self.start_btn.configure(state="disabled"))
            log_print(self.logger, "Homing printer... Please wait.")
            success = send_cmd_and_wait_ok(self.ser, "G28", self.logger, self.check_abort, timeout=60)
            if success:
                log_print(self.logger, "Printer homed successfully.")
                log_print(self.logger, "Moving to start position (X0 Y70 Z195)...")
                send_cmd_and_wait_ok(self.ser, "G1 Z195 F3000", self.logger, self.check_abort)
                send_cmd_and_wait_ok(self.ser, "G1 X0 Y70 F6000", self.logger, self.check_abort)
            else:
                log_print(self.logger, "Homing failed or aborted.", "error")
            self.root.after(0, lambda: self.home_btn.configure(state="normal"))
            self.root.after(0, lambda: self.start_btn.configure(state="normal"))
            
        threading.Thread(target=_home, daemon=True).start()

    def gui_jog(self, axis, direction):
        if not self.hw_initialized or self.scan_state != "IDLE":
            log_print(self.logger, "Cannot jog while scan is running or hardware is offline.", "warning")
            return
            
        try:
            step = float(self.jog_step_var.get())
        except ValueError:
            step = 10.0
            
        val = step * direction
        
        self.abort_flag = False
        
        def _jog():
            self.root.after(0, lambda: self.home_btn.configure(state="disabled"))
            self.root.after(0, lambda: self.start_btn.configure(state="disabled"))
            
            # Use relative positioning (G91) to jog, then switch back to absolute (G90)
            cmd1 = "G91"
            cmd2 = f"G1 {axis}{val} F6000"
            cmd3 = "G90"
            
            log_print(self.logger, f"Jogging {axis} by {val}mm...")
            send_cmd_and_wait_ok(self.ser, cmd1, self.logger, self.check_abort)
            send_cmd_and_wait_ok(self.ser, cmd2, self.logger, self.check_abort)
            send_cmd_and_wait_ok(self.ser, cmd3, self.logger, self.check_abort)
            
            self.root.after(0, lambda: self.home_btn.configure(state="normal"))
            self.root.after(0, lambda: self.start_btn.configure(state="normal"))
            
        threading.Thread(target=_jog, daemon=True).start()

    def gui_start_pause_continue(self):
        if not self.hw_initialized:
            log_print(self.logger, "Cannot start scan, hardware not initialized.", "error")
            return
            
        if self.scan_state == "IDLE":
            model = self.model_var.get().strip()
            name = self.name_var.get().strip()
            side = self.side_var.get().strip()

            if not name:
                messagebox.showerror("Error", "Please enter a Board Name.")
                return

            self.scan_state = "RUNNING"
            self.abort_flag = False
            self.start_btn.configure(text="Pause")
            self.home_btn.configure(state="disabled")
            
            self.worker_thread = threading.Thread(target=self.run_scan, args=(model, name, side), daemon=True)
            self.worker_thread.start()
            
        elif self.scan_state == "RUNNING":
            self.scan_state = "PAUSED"
            self.start_btn.configure(text="Continue")
            log_print(self.logger, "Scan paused by user. Press 'Continue' to resume.")
            
        elif self.scan_state == "PAUSED":
            self.scan_state = "RUNNING"
            self.start_btn.configure(text="Pause")
            log_print(self.logger, "Resuming scan...")

    def gui_finish_scan(self):
        self.scan_state = "IDLE"
        self.start_btn.configure(text="Start Scan")
        self.home_btn.configure(state="normal")

    def gui_emergency_stop(self):
        log_print(self.logger, "!!! STOP REQUESTED !!!", "error")
        self.abort_flag = True
        
        # Reset state back to IDLE
        self.gui_finish_scan()
        
        # Send instant stop to printer if possible (M410 clears buffer)
        try:
            if self.ser and self.ser.is_open:
                self.ser.write(b"M410\n")
                log_print(self.logger, "Sent M410 quick stop to printer.")
        except Exception:
            pass

    def run_scan(self, board_model, board_name, board_side):
        try:
            model_upper = board_model.upper()
            if model_upper == "A0": gcode_filename = "S3.gcode"
            elif model_upper == "A1": gcode_filename = "A1.gcode"
            elif model_upper == "NEWLED BOARD A0": gcode_filename = "NEWLED BOARD A0.gcode"
            else: gcode_filename = "S3.gcode"

            gcode_path = Path(gcode_filename)
            if not gcode_path.exists():
                log_print(self.logger, f"G-code file not found: {gcode_filename}", "error")
                self.gui_emergency_stop()
                return

            current_board_dir = self.out_dir / board_model / board_name / board_side
            current_board_dir.mkdir(parents=True, exist_ok=True)
            
            row, index = 0, 0
            last_x = None
            pos_start_seen = False

            log_print(self.logger, f"Scan started from line 1 - Model: {board_model}, Board: {board_name}, Side: {board_side}")
            
            with gcode_path.open("r", encoding="utf-8", errors="ignore") as f:
                for raw in f:
                    # Implement Pause Wait Loop
                    while self.scan_state == "PAUSED" and not self.abort_flag:
                        time.sleep(0.1)

                    if self.check_abort():
                        log_print(self.logger, "Scan was aborted.", "error")
                        break
                        
                    cmd = raw.split(";", 1)[0].strip()
                    if not cmd: continue

                    if is_motion_cmd(cmd):
                        x = extract_x_value(cmd)
                        if x is not None:
                            if x == 0.0 and (last_x is None or last_x != 0.0):
                                row += 1
                                log_print(self.logger, f"[ROW] -> {row} (X reset to 0)")
                            last_x = x

                    if (not pos_start_seen) and POS_START_RE.match(cmd):
                        pos_start_seen = True
                        log_print(self.logger, "Reached START POS. Stabilizing...")
                        self.camera_worker.request_discard(20)
                        
                        # We must respect pause/abort during this delay too
                        for _ in range(5): 
                            if self.check_abort(): break
                            time.sleep(0.1)
                            
                        if not send_cmd_and_wait_ok(self.ser, cmd, self.logger, self.check_abort): break
                        continue

                    mcap_cmd = CAPTURE_CMD_RE.match(cmd)
                    if mcap_cmd:
                        x_str, y_str = mcap_cmd.group("x"), mcap_cmd.group("y")
                        target_x = float(x_str) if x_str is not None else None
                        target_y = float(y_str) if y_str is not None else None

                        at_pos = wait_until_printer_at_capture_point(
                            self.ser, self.logger, self.check_abort, target_x, target_y
                        )
                        
                        if not at_pos:
                            log_print(self.logger, "Skipping capture (failed to verify pos or aborted).", "error")
                        else:
                            # wait for capture delay, breaking if aborted
                            for _ in range(5):
                                if self.check_abort(): break
                                time.sleep(0.1)

                            # If paused mid-move, wait here before capturing!
                            while self.scan_state == "PAUSED" and not self.check_abort():
                                time.sleep(0.1)

                            if self.check_abort(): break

                            row_dir = current_board_dir / f"row_{row}"
                            out_path = row_dir / f"img_{index}.png"
                            if self.camera_worker.save_latest(out_path):
                                index += 1
                                log_print(self.logger, f"Captured >> {out_path.name}")
                        
                        if not send_cmd_and_wait_ok(self.ser, cmd, self.logger, self.check_abort): break
                        continue

                    # Regular command Execution
                    if not send_cmd_and_wait_ok(self.ser, cmd, self.logger, self.check_abort): break

            if not self.abort_flag:
                log_print(self.logger, "Scan completed successfully.")
                
        except Exception as e:
            log_print(self.logger, f"Error during scan: {e}", "error")
            import traceback
            log_print(self.logger, traceback.format_exc(), "error")
        finally:
            if not self.abort_flag:
                self.root.after(0, self.gui_finish_scan)

    def on_close(self):
        log_print(self.logger, "Shutting down application...")
        self.abort_flag = True
        if self.worker_thread and self.worker_thread.is_alive():
            self.worker_thread.join(timeout=1.0)
            
        if self.hw_initialized:
            if self.camera_worker: 
                self.camera_worker.stop()
                self.camera_worker.join(timeout=1.0)
            if self.ser and self.ser.is_open:
                self.ser.close()
            if self.remote and self.stream:
                stop_acquisition(self.remote, self.stream)
            try:
                ids_peak.Library.Close()
                ids_peak_afl.Library.Exit()
            except: pass
            
        self.root.destroy()

if __name__ == "__main__":
    root = tk.Tk()
    app = CameraScannerApp(root)
    root.protocol("WM_DELETE_WINDOW", app.on_close)
    root.mainloop()

