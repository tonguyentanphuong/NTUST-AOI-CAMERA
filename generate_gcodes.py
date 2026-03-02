def generate_gcode(filename, x_points, y_points):
    with open(filename, "w") as f:
        f.write("; Generated G-code\n")
        f.write("G21\n")
        f.write("G90\n")
        f.write("M82\n\n")
        f.write("G1 Z195 F3000\n\n")
        
        for i, y in enumerate(y_points):
            f.write(f"; ============================\n")
            f.write(f"; ROW {i+1} (Y={y})\n")
            f.write(f"; ============================\n")
            
            for j, x in enumerate(x_points):
                if j == 0:
                    f.write(f"G1 X{x} Y{y} F6000\n")
                    if i == 0:
                        f.write("M118 @POS START\n\n")
                else:
                    f.write(f"G1 X{x} F6000\n")
                
                f.write("M400\n")
                f.write("G4 P100\n")
                f.write(f"M118 @CAPTURE X{x} Y{y}\n\n")
                
        f.write("; ============================\n")
        f.write("; END\n")
        f.write("; ============================\n")
        f.write("G1 X0 Y70 F6000\n")

if __name__ == '__main__':
    # A1
    x_a1 = [0, 40, 80, 120, 160, 200]
    y_a1 = [70, 110, 150, 190, 230]
    generate_gcode("c:\\Users\\OMNI-3125HTT-ADN\\Desktop\\NTUST-AOI-CAMERA\\A1.gcode", x_a1, y_a1)
    
    # NEWLED BOARD A0
    x_newled = [0, 40, 80, 120, 160, 200, 240, 280, 320]
    y_newled = [70, 110, 150, 190]
    generate_gcode("c:\\Users\\OMNI-3125HTT-ADN\\Desktop\\NTUST-AOI-CAMERA\\NEWLED BOARD A0.gcode", x_newled, y_newled)
    print("Gcodes generated")
