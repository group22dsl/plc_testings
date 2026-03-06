"""
FBD (Function Block Diagram) to Structured Text Converter
==========================================================
This script converts F        
        # Local variables (constants) - FIND ALL localVars SECTIONS
        all_local_const_sections = interface.findall('plc:localVars[@constant="true"]', NAMESPACES)
        if all_local_const_sections:
            lines.append("VAR CONSTANT")
            for local_vars in all_local_const_sections:
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
        
        # Local variables (non-constant) - FIND ALL non-constant localVars SECTIONS
        all_local_sections = [lv for lv in interface.findall('plc:localVars', NAMESPACES) 
                             if lv.get('constant') != 'true']
        if all_local_sections:
            lines.append("VAR")
            for local_vars in all_local_sections:
                for var in local_vars.findall('plc:variable', NAMESPACES):
                    var_name = var.get('name')
                    var_type = self._get_type(var.find('.//plc:type', NAMESPACES))
                    init_val = self._get_initial_value(var)
                    
                    if init_val:
                        lines.append(f"    {var_name} : {var_type} := {init_val};")
                    else:
                        lines.append(f"    {var_name} : {var_type};")
            lines.append("END_VAR")
            lines.append("")grams from PLCopen XML to ST code.
It analyzes the block connections and generates equivalent ST logic.

Installation:
    pip install lxml networkx

Usage:
    python 2_fbd_to_st_converter.py input.xml output.st
    python 2_fbd_to_st_converter.py input.xml  (outputs to console)
"""

import sys
from lxml import etree
from pathlib import Path
from collections import defaultdict, OrderedDict

# Namespace for PLCopen XML
NAMESPACES = {
    'plc': 'http://www.plcopen.org/xml/tc6_0200',
    'xhtml': 'http://www.w3.org/1999/xhtml'
}


class FBDBlock:
    """Represents a Function Block Diagram block"""
    def __init__(self, local_id, block_type):
        self.local_id = local_id
        self.block_type = block_type
        self.inputs = {}  # param_name -> connected_block_id
        self.outputs = {}
        self.expression = None  # For inVariable/outVariable
        self.temp_var = None


class FBDToSTConverter:
    """Convert FBD diagrams to Structured Text"""
    
    def __init__(self, xml_file):
        self.xml_file = xml_file
        self.tree = etree.parse(xml_file)
        self.root = self.tree.getroot()
        self.blocks = {}
        self.temp_var_counter = 0
    
    def convert(self):
        """Main conversion method"""
        st_code = []
        
        st_code.append("(*")
        st_code.append(f"  Generated from: {Path(self.xml_file).name}")
        st_code.append(f"  Converter: FBD to ST")
        st_code.append("*)")
        st_code.append("")
        
        # Extract all POUs
        pous = self.root.findall('.//plc:pou', NAMESPACES)
        
        # Store POU names for later use in CONFIGURATION
        pou_names = []
        pou_lines = []
        
        for pou in pous:
            pou_name = pou.get('name')
            pou_type = pou.get('pouType', 'program')
            pou_names.append((pou_name, pou_type))
            pou_lines.extend(self._convert_pou(pou))
            pou_lines.append("")
        
        # Add POUs first
        st_code.extend(pou_lines)
        
        # Add OpenPLC required CONFIGURATION and RESOURCE
        st_code.append("(* OpenPLC Configuration *)")
        st_code.append("CONFIGURATION Config0")
        st_code.append("")
        st_code.append("  RESOURCE Res0 ON PLC")
        
        # Add TASK for programs
        for pou_name, pou_type in pou_names:
            if pou_type.lower() == 'program':
                st_code.append(f"    TASK task0(INTERVAL := T#20ms,PRIORITY := 0);")
                st_code.append(f"    PROGRAM instance0 WITH task0 : {pou_name};")
        
        st_code.append("  END_RESOURCE")
        st_code.append("")
        st_code.append("END_CONFIGURATION")
        
        return '\n'.join(st_code)
    
    def _convert_pou(self, pou):
        """Convert a single POU with FBD body"""
        lines = []
        
        pou_name = pou.get('name')
        pou_type = pou.get('pouType', 'program').upper()
        
        lines.append(f"{pou_type} {pou_name}")
        
        # Extract interface
        interface = pou.find('plc:interface', NAMESPACES)
        var_lines, input_vars, output_vars = self._extract_variables(interface)
        lines.extend(var_lines)
        
        # Extract FBD body
        body = pou.find('plc:body', NAMESPACES)
        fbd_body = body.find('plc:FBD', NAMESPACES) if body is not None else None
        
        if fbd_body is not None:
            logic_lines, temp_vars = self._convert_fbd(fbd_body, input_vars, output_vars)
            
            # Add temporary variables
            if temp_vars:
                lines.append("VAR")
                for temp_var in temp_vars:
                    lines.append(f"    {temp_var} : BOOL;")
                lines.append("END_VAR")
                lines.append("")
            
            # Add logic
            lines.extend(logic_lines)
        
        lines.append(f"END_{pou_type}")
        
        return lines
    
    def _extract_variables(self, interface):
        """Extract variable declarations"""
        lines = []
        input_vars = []
        output_vars = []
        
        if interface is None:
            return lines, input_vars, output_vars
        
        # Input variables - FIND ALL inputVars SECTIONS (not just the first one)
        all_input_vars_sections = interface.findall('plc:inputVars', NAMESPACES)
        if all_input_vars_sections:
            lines.append("VAR_INPUT")
            for input_vars_section in all_input_vars_sections:
                for var in input_vars_section.findall('plc:variable', NAMESPACES):
                    var_name = var.get('name')
                    var_type = self._get_type(var.find('.//plc:type', NAMESPACES))
                    init_val = self._get_initial_value(var)
                    
                    input_vars.append(var_name)
                    
                    if init_val:
                        lines.append(f"    {var_name} : {var_type} := {init_val};")
                    else:
                        lines.append(f"    {var_name} : {var_type};")
            lines.append("END_VAR")
            lines.append("")
        
        # Output variables - FIND ALL outputVars SECTIONS
        all_output_vars_sections = interface.findall('plc:outputVars', NAMESPACES)
        if all_output_vars_sections:
            lines.append("VAR_OUTPUT")
            for output_vars_section in all_output_vars_sections:
                for var in output_vars_section.findall('plc:variable', NAMESPACES):
                    var_name = var.get('name')
                    var_type = self._get_type(var.find('.//plc:type', NAMESPACES))
                    
                    output_vars.append(var_name)
                    lines.append(f"    {var_name} : {var_type};")
            lines.append("END_VAR")
            lines.append("")
        
        # Local variables (constants)
        local_vars = interface.find('plc:localVars', NAMESPACES)
        if local_vars is not None:
            is_constant = local_vars.get('constant') == 'true'
            lines.append("VAR CONSTANT" if is_constant else "VAR")
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
        
        return lines, input_vars, output_vars
    
    def _get_type(self, type_elem):
        """Extract variable type"""
        if type_elem is None:
            return "BOOL"
        
        for basic_type in ['BOOL', 'INT', 'DINT', 'REAL', 'STRING', 'BYTE', 'WORD', 'DWORD']:
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
    
    def _convert_fbd(self, fbd_body, input_vars, output_vars):
        """Convert FBD body to ST logic"""
        lines = []
        temp_vars = []
        
        lines.append("(* Logic from FBD *)")
        
        # Parse all blocks
        self.blocks = {}
        self.temp_var_counter = 0
        
        # Parse inVariables (inputs)
        for in_var in fbd_body.findall('plc:inVariable', NAMESPACES):
            local_id = in_var.get('localId')
            expr = in_var.find('plc:expression', NAMESPACES)
            expression = expr.text if expr is not None else ""
            
            block = FBDBlock(local_id, 'inVariable')
            block.expression = expression
            self.blocks[local_id] = block
        
        # Parse blocks (AND, OR, NOT, GE, LE, etc.)
        for block_elem in fbd_body.findall('plc:block', NAMESPACES):
            local_id = block_elem.get('localId')
            type_name = block_elem.get('typeName')
            
            block = FBDBlock(local_id, type_name)
            
            # Parse input connections
            for input_var in block_elem.findall('.//plc:inputVariables/plc:variable', NAMESPACES):
                formal_param = input_var.get('formalParameter', 'IN')
                conn_point = input_var.find('plc:connectionPointIn', NAMESPACES)
                if conn_point is not None:
                    conn = conn_point.find('plc:connection', NAMESPACES)
                    if conn is not None:
                        ref_local_id = conn.get('refLocalId')
                        block.inputs[formal_param] = ref_local_id
            
            self.blocks[local_id] = block
        
        # Parse outVariables (outputs)
        for out_var in fbd_body.findall('plc:outVariable', NAMESPACES):
            local_id = out_var.get('localId')
            expr = out_var.find('plc:expression', NAMESPACES)
            expression = expr.text if expr is not None else ""
            
            block = FBDBlock(local_id, 'outVariable')
            block.expression = expression
            
            # Get connection
            conn_point = out_var.find('plc:connectionPointIn', NAMESPACES)
            if conn_point is not None:
                conn = conn_point.find('plc:connection', NAMESPACES)
                if conn is not None:
                    ref_local_id = conn.get('refLocalId')
                    block.inputs['IN'] = ref_local_id
            
            self.blocks[local_id] = block
        
        # Generate code by traversing from outputs back to inputs
        for block_id, block in self.blocks.items():
            if block.block_type == 'outVariable':
                # This is an output assignment
                expr_code = self._generate_expression(block.inputs.get('IN'), temp_vars)
                lines.append(f"{block.expression} := {expr_code};")
        
        return lines, temp_vars
    
    def _generate_expression(self, block_id, temp_vars):
        """Recursively generate expression code"""
        if block_id is None:
            return "FALSE"
        
        block = self.blocks.get(block_id)
        if block is None:
            return "FALSE"
        
        # If it's an input variable, return its name
        if block.block_type == 'inVariable':
            return block.expression
        
        # If it's a logic block, generate the operation
        if block.block_type == 'AND':
            in1 = self._generate_expression(block.inputs.get('In1'), temp_vars)
            in2 = self._generate_expression(block.inputs.get('In2'), temp_vars)
            return f"({in1} AND {in2})"
        
        elif block.block_type == 'OR':
            in1 = self._generate_expression(block.inputs.get('In1'), temp_vars)
            in2 = self._generate_expression(block.inputs.get('In2'), temp_vars)
            return f"({in1} OR {in2})"
        
        elif block.block_type == 'NOT':
            in1 = self._generate_expression(block.inputs.get('In1'), temp_vars)
            return f"NOT {in1}"
        
        elif block.block_type == 'XOR':
            in1 = self._generate_expression(block.inputs.get('In1'), temp_vars)
            in2 = self._generate_expression(block.inputs.get('In2'), temp_vars)
            return f"({in1} XOR {in2})"
        
        elif block.block_type == 'GE':
            in1 = self._generate_expression(block.inputs.get('In1'), temp_vars)
            in2 = self._generate_expression(block.inputs.get('In2'), temp_vars)
            return f"({in1} >= {in2})"
        
        elif block.block_type == 'LE':
            in1 = self._generate_expression(block.inputs.get('In1'), temp_vars)
            in2 = self._generate_expression(block.inputs.get('In2'), temp_vars)
            return f"({in1} <= {in2})"
        
        elif block.block_type == 'GT':
            in1 = self._generate_expression(block.inputs.get('In1'), temp_vars)
            in2 = self._generate_expression(block.inputs.get('In2'), temp_vars)
            return f"({in1} > {in2})"
        
        elif block.block_type == 'LT':
            in1 = self._generate_expression(block.inputs.get('In1'), temp_vars)
            in2 = self._generate_expression(block.inputs.get('In2'), temp_vars)
            return f"({in1} < {in2})"
        
        elif block.block_type == 'EQ':
            in1 = self._generate_expression(block.inputs.get('In1'), temp_vars)
            in2 = self._generate_expression(block.inputs.get('In2'), temp_vars)
            return f"({in1} = {in2})"
        
        elif block.block_type == 'NE':
            in1 = self._generate_expression(block.inputs.get('In1'), temp_vars)
            in2 = self._generate_expression(block.inputs.get('In2'), temp_vars)
            return f"({in1} <> {in2})"
        
        return "FALSE"


def main():
    """CLI entry point"""
    if len(sys.argv) < 2:
        print("Usage: python 2_fbd_to_st_converter.py <input.xml> [output.st]")
        print("\nExample:")
        print("  python 2_fbd_to_st_converter.py program.xml")
        print("  python 2_fbd_to_st_converter.py program.xml output.st")
        sys.exit(1)
    
    input_file = sys.argv[1]
    output_file = sys.argv[2] if len(sys.argv) > 2 else None
    
    if not Path(input_file).exists():
        print(f"Error: File '{input_file}' not found!")
        sys.exit(1)
    
    try:
        converter = FBDToSTConverter(input_file)
        st_code = converter.convert()
        
        if output_file:
            with open(output_file, 'w') as f:
                f.write(st_code)
            print(f"✅ FBD converted successfully!")
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
