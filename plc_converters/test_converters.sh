#!/bin/bash
# Quick test script for PLC converters
# This script helps you quickly test all converters

echo "======================================"
echo "  PLC Converters - Quick Test"
echo "======================================"
echo ""

# Check if XML file is provided
if [ -z "$1" ]; then
    echo "Usage: ./test_converters.sh <your_xml_file.xml>"
    echo ""
    echo "Example:"
    echo "  ./test_converters.sh my_program.xml"
    echo ""
    exit 1
fi

XML_FILE="$1"

# Check if file exists
if [ ! -f "$XML_FILE" ]; then
    echo "❌ Error: File '$XML_FILE' not found!"
    exit 1
fi

echo "📄 Testing with file: $XML_FILE"
echo ""

# Test 1: General converter
echo "================================"
echo "Test 1: General PLCopen Converter"
echo "================================"
python plc_converters/1_plcopenxml_converter.py "$XML_FILE"
echo ""

# Test 2: FBD converter
echo "================================"
echo "Test 2: FBD to ST Converter"
echo "================================"
python plc_converters/2_fbd_to_st_converter.py "$XML_FILE"
echo ""

# Test 3: Ladder converter
echo "================================"
echo "Test 3: Ladder to ST Converter"
echo "================================"
python plc_converters/3_ladder_to_st_converter.py "$XML_FILE"
echo ""

# Test 4: SFC converter
echo "================================"
echo "Test 4: SFC to ST Converter"
echo "================================"
python plc_converters/4_sfc_to_st_converter.py "$XML_FILE"
echo ""

echo "======================================"
echo "✅ All tests completed!"
echo "======================================"
echo ""
echo "To save output to a file, use:"
echo "  python plc_converters/2_fbd_to_st_converter.py $XML_FILE output.st"
