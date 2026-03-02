import serial
import time
import re
import threading
import tkinter as tk
from tkinter import ttk, messagebox, scrolledtext
from pathlib import Path

# Regular expressions for serial parsing
OK_RE = re.compile(r"^\s*ok\b", re.IGNORECASE)
ERR_RE = re.compile(r"\berror\b", re.IGNORECASE)
M114_POS_RE = re.compile(r"\bX:?(?P<x>-?\d+(?:\.\d+)?)\s+Y:?(?P<y>-?\d+(?:\.\d+)?)\s+Z:?(?P<z>-?\d+(?:\.\d+)?)\s+E:?(?P<e>-?\d+(?:\.\d+)?)", re.IGNORECASE)

class TeachGCodeApp:
    def __init__(self, root):
        self.root = root
        self.root.title("Teach G-Code points UI")
        self.root.geometry("600x850")
        
        self.ser = None
        self.serial_lock = threading.Lock()
        
        self.p1 = None # Origin (X0, Y0)
        self.p2 = None # Next X (X1, Y0)
        self.p3 = None # Next Y (X0, Y1)
        self.p1_z = 195.0

        self._build_ui()

    def _build_ui(self):
        # Current Position Display (Big Header)
        pos_frame = ttk.Frame(self.root)
        pos_frame.pack(fill="x", padx=10, pady=10)
        
        ttk.Label(pos_frame, text="CURRENT POSITION", font=("Helvetica", 10, "bold")).pack()
        self.lbl_current_pos = ttk.Label(pos_frame, text="X: --.--   Y: --.--   Z: --.--", font=("Helvetica", 20, "bold"), foreground="red")
        self.lbl_current_pos.pack()

        # Connection
        conn_frame = ttk.LabelFrame(self.root, text="Connection", padding=10)
        conn_frame.pack(fill="x", padx=10, pady=5)
        
        ttk.Label(conn_frame, text="COM Port:").pack(side="left", padx=5)
        self.com_var = tk.StringVar(value="COM5")
        ttk.Entry(conn_frame, textvariable=self.com_var, width=10).pack(side="left", padx=5)
        
        self.btn_connect = ttk.Button(conn_frame, text="Connect", command=self.connect_serial)
        self.btn_connect.pack(side="left", padx=5)
        self.btn_disconnect = ttk.Button(conn_frame, text="Disconnect", command=self.disconnect_serial, state="disabled")
        self.btn_disconnect.pack(side="left", padx=5)

        # Jog Control
        jog_frame = ttk.LabelFrame(self.root, text="Manual Jog (X, Y, Z)", padding=10)
        jog_frame.pack(fill="x", padx=10, pady=5)

        home_stop_frame = ttk.Frame(jog_frame)
        home_stop_frame.pack(fill="x", pady=5)

        self.btn_home = ttk.Button(home_stop_frame, text="HOME (G28)", command=self.home_printer, state="disabled")
        self.btn_home.pack(side="left", fill="x", expand=True, padx=2)

        self.btn_stop = ttk.Button(home_stop_frame, text="STOP", command=self.emergency_stop, state="disabled")
        self.btn_stop.pack(side="left", fill="x", expand=True, padx=2)
        
        style = ttk.Style()
        style.configure("EStop.TButton", foreground="red", font=("Helvetica", 10, "bold"))
        self.btn_stop.configure(style="EStop.TButton")

        jog_ctrl = ttk.Frame(jog_frame)
        jog_ctrl.pack(fill="x", pady=5)
        
        ttk.Label(jog_ctrl, text="Step (mm):").grid(row=0, column=0, padx=5, pady=5)
        self.jog_step_var = tk.StringVar(value="10")
        ttk.Combobox(jog_ctrl, textvariable=self.jog_step_var, values=["0.1", "1", "5", "10", "50"], width=5, state="readonly").grid(row=0, column=1, padx=5, pady=5)

        self.btn_x_neg = ttk.Button(jog_ctrl, text="X -", command=lambda: self.jog("X", -1), state="disabled", width=5)
        self.btn_x_neg.grid(row=1, column=0, padx=5, pady=5)
        self.btn_x_pos = ttk.Button(jog_ctrl, text="X +", command=lambda: self.jog("X", 1), state="disabled", width=5)
        self.btn_x_pos.grid(row=1, column=1, padx=5, pady=5)

        self.btn_y_neg = ttk.Button(jog_ctrl, text="Y -", command=lambda: self.jog("Y", -1), state="disabled", width=5)
        self.btn_y_neg.grid(row=1, column=2, padx=5, pady=5)
        self.btn_y_pos = ttk.Button(jog_ctrl, text="Y +", command=lambda: self.jog("Y", 1), state="disabled", width=5)
        self.btn_y_pos.grid(row=1, column=3, padx=5, pady=5)

        self.btn_z_neg = ttk.Button(jog_ctrl, text="Z -", command=lambda: self.jog("Z", -1), state="disabled", width=5)
        self.btn_z_neg.grid(row=1, column=4, padx=5, pady=5)
        self.btn_z_pos = ttk.Button(jog_ctrl, text="Z +", command=lambda: self.jog("Z", 1), state="disabled", width=5)
        self.btn_z_pos.grid(row=1, column=5, padx=5, pady=5)

        # Retrieve Pos
        self.btn_get_pos = ttk.Button(jog_ctrl, text="Update Current Pos (M114)", command=self.get_current_pos_thread, state="disabled")
        self.btn_get_pos.grid(row=2, column=0, columnspan=6, pady=5, sticky="ew")

        # Teach Control
        teach_frame = ttk.LabelFrame(self.root, text="Teach 3 Points (20 points total: 5 X, 4 Y)", padding=10)
        teach_frame.pack(fill="x", padx=10, pady=5)

        # POINT 1
        ttk.Button(teach_frame, text="1. Set Origin (P1)", command=lambda: self.set_point(1)).grid(row=0, column=0, pady=5, sticky="ew", padx=2)
        self.lbl_p1 = ttk.Label(teach_frame, text="P1: Not Set")
        self.lbl_p1.grid(row=0, column=1, sticky="w", padx=5)
        self.btn_go_p1 = ttk.Button(teach_frame, text="Go to P1", command=lambda: self.go_to_point(1), state="disabled")
        self.btn_go_p1.grid(row=0, column=2, padx=5)

        # POINT 2
        ttk.Button(teach_frame, text="2. Set 2nd X Point (P2)", command=lambda: self.set_point(2)).grid(row=1, column=0, pady=5, sticky="ew", padx=2)
        self.lbl_p2 = ttk.Label(teach_frame, text="P2: Not Set")
        self.lbl_p2.grid(row=1, column=1, sticky="w", padx=5)
        self.btn_go_p2 = ttk.Button(teach_frame, text="Go to P2", command=lambda: self.go_to_point(2), state="disabled")
        self.btn_go_p2.grid(row=1, column=2, padx=5)

        # POINT 3
        ttk.Button(teach_frame, text="3. Set 2nd Y Point (P3)", command=lambda: self.set_point(3)).grid(row=2, column=0, pady=5, sticky="ew", padx=2)
        self.lbl_p3 = ttk.Label(teach_frame, text="P3: Not Set")
        self.lbl_p3.grid(row=2, column=1, sticky="w", padx=5)
        self.btn_go_p3 = ttk.Button(teach_frame, text="Go to P3", command=lambda: self.go_to_point(3), state="disabled")
        self.btn_go_p3.grid(row=2, column=2, padx=5)

        ttk.Label(teach_frame, text="Filename:").grid(row=3, column=0, sticky="e", pady=10)
        self.filename_var = tk.StringVar(value="TEACH_S3.gcode")
        ttk.Entry(teach_frame, textvariable=self.filename_var, width=20).grid(row=3, column=1, sticky="w")

        self.btn_gen = ttk.Button(teach_frame, text="Generate G-Code", command=self.generate_gcode)
        self.btn_gen.grid(row=4, column=0, columnspan=3, pady=10, sticky="ew")

        # Logs Frame
        log_frame = ttk.LabelFrame(self.root, text="Logs", padding=10)
        log_frame.pack(fill="both", expand=True, padx=10, pady=5)

        self.log_text = scrolledtext.ScrolledText(log_frame, state='disabled', height=10)
        self.log_text.pack(fill="both", expand=True)

    def log(self, msg):
        def _log():
            self.log_text.configure(state='normal')
            self.log_text.insert(tk.END, msg + "\n")
            self.log_text.see(tk.END)
            self.log_text.configure(state='disabled')
        self.root.after(0, _log)

    def connect_serial(self):
        com = self.com_var.get()
        try:
            self.ser = serial.Serial(com, 250000, timeout=0.5, write_timeout=2)
            self.ser.write(b"\r\n\r\n")
            self.log(f"Connected to {com}")
            self.btn_connect.configure(state="disabled")
            self.btn_disconnect.configure(state="normal")
            
            for btn in [self.btn_home, self.btn_stop, self.btn_x_pos, self.btn_x_neg, self.btn_y_pos, self.btn_y_neg, self.btn_z_pos, self.btn_z_neg, self.btn_get_pos]:
                btn.configure(state="normal")
                
            self.get_current_pos_thread()
        except Exception as e:
            self.log(f"Connection failed: {e}")

    def disconnect_serial(self):
        if self.ser and self.ser.is_open:
            self.ser.close()
            self.log("Disconnected.")
        self.btn_connect.configure(state="normal")
        self.btn_disconnect.configure(state="disabled")
        
        for btn in [self.btn_home, self.btn_stop, self.btn_x_pos, self.btn_x_neg, self.btn_y_pos, self.btn_y_neg, self.btn_z_pos, self.btn_z_neg, self.btn_get_pos]:
            btn.configure(state="disabled")

    def _send_cmd_wait(self, cmd, timeout=10.0):
        if not self.ser: return False
        self.log(f"> {cmd}")
        self.ser.write((cmd + "\n").encode())
        start = time.time()
        while time.time() - start < timeout:
            if self.ser.in_waiting:
                line = self.ser.readline().decode('ascii', errors='ignore').strip()
                if not line: continue
                if OK_RE.match(line) or "ok" in line.lower():
                    return True
                if ERR_RE.search(line):
                    self.log(f"Error from printer: {line}")
                    return False
            else:
                time.sleep(0.01)
        self.log("Timeout waiting for ok")
        return False

    def emergency_stop(self):
        self.log("!!! STOP REQUESTED !!!")
        try:
            if self.ser and self.ser.is_open:
                # Send M410 quick stop to clear firmware buffers instantly
                self.ser.write(b"M410\n")
                self.log("Sent M410 quick stop to printer.")
        except Exception as e:
            self.log(f"Error sending STOP: {e}")

    def home_printer(self):
        def _home():
            with self.serial_lock:
                self.log("Homing... (G28)")
                self._send_cmd_wait("G28", timeout=60)
                self.log("Moving up to Z=195")
                self._send_cmd_wait("G1 Z195 F3000")
                self.log("Homing complete.")
            self.get_current_pos()
        threading.Thread(target=_home, daemon=True).start()

    def jog(self, axis, dir):
        try: step = float(self.jog_step_var.get())
        except: step = 10.0
        val = step * dir
        def _jog():
            with self.serial_lock:
                self._send_cmd_wait("G91")
                self._send_cmd_wait(f"G1 {axis}{val} F6000")
                self._send_cmd_wait("G90")
            self.get_current_pos()
        threading.Thread(target=_jog, daemon=True).start()

    def get_current_pos_thread(self):
        threading.Thread(target=self.get_current_pos, daemon=True).start()

    def get_current_pos(self):
        if not self.ser: return
        with self.serial_lock:
            # Clear old buffer (such as leftover 'ok's) before querying to prevent early loop breaks
            self.ser.reset_input_buffer()
            self.ser.write(b"M400\n") 
            self.ser.write(b"M114\n")
            start = time.time()
            pos_found = False
            while time.time() - start < 3.0:
                if self.ser.in_waiting:
                    line = self.ser.readline().decode('ascii', errors='ignore').strip()
                    if not line: continue
                    m = M114_POS_RE.search(line)
                    if m:
                        x = float(m.group("x"))
                        y = float(m.group("y"))
                        z = float(m.group("z"))
                        
                        # Cache the vars in lambda to prevent cross-thread issues
                        self.root.after(0, lambda cx=x, cy=y, cz=z: self.lbl_current_pos.configure(text=f"X: {cx:>6.2f}   Y: {cy:>6.2f}   Z: {cz:>6.2f}"))
                        self.current_m114 = (x, y, z)
                        pos_found = True
                        
                    if OK_RE.match(line) or "ok" in line.lower():
                        if pos_found:
                            break
                        # If pos_found is false, keep waiting since this 'ok' might be from M400
                else:
                    time.sleep(0.01)

    def go_to_point(self, pt_num):
        pt = None
        if pt_num == 1: pt = self.p1
        elif pt_num == 2: pt = self.p2
        elif pt_num == 3: pt = self.p3
        
        if not pt: return
        
        def _go():
            with self.serial_lock:
                self.log(f"Moving to P{pt_num} (X{pt[0]:.2f} Y{pt[1]:.2f})")
                self._send_cmd_wait("G90")
                self._send_cmd_wait(f"G1 X{pt[0]:.2f} Y{pt[1]:.2f} F6000")
            self.get_current_pos()
        
        threading.Thread(target=_go, daemon=True).start()

    def set_point(self, pt_num):
        if not hasattr(self, 'current_m114'):
            messagebox.showerror("Error", "No valid position Data yet. Please connect and press Update Current Pos.")
            return
        x, y, z = self.current_m114

        if pt_num == 1:
            self.p1 = (x, y)
            self.p1_z = z
            self.lbl_p1.configure(text=f"P1: X={x:.2f}, Y={y:.2f}")
            self.btn_go_p1.configure(state="normal")
            self.log(f"Set P1 to {self.p1}")
        elif pt_num == 2:
            self.p2 = (x, y)
            self.lbl_p2.configure(text=f"P2: X={x:.2f}, Y={y:.2f} (X-Step)")
            self.btn_go_p2.configure(state="normal")
            self.log(f"Set P2 to {self.p2}")
        elif pt_num == 3:
            self.p3 = (x, y)
            self.lbl_p3.configure(text=f"P3: X={x:.2f}, Y={y:.2f} (Y-Step)")
            self.btn_go_p3.configure(state="normal")
            self.log(f"Set P3 to {self.p3}")

    def generate_gcode(self):
        if not self.p1 or not self.p2 or not self.p3:
            messagebox.showerror("Error", "Please record all 3 points first.")
            return
            
        x0, y0 = self.p1
        x1, y1_dummy = self.p2
        x2_dummy, y1 = self.p3
        
        step_x = x1 - x0
        step_y = y1 - y0
        
        if step_x == 0 or step_y == 0:
            messagebox.showerror("Error", "Step size is 0. Did you move the carriage between points?")
            return

        cols = 5
        rows = 4

        filename = self.filename_var.get()
        path = Path(filename)
        
        try:
            with path.open("w", encoding="utf-8") as f:
                f.write("; Generated TEACHING G-code\n")
                f.write("G21\n")
                f.write("G90\n")
                f.write("M82\n\n")
                f.write(f"G1 Z{self.p1_z:.2f} F3000\n\n")
                
                for r in range(rows):
                    y_target = y0 + r * step_y
                    f.write(f"; ============================\n")
                    f.write(f"; ROW {r+1} (Y={y_target:.2f})\n")
                    f.write(f"; ============================\n")
                    
                    for c in range(cols):
                        x_target = x0 + c * step_x
                        
                        if c == 0:
                            f.write(f"G1 X{x_target:.2f} Y{y_target:.2f} F6000\n")
                            if r == 0:
                                f.write("M118 @POS START\n\n")
                        else:
                            f.write(f"G1 X{x_target:.2f} F6000\n")
                            
                        f.write("M400\n")
                        f.write("G4 P100\n")
                        f.write(f"M118 @CAPTURE X{x_target:.2f} Y{y_target:.2f}\n\n")
                        
                f.write("; ============================\n")
                f.write("; END\n")
                f.write("; ============================\n")
                f.write(f"G1 X{x0:.2f} Y{y0:.2f} F6000\n")
                
            self.log(f"Successfully generated 20 points into {path.absolute()}")
            messagebox.showinfo("Success", f"GCode saved to {filename}")
        except Exception as e:
            messagebox.showerror("Error saving code", str(e))

if __name__ == "__main__":
    root = tk.Tk()
    app = TeachGCodeApp(root)
    root.mainloop()
