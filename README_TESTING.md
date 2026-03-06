# OpenPLC Test Generator - Usage Guide

## Overview
The `test_generator.py` script provides automated CSV-driven testing for OpenPLC programs via Modbus TCP.

## Basic Usage

```bash
# Run tests with default settings (test_cases.csv on localhost)
python3 test_generator.py

# Use a different CSV file
python3 test_generator.py -f my_tests.csv

# Connect to a remote PLC
python3 test_generator.py --ip 192.168.1.10 --port 502

# Custom Modbus unit ID
python3 test_generator.py --unit 2
```

## CSV Format

### Required Columns
- `Test_ID`: Unique identifier for each test case

### Optional Columns
- `Delay_ms`: Wait time in milliseconds before reading outputs (default: 100ms)
- `Description`: Human-readable test description

### Input/Output Columns
Any column containing PLC addresses in the header will be auto-detected:

- **Inputs** (write to PLC):
  - `%IX<byte>.<bit>` - Discrete input (e.g., `%IX0.0`)
  - `%IW<index>` - Analog input (e.g., `%IW0`)

- **Outputs** (read from PLC):
  - `%QX<byte>.<bit>` - Discrete output (e.g., `%QX0.0`)
  - `%QW<index>` - Analog output (e.g., `%QW3`)

### Example CSV for Motor Control

```csv
Test_ID,Input_Name (%IX0.0),Input_Stop (%IX0.1),Expected_Output (%QX0.0),Delay_ms,Description
1,1,0,1,100,Start Button Pressed -> Motor On
2,0,0,1,50,Start Released -> Motor Stays On (Latching)
3,0,1,0,100,Stop Button Pressed -> Motor Off
4,0,0,0,50,Both Released -> Motor Stays Off
```

## Special Features

### 1. Address Override in Cell
You can override the column's address for specific test cases:

```csv
Test_ID,Input_Name (%IX0.0),Expected_Output (%QX0.0),Description
1,1,1,Normal input to %IX0.0
2,1 (on %IX0.1),0,This test writes to %IX0.1 instead
```

### 2. TOGGLE Keyword
For testing blinkers/toggles where you want to verify the output **changes** rather than matching an absolute value:

```csv
Test_ID,Expected_Output (%QX0.0),Delay_ms,Description
1,TOGGLE,1200,Baseline read and verify first toggle
2,TOGGLE,1200,Verify output toggled from previous state
3,TOGGLE,1200,Verify output toggled again
```

The `TOGGLE` keyword:
- On first use: records the baseline value (always passes)
- On subsequent uses: expects the output to be the **opposite** of the previous reading
- Perfect for testing timer-based blinkers and oscillators

### 3. Multiple Inputs/Outputs
You can have as many input and output columns as needed:

```csv
Test_ID,Start (%IX0.0),Stop (%IX0.1),Enable (%IX0.2),Motor (%QX0.0),Alarm (%QX0.1),Speed (%QW0),Delay_ms,Description
1,1,0,1,1,0,255,100,Full speed start
```

## Testing Different Program Types

### Timer/Blinker Programs (like blink_3.st)
Use `TOGGLE` to verify periodic behavior:

```csv
Test_ID,Expected_Output (%QX0.0),Delay_ms,Description
1,TOGGLE,1200,Verify blink cycle
2,TOGGLE,1200,Verify continued blinking
```

### Start-Stop Motor Control
Use absolute values with latching checks:

```csv
Test_ID,Start (%IX0.0),Stop (%IX0.1),Expected_Output (%QX0.0),Delay_ms,Description
1,1,0,1,100,Start pressed -> Motor ON
2,0,0,1,50,Start released -> Motor stays ON
3,0,1,0,100,Stop pressed -> Motor OFF
```

### Analog Control
Use word addresses for analog I/O:

```csv
Test_ID,Setpoint (%IW0),Expected_Output (%QW0),Delay_ms,Description
1,100,100,200,Setpoint 100 -> Output 100
2,500,500,200,Setpoint 500 -> Output 500
```

## Troubleshooting

### Connection Failed
- Ensure OpenPLC is running
- Check that Modbus TCP server is enabled in OpenPLC settings
- Verify IP address and port (default: 127.0.0.1:502)

### All Tests Fail with "unexpected keyword argument"
- The script auto-detects pymodbus version compatibility
- If issues persist, try: `pip install --upgrade pymodbus`

### Timing Issues
- Increase `Delay_ms` values if tests are flaky
- OpenPLC scan cycle is typically 100ms
- Add margin for network latency on remote connections

## Return Codes

- `0`: All tests passed ✅
- `1`: One or more tests failed ❌

Use in CI/CD pipelines:
```bash
python3 test_generator.py && echo "Deploy to production" || echo "Tests failed!"
```

## Compatible Pymodbus Versions

The script automatically detects and adapts to:
- pymodbus 2.x (uses `unit` parameter)
- pymodbus 3.0-3.6 (uses `slave` parameter)
- pymodbus 3.7+ / 3.12+ (uses `device_id` parameter)
