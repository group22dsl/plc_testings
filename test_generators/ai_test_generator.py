#!/usr/bin/env python3
"""
AI-Powered ST Test Case Generator
===================================
Uses OpenAI to analyze IEC 61131-3 Structured Text (ST) programs and generate
comprehensive test cases saved as a CSV file compatible with test_generator.py.

Usage:
    python ai_test_generator.py program.st
    python ai_test_generator.py program.st -o my_tests.csv
    python ai_test_generator.py program.st --num-tests 40
    python ai_test_generator.py program.st --model gpt-4o

Environment:
    OPENAI_API_KEY  — your OpenAI API key (required)

CSV Output Format (compatible with test_generator.py):
    Test_ID, Delay_ms, Description,
    Input_<name> (<address>), ...,
    Expected_<name> (<address>), ...

IMPORTANT — Modbus Writability:
    OpenPLC Modbus slave only allows WRITING to %QX (coils) and %QW (holding
    registers). The %IX discrete inputs and %IW input registers are READ-ONLY.

    For testing, ALL physical inputs must be bound to writable addresses:
      - Boolean inputs  → %QX1.0, %QX1.1, %QX1.2, ...  (coils 8, 9, 10, ...)
      - Integer inputs  → %QW1, %QW2, ...               (holding registers 1, 2, ...)
      - Boolean outputs → %QX0.0, %QX0.1, ...           (coils 0, 1, ...)
      - Integer outputs → %QW0                          (holding register 0)

    When the original ST file uses %IX/%IW, this tool rewrites those variable
    bindings to %QX1.x/%QW1+ and saves a new <stem>_testable.st file that you
    must load on the PLC runtime instead of the original.
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
    Parses an IEC 61131-3 Structured Text file.

    Handles two cases:
      1. Explicit VAR_INPUT / VAR_OUTPUT qualifiers  (e.g. th_X_trip.st)
      2. Plain VAR block only                        (e.g. gpt_sample_1.st)
         → OpenAI will classify inputs vs outputs for case 2.

    Input variables are ALWAYS assigned to Modbus-writable addresses:
      - Boolean inputs  → %QX1.0, %QX1.1, ...  (coils 8+, writable via FC5)
      - Integer inputs  → %QW1, %QW2, ...       (holding registers 1+, writable via FC6)
    This ensures the test_generator can write stimulus values over Modbus.
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
        self.inputs: List[dict] = []    # [{'name', 'type', 'address'}]
        self.outputs: List[dict] = []
        self.plain_vars: List[dict] = []  # plain VAR — classification deferred to AI
        self.constants: Dict = {}         # name → value  (from VAR CONSTANT)
        # Whether the ST code contains edge-trigger function blocks (R_TRIG/F_TRIG)
        self.has_edge_triggers: bool = False
        # Rewritten ST code with %QX1.x / %QW1+ input addresses (set after rewrite)
        self.rewritten_st_code: Optional[str] = None

    # ── Address counters ─────────────────────────────────────────────────────
    def _addr_counters(self):
        # Input bits start at byte 1 (%QX1.0) so they never overlap with
        # output bits which start at byte 0 (%QX0.0).
        # Input words start at index 1 (%QW1) for the same reason.
        return {'iw': 1, 'ib': 8, 'qw': 0, 'qb': 0}

    def _next_input_addr(self, vtype: str, c: dict) -> str:
        """Always returns a WRITABLE Modbus address (%QX1.x or %QW1+)."""
        if vtype in self.WORD_TYPES:
            addr = f"%QW{c['iw']}"
            c['iw'] += 1
        else:  # BOOL and others → coil at byte 1+
            byte, bit = divmod(c['ib'], 8)
            addr = f"%QX{byte}.{bit}"
            c['ib'] += 1
        return addr

    def _next_output_addr(self, vtype: str, c: dict) -> str:
        if vtype in self.WORD_TYPES:
            addr = f"%QW{c['qw']}"
            c['qw'] += 1
        else:
            byte, bit = divmod(c['qb'], 8)
            addr = f"%QX{byte}.{bit}"
            c['qb'] += 1
        return addr

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

        # VAR_INPUT
        c = self._addr_counters()
        input_block = self._extract_block(self.st_code, r'VAR_INPUT')
        if input_block:
            for name, vtype, at_addr in self._parse_declarations(input_block):
                self.inputs.append({
                    'name': name,
                    'type': vtype,
                    'address': self._next_input_addr(vtype, c),
                })

        # VAR_OUTPUT
        c_out = self._addr_counters()
        output_block = self._extract_block(self.st_code, r'VAR_OUTPUT')
        if output_block:
            for name, vtype, at_addr in self._parse_declarations(output_block):
                self.outputs.append({
                    'name': name,
                    'type': vtype,
                    'address': self._next_output_addr(vtype, c_out),
                })

        # Plain VAR (no qualifier) — collect for AI classification
        # We skip VAR CONSTANT and any already-found sections
        plain_block = self._extract_block(self.st_code, r'(?<!_)(?<!\w)VAR(?!\s+CONSTANT)(?!_)')
        if plain_block and not input_block and not output_block:
            c_in  = self._addr_counters()
            c_out2 = self._addr_counters()
            for name, vtype, at_addr in self._parse_declarations(plain_block):
                if vtype in self.SKIP_TYPES:
                    continue
                # If the variable has an AT binding we can pre-classify it:
                #   %QX0.x / %QW0 → output (drive from PLC, read by test)
                #   %QX1.x+ / %QW1+ → already a writable test input
                #   %IX / %IW → input that will be remapped to %QX1.x by rewrite
                if at_addr:
                    at_upper = at_addr.upper()
                    is_output = bool(re.match(r'%QX0\.|%QW0$', at_upper))
                    if is_output:
                        self.outputs.append({
                            'name': name, 'type': vtype, 'address': at_addr,
                        })
                    else:
                        # Input — address will be remapped by rewrite_st_for_testing()
                        # Store the original AT addr; we'll update after rewrite
                        self.inputs.append({
                            'name': name, 'type': vtype,
                            'address': at_addr,   # temporary; updated in rewrite
                            '_needs_remap': True,
                        })
                else:
                    # No AT address — let AI classify
                    self.plain_vars.append({'name': name, 'type': vtype, 'at_addr': None})

    # ── AT-address rewrite ───────────────────────────────────────────────────
    def rewrite_st_for_testing(self) -> str:
        """
        Scan the plain VAR block for 'AT %IX' / 'AT %IW' bindings and replace
        them with Modbus-writable 'AT %QX1.x' / 'AT %QW1+' addresses.

        Also replaces bare '%IX...' / '%IW...' literals elsewhere in the code.
        Returns the modified ST source (and stores it in self.rewritten_st_code).
        If no %IX/%IW addresses exist, returns the original source unchanged.
        """
        code = self.st_code

        # Build a mapping: original_addr_string → replacement_addr_string
        # by scanning all AT %IX / AT %IW declarations
        addr_map: Dict[str, str] = {}
        bit_ctr = 8   # start at byte 1 → %QX1.0
        word_ctr = 1  # start at %QW1

        pattern = re.compile(
            r'AT\s+(%(?:IX(\d+)\.(\d+)|IW(\d+)))',
            re.IGNORECASE
        )
        for m in pattern.finditer(code):
            orig = m.group(1)   # e.g. '%IX0.0' or '%IW2'
            if orig in addr_map:
                continue        # already mapped
            if m.group(2) is not None:   # bit address %IX<byte>.<bit>
                byte, bit = divmod(bit_ctr, 8)
                addr_map[orig] = f'%QX{byte}.{bit}'
                bit_ctr += 1
            else:                        # word address %IW<index>
                addr_map[orig] = f'%QW{word_ctr}'
                word_ctr += 1

        if not addr_map:
            self.rewritten_st_code = code
            return code

        # Replace every occurrence of the mapped addresses in the full source
        for orig, replacement in addr_map.items():
            # Escape for regex; match case-insensitively
            escaped = re.escape(orig)
            code = re.sub(escaped, replacement, code, flags=re.IGNORECASE)

        self.rewritten_st_code = code

        # Update any pre-classified inputs that had _needs_remap set
        for v in self.inputs:
            if v.pop('_needs_remap', False):
                old_addr = v['address']
                for orig, replacement in addr_map.items():
                    if orig.upper() == old_addr.upper():
                        v['address'] = replacement
                        break

        return code

    # ── Add AT bindings for unaddressed variables ────────────────────────────
    def rewrite_st_add_at_bindings(self, inputs: list, outputs: list) -> str:
        """
        When variables have no AT addresses (plain VAR block only), rewrite the
        VAR block to insert 'AT <address>' bindings for classified inputs and
        outputs, then split it into TWO separate VAR blocks:

          VAR  (* I/O located variables with AT bindings *)
              StartButton AT %QX1.0 : BOOL;
              ...
          END_VAR

          VAR  (* internal variables — latches, FB instances, timers *)
              PumpRunLatch    : BOOL;
              StartEdge       : R_TRIG;
              ...
          END_VAR

        MATIEC (OpenPLC's compiler) rejects mixing located (AT) and non-located
        variables inside the same VAR block, so the split is mandatory.

        Returns the modified ST source and stores it in self.rewritten_st_code.
        """
        addr_map: Dict[str, str] = {}
        for v in inputs:
            addr_map[v['name']] = v['address']
        for v in outputs:
            addr_map[v['name']] = v['address']

        if not addr_map:
            self.rewritten_st_code = self.st_code
            return self.st_code

        # Match plain VAR...END_VAR (not VAR_INPUT, VAR_OUTPUT, VAR CONSTANT, etc.)
        block_re = re.compile(
            r'(\n|^)([ \t]*VAR(?!\s*CONSTANT|_\w+)[ \t]*\n)(.*?)([ \t]*END_VAR)',
            re.DOTALL | re.IGNORECASE,
        )

        def _rewrite_block(m: re.Match) -> str:
            leading   = m.group(1)   # leading newline (or start-of-string)
            body      = m.group(3)   # lines between VAR and END_VAR
            indent    = '    '

            located_decls: List[str] = []
            internal_decls: List[str] = []

            for line in body.split('\n'):
                stripped = line.strip()
                if not stripped:
                    continue  # drop blank separators; blocks are rebuilt cleanly

                decl_m = re.match(r'^(\w+)\s*(?:AT\s+%[^\s:]+\s*)?:\s*', stripped)
                if decl_m:
                    name = decl_m.group(1)
                    addr = addr_map.get(name)
                    has_at = bool(re.search(r'\bAT\b', stripped, re.IGNORECASE))
                    if addr and not has_at:
                        # Insert AT binding before the colon
                        colon_pos = stripped.index(':')
                        new_decl = stripped[:colon_pos].rstrip() + f' AT {addr} :' + stripped[colon_pos + 1:]
                        located_decls.append(indent + new_decl)
                    elif has_at:
                        # Already has an AT binding — keep in located block
                        located_decls.append(indent + stripped)
                    else:
                        # Internal variable (latch, FB instance, timer, etc.)
                        internal_decls.append(indent + stripped)
                else:
                    # Comment or unrecognised line → treat as internal
                    internal_decls.append(indent + stripped)

            # Build the replacement: located block first, then internal block
            result = f'{leading}VAR\n'
            result += '\n'.join(located_decls) + '\n'
            result += 'END_VAR'

            if internal_decls:
                result += '\nVAR\n'
                result += '\n'.join(internal_decls) + '\n'
                result += 'END_VAR'

            return result

        code = block_re.sub(_rewrite_block, self.st_code)
        self.rewritten_st_code = code
        return code

    # ── Add AT bindings to VAR_INPUT / VAR_OUTPUT blocks ────────────────────
    def rewrite_st_add_at_bindings_explicit_io(self) -> str:
        """
        Convert VAR_INPUT / VAR_OUTPUT blocks into a plain VAR block with
        AT address bindings, which is the only syntax MATIEC (OpenPLC) accepts
        for hardware-mapped variables inside a PROGRAM.

        VAR_INPUT / VAR_OUTPUT do NOT support AT bindings — putting AT inside
        them causes a compiler error.  The correct approach is:

          VAR                          ← located I/O variables
              iStartPB AT %QX1.0 : BOOL;
              qMotorRun AT %QX0.0 : BOOL;
          END_VAR

        All VAR_INPUT and VAR_OUTPUT declarations are merged into one VAR block
        (placed where the first qualified block appeared), and the original
        VAR_INPUT / VAR_OUTPUT blocks are removed.

        Returns the modified ST source (stored in self.rewritten_st_code).
        If no VAR_INPUT/VAR_OUTPUT blocks exist, returns the original source.
        """
        addr_map: Dict[str, str] = {v['name']: v['address'] for v in self.inputs}
        addr_map.update({v['name']: v['address'] for v in self.outputs})

        if not addr_map:
            self.rewritten_st_code = self.st_code
            return self.st_code

        indent = '    '
        io_decls: List[str] = []

        def _collect_and_remove(match: re.Match) -> str:
            """Collect declarations from a VAR_INPUT/VAR_OUTPUT block; return empty string."""
            body = match.group(2)
            for line in body.split('\n'):
                stripped = line.strip()
                m = re.match(r'^(\w+)\s*(?:AT\s+%[^\s:]+\s*)?:\s*(\w+)(.*)', stripped)
                if not m:
                    continue
                name = m.group(1)
                rest = re.sub(r'^\s*;', '', m.group(3)).strip()   # strip leading ';', keep comment
                addr = addr_map.get(name)
                if addr:
                    comment = f'  {rest}' if rest else ''
                    io_decls.append(f'{indent}{name} AT {addr} : {m.group(2)};{comment}')
                else:
                    # Variable not in our address map — keep as-is in the VAR block
                    io_decls.append(f'{indent}{stripped if stripped.endswith(";") else stripped + ";"}')
            return ''   # remove this block entirely

        block_re = re.compile(
            r'([ \t]*VAR_(?:INPUT|OUTPUT)\b)(.*?)([ \t]*END_VAR[ \t]*\n?)',
            re.DOTALL | re.IGNORECASE,
        )

        code = self.st_code

        # Find position of first VAR_INPUT/VAR_OUTPUT to insert our new VAR block
        first_match = block_re.search(code)
        if not first_match:
            self.rewritten_st_code = code
            return code

        # Collect all declarations and strip all VAR_INPUT/VAR_OUTPUT blocks
        code = block_re.sub(_collect_and_remove, code)

        if not io_decls:
            self.rewritten_st_code = code
            return code

        # Build the replacement VAR block
        new_var_block = 'VAR\n' + '\n'.join(io_decls) + '\nEND_VAR\n'

        # Insert the new VAR block at the position where the first
        # VAR_INPUT/VAR_OUTPUT block started (after the preceding newline)
        insert_pos = first_match.start()
        # Preserve any leading whitespace/newlines up to that point
        code = code[:insert_pos] + new_var_block + code[insert_pos:]

        # Clean up any double-blank lines left by block removal
        code = re.sub(r'\n{3,}', '\n\n', code)

        self.rewritten_st_code = code
        return code

    @property
    def has_explicit_io(self) -> bool:
        return bool(self.inputs or self.outputs)


# ──────────────────────────────────────────────────────────────────────────────
# OpenPLC CONFIGURATION block helper
# ──────────────────────────────────────────────────────────────────────────────

def ensure_configuration_block(st_code: str, program_name: str) -> str:
    """
    OpenPLC requires a CONFIGURATION … END_CONFIGURATION block to compile.
    MATIEC generates Config0.c / Config0.h / Res0.c only when this block is
    present.  If the ST source already has one, return it unchanged.
    Otherwise append a standard block referencing the given program_name.
    """
    if re.search(r'\bCONFIGURATION\b', st_code, re.IGNORECASE):
        return st_code
    config_block = (
        f'\n(* OpenPLC Configuration *)\n'
        f'CONFIGURATION Config0\n'
        f'\n'
        f'  RESOURCE Res0 ON PLC\n'
        f'    TASK task0(INTERVAL := T#20ms, PRIORITY := 0);\n'
        f'    PROGRAM instance0 WITH task0 : {program_name};\n'
        f'  END_RESOURCE\n'
        f'\n'
        f'END_CONFIGURATION\n'
    )
    return st_code.rstrip() + '\n' + config_block


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
  - All input variables are mapped to WRITABLE Modbus addresses (%QX1.x for BOOL, %QW1+ for INT).
  - All output variables are mapped to readable Modbus addresses (%QX0.x for BOOL, %QW0 for INT).
  - The test runner writes inputs then waits delay_ms before reading outputs.
  - Each test row is independent; do NOT assume state carries over from a previous row.

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
"""


def _build_prompt_classify_io(parser: STParser, num_tests: int) -> str:
    """Prompt used when only plain VAR was found — ask AI to classify too."""
    vars_desc = "\n".join(
        f"  {v['name']} : {v['type']}"
        + (f"  (* original AT {v['at_addr']} *)" if v.get('at_addr') else "")
        for v in parser.plain_vars
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

    return f"""Analyze this IEC 61131-3 ST program. First identify which variables are
physical inputs (driven externally), which are physical outputs (the results
we want to verify), and which are purely internal / function-block instances.

Then generate {num_tests} test cases.

Program: {parser.program_name}

Variables declared (plain VAR, no qualifier):
{vars_desc}

ST Code:
```
{parser.st_code}
```
{edge_note}
Address assignment rules — MODBUS WRITABILITY IS REQUIRED:
  These tests run by writing Modbus coils/registers to a live OpenPLC runtime.
  %IX (discrete inputs) and %IW (input registers) are READ-ONLY via Modbus
  and CANNOT be driven by the test tool. You MUST use WRITABLE addresses:

  Physical input  BOOL  → %QX1.0, %QX1.1, %QX1.2, ...  (Modbus coils 8, 9, 10, ...)
  Physical input  INT/WORD/REAL → %QW1, %QW2, %QW3, ... (Modbus holding registers 1, 2, ...)
  Physical output BOOL  → %QX0.0, %QX0.1, ...           (Modbus coils 0, 1, ...)
  Physical output INT/WORD/REAL → %QW0                   (Modbus holding register 0)

  Assign addresses in declaration order.  Do NOT use %IX or %IW for inputs.

Return ONLY this JSON schema, no extra text:
{{
  "inputs": [
    {{ "name": "<var_name>", "type": "<BOOL|INT|...>", "address": "<IEC address>" }}
  ],
  "outputs": [
    {{ "name": "<var_name>", "type": "<BOOL|INT|...>", "address": "<IEC address>" }}
  ],
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
- Use 0/1 for BOOL values
- Each test row is self-contained; do NOT assume state carries over from a previous row
- Cover ALL boundary conditions and boolean flag combinations
- Generate exactly {num_tests} test cases
"""


def call_openai(client: OpenAI, model: str, st_parser: STParser, num_tests: int) -> dict:
    if st_parser.has_explicit_io:
        user_prompt = _build_prompt_known_io(st_parser, num_tests)
    else:
        user_prompt = _build_prompt_classify_io(st_parser, num_tests)

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
    """
    Returns (inputs, outputs, test_cases).
    If parser had explicit I/O, use those with their locally-assigned addresses.
    Otherwise use what the AI returned — but validate that no %IX/%IW slipped through.
    """
    if parser.has_explicit_io:
        inputs  = parser.inputs
        outputs = parser.outputs
    else:
        inputs  = ai_result.get('inputs',  [])
        outputs = ai_result.get('outputs', [])

        # Safety net: if AI ignored the address rules and used %IX/%IW, remap them
        bit_ctr  = 8   # %QX1.0
        word_ctr = 1   # %QW1
        for v in inputs:
            addr = v.get('address', '')
            if re.match(r'%IX', addr, re.IGNORECASE):
                # Parse bit position and remap
                m = re.match(r'%IX(\d+)\.(\d+)', addr, re.IGNORECASE)
                byte, bit = divmod(bit_ctr, 8)
                v['address'] = f'%QX{byte}.{bit}'
                bit_ctr += 1
                print(f"  ⚠️  Remapped AI-assigned {addr} → {v['address']} (not Modbus-writable)")
            elif re.match(r'%IW', addr, re.IGNORECASE):
                v['address'] = f'%QW{word_ctr}'
                word_ctr += 1
                print(f"  ⚠️  Remapped AI-assigned {addr} → {v['address']} (not Modbus-writable)")

    test_cases = ai_result.get('test_cases', [])

    # Re-number test_id sequentially to be safe
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
    api_key = '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'
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

    # Rewrite %IX/%IW addresses to Modbus-writable equivalents
    rewritten = st_parser.rewrite_st_for_testing()
    if rewritten != st_code:
        testable_st_path = st_path.parent / f"{st_path.stem}_testable.st"
        rewritten = ensure_configuration_block(rewritten, st_parser.program_name)
        testable_st_path.write_text(rewritten, encoding='utf-8')
        print(f"⚠️  Original ST uses %IX/%IW (read-only via Modbus).")
        print(f"   Rewritten ST saved → {testable_st_path}")
        print(f"   *** Load '{testable_st_path.name}' on the PLC runtime, NOT the original. ***")

    print(f"Program:  {st_parser.program_name}")

    if st_parser.has_explicit_io:
        inputs_str  = ', '.join(v['name'] + ' (' + v['address'] + ')' for v in st_parser.inputs)
        outputs_str = ', '.join(v['name'] + ' (' + v['address'] + ')' for v in st_parser.outputs)
        print(f"Inputs  : {inputs_str}")
        print(f"Outputs : {outputs_str}")
    else:
        print(f"  No VAR_INPUT/VAR_OUTPUT found — OpenAI will classify variables.")
        print(f"  Plain vars: {', '.join(v['name'] for v in st_parser.plain_vars)}")

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

    if st_parser.has_explicit_io:
        # VAR_INPUT / VAR_OUTPUT exist — add AT bindings if missing
        rewritten_explicit = st_parser.rewrite_st_add_at_bindings_explicit_io()
        rewritten_explicit = ensure_configuration_block(rewritten_explicit, st_parser.program_name)
        if rewritten_explicit != st_code:
            testable_st_path = st_path.parent / f"{st_path.stem}_testable.st"
            testable_st_path.write_text(rewritten_explicit, encoding='utf-8')
            print(f"⚠️  ST file has VAR_INPUT/VAR_OUTPUT without AT address bindings.")
            print(f"   Rewritten ST saved → {testable_st_path}")
            print(f"   *** Load '{testable_st_path.name}' on the PLC runtime, NOT the original. ***")
    else:
        # AI classified variables — print what it found
        inputs_str2  = ', '.join(v['name'] + ' (' + v['address'] + ')' for v in inputs)
        outputs_str2 = ', '.join(v['name'] + ' (' + v['address'] + ')' for v in outputs)
        print(f"Inputs  : {inputs_str2}")
        print(f"Outputs : {outputs_str2}")

        # Rewrite the ST file to add AT <address> bindings so the PLC maps the
        # variables to Modbus-accessible registers.  Without these bindings the
        # test runner cannot read/write the variables over Modbus.
        rewritten_with_bindings = st_parser.rewrite_st_add_at_bindings(inputs, outputs)
        rewritten_with_bindings = ensure_configuration_block(rewritten_with_bindings, st_parser.program_name)
        if rewritten_with_bindings != st_code:
            testable_st_path = st_path.parent / f"{st_path.stem}_testable.st"
            testable_st_path.write_text(rewritten_with_bindings, encoding='utf-8')
            print(f"⚠️  Original ST has no hardware AT addresses.")
            print(f"   Testable ST with AT bindings saved → {testable_st_path}")
            print(f"   *** Load '{testable_st_path.name}' on the PLC runtime, NOT the original. ***")

    print(f"Generated {len(test_cases)} test cases.")

    # ── Save CSV ─────────────────────────────────────────────────────────────
    save_csv(str(output_path), inputs, outputs, test_cases)
    print(f"Saved:    {output_path}")
    print()
    print("Next steps:")
    if st_parser.rewritten_st_code and st_parser.rewritten_st_code != st_code:
        print(f"  1. Load '{st_path.stem}_testable.st' on the PLC runtime.")
        print(f"  2. Run: python3 test_generators/test_generator.py -f {output_path}")
    else:
        print(f"  1. Load '{st_path.name}' on the PLC runtime.")
        print(f"  2. Run: python3 test_generators/test_generator.py -f {output_path}")


if __name__ == '__main__':
    main()
