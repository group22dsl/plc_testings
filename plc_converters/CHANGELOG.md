# Changelog - PLC Converters

## Version 1.1 - March 2, 2026

### 🐛 Bug Fixes

#### 1. **Multiple Variable Sections Support**
- **Issue**: Converters only read the first `<inputVars>`, `<outputVars>`, or `<localVars>` section
- **Impact**: Missing variable declarations when XML has multiple sections of the same type
- **Fix**: Changed from `find()` to `findall()` to parse ALL variable sections
- **Files Updated**: All 4 converters

**Example Problem:**
```xml
<inputVars>
  <variable name="f_X">...</variable>
</inputVars>
<inputVars>  <!-- This was being ignored! -->
  <variable name="f_Module_Error">...</variable>
  <variable name="f_Channel_Error">...</variable>
</inputVars>
```

**Now Fixed:** All input variables are correctly extracted.

---

#### 2. **OpenPLC Compilation Compatibility**
- **Issue**: Generated ST files missing CONFIGURATION and RESOURCE sections
- **Error**: `mv: cannot stat 'Config0.c': No such file or directory`
- **Impact**: Files wouldn't compile in OpenPLC
- **Fix**: Added OpenPLC-required CONFIGURATION structure to all converters

**What was added:**
```iec-st
CONFIGURATION Config0
  RESOURCE Res0 ON PLC
    TASK task0(INTERVAL := T#20ms,PRIORITY := 0);
    PROGRAM instance0 WITH task0 : your_program;
  END_RESOURCE
END_CONFIGURATION
```

**Files Updated**: All 4 converters (1-4)

---

## Complete Fix Example

### Before (Missing Variables + No Config):
```iec-st
PROGRAM th_X_trip
VAR_INPUT
    f_X : INT;  (* Only this variable! *)
END_VAR
(* Missing 3 other input variables *)
(* Logic *)
tx_X_trip := ...;
END_PROGRAM
(* Missing CONFIGURATION - won't compile! *)
```

### After (Complete + OpenPLC Compatible):
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
(* Logic *)
tx_X_trip := ((NOT ((f_X >= K_X_Min) AND (f_X <= k_X_Max)) OR f_Module_Error) OR (f_Channel_Error OR tx_X_Logic_Trip));
END_PROGRAM

(* OpenPLC Configuration *)
CONFIGURATION Config0
  RESOURCE Res0 ON PLC
    TASK task0(INTERVAL := T#20ms,PRIORITY := 0);
    PROGRAM instance0 WITH task0 : th_X_trip;
  END_RESOURCE
END_CONFIGURATION
```

---

## How to Update

If you have the old version, just use the updated converters:

```bash
# Reconvert your files
python plc_converters/2_fbd_to_st_converter.py your_file.xml output.st

# The new output will:
# ✅ Include ALL variables from ALL sections
# ✅ Include OpenPLC CONFIGURATION
# ✅ Compile without errors in OpenPLC
```

---

## Testing

Test file: `th_X_trip.xml`
- ✅ All 4 input variables extracted
- ✅ Output variables extracted
- ✅ Constants extracted with initial values
- ✅ FBD logic converted to ST
- ✅ OpenPLC configuration added
- ✅ Compiles successfully in OpenPLC

---

## Version 1.0 - Initial Release
- Basic PLCopen XML parsing
- FBD, Ladder, SFC, and general converters
- Variable extraction (single sections only)
- Logic conversion
