"""
SFC (Sequential Function Chart) to Structured Text Converter
=============================================================
This script converts Sequential Function Charts from PLCopen XML to ST code.
It generates state machine logic with steps, transitions, and actions.

Installation:
    pip install lxml

Usage:
    python 4_sfc_to_st_converter.py input.xml output.st
    python 4_sfc_to_st_converter.py input.xml  (outputs to console)
"""

import sys
from lxml import etree
from pathlib import Path

# Namespace for PLCopen XML
NAMESPACES = {
    'plc': 'http://www.plcopen.org/xml/tc6_0200',
    'xhtml': 'http://www.w3.org/1999/xhtml'
}


class SFCStep:
    """Represents an SFC step"""
    def __init__(self, name, initial=False):
        self.name = name
        self.initial = initial
        self.actions = []
        self.transitions = []


class SFCTransition:
    """Represents an SFC transition"""
    def __init__(self, name, condition):
        self.name = name
        self.condition = condition
        self.from_step = None
        self.to_step = None


class SFCToSTConverter:
    """Convert Sequential Function Charts to Structured Text"""
    
    def __init__(self, xml_file):
        self.xml_file = xml_file
        self.tree = etree.parse(xml_file)
        self.root = self.tree.getroot()
    
    def convert(self):
        """Main conversion method"""
        st_code = []
        
        st_code.append("(*")
        st_code.append(f"  Generated from: {Path(self.xml_file).name}")
        st_code.append(f"  Converter: SFC (Sequential Function Chart) to ST")
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
        """Convert a single POU with SFC body"""
        lines = []
        
        pou_name = pou.get('name')
        pou_type = pou.get('pouType').upper()
        
        lines.append(f"{pou_type} {pou_name}")
        
        # Extract interface
        interface = pou.find('plc:interface', NAMESPACES)
        var_lines, step_vars = self._extract_variables(interface)
        lines.extend(var_lines)
        
        # Extract SFC body
        body = pou.find('plc:body', NAMESPACES)
        sfc_body = body.find('plc:SFC', NAMESPACES) if body is not None else None
        
        if sfc_body is not None:
            logic_lines, additional_vars = self._convert_sfc(sfc_body, step_vars)
            
            # Add state machine variables if needed
            if additional_vars:
                lines.append("VAR")
                for var_line in additional_vars:
                    lines.append(f"    {var_line}")
                lines.append("END_VAR")
                lines.append("")
            
            lines.extend(logic_lines)
        else:
            lines.append("")
            lines.append("(* No SFC body found *)")
        
        lines.append(f"END_{pou_type}")
        
        return lines
    
    def _extract_variables(self, interface):
        """Extract variable declarations"""
        lines = []
        step_vars = []
        
        if interface is None:
            return lines, step_vars
        
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
                
                # Track step variables
                if var_name.endswith('.X') or 'step' in var_name.lower():
                    step_vars.append(var_name)
                
                if init_val:
                    lines.append(f"    {var_name} : {var_type} := {init_val};")
                else:
                    lines.append(f"    {var_name} : {var_type};")
            lines.append("END_VAR")
            lines.append("")
        
        return lines, step_vars
    
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
    
    def _convert_sfc(self, sfc_body, step_vars):
        """Convert SFC body to ST state machine logic"""
        lines = []
        additional_vars = []
        
        lines.append("(* State Machine from SFC *)")
        lines.append("")
        
        # Parse steps
        steps = []
        initial_step = None
        
        for step_elem in sfc_body.findall('plc:step', NAMESPACES):
            step_name = step_elem.get('name')
            is_initial = step_elem.get('initialStep') == 'true'
            
            step = SFCStep(step_name, is_initial)
            if is_initial:
                initial_step = step_name
            
            steps.append(step)
        
        # Parse transitions
        transitions = []
        for trans_elem in sfc_body.findall('plc:transition', NAMESPACES):
            trans_name = trans_elem.get('name', f'Trans{len(transitions)}')
            
            # Get transition condition
            condition = self._extract_transition_condition(trans_elem)
            
            transition = SFCTransition(trans_name, condition)
            transitions.append(transition)
        
        # Parse actions
        actions = []
        for action_elem in sfc_body.findall('plc:actionBlock', NAMESPACES):
            action_name = action_elem.get('name', 'Action')
            actions.append(action_name)
        
        # Generate state machine variables
        if not step_vars:
            additional_vars.append("current_state : INT := 0;")
            for i, step in enumerate(steps):
                additional_vars.append(f"{step.name}_active : BOOL := {'TRUE' if step.initial else 'FALSE'};")
        
        # Generate state machine logic
        lines.append("(* Step activation logic *)")
        
        if initial_step:
            lines.append(f"(* Initial step: {initial_step} *)")
            lines.append("")
        
        # Generate CASE statement for state machine
        if steps:
            lines.append("(* State machine implementation *)")
            lines.append("CASE current_state OF")
            
            for i, step in enumerate(steps):
                lines.append(f"    {i}: (* Step: {step.name} *)")
                lines.append(f"        (* Actions for {step.name} *)")
                
                # Add transition logic
                if i < len(transitions) and transitions[i].condition:
                    lines.append(f"        IF {transitions[i].condition} THEN")
                    next_step = i + 1 if i + 1 < len(steps) else 0
                    lines.append(f"            current_state := {next_step};")
                    lines.append(f"        END_IF;")
                
                lines.append("")
            
            lines.append("END_CASE;")
        else:
            lines.append("(* No steps defined in SFC *)")
            lines.append("(* Steps: *)")
            for step_var in step_vars:
                lines.append(f"(*   - {step_var} *)")
            
            lines.append("")
            lines.append("(* Transitions: *)")
            for trans in transitions:
                lines.append(f"(*   - {trans.name}: {trans.condition} *)")
            
            lines.append("")
            lines.append("(* Actions: *)")
            for action in actions:
                lines.append(f"(*   - {action} *)")
        
        return lines, additional_vars
    
    def _extract_transition_condition(self, trans_elem):
        """Extract transition condition expression"""
        # Try inline condition
        inline_st = trans_elem.find('.//plc:inline/plc:ST', NAMESPACES)
        if inline_st is not None and inline_st.text:
            return inline_st.text.strip()
        
        # Try reference
        ref = trans_elem.find('plc:reference', NAMESPACES)
        if ref is not None:
            ref_name = ref.get('name')
            if ref_name:
                return ref_name
        
        # Try condition element
        condition_elem = trans_elem.find('plc:condition', NAMESPACES)
        if condition_elem is not None:
            # Try various condition formats
            connection = condition_elem.find('.//plc:connection', NAMESPACES)
            if connection is not None:
                ref_id = connection.get('refLocalId')
                return f"condition_{ref_id}"
        
        return "TRUE (* Condition not specified *)"


def main():
    """CLI entry point"""
    if len(sys.argv) < 2:
        print("Usage: python 4_sfc_to_st_converter.py <input.xml> [output.st]")
        print("\nExample:")
        print("  python 4_sfc_to_st_converter.py program.xml")
        print("  python 4_sfc_to_st_converter.py program.xml output.st")
        sys.exit(1)
    
    input_file = sys.argv[1]
    output_file = sys.argv[2] if len(sys.argv) > 2 else None
    
    if not Path(input_file).exists():
        print(f"Error: File '{input_file}' not found!")
        sys.exit(1)
    
    try:
        converter = SFCToSTConverter(input_file)
        st_code = converter.convert()
        
        if output_file:
            with open(output_file, 'w') as f:
                f.write(st_code)
            print(f"✅ SFC converted successfully!")
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
