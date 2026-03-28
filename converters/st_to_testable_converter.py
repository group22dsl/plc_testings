#!/usr/bin/env python3
"""
ST to Testable ST Converter
============================
Converts an IEC 61131-3 Structured Text (.st) file to a version ready for
hardware-in-the-loop testing via OpenPLC Runtime + Modbus.

What it changes (LOGIC IS NEVER MODIFIED):
  1. VAR_INPUT / VAR_OUTPUT blocks are REPLACED by a plain VAR block with
     AT address bindings.  This is mandatory because MATIEC (OpenPLC compiler)
     does NOT allow AT bindings inside VAR_INPUT or VAR_OUTPUT — doing so
     produces "invalid input variable(s) declaration" errors.
  2. If no CONFIGURATION block is present, a standard one is appended.
     Any existing CONFIGURATION is replaced when a FUNCTION_BLOCK or FUNCTION
     is promoted to PROGRAM, so that the TASK/PROGRAM scheduling lines are
     correct.
  3. FUNCTION_BLOCK POUs are promoted to PROGRAM (AT bindings are only
     valid inside PROGRAM blocks in MATIEC / OpenPLC).
  4. FUNCTION POUs have their VAR_INPUT/VAR_OUTPUT rewritten the same way.
  5. 8-bit integer types (SINT, USINT, BYTE) are promoted to their 16-bit
     equivalents throughout all VAR declarations to avoid MATIEC type-width
     mismatches.
  6. 64-bit types unsupported by MATIEC (LREAL -> REAL, LINT -> DINT,
     ULINT -> UDINT, LWORD -> DWORD) are demoted to their 32-bit equivalents.
  7. VAR_IN_OUT blocks are converted to plain VAR (no AT) when the POU is
     promoted to PROGRAM, since VAR_IN_OUT is invalid in PROGRAM scope.
  8. RETAIN qualifier is stripped from AT-located VAR blocks (MATIEC does
     not support RETAIN on located variables in OpenPLC targets).

Address assignment (Modbus writability -- NO overlap between inputs/outputs):

  OpenPLC reserves the first 100 addresses in each space for physical I/O:
    %IX0.0-%IX12.7  / %IW0-%IW99   -> physical digital/analog inputs
    %QX0.0-%QX12.7  / %QW0-%QW99   -> physical digital/analog outputs

  ALL test-drivable variables (both inputs and outputs) use %QX / %QW
  holding-register space, because %IW input registers are READ-ONLY via
  standard Modbus — an external test runner cannot write to them.

  Inputs  start at offset 200 (well above the output range of 100-199):
  Input  BOOL         ->  AT %QX200.0, %QX200.1, ...  (bit index 1600 onward)
  Input  WORD/INT     ->  AT %QW200, %QW201, ...
  Input  DWORD/DINT   ->  AT %QD200, %QD201, ...
  Output BOOL         ->  AT %QX100.0, %QX100.1, ...
  Output WORD/INT     ->  AT %QW100, %QW101, ...
  Output DWORD/DINT   ->  AT %QD100, %QD101, ...

  (REAL is 32-bit and uses %ID/%QD; LREAL is promoted to REAL.)

  VAR CONSTANT, VAR_IN_OUT, VAR_GLOBAL, VAR_EXTERNAL, VAR_TEMP,
  and plain VAR (internal) blocks: left untouched (except RETAIN stripping
  on blocks that gain AT clauses).

Usage:
    python st_to_testable_converter.py input.st
    python st_to_testable_converter.py input.st -o output_testable.st
"""

import sys
import re
import argparse
from pathlib import Path

# ---------------------------------------------------------------------------
# Type classifications
# ---------------------------------------------------------------------------

# 16-bit types -> %QW  (Modbus holding registers, 1 register each)
WORD16_TYPES = frozenset({
    'INT', 'UINT', 'WORD',
})

# 32-bit types -> %ID / %QD  (double-word registers, 2 Modbus registers each)
WORD32_TYPES = frozenset({
    'DINT', 'UDINT', 'DWORD', 'REAL',
})

# All word-width types (16 or 32-bit) -- used to decide bit vs word addressing.
WORD_TYPES = WORD16_TYPES | WORD32_TYPES

# MATIEC / OpenPLC does not support true 64-bit located variables.
# Demote these to their 32-bit equivalents everywhere (type and value-width).
_WIDE_DEMOTE = {
    'LREAL': 'REAL',   # 64-bit float   -> 32-bit float
    'LINT':  'DINT',   # 64-bit signed  -> 32-bit signed
    'ULINT': 'UDINT',  # 64-bit unsigned-> 32-bit unsigned
    'LWORD': 'DWORD',  # 64-bit bitstr  -> 32-bit bitstr
}

# MATIEC rejects AT address bit-size mismatches (e.g. SINT=8-bit at %QW=16-bit).
# Promote byte-sized integer types to their 16-bit equivalents everywhere.
_SMALL_INT_PROMOTE = {'SINT': 'INT', 'USINT': 'UINT', 'BYTE': 'WORD'}

# Combined type normalisation table (wide -> 32-bit first, then 8-bit -> 16-bit)
_ALL_PROMOTIONS = {**_WIDE_DEMOTE, **_SMALL_INT_PROMOTE}


def _promote_type(vtype: str) -> str:
    """
    Return the MATIEC-safe type for a given IEC 61131-3 type string.
    Handles plain types AND 'ARRAY[x..y] OF TYPE' forms.
    Steps: 1) demote 64-bit unsupported types, 2) promote 8-bit types.
    """
    upper = vtype.upper().strip()
    # Plain type -- apply all promotions in one pass
    promoted = _ALL_PROMOTIONS.get(upper)
    if promoted:
        return promoted
    # ARRAY[x..y] OF <elem_type>
    arr_m = re.match(r'^(ARRAY\s*\[.*?\]\s*OF\s+)(\w+)$', upper, re.IGNORECASE)
    if arr_m:
        elem = _ALL_PROMOTIONS.get(arr_m.group(2).upper(), arr_m.group(2))
        return arr_m.group(1) + elem
    return vtype


def _is_32bit(vtype: str) -> bool:
    """Return True if *vtype* (after promotion) maps to a 32-bit MATIEC register."""
    return vtype.upper() in WORD32_TYPES


# Function-block instance types that must NEVER get an AT binding AND must
# not be re-declared (they already live in the plain VAR block unchanged).
_FB_INSTANCE_TYPES = frozenset({
    'R_TRIG', 'F_TRIG', 'TON', 'TOF', 'TP',
    'CTU', 'CTD', 'CTUD', 'SR', 'RS', 'SEMA',
})

# Types that cannot be Modbus-addressed but ARE valid IEC 61131-3 variables.
# These are preserved as plain VAR declarations (no AT clause).
_NO_AT_TYPES = frozenset({
    'TIME', 'DATE', 'DT', 'TOD', 'STRING', 'WSTRING',
})

# Combined set for external callers.
SKIP_TYPES = _FB_INSTANCE_TYPES | _NO_AT_TYPES


# ---------------------------------------------------------------------------
# Address allocator
# ---------------------------------------------------------------------------

# Outputs start at offset 100 (%QW100 / %QX100.0).
# Inputs start at offset 200 (%QW200 / %QX200.0) — also in %QW/%QX (holding
# register / coil) space so they are WRITABLE via Modbus FC6/FC5.
# %IW input registers are read-only in standard Modbus and must NOT be used.
_OUT_WORD_OFFSET = 100   # first usable output index: %QW100, %QX100.0
_IN_WORD_OFFSET  = 200   # first usable input  index: %QW200, %QX200.0
_OUT_BIT_OFFSET  = 800   # bit index for %QX100.0  (100 bytes × 8 bits)
_IN_BIT_OFFSET   = 1600  # bit index for %QX200.0  (200 bytes × 8 bits)


class AddressAllocator:
    """
    Allocates sequential Modbus-writable AT addresses with ZERO overlap.

    ALL variables use %QX (coils) / %QW (holding registers) / %QD space so
    that an external Modbus client can write inputs AND read outputs.
    Inputs are placed at offset 200+, outputs at offset 100+.
    """

    def __init__(self):
        self._in_word16  = _IN_WORD_OFFSET   # %QW200, %QW201, ...
        self._in_word32  = _IN_WORD_OFFSET   # %QD200, %QD201, ...
        self._in_bit     = _IN_BIT_OFFSET    # bit index -> %QX200.0, ...
        self._out_word16 = _OUT_WORD_OFFSET  # %QW100, %QW101, ...
        self._out_word32 = _OUT_WORD_OFFSET  # %QD100, %QD101, ...
        self._out_bit    = _OUT_BIT_OFFSET   # bit index -> %QX100.0, ...

    @staticmethod
    def _bit_addr(prefix: str, idx: int) -> str:
        return f'%{prefix}X{idx // 8}.{idx % 8}'

    def next_input(self, vtype: str) -> str:
        upper = vtype.upper()
        if upper in WORD32_TYPES or (upper.startswith('ARRAY') and _is_32bit(upper)):
            addr = f'%QD{self._in_word32}'
            self._in_word32 += 1
        elif upper in WORD16_TYPES or upper.startswith('ARRAY'):
            addr = f'%QW{self._in_word16}'
            self._in_word16 += 1
        else:  # BOOL / BIT
            addr = self._bit_addr('Q', self._in_bit)
            self._in_bit += 1
        return addr

    def next_output(self, vtype: str) -> str:
        upper = vtype.upper()
        if upper in WORD32_TYPES or (upper.startswith('ARRAY') and _is_32bit(upper)):
            addr = f'%QD{self._out_word32}'
            self._out_word32 += 1
        elif upper in WORD16_TYPES or upper.startswith('ARRAY'):
            addr = f'%QW{self._out_word16}'
            self._out_word16 += 1
        else:  # BOOL / BIT
            addr = self._bit_addr('Q', self._out_bit)
            self._out_bit += 1
        return addr


# ---------------------------------------------------------------------------
# Regex patterns
# ---------------------------------------------------------------------------

# Matches a complete VAR...END_VAR block (non-greedy body).
# Order matters: more-specific keywords must appear before plain 'VAR'.
# Supports optional qualifiers: RETAIN, NON_RETAIN, CONSTANT (after keyword).
BLOCK_RE = re.compile(
    r'(?P<kw>'
    r'VAR_INPUT|VAR_OUTPUT|VAR_IN_OUT|VAR_EXTERNAL|VAR_TEMP'
    r'|VAR_GLOBAL|VAR_ACCESS'
    r'|VAR\s+CONSTANT|VAR\s+RETAIN|VAR\s+NON_RETAIN'
    r'|VAR(?![\w_])'   # plain VAR only — must NOT match the VAR prefix of VAR_INPUT etc.
    r')'
    r'(?P<qualifier>(?:\s+(?:RETAIN|NON_RETAIN|CONSTANT))*)'  # optional trailing qualifiers
    r'(?P<body>.*?)'
    r'END_VAR',
    re.DOTALL | re.IGNORECASE,
)

# Matches one variable declaration, supporting:
#   single name:       myVar : BOOL;
#   multi-name:        a, b, c : INT;    <- IEC 61131-3 s2.4.3 list syntax
#   existing AT:       myVar AT %QX0.0 : BOOL;   (old AT clause stripped)
#   array type:        buf : ARRAY[1..4] OF INT;
#   init value:        x : INT := 42;
#   complex init:      arr : ARRAY[1..3] OF INT := [1,2,3];
DECL_RE = re.compile(
    r'^(?P<indent>[ \t]+)'
    r'(?P<names>\w+(?:\s*,\s*\w+)*)'          # one or more comma-separated names
    r'(?P<at_clause>[ \t]+AT[ \t]+%[\w.]+)?'  # optional existing AT clause (discarded)
    r'(?P<colon>[ \t]*:[ \t]*)'
    r'(?P<type>ARRAY\s*\[.*?\]\s*OF\s+\w+|\w+(?:\[.*?\])?)'  # plain or ARRAY type
    r'(?P<init>(?:[ \t]*:=[ \t]*(?:[^;]|\n)*?)?)'            # optional initialiser
    r'(?P<semi>[ \t]*;)',
    re.MULTILINE,
)


# ---------------------------------------------------------------------------
# Comment stripper (preserves line positions for error reporting)
# ---------------------------------------------------------------------------

def _strip_comments(code: str) -> str:
    """Remove (* ... *) block comments (possibly nested) and // line comments."""
    result = []
    i = 0
    n = len(code)
    while i < n:
        # Block comment (* ... *) -- handle nesting
        if code[i:i+2] == '(*':
            depth = 1
            i += 2
            while i < n and depth:
                if code[i:i+2] == '(*':
                    depth += 1; i += 2
                elif code[i:i+2] == '*)':
                    depth -= 1; i += 2
                else:
                    i += 1
        # Line comment //
        elif code[i:i+2] == '//':
            while i < n and code[i] != '\n':
                i += 1
        else:
            result.append(code[i])
            i += 1
    return ''.join(result)


# ---------------------------------------------------------------------------
# Declaration parser
# ---------------------------------------------------------------------------

def _parse_decls(body: str) -> list:
    """
    Parse all variable declarations from a VAR block body.

    Returns list of dicts: {name, type, init, no_at}
      - init may be None
      - no_at=True means include as a plain VAR (no AT clause)
      - FB instance types are skipped entirely
      - Multi-name declarations (a, b : INT) are expanded to one dict each
    """
    body_no_comments = _strip_comments(body)
    result = []
    for m in DECL_RE.finditer(body_no_comments):
        raw_type = m.group('type').strip()
        vtype_upper = raw_type.upper()

        # Detect ARRAY element type for FB / no-AT classification
        arr_elem_m = re.match(r'ARRAY\s*\[.*?\]\s*OF\s+(\w+)', vtype_upper)
        base_type_upper = arr_elem_m.group(1) if arr_elem_m else vtype_upper

        # FB instances in VAR_INPUT/VAR_OUTPUT are non-standard; skip entirely.
        if base_type_upper in _FB_INSTANCE_TYPES:
            continue

        # ':= VALUE' -- strip the leading ':='
        init_raw = m.group('init').strip() if m.group('init') else None
        if init_raw:
            init = re.sub(r'^:=\s*', '', init_raw).strip() or None
        else:
            init = None

        no_at = base_type_upper in _NO_AT_TYPES

        # Expand comma-separated name lists: "a, b, c : INT" -> three entries
        for name in re.split(r'\s*,\s*', m.group('names')):
            name = name.strip()
            if name:
                result.append({
                    'name': name,
                    'type': raw_type,
                    'init': init,
                    'no_at': no_at,
                })
    return result


# ---------------------------------------------------------------------------
# CONFIGURATION block helper
# ---------------------------------------------------------------------------

def _build_configuration(program_name: str) -> str:
    """Return a complete, well-formed OpenPLC CONFIGURATION block string."""
    return (
        '\n(* OpenPLC Configuration *)\n'
        'CONFIGURATION Config0\n\n'
        '  RESOURCE Res0 ON PLC\n'
        '    TASK task0(INTERVAL := T#20ms, PRIORITY := 0);\n'
        f'    PROGRAM instance0 WITH task0 : {program_name};\n'
        '  END_RESOURCE\n\n'
        'END_CONFIGURATION\n'
    )


def _ensure_configuration(st_code: str, program_name: str) -> str:
    """
    Append a standard OpenPLC CONFIGURATION block if one is not present.
    If a CONFIGURATION block IS present but does not schedule *program_name*
    (e.g. it references the old FUNCTION_BLOCK name, or lacks TASK/PROGRAM
    scheduling lines), replace it entirely with a correct one.
    """
    cfg_match = re.search(
        r'\bCONFIGURATION\b.*?\bEND_CONFIGURATION\b',
        st_code,
        re.DOTALL | re.IGNORECASE,
    )

    if cfg_match:
        cfg_text = cfg_match.group(0)
        # Check whether the existing CONFIGURATION references the correct program
        # and has at least one TASK and one PROGRAM scheduling line.
        has_task    = bool(re.search(r'\bTASK\b',    cfg_text, re.IGNORECASE))
        has_program = bool(re.search(r'\bPROGRAM\b', cfg_text, re.IGNORECASE))
        has_name    = bool(re.search(
            r'\b' + re.escape(program_name) + r'\b', cfg_text, re.IGNORECASE
        ))

        if has_task and has_program and has_name:
            return st_code  # existing CONFIGURATION is already correct

        # Replace the stale / incomplete CONFIGURATION block.
        st_code = st_code[:cfg_match.start()] + st_code[cfg_match.end():]
        st_code = re.sub(r'\n{3,}', '\n\n', st_code).rstrip()

    return st_code.rstrip() + '\n' + _build_configuration(program_name)


# ---------------------------------------------------------------------------
# Main conversion function
# ---------------------------------------------------------------------------

def convert(st_code: str):
    """
    Convert *st_code* to its testable form.

    Strategy
    --------
    MATIEC (OpenPLC compiler) rejects AT address bindings inside VAR_INPUT or
    VAR_OUTPUT blocks.  The only valid place for located (AT) variables inside
    a PROGRAM is a plain VAR block.

    Therefore this function:
      1.  Collects every declaration from all VAR_INPUT blocks (input AT addrs).
      2.  Collects every declaration from all VAR_OUTPUT blocks (output AT addrs).
      3.  Removes all VAR_INPUT and VAR_OUTPUT blocks from the source.
      4.  Inserts a single plain VAR block -- with AT bindings -- at the position
          where the first VAR_INPUT/VAR_OUTPUT block appeared.
      5.  Leaves VAR CONSTANT, VAR_GLOBAL, VAR_EXTERNAL, VAR_TEMP, and plain
          VAR blocks untouched.
      6.  Promotes FUNCTION_BLOCK POUs to PROGRAM (AT bindings require PROGRAM).
      7.  Promotes FUNCTION POUs (AT bindings require PROGRAM scope).
      8.  Promotes 8-bit integer types -> 16-bit throughout (MATIEC width rule).
      9.  Demotes unsupported 64-bit types -> 32-bit throughout.
      10. Converts VAR_IN_OUT to plain VAR when POU is promoted to PROGRAM
          (VAR_IN_OUT is invalid in PROGRAM scope).
      11. Strips RETAIN qualifier from blocks that acquire AT clauses
          (MATIEC rejects RETAIN on located variables for OpenPLC targets).
      12. Appends / replaces CONFIGURATION block with a correctly scheduled one.
      13. AT addresses start at offset 100 to skip OpenPLC's physical-I/O
          reserved range (%IX0.0-%IX12.7 / %IW0-%IW99 etc.).
      14. 32-bit types (DINT, UDINT, DWORD, REAL) use %ID / %QD addresses;
          16-bit types (INT, UINT, WORD) use %IW / %QW addresses.

    Returns:
        testable_code (str)  -- rewritten ST source
        summary       (list) -- list of (io_kind, var_name, address, var_type)
    """
    # -- Program name (strip comments to avoid false matches) -----------------
    _no_comments = _strip_comments(st_code)
    m = re.search(
        r'^\s*(PROGRAM|FUNCTION_BLOCK|FUNCTION)\s+(\w+)',
        _no_comments,
        re.IGNORECASE | re.MULTILINE,
    )
    pou_kind          = m.group(1).upper() if m else 'PROGRAM'
    is_function_block = pou_kind == 'FUNCTION_BLOCK'
    is_function       = pou_kind == 'FUNCTION'
    program_name      = m.group(2) if m else 'UnknownProgram'

    allocator = AddressAllocator()
    summary   = []          # (label, var_name, address, var_type)

    # -- Collect all VAR_INPUT / VAR_OUTPUT declarations ---------------------
    io_decls = []           # list of {'name', 'type', 'init', 'address', 'label'}

    for bm in BLOCK_RE.finditer(st_code):
        kw_raw = bm.group('kw')
        kw = re.sub(r'\s+', ' ', kw_raw).upper().strip()
        if kw not in ('VAR_INPUT', 'VAR_OUTPUT'):
            continue
        assign = allocator.next_input if kw == 'VAR_INPUT' else allocator.next_output
        label  = kw
        for decl in _parse_decls(bm.group('body')):
            if decl['no_at']:
                # Non-addressable type (TIME, STRING, etc.) -- preserve as plain VAR
                io_decls.append({**decl, 'address': None, 'label': label})
            else:
                # Promote / demote types before allocating address so the AT clause
                # type matches register width.
                promoted_type = _promote_type(decl['type'])
                addr = assign(promoted_type)
                io_decls.append({
                    **decl,
                    'type': promoted_type,
                    'address': addr,
                    'label': label,
                })
                summary.append((label, decl['name'], addr, promoted_type))

    if not io_decls:
        # No VAR_INPUT/VAR_OUTPUT found -- only add/fix CONFIGURATION if needed.
        result = _ensure_configuration(st_code, program_name)
        return result, summary

    # -- Resolve variable names that clash with the program name --------------
    # IEC 61131-3 identifiers are case-insensitive; a VAR_INPUT named the same
    # as the enclosing PROGRAM causes MATIEC compilation errors.
    _body_renames: dict = {}   # original_name -> safe_new_name
    for d in io_decls:
        if d['name'].upper() == program_name.upper():
            new_name = d['name'] + '_in'
            _body_renames[d['name']] = new_name
            d['name'] = new_name
    # Propagate renames into the summary list as well
    if _body_renames:
        summary = [
            (kw, _body_renames.get(n, n), addr, vtype)
            for kw, n, addr, vtype in summary
        ]

    # -- Build replacement VAR blocks ----------------------------------------
    # IEC 61131-3 requires located (AT) vars and plain vars in separate blocks.
    at_decls    = [d for d in io_decls if d['address'] is not None]
    no_at_decls = [d for d in io_decls if d['address'] is None]

    block_lines = []
    if at_decls:
        # Plain 'VAR' -- no RETAIN qualifier; MATIEC rejects RETAIN on located vars.
        block_lines.append('VAR')
        for d in at_decls:
            line = f"    {d['name']} AT {d['address']} : {d['type']}"
            if d['init']:
                line += f" := {d['init']}"
            line += ';'
            block_lines.append(line)
        block_lines.append('END_VAR')

    if no_at_decls:
        if block_lines:
            block_lines.append('')          # blank line between blocks
        block_lines.append('VAR')
        for d in no_at_decls:
            line = f"    {d['name']} : {d['type']}"
            if d['init']:
                line += f" := {d['init']}"
            line += ';'
            block_lines.append(line)
        block_lines.append('END_VAR')

    new_var_block = '\n'.join(block_lines) + '\n'

    # -- Replace first VAR_INPUT/VAR_OUTPUT block with the new VAR block;
    #    remove all subsequent VAR_INPUT/VAR_OUTPUT blocks.
    _first_io_replaced = [False]

    def _replace_io_block(bm):
        kw = re.sub(r'\s+', ' ', bm.group('kw')).upper().strip()
        if kw in ('VAR_INPUT', 'VAR_OUTPUT'):
            if not _first_io_replaced[0]:
                _first_io_replaced[0] = True
                return new_var_block   # replace first IO block in-place
            return ''                  # delete subsequent IO blocks
        return bm.group(0)            # leave everything else unchanged

    result = BLOCK_RE.sub(_replace_io_block, st_code)

    # Clean up excessive blank lines left by block removal.
    result = re.sub(r'\n{3,}', '\n\n', result)

    # -- Promote FUNCTION_BLOCK / FUNCTION to PROGRAM -------------------------
    # AT bindings are only valid in PROGRAM blocks in MATIEC / OpenPLC.
    if is_function_block or is_function:
        if is_function_block:
            result = re.sub(
                r'\bFUNCTION_BLOCK\b', 'PROGRAM', result, flags=re.IGNORECASE
            )
            result = re.sub(
                r'\bEND_FUNCTION_BLOCK\b', 'END_PROGRAM', result, flags=re.IGNORECASE
            )
        else:
            # FUNCTION: strip the return-type annotation (": RETURNTYPE") from
            # the header line so it becomes a valid PROGRAM declaration.
            result = re.sub(
                r'\bFUNCTION\b(\s+\w+)\s*:\s*\w+',
                r'PROGRAM\1',
                result,
                count=1,
                flags=re.IGNORECASE,
            )
            result = re.sub(
                r'\bEND_FUNCTION\b', 'END_PROGRAM', result, flags=re.IGNORECASE
            )

        # VAR_IN_OUT is invalid in PROGRAM scope; convert to plain VAR.
        # (No AT clause -- these are internal pass-through variables.)
        result = re.sub(
            r'\bVAR_IN_OUT\b', 'VAR', result, flags=re.IGNORECASE
        )

        # Strip any existing CONFIGURATION block -- it may reference the old
        # FUNCTION_BLOCK name or lack TASK/PROGRAM scheduling lines.
        # _ensure_configuration will append a correct one below.
        result = re.sub(
            r'^\s*CONFIGURATION\b.*?\bEND_CONFIGURATION\b',
            '',
            result,
            flags=re.DOTALL | re.IGNORECASE | re.MULTILINE,
        )
        result = re.sub(r'\(\*\s*OpenPLC Configuration\s*\*\)\s*\n?', '', result)
        result = result.rstrip()

    # -- Strip any BEGIN keyword — MATIEC does not use it --------------------
    # MATIEC does not support the BEGIN keyword. If present (e.g. from a
    # previous converter run or hand-written source), it is treated as an
    # undeclared variable name causing "invalid variable before ':='" errors.
    result = re.sub(r'\nBEGIN[ \t]*\n', '\n', result)

    # -- Apply body renames (case-insensitive to match IEC 61131-3 semantics) -
    # The rename regex is case-insensitive (to honour IEC 61131-3 identifier
    # case-insensitivity), but that can accidentally match the POU name on the
    # PROGRAM/FUNCTION_BLOCK declaration line (e.g. variable 'TRIP_LOGIC'
    # matches program name 'trip_logic').  We save the original declaration
    # text, apply all renames, then restore the header to keep the program name
    # unchanged so no variable can clash with it.
    _header_re = re.compile(
        r'^([ \t]*(?:PROGRAM|FUNCTION_BLOCK|FUNCTION)[ \t]+)(\w+)',
        re.IGNORECASE | re.MULTILINE,
    )
    _header_m = _header_re.search(result)

    for old_name, new_name in _body_renames.items():
        result = re.sub(
            r'\b' + re.escape(old_name) + r'\b',
            new_name,
            result,
            flags=re.IGNORECASE,
        )

    # Restore the POU header identifier — undo any accidental rename.
    if _header_m:
        result = _header_re.sub(
            lambda m: m.group(1) + _header_m.group(2),
            result,
            count=1,
        )

    # -- Promote 8-bit types -> 16-bit; demote 64-bit types -> 32-bit ---------
    # Covers both:
    #   ': SINT'              -> ': INT'
    #   'ARRAY[..] OF LREAL'  -> 'ARRAY[..] OF REAL'
    for small, large in _ALL_PROMOTIONS.items():
        # Plain declaration:  ": SINT"
        result = re.sub(
            r'(:\s*)' + small + r'\b',
            lambda m_r, lg=large: m_r.group(1) + lg,
            result,
            flags=re.IGNORECASE,
        )
        # Array element type: "ARRAY[x..y] OF SINT"
        result = re.sub(
            r'(\bARRAY\b.*?\bOF\b\s*)' + small + r'\b',
            lambda m_r, lg=large: m_r.group(1) + lg,
            result,
            flags=re.IGNORECASE | re.DOTALL,
        )

    result = _ensure_configuration(result, program_name)

    # -- Ensure END_IF / END_FOR / END_WHILE / END_REPEAT / END_CASE have ';' --
    # MATIEC grammar: statement_list ::= { statement ';' }*
    # Every statement, including structured control blocks, must end with ';'.
    # Some XML exporters and hand-written ST omit the semicolon after END_IF etc.
    result = re.sub(
        r'^(\s*(?:END_IF|END_FOR|END_WHILE|END_REPEAT|END_CASE|RETURN|EXIT))\s*$',
        lambda m: m.group(1) + ';',
        result,
        flags=re.IGNORECASE | re.MULTILINE,
    )

    return result, summary


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description='Add AT address bindings to ST variables for Modbus-based testing.'
    )
    parser.add_argument('st_file', help='Input .st file path')
    parser.add_argument(
        '-o', '--output', default=None,
        help='Output file path (default: <stem>_testable.st alongside input)',
    )
    args = parser.parse_args()

    st_path = Path(args.st_file)
    if not st_path.exists():
        print(f"Error: '{st_path}' not found", file=sys.stderr)
        sys.exit(1)

    out_path = (
        Path(args.output) if args.output
        else st_path.with_name(f'{st_path.stem}_testable.st')
    )

    st_code           = st_path.read_text(encoding='utf-8')
    testable, summary = convert(st_code)

    out_path.write_text(testable, encoding='utf-8')

    print(f'Input  : {st_path}')
    print(f'Output : {out_path}')

    if summary:
        name_w = max(len(r[1]) for r in summary)
        print('\nAddress assignments:')
        print(f'  {"Variable":<{name_w}}  {"Address":<12}  {"Type":<12}  Block')
        print(f'  {"-"*name_w}  {"-"*12}  {"-"*12}  {"-"*12}')
        for kw, name, addr, vtype in summary:
            print(f'  {name:<{name_w}}  {addr:<12}  {vtype:<12}  {kw}')
    else:
        print('No VAR_INPUT / VAR_OUTPUT variables found -- file copied unchanged.')


if __name__ == '__main__':
    main()