import time
import sys

# --- VERSION AGNOSTIC IMPORTS ---
try:
    # Most recent Pymodbus (3.5+)
    from pymodbus.client import ModbusTcpClient
except ImportError:
    try:
        # Pymodbus 3.0 - 3.4
        from pymodbus.client.tcp import ModbusTcpClient
    except ImportError:
        try:
            # Pymodbus 2.x
            from pymodbus.client.sync import ModbusTcpClient
        except ImportError:
            print("Error: pymodbus is not installed. Run: pip install pymodbus")
            sys.exit(1)

# --- TEST CONFIGURATION ---
PLC_IP = '127.0.0.1'
PORT = 502  # Default OpenPLC Modbus Port

def run_test():
    client = ModbusTcpClient(PLC_IP, port=PORT)
    
    print(f"--- Connecting to OpenPLC at {PLC_IP}:{PORT} ---")
    if not client.connect():
        print("FAIL: Could not connect. Is OpenPLC Running?")
        return

    print("Checking %QX0.0 (Blinker) for 5 seconds...")
    states = []
    
    for i in range(25): # 25 samples * 0.2s = 5 seconds
        # OpenPLC registers start at 0
        result = client.read_coils(0, 1, slave=1)
        
        if not result.isError():
            state = result.bits[0]
            states.append(state)
            # Simple visual toggle in console
            print(" [ ON ] " if state else " [ OFF ] ", end="\r")
        else:
            print(f"\nRead Error: {result}")
            break
            
        time.sleep(0.2)

    # Analyze results
    changes = sum(1 for i in range(1, len(states)) if states[i] != states[i-1])
    
    print(f"\n\nTest Finished.")
    print(f"Detected {changes} state transitions.")

    if changes >= 4:
        print("✅ RESULT: PASS (Blinker logic is active)")
    else:
        print("❌ RESULT: FAIL (Output did not toggle enough times)")

    client.close()

if __name__ == "__main__":
    run_test()