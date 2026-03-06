# 🚀 Quick Start Guide

## Installation (One-time setup)

```bash
cd /home/nuwan/Documents/openplc/plc_converters

# Install required Python packages
pip install -r requirements.txt
```

---

## Usage Examples

### Option 1: Quick Test (Test all converters at once)

```bash
# Make sure you're in the openplc directory
cd /home/nuwan/Documents/openplc

# Run test on your XML file
./plc_converters/test_converters.sh your_program.xml
```

### Option 2: Convert Specific Format

#### For FBD (Function Block Diagram) programs:
```bash
python plc_converters/2_fbd_to_st_converter.py your_program.xml output.st
```

#### For Ladder Logic programs:
```bash
python plc_converters/3_ladder_to_st_converter.py your_program.xml output.st
```

#### For SFC (Sequential Function Chart) programs:
```bash
python plc_converters/4_sfc_to_st_converter.py your_program.xml output.st
```

#### For general inspection:
```bash
python plc_converters/1_plcopenxml_converter.py your_program.xml
```

---

## 📋 Step-by-Step: Your First Conversion

### Step 1: Save your XML file
Save your PLCopen XML content to a file, for example: `my_program.xml`

### Step 2: Identify the diagram type
Run the general converter to see what type it is:
```bash
python plc_converters/1_plcopenxml_converter.py my_program.xml
```

It will tell you if it's FBD, Ladder, SFC, or ST.

### Step 3: Use the appropriate converter
Based on Step 2, use the right converter:

```bash
# If it's FBD:
python plc_converters/2_fbd_to_st_converter.py my_program.xml my_program.st

# If it's Ladder:
python plc_converters/3_ladder_to_st_converter.py my_program.xml my_program.st

# If it's SFC:
python plc_converters/4_sfc_to_st_converter.py my_program.xml my_program.st
```

### Step 4: Check the output
```bash
cat my_program.st
```

### Step 5: Use in OpenPLC
Upload `my_program.st` to OpenPLC and compile!

---

## 🎯 Example with th_X_trip

Your provided XML example is an FBD program. Here's how to convert it:

### Create the XML file:
```bash
# Save your XML content to th_X_trip.xml
nano th_X_trip.xml
# (paste your XML content and save)
```

### Convert it:
```bash
python plc_converters/2_fbd_to_st_converter.py th_X_trip.xml th_X_trip.st
```

### View the result:
```bash
cat th_X_trip.st
```

You should see Structured Text output like:
```iec-st
PROGRAM th_X_trip
VAR_INPUT
    f_X : INT;
    f_Module_Error : BOOL;
    ...
END_VAR
...
END_PROGRAM
```

---

## 💡 Tips

1. **Always start with the general converter** (#1) to identify the diagram type
2. **Review the output** - complex logic may need manual adjustments
3. **Test in OpenPLC** - compile the generated .st file to verify syntax
4. **Check the README.md** for detailed documentation

---

## 🆘 Need Help?

Check these files:
- `README.md` - Full documentation
- `test_converters.sh` - Test all converters
- `example_conversion.sh` - Example conversion workflow

---

## 📞 Common Issues

**"ModuleNotFoundError: No module named 'lxml'"**
```bash
pip install lxml
```

**"Permission denied"**
```bash
chmod +x plc_converters/*.sh
```

**"File not found"**
Make sure you're in the correct directory:
```bash
cd /home/nuwan/Documents/openplc
ls plc_converters/  # Should show all converter files
```

---

Happy Converting! 🎉
