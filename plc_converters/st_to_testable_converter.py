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

Address assignment rules (Modbus writability):
  Input  BOOL  →  AT %QX1.0, %QX1.1, …   coils 8, 9, 10, …  (writable via FC5)
  Input  INT+  →  AT %QW1,   %QW2,   …   holding regs 1, 2, … (writable via FC6)
  Output BOOL  →  AT %QX0.0, %QX0.1, …   coils 0, 1, …       (readable via FC1)
  Output INT+  →  AT %QW0,   %QW1,   …   holding regs 0, 1, … (readable via FC3)

  VAR CONSTANT, VAR_IN_OUT, and plain VAR (internal) blocks: left untouched.

Usage:
    python 5_st_to_testable_converter.py input.st
    python 5_st_to_testable_converter.py input.st -o output_testable.st
"""

import sys
import re
import argparse
from pathlib import Path

# ---------------------------------------------------------------------------
# Type classifications
# ---------------------------------------------------------------------------

# These types occupy a 16-bit (or wider) register → use %QW addresses.
# NOTE: SINT/USINT/BYTE are 8-bit and are intentionally excluded here;
# they are promoted to their 16-bit equivalents before address allocation.
WORD_TYPES = frozenset({
    'INT', 'UINT', 'DINT', 'UDINT',
    'LINT', 'ULINT', 'WORD', 'DWORD', 'LWORD', 'REAL', 'LREAL',
})

# MATIEC (OpenPLC compiler) rejects AT address bit-size mismatches
# (e.g. SINT=8-bit at %QW=16-bit) and does not implicitly promote
# operand types in arithmetic (INT + SINT is a compile error).
# Promote byte-sized integer types to their 16-bit equivalents.
_SMALL_INT_PROMOTE = {'SINT': 'INT', 'USINT': 'UINT', 'BYTE': 'WORD'}


def _promote_type(vtype: str) -> str:
    """Return the 16-bit promoted type for byte-sized integers; others unchanged."""
    return _SMALL_INT_PROMOTE.get(vtype.upper(), vtype)

# Function-block instance types that must NEVER get an AT binding AND must
# not be re-declared (they already live in the plain VAR block unchanged).
_FB_INSTANCE_TYPES = frozenset({
    'R_TRIG', 'F_TRIG', 'TON', 'TOF', 'TP',
    'CTU', 'CTD', 'CTUD', 'SR', 'RS', 'SEMA',
})

# Types that cannot be Modbus-addressed but ARE valid IEC 61131-3 variables.
# These must be preserved as plain VAR declarations (no AT clause).
_NO_AT_TYPES = frozenset({
    'TIME', 'DATE', 'DT', 'TOD', 'STRING', 'WSTRING',
})

# Combined set kept for any external callers that imported SKIP_TYPES.
SKIP_TYPES = _FB_INSTANCE_TYPES | _NO_AT_TYPES


# ---------------------------------------------------------------------------
# Address allocator
# ---------------------------------------------------------------------------

class AddressAllocator:
    """
    Allocates sequential Modbus-writable AT addresses for physical I/O.

    Input addresses start at byte 1 (%QX1.0 / %QW1) so they never overlap
    with output addresses that start at byte 0 (%QX0.0 / %QW0).
    """

    def __init__(self):
        self._in_word  = 1   # Input  words : %QW1, %QW2, …
        self._in_bit   = 8   # Input  bits  : index 8 → %QX1.0, 9 → %QX1.1, …
        self._out_word = 0   # Output words : %QW0, %QW1, …
        self._out_bit  = 0   # Output bits  : index 0 → %QX0.0, 1 → %QX0.1, …

    def next_input(self, vtype: str) -> str:
        if vtype.upper() in WORD_TYPES:
            addr = f'%QW{self._in_word}'
            self._in_word += 1
        else:
            addr = f'%QX{self._in_bit // 8}.{self._in_bit % 8}'
            self._in_bit += 1
        return addr

    def next_output(self, vtype: str) -> str:
        if vtype.upper() in WORD_TYPES:
            addr = f'%QW{self._out_word}'
            self._out_word += 1
        else:
            addr = f'%QX{self._out_bit // 8}.{self._out_bit % 8}'
            self._out_bit += 1
        return addr


# ---------------------------------------------------------------------------
# Regex patterns
# ---------------------------------------------------------------------------

# Matches a complete VAR…END_VAR block (non-greedy body).
# Order matters: more-specific keywords must appear before plain 'VAR'.
BLOCK_RE = re.compile(
    r'(?P<kw>VAR_INPUT|VAR_OUTPUT|VAR_IN_OUT|VAR_EXTERNAL|VAR_TEMP'
    r'|VAR\s+CONSTANT|VAR)'
    r'(?P<body>.*?)'
    r'END_VAR',
    re.DOTALL | re.IGNORECASE,
)

# Matches one variable declaration within a VAR block body, e.g.:
#   "    myVar : BOOL;"
#   "    myVar AT %QX0.0 : BOOL;"
#   "    myVar : INT := -55;"
DECL_RE = re.compile(
    r'^(?P<indent>[ \t]+)'
    r'(?P<name>\w+)'
    r'(?P<at_clause>[ \t]+AT[ \t]+%\w+(?:\.\d+)?)?'
    r'(?P<colon>[ \t]*:[ \t]*)'
    r'(?P<type>\w+(?:\[.*?\])?)'
    r'(?P<init>(?:[ \t]*:=[ \t]*[^;]+)?)'
    r'(?P<semi>[ \t]*;)',
    re.MULTILINE,
)


# ---------------------------------------------------------------------------
# Declaration parser
# ---------------------------------------------------------------------------

def _parse_decls(body: str) -> list:
    """
    Parse all variable declarations from a VAR block body.
    Returns list of dicts: {name, type, init, no_at} — init may be None.
    no_at=True means include the variable as a plain VAR (no AT clause) because
    the type cannot be bound to a Modbus address (e.g. TIME, STRING).
    FB instance types (TON, R_TRIG, …) are skipped entirely.
    """
    # Strip block comments before parsing: inline comments like
    # 'myVar : BOOL (* comment *);' confuse DECL_RE's semicolon detection.
    body_no_comments = re.sub(r'\(\*.*?\*\)', '', body, flags=re.DOTALL)
    result = []
    for m in DECL_RE.finditer(body_no_comments):
        vtype = m.group('type').strip()
        vtype_upper = vtype.upper()
        # FB instances in VAR_INPUT/VAR_OUTPUT are non-standard; skip entirely.
        if vtype_upper in _FB_INSTANCE_TYPES:
            continue
        # DECL_RE captures ':= VALUE' (including ':=') in the init group;
        # strip the leading ':=' so we don't produce ':= := VALUE' when writing.
        init_raw = m.group('init').strip() if m.group('init') else None
        if init_raw:
            init = re.sub(r'^:=\s*', '', init_raw).strip() or None
        else:
            init = None
        no_at = vtype_upper in _NO_AT_TYPES
        result.append({'name': m.group('name'), 'type': vtype, 'init': init, 'no_at': no_at})
    return result


# ---------------------------------------------------------------------------
# CONFIGURATION block helper
# ---------------------------------------------------------------------------

def _ensure_configuration(st_code: str, program_name: str) -> str:
    """Append a standard OpenPLC CONFIGURATION block if one is not present."""
    if re.search(r'\bCONFIGURATION\b', st_code, re.IGNORECASE):
        return st_code

    config = (
        '\n(* OpenPLC Configuration *)\n'
        'CONFIGURATION Config0\n\n'
        '  RESOURCE Res0 ON PLC\n'
        '    TASK task0(INTERVAL := T#20ms, PRIORITY := 0);\n'
        f'    PROGRAM instance0 WITH task0 : {program_name};\n'
        '  END_RESOURCE\n\n'
        'END_CONFIGURATION\n'
    )
    return st_code.rstrip() + '\n' + config


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
      1. Collects every declaration from all VAR_INPUT blocks (input AT addrs).
      2. Collects every declaration from all VAR_OUTPUT blocks (output AT addrs).
      3. Removes all VAR_INPUT and VAR_OUTPUT blocks from the source.
      4. Inserts a single plain VAR block — with AT bindings — at the position
         where the first VAR_INPUT/VAR_OUTPUT block appeared.
      5. Leaves VAR CONSTANT, VAR_IN_OUT, and plain VAR blocks untouched.
      6. Appends CONFIGURATION block if missing.

    Returns:
        testable_code (str)  — rewritten ST source
        summary       (list) — list of (io_kind, var_name, address, var_type)
    """
    # ── Program name (strip block comments to avoid false matches) ──────────
    _no_comments = re.sub(r'\(\*.*?\*\)', '', st_code, flags=re.DOTALL)
    m = re.search(r'^\s*(PROGRAM|FUNCTION_BLOCK)\s+(\w+)', _no_comments, re.IGNORECASE | re.MULTILINE)
    is_function_block = m and m.group(1).upper() == 'FUNCTION_BLOCK'
    program_name = m.group(2) if m else 'UnknownProgram'

    allocator = AddressAllocator()
    summary   = []          # (label, var_name, address, var_type)

    # ── Collect all VAR_INPUT / VAR_OUTPUT declarations ─────────────────────
    io_decls = []           # list of {'name', 'type', 'init', 'address', 'label'}
    first_io_pos = None     # character position of the first IO block

    for bm in BLOCK_RE.finditer(st_code):
        kw = re.sub(r'\s+', ' ', bm.group('kw')).upper().strip()
        if kw not in ('VAR_INPUT', 'VAR_OUTPUT'):
            continue
        if first_io_pos is None:
            first_io_pos = bm.start()
        assign = allocator.next_input if kw == 'VAR_INPUT' else allocator.next_output
        label  = 'VAR_INPUT' if kw == 'VAR_INPUT' else 'VAR_OUTPUT'
        for decl in _parse_decls(bm.group('body')):
            if decl['no_at']:
                # Non-addressable type (TIME, STRING, etc.) — preserve as plain VAR
                io_decls.append({**decl, 'address': None, 'label': label})
            else:
                # Promote 8-bit types to 16-bit before allocating address so
                # the AT clause type matches the %QW register width.
                promoted_type = _promote_type(decl['type'])
                addr = assign(promoted_type)
                io_decls.append({**decl, 'type': promoted_type, 'address': addr, 'label': label})
                summary.append((label, decl['name'], addr, promoted_type))

    if not io_decls:
        # No VAR_INPUT/VAR_OUTPUT found — only add CONFIGURATION if needed
        result = _ensure_configuration(st_code, program_name)
        return result, summary

    # ── Resolve variable names that clash with the program name ──────────────
    # IEC 61131-3 identifiers are case-insensitive, so a VAR_INPUT named e.g.
    # TRIP_LOGIC inside PROGRAM trip_logic causes MATIEC compilation errors
    # (both "invalid located variable declaration" and expression parse errors).
    _body_renames: dict = {}   # original_name → safe_new_name
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

    # ── Build replacement VAR blocks ─────────────────────────────────────────
    # IEC 61131-3 requires located (AT) vars and plain vars in separate blocks.
    at_decls    = [d for d in io_decls if d['address'] is not None]
    no_at_decls = [d for d in io_decls if d['address'] is None]

    block_lines = []
    if at_decls:
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

    # ── Replace first VAR_INPUT/VAR_OUTPUT block with the new VAR block;
    #    remove all subsequent VAR_INPUT/VAR_OUTPUT blocks.
    #
    #    This keeps the new VAR block at the exact position where the I/O
    #    declarations were — i.e. inside the PROGRAM body before the logic,
    #    not after END_PROGRAM/END_FUNCTION_BLOCK.
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

    # Clean up excessive blank lines left by block removal
    result = re.sub(r'\n{3,}', '\n\n', result)

    # ── If source was a FUNCTION_BLOCK, promote to PROGRAM (AT bindings
    #    are only valid in PROGRAM blocks in MATIEC / OpenPLC) ──────────────
    if is_function_block:
        result = re.sub(
            r'\bFUNCTION_BLOCK\b', 'PROGRAM', result, flags=re.IGNORECASE
        )
        result = re.sub(
            r'\bEND_FUNCTION_BLOCK\b', 'END_PROGRAM', result, flags=re.IGNORECASE
        )
        # Strip the bare CONFIGURATION block that xml_to_st_converter emits for
        # function blocks (it has no TASK/PROGRAM lines).  _ensure_configuration
        # will append a proper fully-scheduled block below.
        # Anchor to start-of-line so we don't accidentally match the word
        # "Configuration" that appears inside comment (* OpenPLC Configuration *)
        result = re.sub(
            r'^\s*CONFIGURATION\b.*?\bEND_CONFIGURATION\b',
            '',
            result,
            flags=re.DOTALL | re.IGNORECASE | re.MULTILINE,
        )
        # Strip the preceding "(* OpenPLC Configuration *)" comment line
        result = re.sub(r'\(\*\s*OpenPLC Configuration\s*\*\)\s*\n?', '', result)
        result = result.rstrip()

    # ── Apply body renames: replace old variable names with their safe aliases ─
    # Use case-sensitive matching so only the exact original casing is replaced.
    # This avoids touching the (typically lowercase) program name in the header,
    # configuration block, and comments, which share the same letters but differ
    # in case from the (typically uppercase) VAR declarations.
    for old_name, new_name in _body_renames.items():
        result = re.sub(
            r'\b' + re.escape(old_name) + r'\b',
            new_name,
            result,
        )

    # ── Promote 8-bit integer types to 16-bit throughout all VAR declarations ─
    # Applied after block-substitution so it covers both the new AT-mapped VAR
    # block AND any plain VAR blocks that were left untouched (e.g. internal
    # variables like 'hello : SINT').  This prevents MATIEC from rejecting
    # mixed-width arithmetic such as 'INT + SINT' in the program body.
    # The pattern ': TYPENAME' is only valid on the right-hand side of variable
    # declarations in ST, so this substitution is safe across the entire file.
    for small, large in _SMALL_INT_PROMOTE.items():
        result = re.sub(
            r'(:\s*)' + small + r'\b',
            lambda m, lg=large: m.group(1) + lg,
            result,
            flags=re.IGNORECASE,
        )

    result = _ensure_configuration(result, program_name)
    return result, summary


def _find_body_start(st_code: str) -> int:
    """
    Find the character index in *st_code* where the POU body starts — i.e.
    the first non-blank, non-comment line after all VAR…END_VAR blocks inside
    the PROGRAM.  Returns None if not found.
    """
    # Walk line by line; track whether we are inside a VAR…END_VAR block
    in_var = False
    last_end_var_pos = None
    lines = st_code.split('\n')
    pos = 0
    for line in lines:
        stripped = line.strip()
        if re.match(r'\bVAR\b', stripped, re.IGNORECASE):
            in_var = True
        if re.match(r'\bEND_VAR\b', stripped, re.IGNORECASE):
            in_var = False
            last_end_var_pos = pos + len(line) + 1   # +1 for the '\n'
        pos += len(line) + 1

    return last_end_var_pos


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

    st_code          = st_path.read_text(encoding='utf-8')
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
        print('No VAR_INPUT / VAR_OUTPUT variables found — file copied unchanged.')


if __name__ == '__main__':
    main()
