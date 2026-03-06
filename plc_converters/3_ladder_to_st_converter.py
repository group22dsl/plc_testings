"""
Ladder Diagram (LD) to Structured Text Converter
=================================================
This script converts Ladder Logic diagrams from PLCopen XML to ST code.
It analyzes rungs, contacts, and coils to generate equivalent ST logic.

Installation:
    pip install lxml

Usage:
    python 3_ladder_to_st_converter.py input.xml output.st
    python 3_ladder_to_st_converter.py input.xml  (outputs to console)
"""

import sys
from lxml import etree
from pathlib import Path

# Namespace for PLCopen XML
NAMESPACES = {
    'plc': 'http://www.plcopen.org/xml/tc6_0200',
    'xhtml': 'http://www.w3.org/1999/xhtml'
}


class LDElement:
    """Represents a Ladder Diagram element"""
    def __init__(self, local_id, elem_type):
        self.local_id = local_id
        self.elem_type = elem_type  # contact, coil, block
        self.variable = None
        self.negated = False
        self.connections = []


class LadderToSTConverter:
    """Convert Ladder Diagrams to Structured Text"""
    
    def __init__(self, xml_file):
        self.xml_file = xml_file
        self.tree = etree.parse(xml_file)
        self.root = self.tree.getroot()
    
    def convert(self):
        """Main conversion method"""
        st_code = []
        
        st_code.append("(*")
        st_code.append(f"  Generated from: {Path(self.xml_file).name}")
        st_code.append(f"  Converter: Ladder Diagram (LD) to ST")
        st_code.append("*)")
        st_code.append("")
        
        # Extract all POUs
        pous = self.root.findall('.//plc:pou', NAMESPACES)
        
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
        """Convert a single POU with LD body"""
        lines = []
        
        pou_name = pou.get('name')
        pou_type = pou.get('pouType').upper()
        
        lines.append(f"{pou_type} {pou_name}")
        
        # Extract interface
        interface = pou.find('plc:interface', NAMESPACES)
        var_lines = self._extract_variables(interface)
        lines.extend(var_lines)
        
        # Extract LD body
        body = pou.find('plc:body', NAMESPACES)
        ld_body = body.find('plc:LD', NAMESPACES) if body is not None else None
        
        if ld_body is not None:
            logic_lines = self._convert_ladder(ld_body)
            lines.extend(logic_lines)
        else:
            lines.append("")
            lines.append("(* No Ladder Diagram body found *)")
        
        lines.append(f"END_{pou_type}")
        
        return lines
    
    def _extract_variables(self, interface):
        """Extract variable declarations"""
        lines = []
        
        if interface is None:
            return lines
        
        # Input variables
        input_vars_section = interface.find('plc:inputVars', NAMESPACES)
        if input_vars_section is not None and len(input_vars_section) > 0:
            lines.append("VAR_INPUT")
            for var in input_vars_section.findall('plc:variable', NAMESPACES):
                var_name = var.get('name')
                var_type = self._get_type(var.find('.//plc:type', NAMESPACES))
                lines.append(f"    {var_name} : {var_type};")
            lines.append("END_VAR")
            lines.append("")
        
        # Output variables
        output_vars_section = interface.find('plc:outputVars', NAMESPACES)
        if output_vars_section is not None and len(output_vars_section) > 0:
            lines.append("VAR_OUTPUT")
            for var in output_vars_section.findall('plc:variable', NAMESPACES):
                var_name = var.get('name')
                var_type = self._get_type(var.find('.//plc:type', NAMESPACES))
                lines.append(f"    {var_name} : {var_type};")
            lines.append("END_VAR")
            lines.append("")
        
        # Local variables
        local_vars = interface.find('plc:localVars', NAMESPACES)
        if local_vars is not None and len(local_vars) > 0:
            lines.append("VAR")
            for var in local_vars.findall('plc:variable', NAMESPACES):
                var_name = var.get('name')
                var_type = self._get_type(var.find('.//plc:type', NAMESPACES))
                init_val = self._get_initial_value(var)
                
                if init_val:
                    lines.append(f"    {var_name} : {var_type} := {init_val};")
                else:
                    lines.append(f"    {var_name} : {var_type};")
            lines.append("END_VAR")
            lines.append("")
        
        return lines
    
    def _get_type(self, type_elem):
        """Extract variable type"""
        if type_elem is None:
            return "BOOL"
        
        for basic_type in ['BOOL', 'INT', 'DINT', 'REAL', 'STRING', 'BYTE', 'WORD', 'DWORD', 'TIME']:
            if type_elem.find(f'plc:{basic_type}', NAMESPACES) is not None:
                return basic_type
        
        return "BOOL"
    
    def _get_initial_value(self, var):
        """Extract initial value"""
        init_val = var.find('.//plc:initialValue', NAMESPACES)
        if init_val is not None:
            simple_val = init_val.find('plc:simpleValue', NAMESPACES)
            if simple_val is not None:
                return simple_val.get('value')
        return None
    
    def _convert_ladder(self, ld_body):
        """Convert Ladder Diagram body to ST logic"""
        lines = []
        lines.append("(* Logic from Ladder Diagram *)")
        lines.append("")
        
        # Parse left power rail
        left_rail = ld_body.find('plc:leftPowerRail', NAMESPACES)
        
        # Parse contacts (normally open, normally closed)
        contacts = ld_body.findall('plc:contact', NAMESPACES)
        
        # Parse coils (normal, set, reset, negated)
        coils = ld_body.findall('plc:coil', NAMESPACES)
        
        # Parse right power rail
        right_rail = ld_body.find('plc:rightPowerRail', NAMESPACES)
        
        # Simple ladder conversion: build rungs
        rungs = self._build_rungs(left_rail, contacts, coils, right_rail)
        
        for rung_num, rung in enumerate(rungs, 1):
            lines.append(f"(* Rung {rung_num} *)")
            lines.append(rung)
            lines.append("")
        
        if not rungs:
            lines.append("(* Complex ladder logic detected *)")
            lines.append("(* Manual conversion recommended *)")
            lines.append("")
            
            # List all contacts and coils found
            if contacts:
                lines.append("(* Contacts found: *)")
                for contact in contacts:
                    var_elem = contact.find('plc:variable', NAMESPACES)
                    if var_elem is not None:
                        var_name = var_elem.text
                        negated = contact.get('negated') == 'true'
                        lines.append(f"(*   - {var_name} {'(NC)' if negated else '(NO)'} *)")
            
            if coils:
                lines.append("(* Coils found: *)")
                for coil in coils:
                    var_elem = coil.find('plc:variable', NAMESPACES)
                    if var_elem is not None:
                        var_name = var_elem.text
                        storage = coil.get('storage', 'normal')
                        lines.append(f"(*   - {var_name} ({storage}) *)")
        
        return lines
    
    def _build_rungs(self, left_rail, contacts, coils, right_rail):
        """Build ladder rungs (simplified approach)"""
        rungs = []
        
        # Simple case: series contacts to single coil
        # Pattern: [Contact1] -AND- [Contact2] -AND- ... -> (Coil)
        
        if not coils:
            return rungs
        
        for coil in coils:
            var_elem = coil.find('plc:variable', NAMESPACES)
            if var_elem is None:
                continue
            
            coil_var = var_elem.text
            storage = coil.get('storage', 'normal')
            negated = coil.get('negated') == 'true'
            
            # Build condition from contacts
            conditions = []
            for contact in contacts:
                contact_var_elem = contact.find('plc:variable', NAMESPACES)
                if contact_var_elem is not None:
                    contact_var = contact_var_elem.text
                    contact_negated = contact.get('negated') == 'true'
                    
                    if contact_negated:
                        conditions.append(f"NOT {contact_var}")
                    else:
                        conditions.append(contact_var)
            
            # Generate assignment
            if conditions:
                condition_expr = " AND ".join(conditions)
            else:
                condition_expr = "TRUE"
            
            if storage == 'set':
                # Set coil (latch)
                rung = f"IF {condition_expr} THEN\n    {coil_var} := TRUE;\nEND_IF;"
            elif storage == 'reset':
                # Reset coil
                rung = f"IF {condition_expr} THEN\n    {coil_var} := FALSE;\nEND_IF;"
            else:
                # Normal coil
                if negated:
                    rung = f"{coil_var} := NOT ({condition_expr});"
                else:
                    rung = f"{coil_var} := {condition_expr};"
            
            rungs.append(rung)
        
        return rungs


def main():
    """CLI entry point"""
    if len(sys.argv) < 2:
        print("Usage: python 3_ladder_to_st_converter.py <input.xml> [output.st]")
        print("\nExample:")
        print("  python 3_ladder_to_st_converter.py program.xml")
        print("  python 3_ladder_to_st_converter.py program.xml output.st")
        sys.exit(1)
    
    input_file = sys.argv[1]
    output_file = sys.argv[2] if len(sys.argv) > 2 else None
    
    if not Path(input_file).exists():
        print(f"Error: File '{input_file}' not found!")
        sys.exit(1)
    
    try:
        converter = LadderToSTConverter(input_file)
        st_code = converter.convert()
        
        if output_file:
            with open(output_file, 'w') as f:
                f.write(st_code)
            print(f"✅ Ladder Diagram converted successfully!")
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
