#!/usr/bin/env python3
"""
AI-Powered ST Test Case Generator
===================================
Uses OpenAI to analyze a TESTABLE IEC 61131-3 Structured Text (.st) file and
generate comprehensive test cases saved as a CSV compatible with test_generator.py.

Pre-requisite:
    First convert your ST file to a testable form using the dedicated converter:
        python plc_converters/5_st_to_testable_converter.py program.st
    This produces <program>_testable.st with Modbus-writable AT address bindings.
    Feed THAT file to this script.

Usage:
    python ai_test_generator.py program_testable.st
    python ai_test_generator.py program_testable.st -o my_tests.csv
    python ai_test_generator.py program_testable.st --num-tests 40
    python ai_test_generator.py program_testable.st --model gpt-4o

Environment:
    OPENAI_API_KEY  — your OpenAI API key (required)

CSV Output Format (compatible with test_generator.py):
    Test_ID, Delay_ms, Description,
    Input_<name> (<address>), ...,
    Expected_<name> (<address>), ...
"""

import sys
import os
import csv
import json
import argparse
import re
from pathlib import Path
from typing import Optional, List, Dict, Tuple

try:
    from openai import OpenAI
except ImportError:
    print("Error: openai package not installed. Run: pip install openai")
    sys.exit(1)


# ──────────────────────────────────────────────────────────────────────────────
# ST File Parser  — extracts variables and assigns IEC 61131-3 addresses
# ──────────────────────────────────────────────────────────────────────────────

class STParser:
    """
    Parses a TESTABLE IEC 61131-3 ST file produced by 5_st_to_testable_converter.py.

    Variables live in a plain VAR block with AT address bindings.
    Classification is done by address pattern — no AI involvement:
      Inputs  : AT %QX1.x / %QW1+   (Modbus-writable, driven by test runner)
      Outputs : AT %QX0.x / %QW0    (Modbus-readable, checked by test runner)
    """

    # Types that map to word (register) addresses
    WORD_TYPES = {'INT', 'UINT', 'DINT', 'UDINT', 'WORD', 'DWORD', 'REAL', 'LREAL'}

    # Function-block / timer types — never physical I/O
    FB_TYPES = {'R_TRIG', 'F_TRIG', 'TON', 'TOF', 'TP', 'CTU', 'CTD', 'CTUD',
                'SR', 'RS', 'SEMA'}
    SKIP_TYPES = FB_TYPES | {'TIME', 'DATE', 'DT', 'TOD'}

    def __init__(self, st_code: str):
        self.st_code = st_code
        self.program_name: str = ''
        self.inputs: List[dict] = []   # [{'name', 'type', 'address'}]
        self.outputs: List[dict] = []  # [{'name', 'type', 'address'}]
        self.constants: Dict = {}      # name → value  (from VAR CONSTANT)
        self.has_edge_triggers: bool = False


    # ── Block extraction ─────────────────────────────────────────────────────
    @staticmethod
    def _extract_block(text: str, keyword: str) -> Optional[str]:
        """Extract the content of a named VAR block (e.g. VAR_INPUT…END_VAR)."""
        pattern = rf'{keyword}\b(.*?)END_VAR'
        m = re.search(pattern, text, re.DOTALL | re.IGNORECASE)
        return m.group(1) if m else None

    @staticmethod
    def _parse_declarations(block: str) -> List[Tuple[str, str, Optional[str]]]:
        """
        Parse 'name [AT %addr] : TYPE [;]' declarations.
        Returns list of (name, TYPE, at_address_or_None).
        """
        result = []
        for decl in block.split(';'):
            decl = decl.strip()
            if not decl:
                continue
            # Remove inline comments
            decl = re.sub(r'\(\*.*?\*\)', '', decl, flags=re.DOTALL).strip()
            # Match:  name  [AT %...address...]  :  TYPE
            m = re.match(
                r'(\w+)\s*(?:AT\s+(%[^\s:]+))?\s*:\s*(\w+)',
                decl, re.IGNORECASE
            )
            if m:
                result.append((m.group(1), m.group(3).upper(), m.group(2)))
        return result

    # ── Extract constants ────────────────────────────────────────────────────
    def _extract_constants(self):
        block = self._extract_block(self.st_code, r'VAR\s+CONSTANT')
        if not block:
            return
        for decl in block.split(';'):
            m = re.match(r'\s*(\w+)\s*:\s*\w+\s*:=\s*([^\s;]+)', decl)
            if m:
                try:
                    self.constants[m.group(1)] = float(m.group(2))
                except ValueError:
                    self.constants[m.group(1)] = m.group(2)

    # ── Main parse ───────────────────────────────────────────────────────────
    def parse(self):
        # Program name — strip block comments first so header comment lines
        # like "Program Name : Foo" don't shadow the actual PROGRAM keyword.
        _code_no_comments = re.sub(r'\(\*.*?\*\)', '', self.st_code, flags=re.DOTALL)
        m = re.search(r'^\s*PROGRAM\s+(\w+)', _code_no_comments, re.IGNORECASE | re.MULTILINE)
        self.program_name = m.group(1) if m else 'Unknown'

        # Constants
        self._extract_constants()

        # Detect edge-trigger FBs in the full source
        self.has_edge_triggers = bool(
            re.search(r'\bR_TRIG\b|\bF_TRIG\b', self.st_code, re.IGNORECASE)
        )

    # Plain VAR block — classify by AT address direction letter:
        #   Outputs : %QX100.x / %QW100-199 / %QD100-199  (offset 100, readable by test runner)
        #   Inputs  : %QX200.x+ / %QW200+   / %QD200+     (offset 200, writable by test runner)
        #   No AT binding → internal variable, skipped silently
        #
        # Both inputs and outputs use %Q space (holding registers / coils) so
        # that an external Modbus client can WRITE inputs.  %IW/%IX are legacy
        # read-only input registers and are treated as inputs when encountered.
        plain_block = self._extract_block(self.st_code, r'(?<!_)(?<!\w)VAR(?!\s+CONSTANT)(?!_)')
        if plain_block:
            for name, vtype, at_addr in self._parse_declarations(plain_block):
                if vtype in self.SKIP_TYPES or not at_addr:
                    continue
                is_output = self._addr_is_output(at_addr)
                if is_output:
                    self.outputs.append({'name': name, 'type': vtype, 'address': at_addr})
                else:
                    self.inputs.append({'name': name, 'type': vtype, 'address': at_addr})

    @staticmethod
    def _addr_is_output(at_addr: str) -> bool:
        """
        Return True if the AT address belongs to an output channel.

        Convention (st_to_testable_converter.py):
          Outputs : %QW100-199 / %QD100-199 / %QX100.x (bit index 800-1599)
          Inputs  : %QW200+    / %QD200+    / %QX200.x (bit index 1600+)
          Legacy  : %IW / %IX  → always treated as inputs (read-only Modbus)
                    %QW0-99    → physical outputs (treat as output)
        """
        import re as _re
        a = at_addr.upper().strip()
        # Legacy input registers (%IW / %IX / %ID) → input
        if a.startswith('%I'):
            return False
        # Word addresses: %QW or %QD
        m = _re.match(r'%Q[DW](\d+)$', a)
        if m:
            return int(m.group(1)) < 200   # 0-199 = output, 200+ = input
        # Bit addresses: %QX<byte>.<bit>
        m = _re.match(r'%QX(\d+)\.(\d+)$', a)
        if m:
            bit_idx = int(m.group(1)) * 8 + int(m.group(2))
            return bit_idx < 1600          # 0-1599 = output, 1600+ = input
        return False   # unknown format → treat as input

    @property
    def has_explicit_io(self) -> bool:
        return bool(self.inputs or self.outputs)



# ──────────────────────────────────────────────────────────────────────────────
# OpenAI Integration
# ──────────────────────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """\
You are an expert PLC test engineer specializing in IEC 61131-3 Structured Text.
You analyze ST programs and produce comprehensive test cases that:
  1. Test boundary values (min, max, one-below-min, one-above-max) for every numeric input
  2. Test all combinations of boolean error/trip/mode flags
  3. Cover all logic branches (AND, OR, NOT, comparisons)
  4. Verify normal in-range operation
  5. Verify out-of-range / fault conditions

Modbus testing constraints (CRITICAL):
  - All input variables are at %QX200.x (BOOL) / %QW200+ (INT) addresses — writable via Modbus FC5/FC6.
  - All output variables are at %QX100.x (BOOL) / %QW100-199 (INT) addresses — readable via Modbus FC1/FC3.
  - The test runner writes inputs then waits delay_ms before reading outputs.
  - Each test row is independent; do NOT assume state carries over from a previous row.

IEC 61131-3 Standard Function Semantics (CRITICAL — compute expected outputs exactly):
  SEL(G, IN0, IN1):
    G = FALSE (0) → returns IN0   (first data argument)
    G = TRUE  (1) → returns IN1   (second data argument)
    Example: SEL(BYPASS, ACK, RQ) with BYPASS=1, ACK=1, RQ=0  →  result = RQ = 0
    WARNING: This is the OPPOSITE of a typical conditional expression. G=1 selects IN1, NOT IN0.

  MUX(K, IN0, IN1, ..., INn):
    Returns the INk-th argument (0-indexed). MUX(0,A,B,C)=A; MUX(1,A,B,C)=B; MUX(2,A,B,C)=C.

  LIMIT(MN, IN, MX):  returns MN if IN<MN, MX if IN>MX, otherwise IN.

  MAX(IN0,IN1,...) / MIN(IN0,IN1,...): return the largest / smallest value.

  SR (Set-dominant latch):  Q1 := S1 OR (NOT RESET1 AND Q1)
  RS (Reset-dominant latch): Q1 := NOT R1 AND (S AND Q1)    ← RESET wins if both=1

  TON (on-delay timer):  Q becomes TRUE only after IN has been TRUE for >= PT.
    The test runner works as follows: (1) write inputs to PLC, (2) wait delay_ms real
    milliseconds (the PLC scans every ~20ms during this wait), (3) read outputs.
    Therefore:
      • If IN evaluates to TRUE when inputs are applied AND delay_ms >= PT  →  Q = TRUE
      • If IN evaluates to FALSE when inputs are applied                    →  Q = FALSE
      • If delay_ms < PT (rare)                                            →  Q = FALSE
    Example: TON(IN := ACK AND RQ, PT := TIME#14ms) with delay_ms=100 ms:
      ACK=1, RQ=1 → IN=1, 100ms >= 14ms → Q = TRUE  (HVAC_RN = 1)
      ACK=1, RQ=0 → IN=0               → Q = FALSE (HVAC_RN = 0)
    CRITICAL: Do NOT set timer outputs to 0 just because each test is "independent".
    Independence means inputs are written fresh each test — but the PLC still runs
    for delay_ms after each write. If IN=TRUE and delay_ms >= PT, Q WILL be TRUE.

  TOF (off-delay timer):  Q becomes FALSE only after IN has been FALSE for >= PT.
    If IN=TRUE when inputs are applied → Q = TRUE at readback.
    If IN=FALSE and delay_ms >= PT     → Q = FALSE at readback.

  Instruction: before writing each test case's expected_outputs, trace through every
  expression in the ST code step-by-step with the chosen input values. For every timer
  FB, explicitly compute: "IN = <expression> = <value>. delay_ms=<N> vs PT=<M>. Q=<result>."
  Only write what the code actually computes — do not guess or use intuition about function names alone.

Edge-trigger (R_TRIG / F_TRIG) handling:
  - If the program uses R_TRIG, the trigger fires only on a 0→1 transition.
  - To start the pump/latch in a test, first include a row that sets the input to 0, then
    a row that sets it to 1. Both rows are needed to guarantee a rising edge.
  - Always reset latch/trigger inputs to 0 before a new start sequence in later tests.

Return a JSON object ONLY — no markdown, no prose, just the JSON.
"""


def _build_prompt_known_io(parser: STParser, num_tests: int) -> str:
    """Prompt used when VAR_INPUT / VAR_OUTPUT were parsed locally."""
    inputs_desc = "\n".join(
        f"  {v['name']} ({v['type']}) → {v['address']}" for v in parser.inputs
    )
    outputs_desc = "\n".join(
        f"  {v['name']} ({v['type']}) → {v['address']}" for v in parser.outputs
    )
    constants_desc = ""
    if parser.constants:
        constants_desc = "Constants:\n" + "\n".join(
            f"  {k} = {v}" for k, v in parser.constants.items()
        )

    edge_note = ""
    if parser.has_edge_triggers:
        edge_note = (
            "\nEDGE-TRIGGER NOTE: This program uses R_TRIG/F_TRIG.\n"
            "  To activate a latch, you MUST generate a 0→1 transition.\n"
            "  Represent this as TWO consecutive test rows:\n"
            "    Row A: set trigger input = 0 (reset), expected output = current state\n"
            "    Row B: set trigger input = 1 (rising edge), expected output = latched state\n"
            "  Always include such a pair before any test that needs the latch active.\n"
            "  After a stop condition, set trigger input back to 0 before the next start sequence.\n"
        )

    return f"""Analyze this IEC 61131-3 ST program and generate {num_tests} test cases.

Program: {parser.program_name}

Inputs (already address-assigned — do NOT change):
{inputs_desc}

Outputs (already address-assigned — do NOT change):
{outputs_desc}

{constants_desc}
{edge_note}
ST Code:
```
{parser.st_code}
```

Return ONLY this JSON schema, no extra text:
{{
  "test_cases": [
    {{
      "test_id": 1,
      "delay_ms": 100,
      "description": "<clear description>",
      "inputs":           {{ "<var_name>": <value>, ... }},
      "expected_outputs": {{ "<var_name>": <value>, ... }}
    }}
  ]
}}

Rules:
- Use 0/1 for BOOL values (0 = FALSE, 1 = TRUE)
- Use integer values for INT inputs
- Each test row is self-contained; assume ALL inputs default to 0 at the start of each row
- Cover ALL boundary values and ALL boolean flag combinations
- Descriptions must be human-readable (e.g. "Boundary min (-55): all errors active")
- Generate exactly {num_tests} test cases
- For EVERY test case, evaluate each output expression by substituting the exact input
  values from that row into the ST code. Do NOT guess — trace the expressions literally.
- Pay special attention to SEL(G, IN0, IN1): G=1 returns the SECOND argument (IN1), not the first.
- For TON timers: if the IN expression evaluates to TRUE and delay_ms >= PT, Q = TRUE (1).
  NEVER output Q=0 for a TON whose IN=TRUE and delay_ms >= PT — it will always have fired.
"""

def call_openai(client: OpenAI, model: str, st_parser: STParser, num_tests: int) -> dict:
    user_prompt = _build_prompt_known_io(st_parser, num_tests)

    print(f"  Sending request to OpenAI ({model}) …")
    response = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user",   "content": user_prompt},
        ],
        response_format={"type": "json_object"},
        temperature=0.2,
    )
    raw = response.choices[0].message.content
    return json.loads(raw)


# ──────────────────────────────────────────────────────────────────────────────
# Merge AI response with locally-parsed structure
# ──────────────────────────────────────────────────────────────────────────────

def merge_result(ai_result: dict, parser: STParser) -> Tuple[list, list, list]:
    """Returns (inputs, outputs, test_cases) using addresses already parsed from the file."""
    inputs     = parser.inputs
    outputs    = parser.outputs
    test_cases = ai_result.get('test_cases', [])
    for i, tc in enumerate(test_cases, start=1):
        tc['test_id'] = i
    return inputs, outputs, test_cases


# ──────────────────────────────────────────────────────────────────────────────
# CSV generation
# ──────────────────────────────────────────────────────────────────────────────

def build_headers(inputs: list, outputs: list) -> List[str]:
    headers = ['Test_ID', 'Delay_ms', 'Description']
    for v in inputs:
        headers.append(f"Input_{v['name']} ({v['address']})")
    for v in outputs:
        headers.append(f"Expected_{v['name']} ({v['address']})")
    return headers


def save_csv(path: str, inputs: list, outputs: list, test_cases: list) -> None:
    headers = build_headers(inputs, outputs)
    input_names  = [v['name'] for v in inputs]
    output_names = [v['name'] for v in outputs]

    with open(path, 'w', newline='', encoding='utf-8') as f:
        writer = csv.writer(f)
        writer.writerow(headers)
        for tc in test_cases:
            row = [
                tc.get('test_id', ''),
                tc.get('delay_ms', 100),
                tc.get('description', ''),
            ]
            for name in input_names:
                row.append(tc.get('inputs', {}).get(name, 0))
            for name in output_names:
                row.append(tc.get('expected_outputs', {}).get(name, 0))
            writer.writerow(row)


# ──────────────────────────────────────────────────────────────────────────────
# CLI entry point
# ──────────────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description='Generate PLC test cases from an ST file using OpenAI.'
    )
    parser.add_argument('st_file',
                        help='Path to the Structured Text (.st) file')
    parser.add_argument('-o', '--output', default=None,
                        help='Output CSV file path (default: test_cases_<stem>.csv '
                             'next to the ST file)')
    parser.add_argument('--num-tests', type=int, default=32,
                        help='Number of test cases to generate (default: 32)')
    parser.add_argument('--model', default='gpt-4o',
                        help='OpenAI model (default: gpt-4o)')
    args = parser.parse_args()

    # ── Validate ST file ─────────────────────────────────────────────────────
    st_path = Path(args.st_file)
    if not st_path.exists():
        print(f"Error: ST file not found: {st_path}")
        sys.exit(1)

    # ── OpenAI API key ───────────────────────────────────────────────────────
    #api_key = os.environ.get('OPENAI_API_KEY', '').strip()
    api_key = os.getenv('OPENAI_API_KEY')
    if not api_key:
        print("Error: OPENAI_API_KEY environment variable is not set.")
        print("  Set it with:  export OPENAI_API_KEY='sk-...'")
        sys.exit(1)

    # ── Determine output path ────────────────────────────────────────────────
    if args.output:
        output_path = Path(args.output)
    else:
        output_path = st_path.parent / f"test_cases_{st_path.stem}.csv"

    # ── Read and parse ST file ───────────────────────────────────────────────
    print(f"Reading:  {st_path}")
    st_code = st_path.read_text(encoding='utf-8')

    st_parser = STParser(st_code)
    st_parser.parse()

    print(f"Program:  {st_parser.program_name}")

    if not (st_parser.inputs or st_parser.outputs):
        print("Error: No I/O variables with AT address bindings found.")
        print("  Run 5_st_to_testable_converter.py on your ST file first.")
        sys.exit(1)
    inputs_str  = ', '.join(v['name'] + ' (' + v['address'] + ')' for v in st_parser.inputs)
    outputs_str = ', '.join(v['name'] + ' (' + v['address'] + ')' for v in st_parser.outputs)
    print(f"Inputs  : {inputs_str}")
    print(f"Outputs : {outputs_str}")

    if st_parser.constants:
        consts_str = ', '.join(k + '=' + str(v) for k, v in st_parser.constants.items())
        print(f"Constants: {consts_str}")

    # ── Call OpenAI ──────────────────────────────────────────────────────────
    client = OpenAI(api_key=api_key)

    try:
        ai_result = call_openai(client, args.model, st_parser, args.num_tests)
    except json.JSONDecodeError as e:
        print(f"Error: OpenAI returned invalid JSON: {e}")
        sys.exit(1)
    except Exception as e:
        print(f"Error calling OpenAI: {e}")
        sys.exit(1)

    # ── Merge and validate ───────────────────────────────────────────────────
    inputs, outputs, test_cases = merge_result(ai_result, st_parser)

    if not test_cases:
        print("Error: OpenAI returned 0 test cases.")
        sys.exit(1)


    print(f"Generated {len(test_cases)} test cases.")

    # ── Save CSV ─────────────────────────────────────────────────────────────
    save_csv(str(output_path), inputs, outputs, test_cases)
    print(f"Saved:    {output_path}")
    print()
    print("Next steps:")
    print(f"  1. Load '{st_path.name}' on the PLC runtime.")
    print(f"  2. Run: python3 test_generators/test_generator.py -f {output_path}")


if __name__ == '__main__':
    main()
