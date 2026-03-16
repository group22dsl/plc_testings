"""
ST Code to CSV Test Case Generator
===================================
Analyzes Structured Text (ST) code and generates a CSV file with test cases.

Usage:
    python st_to_csv_generator.py input.st -o test_cases.csv
    python st_to_csv_generator.py input.st --num-tests 10

Features:
  - Parses ST code to identify inputs (%IX, %IW) and outputs (%QX, %QW)
  - Detects timers (TON, TOF, TP) and extracts timing information
  - Generates appropriate test cases based on program logic
  - Creates CSV file compatible with test_generator.py
"""

import re
import sys
import argparse
import csv
from pathlib import Path
from typing import List, Dict, Tuple, Optional


class STCodeAnalyzer:
    """Analyzes Structured Text code to extract program structure."""
    
    def __init__(self, st_code: str):
        self.st_code = st_code
        self.inputs = []
        self.outputs = []
        self.timers = []
        self.variables = {}
        
    def analyze(self):
        """Main analysis method."""
        self._extract_variables()
        self._extract_timers()
        self._extract_logic()
        
    def _extract_variables(self):
        """Extract variable declarations from VAR section."""
        # Match VAR sections
        var_pattern = r'VAR\s+(.*?)\s+END_VAR'
        var_matches = re.finditer(var_pattern, self.st_code, re.DOTALL | re.IGNORECASE)
        
        for match in var_matches:
            var_section = match.group(1)
            # Parse individual variable declarations
            # Format: variable_name AT %address : TYPE := initial_value;
            # or:     variable_name : TYPE := initial_value;
            var_lines = var_section.split(';')
            for line in var_lines:
                line = line.strip()
                if not line:
                    continue
                
                # Check for AT declaration first: var_name AT %address : TYPE
                at_match = re.match(r'(\w+)\s+AT\s+(%[IQ][XW]\d+(?:\.\d+)?)\s*:\s*(\w+)(?:\s*:=\s*(.+))?', line, re.IGNORECASE)
                if at_match:
                    var_name = at_match.group(1)
                    address = at_match.group(2).upper()
                    var_type = at_match.group(3)
                    var_init = at_match.group(4) if at_match.group(4) else None
                    
                    self.variables[var_name] = {
                        'type': var_type,
                        'initial': var_init,
                        'address': address
                    }
                    
                    # Add to inputs or outputs
                    if address.startswith('%I'):
                        self.inputs.append({'name': var_name, 'address': address, 'type': var_type})
                    elif address.startswith('%Q'):
                        self.outputs.append({'name': var_name, 'address': address, 'type': var_type})
                    continue
                
                # Regular variable declaration: var_name : TYPE := initial_value
                var_match = re.match(r'(\w+)\s*:\s*(\w+)(?:\s*:=\s*(.+))?', line)
                if var_match:
                    var_name = var_match.group(1)
                    var_type = var_match.group(2)
                    var_init = var_match.group(3) if var_match.group(3) else None
                    
                    self.variables[var_name] = {
                        'type': var_type,
                        'initial': var_init
                    }
                    
                    # Classify as timer or regular variable
                    if var_type.upper() in ['TON', 'TOF', 'TP', 'TIMER']:
                        self.timers.append(var_name)
        
    def _extract_timers(self):
        """Extract timer configurations and preset times."""
        # Find timer calls: T1(IN := ..., PT := T#1s);
        timer_pattern = r'(\w+)\s*\(\s*IN\s*:=\s*([^,]+)\s*,\s*PT\s*:=\s*T#([^)]+)\)'
        timer_matches = re.finditer(timer_pattern, self.st_code, re.IGNORECASE)
        
        timer_info = {}
        for match in timer_matches:
            timer_name = match.group(1)
            timer_input = match.group(2).strip()
            timer_preset = match.group(3).strip()
            
            # Parse preset time (e.g., "1s", "500ms", "1.5s")
            delay_ms = self._parse_time_to_ms(timer_preset)
            
            timer_info[timer_name] = {
                'input': timer_input,
                'preset': timer_preset,
                'delay_ms': delay_ms
            }
        
        # Update timers list with additional info
        self.timer_configs = timer_info
        
    def _parse_time_to_ms(self, time_str: str) -> int:
        """Convert IEC time string to milliseconds."""
        time_str = time_str.strip().lower()
        
        # Match patterns like "1s", "500ms", "1.5s", "1000ms"
        if 'ms' in time_str:
            value = float(time_str.replace('ms', ''))
            return int(value)
        elif 's' in time_str:
            value = float(time_str.replace('s', ''))
            return int(value * 1000)
        elif 'm' in time_str:  # minutes
            value = float(time_str.replace('m', ''))
            return int(value * 60 * 1000)
        else:
            # Default: assume milliseconds
            return int(float(time_str))
    
    def _extract_logic(self):
        """Analyze program logic to identify input/output relationships."""
        # Look for AT declarations (physical I/O mapping)
        # Format: variable_name AT %IX0.0 : BOOL;
        at_pattern = r'(\w+)\s+AT\s+(%[IQ][XW]\d+(?:\.\d+)?)\s*:\s*(\w+)'
        at_matches = re.finditer(at_pattern, self.st_code, re.IGNORECASE)
        
        for match in at_matches:
            var_name = match.group(1)
            address = match.group(2)
            var_type = match.group(3)
            
            if address.startswith('%I'):
                self.inputs.append({'name': var_name, 'address': address, 'type': var_type})
            elif address.startswith('%Q'):
                self.outputs.append({'name': var_name, 'address': address, 'type': var_type})
        
        # Also look for direct address references in code
        addr_pattern = r'%([IQ])([XW])(\d+)(?:\.(\d+))?'
        addr_matches = re.finditer(addr_pattern, self.st_code, re.IGNORECASE)
        
        found_addresses = set()
        for match in addr_matches:
            io_type = match.group(1).upper()
            data_type = match.group(2).upper()
            byte_num = match.group(3)
            bit_num = match.group(4) if match.group(4) else None
            
            if bit_num:
                address = f"%{io_type}{data_type}{byte_num}.{bit_num}"
            else:
                address = f"%{io_type}{data_type}{byte_num}"
            
            if address in found_addresses:
                continue
            found_addresses.add(address)
            
            if io_type == 'I' and not any(i['address'] == address for i in self.inputs):
                self.inputs.append({'name': address, 'address': address, 'type': 'BOOL' if data_type == 'X' else 'INT'})
            elif io_type == 'Q' and not any(o['address'] == address for o in self.outputs):
                self.outputs.append({'name': address, 'address': address, 'type': 'BOOL' if data_type == 'X' else 'INT'})


class TestCaseGenerator:
    """Generates test cases based on analyzed ST code."""
    
    def __init__(self, analyzer: STCodeAnalyzer):
        self.analyzer = analyzer
        
    def generate_test_cases(self, num_tests: int = 5) -> List[Dict]:
        """Generate test cases based on program structure."""
        test_cases = []
        
        # Detect program type and generate appropriate tests
        if self.analyzer.timers or self.analyzer.timer_configs:
            # Timer-based program (like blinker)
            test_cases = self._generate_timer_tests(num_tests)
        elif self.analyzer.inputs and self.analyzer.outputs:
            # Logic-based program
            test_cases = self._generate_logic_tests(num_tests)
        else:
            # Generic tests
            test_cases = self._generate_generic_tests(num_tests)
        
        return test_cases
    
    def _generate_timer_tests(self, num_tests: int) -> List[Dict]:
        """Generate test cases for timer-based programs."""
        test_cases = []
        
        # Get timer delay (use first timer found)
        delay_ms = 100  # default
        if hasattr(self.analyzer, 'timer_configs') and self.analyzer.timer_configs:
            first_timer = list(self.analyzer.timer_configs.values())[0]
            delay_ms = first_timer.get('delay_ms', 100)
        
        # Generate varied test cases for timer behavior
        test_scenarios = [
            {
                'delay': int(delay_ms * 0.5),
                'desc': f'Test before timer expires ({delay_ms * 0.5:.0f}ms < {delay_ms}ms)',
                'expected': 'NO_CHANGE'
            },
            {
                'delay': int(delay_ms * 1.2),
                'desc': f'Test after first timer cycle ({delay_ms}ms elapsed)',
                'expected': 'TOGGLE'
            },
            {
                'delay': int(delay_ms * 0.8),
                'desc': f'Test partial second cycle ({delay_ms * 0.8:.0f}ms)',
                'expected': 'NO_CHANGE'
            },
            {
                'delay': int(delay_ms * 1.2),
                'desc': f'Test second complete cycle',
                'expected': 'TOGGLE'
            },
            {
                'delay': int(delay_ms * 1.2),
                'desc': f'Test third complete cycle',
                'expected': 'TOGGLE'
            },
            {
                'delay': int(delay_ms * 1.2),
                'desc': f'Test fourth complete cycle - verify consistent behavior',
                'expected': 'TOGGLE'
            },
            {
                'delay': int(delay_ms * 2.5),
                'desc': f'Test double cycle wait ({delay_ms * 2}ms)',
                'expected': 'TOGGLE'
            },
            {
                'delay': int(delay_ms * 0.2),
                'desc': f'Test very short delay ({delay_ms * 0.2:.0f}ms)',
                'expected': 'NO_CHANGE'
            },
        ]
        
        # Take only the requested number of tests
        for i, scenario in enumerate(test_scenarios[:num_tests], 1):
            test_case = {
                'Test_ID': i,
                'Delay_ms': scenario['delay'],
                'Description': scenario['desc']
            }
            
            # Add input columns if any
            for inp in self.analyzer.inputs:
                col_name = f"{inp['name']} ({inp['address']})"
                test_case[col_name] = ''  # Empty for timer-based tests
            
            # Add output columns
            if self.analyzer.outputs:
                for out in self.analyzer.outputs:
                    col_name = f"Expected_Output ({out['address']})"
                    test_case[col_name] = scenario['expected']
            else:
                # If no outputs detected, add a generic output column
                test_case['Expected_Output (%QX0.0)'] = scenario['expected']
            
            test_cases.append(test_case)
        
        return test_cases
    
    def _generate_logic_tests(self, num_tests: int) -> List[Dict]:
        """Generate test cases for logic-based programs."""
        test_cases = []
        
        # Generate combinations of inputs
        input_combinations = self._generate_input_combinations(min(num_tests, 2 ** len(self.analyzer.inputs)))
        
        for i, inputs in enumerate(input_combinations, 1):
            test_case = {
                'Test_ID': i,
                'Delay_ms': 100,
                'Description': f'Test case {i}: Input combination {inputs}'
            }
            
            # Add input values
            for j, inp in enumerate(self.analyzer.inputs):
                col_name = f"{inp['name']} ({inp['address']})"
                test_case[col_name] = inputs[j] if j < len(inputs) else 0
            
            # Add output placeholders
            for out in self.analyzer.outputs:
                col_name = f"Expected_Output ({out['address']})"
                test_case[col_name] = '?'  # User needs to fill in expected values
            
            test_cases.append(test_case)
        
        return test_cases
    
    def _generate_generic_tests(self, num_tests: int) -> List[Dict]:
        """Generate generic test cases."""
        test_cases = []
        
        for i in range(1, num_tests + 1):
            test_case = {
                'Test_ID': i,
                'Delay_ms': 100,
                'Description': f'Generic test case {i}'
            }
            
            # Add outputs if found
            for out in self.analyzer.outputs:
                col_name = f"Expected_Output ({out['address']})"
                test_case[col_name] = '?'
            
            test_cases.append(test_case)
        
        return test_cases
    
    def _generate_input_combinations(self, max_combinations: int) -> List[List[int]]:
        """Generate binary input combinations."""
        num_inputs = len(self.analyzer.inputs)
        if num_inputs == 0:
            return [[]]
        
        combinations = []
        for i in range(min(max_combinations, 2 ** num_inputs)):
            combination = []
            for j in range(num_inputs):
                bit = (i >> j) & 1
                combination.append(bit)
            combinations.append(combination)
        
        return combinations


def main():
    parser = argparse.ArgumentParser(
        description='Generate CSV test cases from Structured Text (ST) code',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s blink.st
  %(prog)s blink.st -o my_tests.csv
  %(prog)s program.st --num-tests 10
        """
    )
    
    parser.add_argument('input_file', help='Input ST code file')
    parser.add_argument('-o', '--output', help='Output CSV file (default: <input>_tests.csv)')
    parser.add_argument('-n', '--num-tests', type=int, default=5, help='Number of test cases to generate (default: 5)')
    parser.add_argument('-v', '--verbose', action='store_true', help='Verbose output')
    
    args = parser.parse_args()
    
    # Read input file
    input_path = Path(args.input_file)
    if not input_path.exists():
        print(f"Error: Input file '{args.input_file}' not found")
        sys.exit(1)
    
    with open(input_path, 'r') as f:
        st_code = f.read()
    
    if args.verbose:
        print(f"Reading ST code from: {input_path}")
        print(f"Code length: {len(st_code)} characters\n")
    
    # Analyze ST code
    analyzer = STCodeAnalyzer(st_code)
    analyzer.analyze()
    
    if args.verbose:
        print("Analysis Results:")
        print(f"  Variables: {list(analyzer.variables.keys())}")
        print(f"  Timers: {analyzer.timers}")
        if hasattr(analyzer, 'timer_configs'):
            print(f"  Timer configs: {analyzer.timer_configs}")
        print(f"  Inputs: {analyzer.inputs}")
        print(f"  Outputs: {analyzer.outputs}")
        print()
    
    # Generate test cases
    generator = TestCaseGenerator(analyzer)
    test_cases = generator.generate_test_cases(args.num_tests)
    
    if not test_cases:
        print("Warning: No test cases generated. Check if ST code is valid.")
        sys.exit(1)
    
    # Determine output filename
    if args.output:
        output_path = Path(args.output)
    else:
        output_path = input_path.with_name(f"{input_path.stem}_tests.csv")
    
    # Write CSV file
    if test_cases:
        fieldnames = list(test_cases[0].keys())
        
        with open(output_path, 'w', newline='') as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(test_cases)
        
        print(f"✓ Generated {len(test_cases)} test cases")
        print(f"✓ CSV file saved to: {output_path}")
        
        if args.verbose:
            print("\nGenerated test cases:")
            for tc in test_cases:
                print(f"  Test {tc['Test_ID']}: {tc.get('Description', 'N/A')}")
    else:
        print("Error: Failed to generate test cases")
        sys.exit(1)


if __name__ == '__main__':
    main()
