#!/usr/bin/env python3
"""
Quick test to find the correct Modbus address mapping for OpenPLC %IX inputs
"""
try:
    from pymodbus.client import ModbusTcpClient
except ImportError:
    from pymodbus.client.tcp import ModbusTcpClient

import time

client = ModbusTcpClient('127.0.0.1', port=502)
if not client.connect():
    print("Failed to connect to PLC")
    exit(1)

print("Connected to PLC. Testing different Modbus address mappings...\n")

# Test different offsets for %IX0.0
test_offsets = [
    (0, "Direct coil 0"),
    (8192, "Coil offset 8192"),
    (16384, "Coil offset 16384"),
]

for offset, description in test_offsets:
    print(f"Testing {description} (address {offset})...")
    try:
        # Write 1 to %IX0.0
        result = client.write_coil(address=offset, value=True)
        time.sleep(0.2)
        
        # Read %QX0.0 (output) to see if program responded
        result = client.read_coils(address=0, count=4)
        if not result.isError():
            outputs = result.bits[:4]
            print(f"  Outputs: D3={outputs[3]}, D2={outputs[2]}, D1={outputs[1]}, D0={outputs[0]}")
        else:
            print(f"  Error reading outputs: {result}")
    except Exception as e:
        print(f"  Error: {e}")

print("\n" + "="*50)
print("Testing with holding registers...")

# Try writing to holding registers
for offset in [0, 1024, 8192]:
    print(f"Testing holding register {offset}...")
    try:
        # Write to holding register
        result = client.write_register(address=offset, value=1)
        time.sleep(0.2)
        
        # Read outputs
        result = client.read_coils(address=0, count=4)
        if not result.isError():
            outputs = result.bits[:4]
            print(f"  Outputs: D3={outputs[3]}, D2={outputs[2]}, D1={outputs[1]}, D0={outputs[0]}")
        else:
            print(f"  Error: {result}")
    except Exception as e:
        print(f"  Error: {e}")

print("\n" + "="*50)
print("Testing combined: B0=1, B1=0, B2=0, B3=0 (should give Gray 0001)")

# Try writing all 4 bits as coils at offset 8192
try:
    client.write_coil(address=8192+0, value=True)  # B0 = 1
    client.write_coil(address=8192+1, value=False) # B1 = 0
    client.write_coil(address=8192+2, value=False) # B2 = 0
    client.write_coil(address=8192+3, value=False) # B3 = 0
    time.sleep(0.2)
    
    result = client.read_coils(address=0, count=4)
    if not result.isError():
        outputs = result.bits[:4]
        print(f"Outputs: D3={outputs[3]}, D2={outputs[2]}, D1={outputs[1]}, D0={outputs[0]}")
        print(f"Expected: D3=0, D2=0, D1=0, D0=1")
    else:
        print(f"Error: {result}")
except Exception as e:
    print(f"Error: {e}")

client.close()
print("\nTest complete!")
