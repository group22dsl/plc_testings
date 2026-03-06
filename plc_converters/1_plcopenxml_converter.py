"""
PLCopen XML to ST Converter using plcopenxml and lxml libraries
=================================================================
This script uses the plcopenxml library to parse PLCopen XML files
and convert them to Structured Text (ST) format.

Installation:
    pip install lxml plcopenxml

Usage:
    python 1_plcopenxml_converter.py input.xml output.st
    python 1_plcopenxml_converter.py input.xml  (outputs to console)
"""

import sys
import xml.etree.ElementTree as ET
from lxml import etree
from pathlib import Path

# Namespace for PLCopen XML
NAMESPACES = {
    'plc': 'http://www.plcopen.org/xml/tc6_0200',
    'xhtml': 'http://www.w3.org/1999/xhtml'
}


class PLCopenXMLConverter:
    """Convert PLCopen XML to Structured Text"""
    
    def __init__(self, xml_file):
        self.xml_file = xml_file
        self.tree = etree.parse(xml_file)
        self.root = self.tree.getroot()
    
    def convert(self):
        """Main conversion method"""
        st_code = []
        
        # Add header comment
        st_code.append("(*")
        st_code.append(f"  Generated from: {Path(self.xml_file).name}")
        st_code.append(f"  Converter: PLCopen XML to ST (using plcopenxml/lxml)")
        st_code.append("*)")
        st_code.append("")
        
        # Extract all POUs (Program Organization Units)
        pous = self.root.findall('.//plc:pou', NAMESPACES)
        
        # Store POU info for configuration
        pou_names = []
        
        for pou in pous:
            pou_name = pou.get('name')
            pou_type = pou.get('pouType', 'program')
            pou_names.append((pou_name, pou_type))
            st_code.extend(self._convert_pou(pou))
            st_code.append("")
        
        # Add OpenPLC Configuration
        st_code.append("(* OpenPLC Configuration *)")
        st_code.append("CONFIGURATION Config0")
        st_code.append("")
        st_code.append("  RESOURCE Res0 ON PLC")
        
        for pou_name, pou_type in pou_names:
            if pou_type.lower() == 'program':
                st_code.append(f"    TASK task0(INTERVAL := T#20ms,PRIORITY := 0);")
                st_code.append(f"    PROGRAM instance0 WITH task0 : {pou_name};")
        
        st_code.append("  END_RESOURCE")
        st_code.append("")
        st_code.append("END_CONFIGURATION")
        
        return '\n'.join(st_code)
    
    def _convert_pou(self, pou):
        """Convert a single POU to ST"""
        lines = []
        
        pou_name = pou.get('name')
        pou_type = pou.get('pouType').upper()  # PROGRAM, FUNCTION, FUNCTION_BLOCK
        
        lines.append(f"{pou_type} {pou_name}")
        
        # Extract interface (variables)
        interface = pou.find('plc:interface', NAMESPACES)
        if interface is not None:
            lines.extend(self._extract_variables(interface))
        
        # Extract body (logic)
        body = pou.find('plc:body', NAMESPACES)
        if body is not None:
            lines.extend(self._extract_body(body))
        
        lines.append(f"END_{pou_type}")
        
        return lines
    
    def _extract_variables(self, interface):
        """Extract variable declarations"""
        lines = []
        
        # Input variables
        input_vars = interface.find('plc:inputVars', NAMESPACES)
        if input_vars is not None and len(input_vars) > 0:
            lines.append("VAR_INPUT")
            lines.extend(self._parse_var_list(input_vars))
            lines.append("END_VAR")
            lines.append("")
        
        # Output variables
        output_vars = interface.find('plc:outputVars', NAMESPACES)
        if output_vars is not None and len(output_vars) > 0:
            lines.append("VAR_OUTPUT")
            lines.extend(self._parse_var_list(output_vars))
            lines.append("END_VAR")
            lines.append("")
        
        # Input/Output variables
        inout_vars = interface.find('plc:inOutVars', NAMESPACES)
        if inout_vars is not None and len(inout_vars) > 0:
            lines.append("VAR_IN_OUT")
            lines.extend(self._parse_var_list(inout_vars))
            lines.append("END_VAR")
            lines.append("")
        
        # Local variables
        local_vars = interface.find('plc:localVars', NAMESPACES)
        if local_vars is not None and len(local_vars) > 0:
            is_constant = local_vars.get('constant') == 'true'
            lines.append("VAR CONSTANT" if is_constant else "VAR")
            lines.extend(self._parse_var_list(local_vars))
            lines.append("END_VAR")
            lines.append("")
        
        return lines
    
    def _parse_var_list(self, var_section):
        """Parse a list of variables"""
        lines = []
        
        for var in var_section.findall('plc:variable', NAMESPACES):
            var_name = var.get('name')
            
            # Get type
            type_elem = var.find('.//plc:type', NAMESPACES)
            var_type = self._get_type(type_elem)
            
            # Get initial value
            init_value = self._get_initial_value(var)
            
            if init_value:
                lines.append(f"    {var_name} : {var_type} := {init_value};")
            else:
                lines.append(f"    {var_name} : {var_type};")
        
        return lines
    
    def _get_type(self, type_elem):
        """Extract variable type"""
        if type_elem is None:
            return "BOOL"
        
        # Check for basic types
        for basic_type in ['BOOL', 'INT', 'DINT', 'REAL', 'STRING', 'BYTE', 'WORD', 'DWORD', 'TIME']:
            if type_elem.find(f'plc:{basic_type}', NAMESPACES) is not None:
                return basic_type
        
        # Check for derived/custom types
        derived = type_elem.find('plc:derived', NAMESPACES)
        if derived is not None:
            return derived.get('name', 'UNKNOWN_TYPE')
        
        return "BOOL"
    
    def _get_initial_value(self, var):
        """Extract initial value if present"""
        init_val = var.find('.//plc:initialValue', NAMESPACES)
        if init_val is not None:
            simple_val = init_val.find('plc:simpleValue', NAMESPACES)
            if simple_val is not None:
                return simple_val.get('value')
        return None
    
    def _extract_body(self, body):
        """Extract POU body logic"""
        lines = []
        
        # Check for ST (Structured Text)
        st_body = body.find('plc:ST', NAMESPACES)
        if st_body is not None:
            lines.append("")
            lines.append("(* Structured Text Code *)")
            if st_body.text:
                lines.extend(st_body.text.strip().split('\n'))
            return lines
        
        # Check for FBD (Function Block Diagram)
        fbd_body = body.find('plc:FBD', NAMESPACES)
        if fbd_body is not None:
            lines.append("")
            lines.append("(* Converted from FBD *)")
            lines.append("(* Note: FBD to ST conversion requires logic analysis *)")
            lines.append("(* See 2_fbd_to_st_converter.py for full FBD conversion *)")
            return lines
        
        # Check for LD (Ladder Diagram)
        ld_body = body.find('plc:LD', NAMESPACES)
        if ld_body is not None:
            lines.append("")
            lines.append("(* Converted from Ladder Diagram *)")
            lines.append("(* Note: LD to ST conversion requires logic analysis *)")
            lines.append("(* See 3_ladder_to_st_converter.py for full LD conversion *)")
            return lines
        
        # Check for SFC (Sequential Function Chart)
        sfc_body = body.find('plc:SFC', NAMESPACES)
        if sfc_body is not None:
            lines.append("")
            lines.append("(* Converted from SFC *)")
            lines.append("(* Note: SFC to ST conversion requires state machine logic *)")
            lines.append("(* See 4_sfc_to_st_converter.py for full SFC conversion *)")
            return lines
        
        lines.append("")
        lines.append("(* Body type not recognized *)")
        return lines


def main():
    """CLI entry point"""
    if len(sys.argv) < 2:
        print("Usage: python 1_plcopenxml_converter.py <input.xml> [output.st]")
        print("\nExample:")
        print("  python 1_plcopenxml_converter.py program.xml")
        print("  python 1_plcopenxml_converter.py program.xml output.st")
        sys.exit(1)
    
    input_file = sys.argv[1]
    output_file = sys.argv[2] if len(sys.argv) > 2 else None
    
    if not Path(input_file).exists():
        print(f"Error: File '{input_file}' not found!")
        sys.exit(1)
    
    try:
        converter = PLCopenXMLConverter(input_file)
        st_code = converter.convert()
        
        if output_file:
            with open(output_file, 'w') as f:
                f.write(st_code)
            print(f"✅ Converted successfully!")
            print(f"   Output: {output_file}")
        else:
            print(st_code)
    
    except Exception as e:
        print(f"❌ Error during conversion: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
