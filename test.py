import serial

PORT = "COM9"        
BYTEORDER = "big"    # change to "big" if values look wrong

ser = serial.Serial(PORT, 115200, timeout=1)
buf = bytearray()

print(f"Listening on {PORT}...")

try:
    while True:
        buf += ser.read(ser.in_waiting or 1)

        # Expected packet:
        # 50 00 00 44 40 [64 data bytes] SUM XOR 0D 0A
        while len(buf) >= 5:
            if buf[0] != 0x50:
                del buf[0]
                continue

            n = buf[4]
            frame_len = 5 + n + 4

            if len(buf) < frame_len:
                break

            frame = bytes(buf[:frame_len])
            del buf[:frame_len]

            if frame[-2:] != b"\r\n":
                continue

            command = frame[3]
            data = frame[5 : 5 + n]

            if command != 0x44 or n != 64:
                continue

            u16 = lambda i: int.from_bytes(
                data[i : i + 2], BYTEORDER
            )

            stable = bool(data[0])
            system_status = u16(2)
            driver_status = data[4]

            currents = [
                u16(7) / 100,
                u16(14) / 100,
                u16(21) / 100,
            ]

            pd_values = [
                u16(28),
                u16(30),
                u16(32),
                u16(34),
            ]

            pd_status = list(data[36:40])

            temperatures = [
                u16(42) / 100,
                u16(44) / 100,
                u16(46) / 100,
                u16(48) / 100,
                u16(50) / 100,
            ]

            checksum_data = frame[1:-4]
            sum_ok = frame[-4] == sum(checksum_data) & 0xFF

            xor = 0
            for x in checksum_data:
                xor ^= x
            xor_ok = frame[-3] == xor

            print("\n0x44 status")
            print("checksum:", sum_ok and xor_ok)
            print("power stabilization:", stable)
            print(f"system status: 0x{system_status:04X}")
            print(f"driver status: 0x{driver_status:02X}")
            print("currents [A]:", currents)
            print("PD values:", pd_values)
            print("PD status:", [f"0x{x:02X}" for x in pd_status])
            print("temperatures [C]:", temperatures)

except KeyboardInterrupt:
    pass
finally:
    ser.close()