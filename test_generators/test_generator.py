"""
OpenPLC Automated Test Generator & Runner
==========================================
Reads a CSV file with PLC test cases and automatically:
  1. Parses input/output columns by detecting IEC 61131-3 addresses (%IX, %QX, %IW, %QW, etc.)
  2. Maps addresses to Modbus registers (OpenPLC conventions)
  3. Writes inputs, waits, reads outputs, and compares against expected values
  4. Generates a summary report with PASS/FAIL for each test case

CSV Format Requirements:
  - Must have a 'Test_ID' column
  - Input columns:  any column whose header starts with 'Input'  (e.g., "Input_Name (%QX1.0)")
                    The address in the header tells the generator WHERE to write.
                    Use writable Modbus addresses: %QX (coils 0-8191) or %QW (HR 0-1023).
  - Output columns: any column whose header starts with 'Expected' (e.g., "Expected_Out (%QX0.0)")
                    The address in the header tells the generator WHERE to read.
  - Optional 'Delay_ms' column (defaults to 100ms if absent)
  - Optional 'Description' column

  NOTE — Why %QX/%QW for inputs?
  Standard OpenPLC Modbus slave only allows WRITING to output-mapped addresses:
    %QX coils   (Modbus FC5/FC15, addresses 0-8191)  → writable
    %QW holding registers (Modbus FC6/FC16, addr 0-1023) → writable
  %IX discrete inputs and %IW input registers are READ-ONLY via Modbus and
  cannot be forced externally.  Bind logical test inputs to %QX1.x/%QW1+
  so the test generator can drive them, and keep the real output at %QX0.0.

Supported PLC Address Types (in column headers):
  %QX<byte>.<bit>  -> Modbus coil  (write coil = byte*8+bit; read coil = same)
  %QW<index>       -> Modbus holding register (write/read HR = index)
  %IX<byte>.<bit>  -> Discrete Input (read-only in standard OpenPLC; avoid for inputs)
  %IW<index>       -> Input Register (read-only in standard OpenPLC; avoid for inputs)
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

# --- CONFIGURATION (defaults, overridable via CLI) ---
DEFAULT_CSV_FILE = 'test_cases.csv'
DEFAULT_PLC_IP = '127.0.0.1'
DEFAULT_PORT = 502
DEFAULT_UNIT_ID = 1
DEFAULT_DELAY_MS = 100  # fallback if no Delay_ms column - Delay between writing inputs and reading outputs (in milliseconds)

# =====================================================================
# Address Parsing Helpers
# =====================================================================

# Regex patterns for IEC 61131-3 located variables
RE_BIT_ADDR = re.compile(r'%([IQ])X(\d+)\.(\d+)')   # e.g. %IX0.0, %QX0.1
RE_WORD_ADDR = re.compile(r'%([IQ])W(\d+)')           # e.g. %IW0,  %QW3


def parse_plc_address(header_text):
    """
    Extract the PLC address from a CSV column header.
    Returns a dict:  { 'direction': 'I'|'Q', 'type': 'X'|'W', 'modbus_addr': int, 'raw': str }
    or None if no address found.
    """
    # Try bit address first
    m = RE_BIT_ADDR.search(header_text)
    if m:
        direction = m.group(1)        # 'I' or 'Q'
        byte_num = int(m.group(2))
        bit_num = int(m.group(3))
        modbus_addr = byte_num * 8 + bit_num
        return {
            'direction': direction,
            'type': 'X',
            'modbus_addr': modbus_addr,
            'raw': m.group(0),
        }
        #ex: { 'direction': 'I'|'Q', 'type': 'X'|'W', 'modbus_addr': int, 'raw': str }

    # Try word address
    m = RE_WORD_ADDR.search(header_text)
    if m:
        direction = m.group(1)
        index = int(m.group(2))
        return {
            'direction': direction,
            'type': 'W',
            'modbus_addr': index,
            'raw': m.group(0),
        }

    return None


def parse_cell_value(cell_text):
    """
    Parse a CSV cell value that may contain an override address.
    Examples:
        "1"                -> value=1, override_addr=None
        "0"                -> value=0, override_addr=None
        "1 (on %IX0.1)"   -> value=1, override_addr={...}  (address parsed)
        "350"              -> value=350, override_addr=None  (analog)
    Returns (numeric_value, override_address_dict_or_None)
    """
    cell_text = str(cell_text).strip()

    # Check for an override address inside the cell (e.g. "1 (on %IX0.1)")
    override_addr = None
    addr_match_bit = RE_BIT_ADDR.search(cell_text)
    addr_match_word = RE_WORD_ADDR.search(cell_text)
    if addr_match_bit:
        override_addr = parse_plc_address(cell_text)
    elif addr_match_word:
        override_addr = parse_plc_address(cell_text)

    # Extract the numeric value (first integer or float found)
    num_match = re.search(r'-?\d+\.?\d*', cell_text)
    if num_match:
        raw_num = num_match.group(0)
        value = float(raw_num) if '.' in raw_num else int(raw_num)
    else:
        value = 0  # default

    return value, override_addr


# =====================================================================
# Modbus Read / Write Helpers
# =====================================================================

def _detect_unit_kwarg():
    """
    Detect the correct keyword argument name for the Modbus unit/slave ID.
    - pymodbus 2.x:    'unit'
    - pymodbus 3.0-3.6:'slave'
    - pymodbus 3.7+:   'slave' (some builds) or 'device_id' (3.12+)
    Returns the keyword name as a string.
    """
    import inspect
    sig = inspect.signature(ModbusTcpClient.read_coils)
    params = list(sig.parameters.keys())
    if 'device_id' in params:
        return 'device_id'
    elif 'slave' in params:
        return 'slave'
    elif 'unit' in params:
        return 'unit'
    # Ultimate fallback: try each at runtime
    return None

# Detect once at import time
_UNIT_KWARG = _detect_unit_kwarg()


def _unit_kw(unit_id):
    """Return a dict like {'slave': 1} or {'device_id': 1} for the installed pymodbus."""
    if _UNIT_KWARG:
        return {_UNIT_KWARG: unit_id}
    # If detection failed, try common names in order
    return {'slave': unit_id}


def write_to_plc(client, addr_info, value, unit_id):
    """Write a value to the PLC based on address type."""
    modbus_addr = addr_info['modbus_addr']
    kw = _unit_kw(unit_id)

    if addr_info['type'] == 'X':
        # Discrete (coil)
        bool_val = bool(int(value))
        # OpenPLC memory mapping:
        # %QX outputs: coils 0-8191
        # %IX inputs: coils 8192+ (offset by 8192)
        if addr_info['direction'] == 'I':
            actual_addr = 8192 + modbus_addr
        else:
            actual_addr = modbus_addr
        result = client.write_coil(address=actual_addr, value=bool_val, **kw)
        if result is None or (hasattr(result, 'isError') and result.isError()):
            raise IOError(
                f"Modbus write FAILED for {addr_info['raw']} at coil {actual_addr}: {result}\n"
                f"  Hint: ensure the ST program declares '{addr_info['raw']}' with an AT location binding\n"
                f"  and that Modbus slave is enabled in OpenPLC with writable input addresses."
            )
    elif addr_info['type'] == 'W':
        # Analog (holding register)
        int_val = int(value)
        # Convert signed INT to unsigned 16-bit (two's complement)
        if int_val < 0:
            int_val = int_val & 0xFFFF
        # OpenPLC Modbus mapping for holding registers:
        # %QW outputs : holding registers 0-1023  (address = index)
        # %QW inputs  : holding registers 200-999 (address = index, same space)
        # Legacy %IW  : NOT writable via standard Modbus — avoid in new programs.
        #               If encountered, attempt offset 1024 (may not work).
        if addr_info['direction'] == 'I':
            actual_addr = 1024 + modbus_addr  # legacy fallback, unreliable
        else:
            actual_addr = modbus_addr
        result = client.write_register(address=actual_addr, value=int_val, **kw)
        if result is None or (hasattr(result, 'isError') and result.isError()):
            raise IOError(
                f"Modbus write FAILED for {addr_info['raw']} at register {actual_addr}: {result}\n"
                f"  Hint: ensure the ST program declares '{addr_info['raw']}' with an AT location binding\n"
                f"  and that Modbus slave is enabled in OpenPLC with writable input addresses."
            )


def read_from_plc(client, addr_info, unit_id):
    """Read a value from the PLC based on address type. Returns numeric value."""
    modbus_addr = addr_info['modbus_addr']
    kw = _unit_kw(unit_id)

    if addr_info['type'] == 'X':
        result = client.read_coils(address=modbus_addr, count=1, **kw)
        if result.isError():
            raise IOError(f"Modbus read error at coil {modbus_addr}: {result}")
        return 1 if result.bits[0] else 0
    elif addr_info['type'] == 'W':
        # %QW holding registers → FC3 read_holding_registers
        # %IW input registers   → FC4 read_input_registers
        if addr_info['direction'] == 'Q':
            result = client.read_holding_registers(address=modbus_addr, count=1, **kw)
        else:
            result = client.read_input_registers(address=modbus_addr, count=1, **kw)
        if result.isError():
            raise IOError(f"Modbus read error at register {modbus_addr}: {result}")
        val = result.registers[0]
        # Convert unsigned 16-bit to signed if value is > 32767
        if val > 32767:
            val = val - 65536
        return val

    raise ValueError(f"Unknown address type: {addr_info['type']}")


# =====================================================================
# CSV Column Classification
# =====================================================================

def classify_columns(columns):
    """
    Scan CSV column headers and classify them as:
      - input columns  (column name starts with 'Input' OR contains %I addresses)
      - output columns (column name starts with 'Expected' OR contains %Q addresses
                        without an 'Input' prefix)
      - meta columns   (Test_ID, Delay_ms, Description, etc.)
    The column-name prefix takes priority over the address direction so that
    logical inputs can be bound to writable %QX/%QW Modbus addresses for testing.
    Returns (input_cols, output_cols, has_delay, has_description)
    Each col entry: { 'col_name': str, 'addr': parsed_addr_dict }
    """
    input_cols = []
    output_cols = []
    has_delay = False
    has_description = False

    for col in columns:
        col_lower = col.strip().lower()

        if 'delay' in col_lower:
            has_delay = True
            continue
        if 'description' in col_lower:
            has_description = True
            continue
        if 'test_id' in col_lower or 'test id' in col_lower or col_lower == 'id':
            continue

        addr = parse_plc_address(col)
        if addr is None:
            continue  # unrecognized column, skip

        entry = {'col_name': col, 'addr': addr}

        # Column-name prefix takes priority over address direction:
        #   "Input_*"    → write to PLC (regardless of %I or %Q in address)
        #   "Expected_*" → read from PLC (regardless of %I or %Q in address)
        # Fallback: classify by address direction for unlabelled columns.
        if col_lower.startswith('input'):
            input_cols.append(entry)
        elif col_lower.startswith('expected'):
            output_cols.append(entry)
        elif addr['direction'] == 'I':
            input_cols.append(entry)
        elif addr['direction'] == 'Q':
            output_cols.append(entry)

    return input_cols, output_cols, has_delay, has_description


# =====================================================================
# Main Test Runner
# =====================================================================

def run_automated_tests(csv_file, plc_ip, port, unit_id):
    """Load CSV, parse columns, run each test row, and report results."""

    # ---- Load CSV ----
    if not os.path.isfile(csv_file):
        print(f"❌ Error: CSV file '{csv_file}' not found!")
        sys.exit(1)

    df = pd.read_csv(csv_file)
    print(f"📄 Loaded '{csv_file}' — {len(df)} test case(s)")

    # ---- Classify columns ----
    input_cols, output_cols, has_delay, has_description = classify_columns(df.columns)

    if not input_cols:
        print("⚠️  Warning: No input columns (%IX / %IW) detected in CSV headers.")
    if not output_cols:
        print("❌ Error: No output columns (%QX / %QW) detected. Cannot verify results.")
        sys.exit(1)

    print(f"   Inputs detected:  {[c['addr']['raw'] for c in input_cols]}")
    print(f"   Outputs detected: {[c['addr']['raw'] for c in output_cols]}")
    print()

    # ---- Find meta column names (case-insensitive lookup) ----
    col_map = {c.strip().lower(): c for c in df.columns}

    delay_col = None
    for key, orig in col_map.items():
        if 'delay' in key:
            delay_col = orig
            break

    desc_col = None
    for key, orig in col_map.items():
        if 'description' in key:
            desc_col = orig
            break

    test_id_col = None
    for key, orig in col_map.items():
        if key in ('test_id', 'test id', 'id'):
            test_id_col = orig
            break

    # ---- Connect to PLC ----
    client = ModbusTcpClient(plc_ip, port=port)

    print(f"🔌 Connecting to OpenPLC at {plc_ip}:{port} ...")
    if not client.connect():
        print("❌ FAIL: Could not connect to OpenPLC. Is it running?")
        sys.exit(1)
    print("✅ Connected!\n")

    # ---- Run tests ----
    print("=" * 70)
    print(f"{'TEST EXECUTION REPORT':^70}")
    print(f"{'Date: ' + datetime.now().strftime('%Y-%m-%d %H:%M:%S'):^70}")
    print("=" * 70)

    results = []  # collect (test_id, description, status, details)
    previous_outputs = {}  # Track previous output values for TOGGLE keyword

    for idx, row in df.iterrows():
        test_id = row[test_id_col] if test_id_col else idx + 1
        description = str(row[desc_col]).strip() if desc_col else ""
        delay_ms = DEFAULT_DELAY_MS
        if delay_col:
            try:
                delay_ms = int(row[delay_col])
            except (ValueError, TypeError):
                delay_ms = DEFAULT_DELAY_MS
        delay_sec = delay_ms / 1000.0

        print(f"\n--- Test {test_id}: {description} ---")

        # === WRITE INPUTS ===
        for ic in input_cols:
            cell = str(row[ic['col_name']]).strip()
            value, override = parse_cell_value(cell)

            # Use override address if present, otherwise the column header address
            addr = override if override else ic['addr']

            try:
                write_to_plc(client, addr, value, unit_id)
                print(f"  ➡️  Write {addr['raw']} = {value}")
            except Exception as e:
                print(f"  ⚠️  Write error on {addr['raw']}: {e}")

        # === WAIT ===
        actual_delay = delay_sec if delay_sec > 0 else DEFAULT_DELAY_MS / 1000.0
        time.sleep(actual_delay)

        # === READ & VERIFY OUTPUTS ===
        all_pass = True
        details = []

        for oc in output_cols:
            cell = str(row[oc['col_name']]).strip().upper()
            addr = oc['addr']
            addr_key = addr['raw']

            try:
                actual_val = read_from_plc(client, addr, unit_id)
                
                # Check for special keywords
                if cell == 'TOGGLE':
                    if addr_key in previous_outputs:
                        expected_val = 1 - previous_outputs[addr_key]  # Flip the bit
                        match = (int(actual_val) == int(expected_val))
                        
                        if match:
                            details.append(f"  ✅ {addr['raw']}: Toggled from {previous_outputs[addr_key]} to {actual_val}  PASS")
                        else:
                            details.append(f"  ❌ {addr['raw']}: Expected toggle to {expected_val}, but got {actual_val}  FAIL")
                            all_pass = False
                    else:
                        # First read - just store the baseline
                        details.append(f"  ℹ️  {addr['raw']}: Baseline reading = {actual_val} (will check toggle on next test)")
                
                elif cell == 'NO_CHANGE':
                    if addr_key in previous_outputs:
                        expected_val = previous_outputs[addr_key]
                        match = (int(actual_val) == int(expected_val))
                        
                        if match:
                            details.append(f"  ✅ {addr['raw']}: No change, value={actual_val}  PASS")
                        else:
                            details.append(f"  ❌ {addr['raw']}: Expected no change (value={expected_val}), but got {actual_val}  FAIL")
                            all_pass = False
                    else:
                        # First read - just store the baseline
                        details.append(f"  ℹ️  {addr['raw']}: Baseline reading = {actual_val} (will check no-change on next test)")
                
                else:
                    # Normal expected value comparison
                    expected_val, _ = parse_cell_value(cell)
                    match = (int(actual_val) == int(expected_val))

                    if match:
                        details.append(f"  ✅ {addr['raw']}: Expected={int(expected_val)}, Actual={actual_val}  PASS")
                    else:
                        details.append(f"  ❌ {addr['raw']}: Expected={int(expected_val)}, Actual={actual_val}  FAIL")
                        all_pass = False
                
                # Store current value for next TOGGLE/NO_CHANGE comparison
                previous_outputs[addr_key] = actual_val
                
            except Exception as e:
                details.append(f"  ⚠️  {addr['raw']}: Read error — {e}")
                all_pass = False

        for d in details:
            print(d)

        status = "PASS" if all_pass else "FAIL"
        results.append((test_id, description, status, details))

    # ---- Summary ----
    client.close()

    pass_count = sum(1 for r in results if r[2] == "PASS")
    fail_count = len(results) - pass_count

    print("\n" + "=" * 70)
    print(f"{'SUMMARY':^70}")
    print("=" * 70)
    print(f"  Total : {len(results)}")
    print(f"  Passed: {pass_count}  ✅")
    print(f"  Failed: {fail_count}  {'❌' if fail_count else ''}")
    print("=" * 70)

    for test_id, desc, status, _ in results:
        icon = "✅" if status == "PASS" else "❌"
        print(f"  {icon} Test {test_id}: {desc}")

    print("=" * 70)
    print("--- Test Suite Complete ---\n")

    return fail_count == 0


# =====================================================================
# CLI Entry Point
# =====================================================================

def main():
    parser = argparse.ArgumentParser(
        description="OpenPLC Automated Test Runner — CSV-driven PLC testing via Modbus TCP",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python test_generator.py
  python test_generator.py -f my_tests.csv
  python test_generator.py -f test_cases.csv --ip 192.168.1.10 --port 502
  python test_generator.py -f test_cases.csv --unit 2
        """,
    )
    parser.add_argument('-f', '--file', default=DEFAULT_CSV_FILE,
                        help=f"Path to the CSV test-cases file (default: {DEFAULT_CSV_FILE})")
    parser.add_argument('--ip', default=DEFAULT_PLC_IP,
                        help=f"PLC IP address (default: {DEFAULT_PLC_IP})")
    parser.add_argument('--port', type=int, default=DEFAULT_PORT,
                        help=f"Modbus TCP port (default: {DEFAULT_PORT})")
    parser.add_argument('--unit', type=int, default=DEFAULT_UNIT_ID,
                        help=f"Modbus slave/unit ID (default: {DEFAULT_UNIT_ID})")

    args = parser.parse_args()

    success = run_automated_tests(
        csv_file=args.file,
        plc_ip=args.ip,
        port=args.port,
        unit_id=args.unit,
    )

    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()