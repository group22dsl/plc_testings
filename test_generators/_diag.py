"""Quick Modbus diagnostic — run while OpenPLC is active."""
from pymodbus.client import ModbusTcpClient
import inspect, time

client = ModbusTcpClient('127.0.0.1', port=502)
assert client.connect(), "Cannot connect"

# Detect correct unit-id keyword (slave / device_id / unit)
sig = inspect.signature(ModbusTcpClient.read_coils)
params = list(sig.parameters.keys())
unit_kw = 'device_id' if 'device_id' in params else ('slave' if 'slave' in params else 'unit')
kw = {unit_kw: 1}
print(f"Using unit keyword: {unit_kw!r}")

# --- 1. Write %QX1.0 (coil 8) = 1, wait, read it back + read output coil 0
print("=== %QX1.x write test ===")
client.write_coil(8, True, **kw)
time.sleep(0.3)
r = client.read_coils(8, count=3, **kw)
print(f"Coil 8-10 readback (should be [True,False,False]): {r.bits[:3]}")
r0 = client.read_coils(0, count=1, **kw)
print(f"Coil 0 tx_X_trip (should be 1 if PLC reloaded): {r0.bits[0]}")

# Reset
client.write_coil(8, False, **kw)
time.sleep(0.1)

# --- 2. Write %QW1 (HR 1) = 0xFF38 (=-200, way out of range), wait, read back + output
print("\n=== %QW1 write test ===")
val = (-200) & 0xFFFF   # 65336
client.write_register(1, val, **kw)
time.sleep(0.3)
r = client.read_holding_registers(1, count=1, **kw)
raw = r.registers[0]
signed = raw - 65536 if raw > 32767 else raw
print(f"HR1 readback raw={raw}, signed={signed}  (should be -200 if write stuck)")
r0 = client.read_coils(0, count=1, **kw)
print(f"Coil 0 tx_X_trip (should be 1 if PLC reloaded and -200 is out of range): {r0.bits[0]}")

# Reset HR1 to 0
client.write_register(1, 0, **kw)
client.close()
print("\nDone.")
