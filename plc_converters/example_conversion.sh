#!/bin/bash
# Example: Convert the th_X_trip program from XML to ST

echo "======================================"
echo "  Example: Converting th_X_trip.xml"
echo "======================================"
echo ""

# Check if example XML exists
if [ ! -f "th_X_trip.xml" ]; then
    echo "⚠️  Example file 'th_X_trip.xml' not found."
    echo ""
    echo "To use this example:"
    echo "  1. Save your XML content to 'th_X_trip.xml'"
    echo "  2. Run this script again: ./example_conversion.sh"
    echo ""
    exit 1
fi

echo "📄 Converting th_X_trip.xml to Structured Text..."
echo ""

# Convert using FBD converter (since the example uses FBD)
python plc_converters/2_fbd_to_st_converter.py th_X_trip.xml th_X_trip.st

if [ $? -eq 0 ]; then
    echo ""
    echo "✅ Conversion successful!"
    echo "   Output file: th_X_trip.st"
    echo ""
    echo "Preview of generated code:"
    echo "-----------------------------------"
    head -n 30 th_X_trip.st
    echo "-----------------------------------"
    echo ""
    echo "Full code saved to: th_X_trip.st"
else
    echo ""
    echo "❌ Conversion failed. Check the error messages above."
fi
