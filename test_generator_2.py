"""
OpenPLC Sequential Test Generator
==================================
A simplified test runner that reads headerless CSV files and maps columns
sequentially to PLC program inputs and outputs.

CSV Format:
  - NO HEADERS
  - Columns map sequentially to inputs, last column is the expected output
  - Example: 100,1,1,0,1
    * Column 1: Value for first input (A1) = 100
    * Column 2: Value for second input (A2) = 1
    * Column 3: Value for third input (A3) = 1
    * Column 4: Value for fourth input (A4) = 0
    * Column 5: Expected output (B1) = 1

Usage:
  python test_generator_2.py -f test_cases.csv -i %IX0.0,%IX0.1,%IX0.2,%IX0.3 -o %QX0.0
  python test_generator_2.py -f test_cases.csv -i %IW0,%IW1 -o %QW0
  
The script will:
  1. Read the CSV file (no headers)
  2. Map columns sequentially to the provided input addresses
  3. Use the last column as the expected output value
  4. Write inputs, wait, read output, and compare
"""

import pandas as pd
import time
import sys
import re
import os
import argparse
from datetime import datetime

# --- VERSION-PROOF PYMODBUS IMPORT ---
try:
    from pymodbus.client import ModbusTcpClient
except ImportError:
    try:
        from pymodbus.client.tcp import ModbusTcpClient
    except ImportError:
        try:
            from pymodbus.client.sync import ModbusTcpClient
        except ImportError:
            print("Error: pymodbus is not installed. Run: pip install pymodbus")
            sys.exit(1)

# --- CONFIGURATION ---
DEFAULT_PLC_IP = '127.0.0.1'
DEFAULT_PORT = 502
DEFAULT_UNIT_ID = 1
DEFAULT_DELAY_MS = 100

# =====================================================================
# Address Parsing
# =====================================================================

RE_BIT_ADDR = re.compile(r'%([IQ])X(\d+)\.(\d+)')   # e.g. %IX0.0, %QX0.1
RE_WORD_ADDR = re.compile(r'%([IQ])W(\d+)')          # e.g. %IW0, %QW3


def parse_plc_address(addr_str):
    """
    Parse a PLC address string.
    Returns: { 'direction': 'I'|'Q', 'type': 'X'|'W', 'modbus_addr': int, 'raw': str }
    """
    addr_str = addr_str.strip()
    
    # Try bit address
    m = RE_BIT_ADDR.match(addr_str)
    if m:
        direction = m.group(1)
        byte_num = int(m.group(2))
        bit_num = int(m.group(3))
        modbus_addr = byte_num * 8 + bit_num
        return {
            'direction': direction,
            'type': 'X',
            'modbus_addr': modbus_addr,
            'raw': addr_str,
        }
    
    # Try word address
    m = RE_WORD_ADDR.match(addr_str)
    if m:
        direction = m.group(1)
        index = int(m.group(2))
        return {
            'direction': direction,
            'type': 'W',
            'modbus_addr': index,
            'raw': addr_str,
        }
    
    raise ValueError(f"Invalid PLC address format: {addr_str}")


# =====================================================================
# Modbus Helpers
# =====================================================================

def _detect_unit_kwarg():
    """Detect the correct keyword for unit/slave ID across pymodbus versions."""
    import inspect
    try:
        sig = inspect.signature(ModbusTcpClient.read_coils)
        params = list(sig.parameters.keys())
        if 'device_id' in params:
            return 'device_id'
        elif 'slave' in params:
            return 'slave'
        elif 'unit' in params:
            return 'unit'
    except:
        pass
    return None

_UNIT_KWARG = _detect_unit_kwarg()


def _unit_kw(unit_id):
    """Return unit ID kwargs for the installed pymodbus version."""
    if _UNIT_KWARG:
        return {_UNIT_KWARG: unit_id}
    return {'slave': unit_id}


def write_to_plc(client, addr_info, value, unit_id):
    """Write a value to the PLC."""
    modbus_addr = addr_info['modbus_addr']
    kw = _unit_kw(unit_id)
    
    if addr_info['type'] == 'X':
        # Digital (coil)
        bool_val = bool(int(value))
        if addr_info['direction'] == 'I':
            # Input coils offset by 8192
            client.write_coil(address=8192 + modbus_addr, value=bool_val, **kw)
        else:
            client.write_coil(address=modbus_addr, value=bool_val, **kw)
    elif addr_info['type'] == 'W':
        # Analog (holding register)
        int_val = int(value)
        if int_val < 0:
            int_val = int_val & 0xFFFF
        if addr_info['direction'] == 'I':
            # Input registers offset by 1024
            client.write_register(address=1024 + modbus_addr, value=int_val, **kw)
        else:
            client.write_register(address=modbus_addr, value=int_val, **kw)


def read_from_plc(client, addr_info, unit_id):
    """Read a value from the PLC."""
    modbus_addr = addr_info['modbus_addr']
    kw = _unit_kw(unit_id)
    
    if addr_info['type'] == 'X':
        # Digital (coil)
        result = client.read_coils(address=modbus_addr, count=1, **kw)
        if result.isError():
            raise IOError(f"Modbus read error at coil {modbus_addr}")
        return 1 if result.bits[0] else 0
    elif addr_info['type'] == 'W':
        # Analog (input register)
        result = client.read_input_registers(address=modbus_addr, count=1, **kw)
        if result.isError():
            raise IOError(f"Modbus read error at register {modbus_addr}")
        val = result.registers[0]
        # Handle signed values
        if val > 32767:
            val = val - 65536
        return val


# =====================================================================
# Main Test Runner
# =====================================================================

def run_sequential_tests(csv_file, input_addrs, output_addrs, plc_ip, port, unit_id, delay_ms):
    """
    Run tests from a headerless CSV file.
    
    Args:
        csv_file: Path to CSV file (no headers)
        input_addrs: List of input PLC addresses in order
        output_addrs: List of output PLC addresses in order
        plc_ip: PLC IP address
        port: Modbus TCP port
        unit_id: Modbus unit ID
        delay_ms: Delay between write and read (milliseconds)
    """
    
    # Validate file exists
    if not os.path.isfile(csv_file):
        print(f"❌ Error: CSV file '{csv_file}' not found!")
        sys.exit(1)
    
    # Parse addresses
    try:
        input_addr_objs = [parse_plc_address(addr) for addr in input_addrs]
        output_addr_objs = [parse_plc_address(addr) for addr in output_addrs]
    except ValueError as e:
        print(f"❌ Error parsing addresses: {e}")
        sys.exit(1)
    
    num_inputs = len(input_addr_objs)
    num_outputs = len(output_addr_objs)
    expected_cols = num_inputs + num_outputs
    
    print(f"📄 Loading '{csv_file}'...")
    print(f"   Expected columns: {expected_cols} ({num_inputs} inputs + {num_outputs} outputs)")
    print(f"   Inputs:  {[a['raw'] for a in input_addr_objs]}")
    print(f"   Outputs: {[a['raw'] for a in output_addr_objs]}")
    print()
    
    # Load CSV without headers
    df = pd.read_csv(csv_file, header=None)
    
    if df.shape[1] != expected_cols:
        print(f"❌ Error: CSV has {df.shape[1]} columns, but expected {expected_cols}")
        print(f"   ({num_inputs} inputs + {num_outputs} outputs)")
        sys.exit(1)
    
    print(f"✅ Loaded {len(df)} test case(s)\n")
    
    # Connect to PLC
    client = ModbusTcpClient(plc_ip, port=port)
    print(f"🔌 Connecting to OpenPLC at {plc_ip}:{port}...")
    if not client.connect():
        print("❌ FAIL: Could not connect to OpenPLC. Is it running?")
        sys.exit(1)
    print("✅ Connected!\n")
    
    # Run tests
    print("=" * 70)
    print(f"{'TEST EXECUTION REPORT':^70}")
    print(f"{'Date: ' + datetime.now().strftime('%Y-%m-%d %H:%M:%S'):^70}")
    print("=" * 70)
    
    results = []
    delay_sec = delay_ms / 1000.0
    
    for idx, row in df.iterrows():
        test_id = idx + 1
        print(f"\n--- Test {test_id} ---")
        
        # Extract input and output values from row
        input_values = row.iloc[:num_inputs].tolist()
        expected_outputs = row.iloc[num_inputs:].tolist()
        
        # Display test case
        print(f"  Inputs:  {input_values}")
        print(f"  Expected: {expected_outputs}")
        
        # Write inputs
        for i, (addr_obj, value) in enumerate(zip(input_addr_objs, input_values)):
            try:
                write_to_plc(client, addr_obj, value, unit_id)
                print(f"  ➡️  Write {addr_obj['raw']} = {value}")
            except Exception as e:
                print(f"  ⚠️  Write error on {addr_obj['raw']}: {e}")
        
        # Wait for PLC cycle
        time.sleep(delay_sec)
        
        # Read and verify outputs
        all_pass = True
        details = []
        
        for i, (addr_obj, expected) in enumerate(zip(output_addr_objs, expected_outputs)):
            try:
                actual = read_from_plc(client, addr_obj, unit_id)
                expected_val = int(float(expected))
                actual_val = int(actual)
                
                match = (actual_val == expected_val)
                
                if match:
                    details.append(f"  ✅ {addr_obj['raw']}: Expected={expected_val}, Actual={actual_val}  PASS")
                else:
                    details.append(f"  ❌ {addr_obj['raw']}: Expected={expected_val}, Actual={actual_val}  FAIL")
                    all_pass = False
                    
            except Exception as e:
                details.append(f"  ⚠️  {addr_obj['raw']}: Read error — {e}")
                all_pass = False
        
        for d in details:
            print(d)
        
        status = "PASS" if all_pass else "FAIL"
        results.append((test_id, status, input_values, expected_outputs))
    
    # Close connection
    client.close()
    
    # Summary
    pass_count = sum(1 for r in results if r[1] == "PASS")
    fail_count = len(results) - pass_count
    
    print("\n" + "=" * 70)
    print(f"{'SUMMARY':^70}")
    print("=" * 70)
    print(f"  Total : {len(results)}")
    print(f"  Passed: {pass_count}  ✅")
    print(f"  Failed: {fail_count}  {'❌' if fail_count else ''}")
    print("=" * 70)
    
    for test_id, status, inputs, expected in results:
        icon = "✅" if status == "PASS" else "❌"
        print(f"  {icon} Test {test_id}: {inputs} → {expected}")
    
    print("=" * 70)
    print("--- Test Suite Complete ---\n")
    
    return fail_count == 0


# =====================================================================
# CLI Entry Point
# =====================================================================

def main():
    parser = argparse.ArgumentParser(
        description="OpenPLC Sequential Test Runner — Headerless CSV with sequential column mapping",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # 4 inputs, 1 output
  python test_generator_2.py -f test.csv -i %IX0.0,%IX0.1,%IX0.2,%IX0.3 -o %QX0.0
  
  # 2 analog inputs, 1 analog output
  python test_generator_2.py -f test.csv -i %IW0,%IW1 -o %QW0
  
  # Custom IP and delay
  python test_generator_2.py -f test.csv -i %IX0.0,%IX0.1 -o %QX0.0 --ip 192.168.1.10 --delay 200
        """,
    )
    
    parser.add_argument('-f', '--file', required=True,
                        help="Path to the CSV test file (no headers)")
    parser.add_argument('-i', '--inputs', required=True,
                        help="Comma-separated list of input addresses (e.g., %IX0.0,%IX0.1,%IW0)")
    parser.add_argument('-o', '--outputs', required=True,
                        help="Comma-separated list of output addresses (e.g., %QX0.0,%QW0)")
    parser.add_argument('--ip', default=DEFAULT_PLC_IP,
                        help=f"PLC IP address (default: {DEFAULT_PLC_IP})")
    parser.add_argument('--port', type=int, default=DEFAULT_PORT,
                        help=f"Modbus TCP port (default: {DEFAULT_PORT})")
    parser.add_argument('--unit', type=int, default=DEFAULT_UNIT_ID,
                        help=f"Modbus unit ID (default: {DEFAULT_UNIT_ID})")
    parser.add_argument('--delay', type=int, default=DEFAULT_DELAY_MS,
                        help=f"Delay between write and read in ms (default: {DEFAULT_DELAY_MS})")
    
    args = parser.parse_args()
    
    # Parse comma-separated addresses
    input_addrs = [addr.strip() for addr in args.inputs.split(',')]
    output_addrs = [addr.strip() for addr in args.outputs.split(',')]
    
    success = run_sequential_tests(
        csv_file=args.file,
        input_addrs=input_addrs,
        output_addrs=output_addrs,
        plc_ip=args.ip,
        port=args.port,
        unit_id=args.unit,
        delay_ms=args.delay
    )
    
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
