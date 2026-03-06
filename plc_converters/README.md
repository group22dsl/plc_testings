# PLCopen XML to Structured Text Converters

This folder contains 4 different converters to transform PLCopen XML files (from CODESYS, etc.) into Structured Text (.st) format for OpenPLC.

## 📁 Converters

1. **1_plcopenxml_converter.py** - General parser using plcopenxml/lxml libraries
2. **2_fbd_to_st_converter.py** - Function Block Diagram (FBD) to ST converter
3. **3_ladder_to_st_converter.py** - Ladder Diagram (LD) to ST converter
4. **4_sfc_to_st_converter.py** - Sequential Function Chart (SFC) to ST converter

---

## 🚀 Installation

### Step 1: Install Required Libraries

```bash
# Install all required packages
pip install lxml

# Optional: If you want to try the plcopenxml library
pip install plcopenxml
```

---

## 📖 Usage Guide

### 1️⃣ **General PLCopen XML Converter** (Using lxml)

Converts PLCopen XML files and extracts variable declarations. Good for initial inspection.

```bash
# Convert and display output
python plc_converters/1_plcopenxml_converter.py your_program.xml

# Convert and save to file
python plc_converters/1_plcopenxml_converter.py your_program.xml output.st
```

**What it does:**
- ✅ Parses variable declarations (inputs, outputs, locals)
- ✅ Extracts data types and initial values
- ✅ Identifies body type (FBD, LD, SFC, ST)
- ⚠️ Basic logic extraction (refer to specialized converters for full conversion)

---

### 2️⃣ **FBD to ST Converter** (Function Block Diagrams)

Converts Function Block Diagrams with logic gates and comparisons.

```bash
# Convert FBD program
python plc_converters/2_fbd_to_st_converter.py your_fbd_program.xml

# Save to file
python plc_converters/2_fbd_to_st_converter.py your_fbd_program.xml output.st
```

**Supported FBD Elements:**
- ✅ Logic gates: AND, OR, NOT, XOR
- ✅ Comparisons: GE (≥), LE (≤), GT (>), LT (<), EQ (=), NE (≠)
- ✅ Input/Output variables
- ✅ Block connections and signal flow

**Example:** Your XML file with FBD logic will be converted to:
```iec-st
PROGRAM th_X_trip
VAR_INPUT
    f_X : INT;
    f_Module_Error : BOOL;
END_VAR

VAR_OUTPUT
    tx_X_trip : BOOL;
END_VAR

(* Logic from FBD *)
tx_X_trip := ((f_X >= K_X_Min) AND (f_X <= k_X_Max)) OR f_Module_Error;

END_PROGRAM
```

---

### 3️⃣ **Ladder to ST Converter** (Ladder Logic)

Converts Ladder Diagrams with contacts and coils.

```bash
# Convert Ladder program
python plc_converters/3_ladder_to_st_converter.py your_ladder_program.xml

# Save to file
python plc_converters/3_ladder_to_st_converter.py your_ladder_program.xml output.st
```

**Supported Ladder Elements:**
- ✅ Normally Open contacts (NO)
- ✅ Normally Closed contacts (NC)
- ✅ Coils (normal, set, reset)
- ✅ Series/parallel contact logic
- ✅ Power rails

**Example Output:**
```iec-st
PROGRAM ladder_example
VAR_INPUT
    start_button : BOOL;
    stop_button : BOOL;
END_VAR

VAR_OUTPUT
    motor_running : BOOL;
END_VAR

(* Logic from Ladder Diagram *)

(* Rung 1 *)
motor_running := start_button AND NOT stop_button;

END_PROGRAM
```

---

### 4️⃣ **SFC to ST Converter** (Sequential Function Charts)

Converts Sequential Function Charts (state machines) to ST.

```bash
# Convert SFC program
python plc_converters/4_sfc_to_st_converter.py your_sfc_program.xml

# Save to file
python plc_converters/4_sfc_to_st_converter.py your_sfc_program.xml output.st
```

**Supported SFC Elements:**
- ✅ Steps (initial and normal)
- ✅ Transitions with conditions
- ✅ Actions
- ✅ State machine structure

**Example Output:**
```iec-st
PROGRAM sfc_example
VAR
    current_state : INT := 0;
    step_init_active : BOOL := TRUE;
    step_run_active : BOOL := FALSE;
END_VAR

(* State Machine from SFC *)
CASE current_state OF
    0: (* Step: Init *)
        IF start_condition THEN
            current_state := 1;
        END_IF;
    
    1: (* Step: Run *)
        IF stop_condition THEN
            current_state := 0;
        END_IF;
END_CASE;

END_PROGRAM
```

---

## 🔍 Which Converter Should I Use?

| Your XML Contains | Use Converter | Command |
|-------------------|---------------|---------|
| Function Block Diagram (logic gates, comparisons) | #2 FBD Converter | `python plc_converters/2_fbd_to_st_converter.py file.xml` |
| Ladder Logic (contacts, coils, rungs) | #3 Ladder Converter | `python plc_converters/3_ladder_to_st_converter.py file.xml` |
| Sequential Function Chart (steps, transitions) | #4 SFC Converter | `python plc_converters/4_sfc_to_st_converter.py file.xml` |
| Not sure / want to inspect | #1 General Converter | `python plc_converters/1_plcopenxml_converter.py file.xml` |

---

## 📝 Example Workflow

### Example 1: Convert your XML file to ST

```bash
# Step 1: Check what's in the XML
python plc_converters/1_plcopenxml_converter.py th_X_trip.xml

# Step 2: It shows FBD logic, so use FBD converter
python plc_converters/2_fbd_to_st_converter.py th_X_trip.xml th_X_trip.st

# Step 3: View the generated ST code
cat th_X_trip.st
```

### Example 2: Convert and test with OpenPLC

```bash
# Convert to ST
python plc_converters/2_fbd_to_st_converter.py my_program.xml my_program.st

# Upload my_program.st to OpenPLC and compile

# Run automated tests (if you have test cases)
python test_generator.py -f my_tests.csv
```

---

## ⚙️ Testing the Converters

You can test with your provided XML file:

```bash
# Save your XML to a file (e.g., th_X_trip.xml)
# Then run:

python plc_converters/2_fbd_to_st_converter.py th_X_trip.xml output.st
```

Expected output file `output.st`:
```iec-st
PROGRAM th_X_trip
VAR_INPUT
    f_X : INT;
    f_Module_Error : BOOL;
    f_Channel_Error : BOOL;
    tx_X_Logic_Trip : BOOL;
END_VAR

VAR_OUTPUT
    tx_X_trip : BOOL;
END_VAR

VAR CONSTANT
    K_X_Min : INT := -55;
    k_X_Max : INT := 125;
END_VAR

(* Logic from FBD *)
tx_X_trip := ((NOT ((f_X >= K_X_Min) AND (f_X <= k_X_Max)) OR f_Module_Error) OR (f_Channel_Error OR tx_X_Logic_Trip));

END_PROGRAM
```

---

## 🐛 Troubleshooting

### Error: "lxml is not installed"
```bash
pip install lxml
```

### Error: "File not found"
Make sure you're in the correct directory or provide full path:
```bash
python plc_converters/2_fbd_to_st_converter.py /full/path/to/your/file.xml
```

### Complex logic not converting properly
Some complex PLCopen XML structures may require manual adjustment. The converters provide a good starting point and comments indicating where manual review is needed.

---

## 📚 Additional Resources

- **PLCopen XML Standard**: https://plcopen.org/technical-activities/xml-exchange-format
- **IEC 61131-3 Structured Text**: Standard for PLC programming languages
- **OpenPLC Documentation**: https://www.openplcproject.com/

---

## 🤝 Contributing

Feel free to enhance these converters! They handle common cases but may need adjustments for:
- Complex nested logic
- Custom function blocks
- Vendor-specific extensions

---

## 📄 License

These converters are provided as-is for educational and development purposes.
