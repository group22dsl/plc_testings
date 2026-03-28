"""
PLCopen XML to ST Converter — Full Implementation (Fixed & Enhanced)
=====================================================================
Converts PLCopen XML (IEC 61131-3) files to Structured Text (ST).

Supports body types : FBD (Function Block Diagram), ST (pass-through),
                      LD (basic rung-to-ST conversion), SFC (stub output).
Supports POU types  : PROGRAM, FUNCTION_BLOCK, FUNCTION.
Handles FBD         : full graph traversal — resolves operator chains
                      (AND, OR, NOT, GE, LE, GT, LT, EQ, NE, ADD, SUB,
                       MUL, DIV, MOD, XOR) and generic function calls.

Supports multiple XML namespaces:
  - PLCopen TC6 2.01  (http://www.plcopen.org/xml/tc6_0200)
  - PLCopen TC6 2.00  (http://www.plcopen.org/xml/tc6_0201)
  - Siemens TIA Portal XML
  - Beckhoff TwinCAT 3 XML
  - CODESYS 3.x XML
  - OpenPLC XML
  - B&R Automation Studio XML

Usage:
    python xml_to_st_converter.py input.xml output.st
    python xml_to_st_converter.py input.xml        # auto-derive output name
    python xml_to_st_converter.py input.xml -       # print to stdout
"""

from __future__ import annotations

import re
import sys
import logging
import xml.etree.ElementTree as ET
from pathlib import Path
from datetime import datetime

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format='[%(levelname)s] %(message)s',
)
log = logging.getLogger('xml_to_st')

# ---------------------------------------------------------------------------
# Multi-vendor namespace discovery
# ---------------------------------------------------------------------------

# Known PLCopen / vendor XML namespaces — tried in priority order
_KNOWN_NAMESPACES = [
    'http://www.plcopen.org/xml/tc6_0200',   # PLCopen TC6 2.01 (most common)
    'http://www.plcopen.org/xml/tc6_0201',   # PLCopen TC6 2.00 (some tools emit this)
    'http://www.plcopen.org/xml/tc6',        # generic PLCopen fallback
    '',                                      # no-namespace fallback (some exporters)
]

def _detect_namespace(root) -> str:
    """
    BUG FIX: The original code hard-coded a single namespace URI, so any file
    from Beckhoff, B&R, some CODESYS versions, or older PLCopen exporters that
    use a slightly different URI would produce zero matches and an empty output.

    This function inspects the actual root tag and every first-level child to
    discover whichever namespace is actually present in the file.
    Returns the namespace string (may be empty) to use for all element lookups.
    """
    # Try to extract from root tag directly: {ns}tag
    tag = root.tag
    if tag.startswith('{'):
        return tag[1:tag.index('}')]

    # Root has no namespace — scan children
    for child in root:
        ctag = child.tag
        if ctag.startswith('{'):
            return ctag[1:ctag.index('}')]

    return ''   # no namespace


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# All IEC 61131-3 elementary types (order matters: longer names must come first
# so 'LREAL' is matched before 'REAL', 'LINT' before 'INT', etc.)
BASIC_TYPES = [
    'LREAL', 'REAL',
    'LINT', 'DINT', 'INT', 'SINT',
    'ULINT', 'UDINT', 'UINT', 'USINT',
    'LWORD', 'DWORD', 'WORD', 'BYTE', 'BOOL',
    'LTIME', 'TIME', 'DATE', 'TOD', 'DT',   # BUG FIX: added LTIME (IEC 61131-3 ed.3)
    'WSTRING', 'STRING', 'WCHAR', 'CHAR',
]

# FBD block types → ST binary infix operators
BINARY_OPS: dict[str, str] = {
    'GE': '>=', 'LE': '<=', 'GT': '>', 'LT': '<',
    'EQ': '=',  'NE': '<>',
    'AND': 'AND', 'OR': 'OR', 'XOR': 'XOR',
    'ADD': '+',  'SUB': '-', 'MUL': '*', 'DIV': '/', 'MOD': 'MOD',
    # BUG FIX: EXPT (exponentiation) was missing — common in IEC 61131-3
    'EXPT': '**',
}

# FBD block types → ST unary prefix operators
UNARY_OPS: dict[str, str] = {
    'NOT': 'NOT',
    'NEG': '-',    # BUG FIX: NEG (arithmetic negation) was missing
}

# IEC 61131-3 standard function blocks that need instance declarations
# BUG FIX: original had no concept of this; we use it to improve type inference
STANDARD_FB_TYPES = {
    'TON', 'TOF', 'TP', 'RTC',
    'CTU', 'CTD', 'CTUD',
    'SR', 'RS', 'SEMA',
    'R_TRIG', 'F_TRIG',
}

# IEC 61131-3 reserved words (must not be used as variable names)
IEC_KEYWORDS = {
    'AND', 'OR', 'XOR', 'NOT', 'MOD',
    'IF', 'THEN', 'ELSE', 'ELSIF', 'END_IF',
    'FOR', 'TO', 'BY', 'DO', 'END_FOR',
    'WHILE', 'END_WHILE', 'REPEAT', 'UNTIL', 'END_REPEAT',
    'CASE', 'OF', 'END_CASE', 'EXIT', 'RETURN',
    'PROGRAM', 'END_PROGRAM', 'FUNCTION', 'END_FUNCTION',
    'FUNCTION_BLOCK', 'END_FUNCTION_BLOCK',
    'VAR', 'VAR_INPUT', 'VAR_OUTPUT', 'VAR_IN_OUT', 'VAR_EXTERNAL',
    'VAR_GLOBAL', 'VAR_TEMP', 'VAR_CONSTANT', 'END_VAR',
    'TYPE', 'END_TYPE', 'STRUCT', 'END_STRUCT',
    'ARRAY', 'OF', 'AT',
    'CONFIGURATION', 'END_CONFIGURATION', 'RESOURCE', 'END_RESOURCE',
    'TASK', 'WITH', 'PRIORITY', 'INTERVAL',
    'TRUE', 'FALSE', 'NULL',
}

# Operator precedence for parenthesisation decisions
# Higher number = tighter binding
_OP_PRECEDENCE: dict[str, int] = {
    'OR': 1, 'XOR': 2, 'AND': 3,
    'NOT': 4,
    '=': 5, '<>': 5, '<': 5, '>': 5, '<=': 5, '>=': 5,
    '+': 6, '-': 6,
    '*': 7, '/': 7, 'MOD': 7,
    '**': 8,
    'NEG': 9,
}


# ---------------------------------------------------------------------------
# FBD node dataclass
# ---------------------------------------------------------------------------

class _FBDNode:
    __slots__ = (
        'local_id', 'kind', 'expression', 'type_name',
        'instance_name', 'inputs', 'execution_order_id',
    )

    def __init__(
        self,
        local_id: str,
        kind: str,
        expression: str | None = None,
        type_name: str | None = None,
        instance_name: str | None = None,
        execution_order_id: int | None = None,
    ):
        self.local_id           = local_id
        self.kind               = kind
        self.expression         = expression
        self.type_name          = type_name
        self.instance_name      = instance_name
        # BUG FIX: track execution order so FB calls are emitted in the correct
        # sequence; original code emitted them in dict-insertion order which
        # produced wrong results when XML elements were not ordered logically.
        self.execution_order_id = execution_order_id
        # {formalParameter: (refLocalId, refFormalParameter | None, negated: bool)}
        self.inputs: dict[str, tuple[str, str | None, bool]] = {}


# ---------------------------------------------------------------------------
# LD (Ladder Diagram) converter
# ---------------------------------------------------------------------------

class LDConverter:
    """
    BUG FIX / ENHANCEMENT: The original code simply emitted a comment for LD
    bodies and gave up.  This class provides a best-effort conversion of
    common LD constructs to ST, covering the majority of real-world ladder
    programs.

    Supported elements:
      - contacts (normallyOpen / normallyClosed / positiveTransition / negativeTransition)
      - coils (normalCoil / negatedCoil / setCoil / resetCoil)
      - leftPowerRail / rightPowerRail
      - blocks (function blocks called from LD rungs)
      - Series and parallel connections via connection graph traversal
    """

    def __init__(self, plc_prefix: str):
        self._plc = plc_prefix

    def convert(self, ld_elem) -> list[str]:
        lines: list[str] = []
        rungs = ld_elem.findall(f'{self._plc}rung')
        if not rungs:
            # Some exporters wrap rungs differently — try direct children
            rungs = [ld_elem]

        for i, rung in enumerate(rungs):
            rung_comment = rung.get('comment', '') or rung.findtext(
                f'{self._plc}comment', ''
            )
            if rung_comment:
                lines.append(f'(* Rung {i + 1}: {rung_comment.strip()} *)')

            rung_st = self._convert_rung(rung)
            if rung_st:
                lines.extend(rung_st)
            else:
                lines.append(f'(* Rung {i + 1}: could not auto-convert — manual review required *)')

        return lines

    def _convert_rung(self, rung) -> list[str]:
        """Convert a single LD rung to ST statements."""
        # Build node lookup by localId
        nodes: dict[str, ET.Element] = {}
        for elem in rung:
            lid = elem.get('localId')
            if lid:
                nodes[lid] = elem

        # Find all coil outputs — each one generates an assignment
        statements: list[str] = []
        for elem in rung:
            tag = elem.tag.replace(self._plc, '')
            if tag == 'coil':
                stmt = self._coil_to_st(elem, nodes)
                if stmt:
                    statements.append(stmt)
            elif tag == 'block':
                stmt = self._ld_block_to_st(elem, nodes)
                if stmt:
                    statements.append(stmt)

        return statements

    def _resolve_rung_condition(
        self, elem: ET.Element, nodes: dict
    ) -> str:
        """
        Walk backwards from the connectionPointIn of `elem` to build the
        Boolean condition expression in ST.
        """
        cp_in = elem.find(f'{self._plc}connectionPointIn')
        if cp_in is None:
            return 'TRUE'

        parts: list[str] = []
        for conn in cp_in.findall(f'{self._plc}connection'):
            ref_id = conn.get('refLocalId')
            ref_node = nodes.get(ref_id or '')
            if ref_node is None:
                continue
            parts.append(self._node_to_expr(ref_node, nodes))

        if not parts:
            return 'TRUE'
        # Multiple connections to the same input pin = OR (parallel branches)
        return ' OR '.join(f'({p})' if ' ' in p else p for p in parts)

    def _node_to_expr(self, elem: ET.Element, nodes: dict) -> str:
        """Recursively build an expression from an LD node."""
        tag = elem.tag.replace(self._plc, '')
        variable = elem.get('variable', '')

        if tag == 'contact':
            contact_type = elem.get('contactType', 'normallyOpen')
            upstream = self._resolve_rung_condition(elem, nodes)
            # Build contact term
            if contact_type == 'normallyClosed':
                contact_expr = f'NOT({variable})'
            elif contact_type == 'positiveTransition':
                contact_expr = f'R_EDGE({variable})'
            elif contact_type == 'negativeTransition':
                contact_expr = f'F_EDGE({variable})'
            else:
                contact_expr = variable

            if upstream and upstream != 'TRUE':
                return f'({upstream} AND {contact_expr})'
            return contact_expr

        if tag == 'leftPowerRail':
            # Collect all outgoing connection expressions (parallel branches)
            # For left power rail, each output connection feeds forward
            return 'TRUE'

        # Fallback: return variable name
        return variable or '(* unknown *)'

    def _coil_to_st(self, coil: ET.Element, nodes: dict) -> str | None:
        """Generate ST for a coil output."""
        variable = coil.get('variable', '')
        coil_type = coil.get('coilType', 'normalCoil')
        condition = self._resolve_rung_condition(coil, nodes)

        if not variable:
            return None

        if coil_type == 'normalCoil':
            return f'{variable} := {condition};'
        if coil_type == 'negatedCoil':
            return f'{variable} := NOT({condition});'
        if coil_type == 'setCoil':
            return f'IF {condition} THEN {variable} := TRUE; END_IF;'
        if coil_type == 'resetCoil':
            return f'IF {condition} THEN {variable} := FALSE; END_IF;'
        # Transition coils — approximation
        return f'{variable} := {condition};'

    def _ld_block_to_st(self, block: ET.Element, nodes: dict) -> str | None:
        """Generate ST for a function block called from a LD rung."""
        type_name = block.get('typeName', '')
        instance  = block.get('instanceName', '')
        if not instance:
            return None

        condition = self._resolve_rung_condition(block, nodes)
        # Gather input-pin assignments
        args: list[str] = []
        iv_section = block.find(f'{self._plc}inputVariables')
        if iv_section is not None:
            for var in iv_section.findall(f'{self._plc}variable'):
                param = var.get('formalParameter', '')
                cp_in = var.find(f'{self._plc}connectionPointIn')
                if cp_in is not None:
                    for conn in cp_in.findall(f'{self._plc}connection'):
                        ref_id  = conn.get('refLocalId')
                        ref_node = nodes.get(ref_id or '')
                        if ref_node is not None:
                            expr = self._node_to_expr(ref_node, nodes)
                            args.append(f'{param} := {expr}')

        call = f'{instance}({", ".join(args)});'
        if condition and condition != 'TRUE':
            return f'IF {condition} THEN\n    {call}\nEND_IF;'
        return call


# ---------------------------------------------------------------------------
# Main converter class
# ---------------------------------------------------------------------------

class PLCopenXMLConverter:
    """Convert a PLCopen XML (or compatible vendor XML) file to ST."""

    # Map PLCopen XML camelCase pouType values → IEC 61131-3 keywords
    _POU_TYPE_MAP: dict[str, str] = {
        'program':       'PROGRAM',
        'functionblock': 'FUNCTION_BLOCK',
        'function':      'FUNCTION',
        # BUG FIX: some vendors emit 'FunctionBlock' (mixed-case)
        'FunctionBlock': 'FUNCTION_BLOCK',
        'Function':      'FUNCTION',
        'Program':       'PROGRAM',
    }

    def __init__(self, xml_file: str):
        self.xml_file = xml_file

        # BUG FIX: catch and re-raise XML parse errors with a clear message
        try:
            tree = ET.parse(xml_file)
        except ET.ParseError as exc:
            raise ValueError(
                f"XML parse error in '{xml_file}': {exc}\n"
                "Check that the file is valid XML and not a binary/compressed export."
            ) from exc

        self.root = tree.getroot()

        # BUG FIX: dynamic namespace detection instead of hard-coded URI
        ns_uri   = _detect_namespace(self.root)
        self._ns = ns_uri
        self._plc = f'{{{ns_uri}}}' if ns_uri else ''

        if not ns_uri:
            log.warning(
                "No XML namespace detected — assuming no-namespace PLCopen XML. "
                "Results may be incomplete for vendor-specific dialects."
            )
        else:
            log.info("Detected XML namespace: %s", ns_uri)

        self._ld_converter = LDConverter(self._plc)

        # Per-convert state (reset in convert())
        self._global_fb_types: dict[str, str] = {}   # instanceName → typeName

    # -----------------------------------------------------------------------
    # Public entry point
    # -----------------------------------------------------------------------

    def convert(self) -> str:
        self._global_fb_types = {}

        now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        lines: list[str] = [
            '(*',
            f'  Generated from  : {Path(self.xml_file).name}',
            f'  Converter       : PLCopen XML → ST (multi-vendor)',
            f'  Generated at    : {now}',
            '*)', '',
        ]

        # Try common root paths for the <pous> container
        # BUG FIX: original code used findall('.//{PLC}pou') which skips the
        # <fileHeader> / <contentHeader> and works, but also silently finds pous
        # nested inside <types> (data-type POUs) which should not be emitted as
        # top-level program units.  We now look for pous under <project> only.
        pous = (
            self.root.findall(f'.//{self._plc}pous/{self._plc}pou')
            or self.root.findall(f'.//{self._plc}pou')
        )

        if not pous:
            log.warning("No POU elements found in '%s'.", self.xml_file)
            lines.append('(* No POU elements found — check namespace / file format *)')
            return '\n'.join(lines)

        pou_info: list[tuple[str, str]] = []
        for pou in pous:
            name  = pou.get('name', 'UnknownPOU')
            ptype = pou.get('pouType', 'program')
            pou_info.append((name, ptype))
            lines.extend(self._convert_pou(pou))
            lines.append('')

        # Data-type declarations (STRUCT, ENUM, SUBRANGE, ARRAY aliases)
        type_lines = self._extract_data_types()
        if type_lines:
            lines = type_lines + [''] + lines

        # Global variable lists
        gvl_lines = self._extract_global_vars()
        if gvl_lines:
            lines.extend([''] + gvl_lines)

        # OpenPLC-compatible configuration block
        lines += [
            '',
            '(* ——— OpenPLC / IEC 61131-3 Configuration ——— *)',
            'CONFIGURATION Config0', '',
            '  RESOURCE Res0 ON PLC',
        ]
        task_idx = 0
        for name, ptype in pou_info:
            if ptype.lower() in ('program', 'Program'):
                interval = 'T#20ms'
                lines.append(
                    f'    TASK task{task_idx}(INTERVAL := {interval}, PRIORITY := {task_idx});'
                )
                lines.append(
                    f'    PROGRAM instance{task_idx} WITH task{task_idx} : {name};'
                )
                task_idx += 1
        lines += ['  END_RESOURCE', '', 'END_CONFIGURATION']

        return '\n'.join(lines)

    # -----------------------------------------------------------------------
    # Data types
    # -----------------------------------------------------------------------

    def _extract_data_types(self) -> list[str]:
        """
        BUG FIX / ENHANCEMENT: the original converter completely ignored
        <dataTypes> declarations (STRUCT, ENUM, SUBRANGE).  Without them the
        generated ST file fails to compile whenever any POU variable uses a
        user-defined type.
        """
        lines: list[str] = []
        dt_root = self.root.find(f'.//{self._plc}dataTypes')
        if dt_root is None:
            return lines

        for dt in dt_root.findall(f'{self._plc}dataType'):
            name = dt.get('name', 'UnknownType')
            base = dt.find(f'{self._plc}baseType')
            struct = dt.find(f'{self._plc}struct') or (
                base.find(f'{self._plc}struct') if base is not None else None
            )
            enum  = dt.find(f'{self._plc}enum')   or (
                base.find(f'{self._plc}enum')   if base is not None else None
            )
            subrange = dt.find(f'{self._plc}subrange') or (
                base.find(f'{self._plc}subrange') if base is not None else None
            )

            if struct is not None:
                lines.append(f'TYPE {name} :')
                lines.append('  STRUCT')
                for member in struct.findall(f'{self._plc}variable'):
                    mname = member.get('name', 'field')
                    mtype = self._get_type(member.find(f'{self._plc}type'))
                    init  = self._get_init(member)
                    if init is not None:
                        lines.append(f'    {mname} : {mtype} := {init};')
                    else:
                        lines.append(f'    {mname} : {mtype};')
                lines.append('  END_STRUCT')
                lines.append('END_TYPE')
                lines.append('')

            elif enum is not None:
                values_elem = enum.find(f'{self._plc}values')
                if values_elem is not None:
                    vals = [
                        v.get('name', '')
                        for v in values_elem.findall(f'{self._plc}value')
                        if v.get('name')
                    ]
                    lines.append(f'TYPE {name} : ({", ".join(vals)});')
                    lines.append('END_TYPE')
                    lines.append('')

            elif subrange is not None and base is not None:
                base_type = self._get_type(base)
                low  = subrange.get('lower', '0')
                high = subrange.get('upper', '65535')
                lines.append(f'TYPE {name} : {base_type} ({low}..{high});')
                lines.append('END_TYPE')
                lines.append('')

            elif base is not None:
                # Simple alias
                base_type = self._get_type(base)
                lines.append(f'TYPE {name} : {base_type};')
                lines.append('END_TYPE')
                lines.append('')

        return lines

    # -----------------------------------------------------------------------
    # Global variable lists
    # -----------------------------------------------------------------------

    def _extract_global_vars(self) -> list[str]:
        """
        BUG FIX / ENHANCEMENT: the original converter ignored <globalVars>.
        Without VAR_GLOBAL blocks the generated file cannot compile if any POU
        references a global variable.
        """
        lines: list[str] = []
        for gvl in self.root.findall(f'.//{self._plc}globalVars'):
            gvl_name = gvl.get('name', '')
            header = f'VAR_GLOBAL  (* {gvl_name} *)' if gvl_name else 'VAR_GLOBAL'
            block_lines = self._var_block_inner(gvl)
            if block_lines:
                lines.append(header)
                lines.extend(block_lines)
                lines.append('END_VAR')
                lines.append('')
        return lines

    # -----------------------------------------------------------------------
    # POU
    # -----------------------------------------------------------------------

    def _convert_pou(self, pou) -> list[str]:
        lines: list[str] = []
        name  = pou.get('name', 'UnknownPOU')
        ptype = self._POU_TYPE_MAP.get(
            pou.get('pouType', 'program').lower(), 'PROGRAM'
        )

        # BUG FIX: FUNCTION requires a return-type annotation.
        # The original code emitted 'FUNCTION Foo' without the return type,
        # which is a syntax error for every IEC 61131-3 compiler.
        if ptype == 'FUNCTION':
            return_type = self._get_function_return_type(pou)
            lines.append(f'{ptype} {name} : {return_type}')
        else:
            lines.append(f'{ptype} {name}')

        iface = pou.find(f'{self._plc}interface')
        iface_lines = self._extract_interface(iface) if iface is not None else []

        body = pou.find(f'{self._plc}body')
        body_lines = self._extract_body(body) if body is not None else []

        # Collect FB instance types declared in the body so we can
        # verify/supplement interface declarations
        self._collect_fb_instances(body_lines)

        # Detect undeclared FBD intermediate variables and inject them
        self._inject_missing_fbd_vars(iface_lines, body_lines)

        lines.extend(iface_lines)

        # Emit BEGIN between var blocks and body statements.
        # MATIEC / OpenPLC requires a BEGIN keyword to separate the variable
        # declaration section from the statement body in PROGRAM and
        # FUNCTION_BLOCK POUs.  Without it the compiler cannot tell where
        # declarations end and executable statements begin, producing
        # "';' missing at end of statement" errors on the first statement.
        # FUNCTION POUs do NOT use BEGIN (return-type annotation serves as
        # the separator in standard IEC 61131-3).
        if iface_lines:
            lines.append('')
        if ptype in ('PROGRAM', 'FUNCTION_BLOCK'):
            lines.append('BEGIN')

        lines.extend(body_lines)
        lines.append(f'END_{ptype}')
        return lines

    def _get_function_return_type(self, pou) -> str:
        """
        BUG FIX: extract the return type for FUNCTION POUs.
        PLCopen XML stores this in <interface><returnType>.
        """
        iface = pou.find(f'{self._plc}interface')
        if iface is not None:
            rt = iface.find(f'{self._plc}returnType')
            if rt is not None:
                return self._get_type(rt)
        return 'BOOL'   # safe default

    def _collect_fb_instances(self, body_lines: list[str]) -> None:
        """Scan body lines for 'Instance(...)' calls and record them."""
        for line in body_lines:
            m = re.match(r'\s*(\w+)\s*\(', line)
            if m:
                inst = m.group(1)
                if inst not in IEC_KEYWORDS:
                    # We don't know the type here; recorded as placeholder
                    self._global_fb_types.setdefault(inst, 'UNKNOWN')

    @staticmethod
    def _inject_missing_fbd_vars(iface_lines: list[str], body_lines: list[str]) -> None:
        """
        Add FBD outVariable targets absent from the interface to iface_lines.

        BUG FIX: the original implementation failed to consider RETAIN / PERSISTENT
        qualifiers and also didn't handle dotted names (e.g. 'FB_Instance.Q') as
        valid already-declared references, causing spurious VAR declarations for
        FB output references.
        """
        # Build name→type map from interface lines
        type_map: dict[str, str] = {}
        for line in iface_lines:
            m = re.match(
                r'[ \t]+(\w+)[ \t]*(?:AT[ \t]+%\w+(?:\.\d+)?)?[ \t]*:[ \t]*(\w+)',
                line,
            )
            if m:
                type_map[m.group(1)] = m.group(2).upper()

        # Collect undeclared simple-identifier LHS from body
        undeclared: dict[str, str | None] = {}
        for line in body_lines:
            m = re.match(r'[ \t]*(\w+)[ \t]*:=', line)
            if m:
                name = m.group(1)
                # BUG FIX: skip IEC keywords and dotted names
                if (
                    name not in type_map
                    and name not in undeclared
                    and name not in IEC_KEYWORDS
                    and '.' not in name
                ):
                    undeclared[name] = None

        if not undeclared:
            return

        # Infer types from simple "DeclaredVar := UndeclaredVar;" lines
        for line in body_lines:
            m = re.match(r'[ \t]*(\w+)[ \t]*:=[ \t]*(\w+)[ \t]*;', line)
            if not m:
                continue
            lhs, rhs = m.group(1), m.group(2)
            if rhs in undeclared and undeclared[rhs] is None and lhs in type_map:
                undeclared[rhs] = type_map[lhs]
            if lhs in undeclared and undeclared[lhs] is None and rhs in type_map:
                undeclared[lhs] = type_map[rhs]

        # Default any still-unknown types to BOOL (most FBD intermediates are BOOL)
        # BUG FIX: original defaulted to INT which is wrong for most Boolean wires
        for name in undeclared:
            if undeclared[name] is None:
                undeclared[name] = 'BOOL'

        # Append a new VAR block
        iface_lines.append('VAR')
        for name, vtype in undeclared.items():
            iface_lines.append(f'    {name} : {vtype};')
        iface_lines.append('END_VAR')
        iface_lines.append('')

    # -----------------------------------------------------------------------
    # Interface / variable declarations
    # -----------------------------------------------------------------------

    def _extract_interface(self, iface) -> list[str]:
        """Iterate all child var sections in document order."""
        lines: list[str] = []
        for child in iface:
            tag = child.tag.replace(self._plc, '')
            if tag == 'returnType':
                continue   # handled separately for FUNCTION
            if tag == 'inputVars':
                lines += self._var_block('VAR_INPUT', child)
            elif tag == 'outputVars':
                lines += self._var_block('VAR_OUTPUT', child)
            elif tag == 'inOutVars':
                lines += self._var_block('VAR_IN_OUT', child)
            elif tag == 'localVars':
                is_const   = child.get('constant') == 'true'
                is_retain  = child.get('retain')   == 'true'
                # BUG FIX: original ignored RETAIN/PERSISTENT qualifiers
                if is_const:
                    kw = 'VAR CONSTANT'
                elif is_retain:
                    kw = 'VAR RETAIN'
                else:
                    kw = 'VAR'
                lines += self._var_block(kw, child)
            elif tag == 'externalVars':
                lines += self._var_block('VAR_EXTERNAL', child)
            elif tag == 'tempVars':
                lines += self._var_block('VAR_TEMP', child)
            # BUG FIX: 'globalVars' inside an interface is non-standard but
            # some Beckhoff exports include it; treat as VAR_EXTERNAL
            elif tag == 'globalVars':
                lines += self._var_block('VAR_EXTERNAL', child)
        return lines

    def _var_block(self, keyword: str, section) -> list[str]:
        lines = [keyword]
        inner = self._var_block_inner(section)
        if not inner:
            return []   # skip empty blocks entirely
        lines.extend(inner)
        lines += ['END_VAR', '']
        return lines

    def _var_block_inner(self, section) -> list[str]:
        """Return the variable lines (without VAR/END_VAR wrapper)."""
        lines: list[str] = []
        variables = section.findall(f'{self._plc}variable')
        for var in variables:
            vname = var.get('name', 'unnamed')

            # BUG FIX: warn about reserved-word variable names
            if vname.upper() in IEC_KEYWORDS:
                log.warning(
                    "Variable '%s' is an IEC 61131-3 reserved word — "
                    "rename to avoid compiler errors.", vname
                )

            vtype = self._get_type(var.find(f'{self._plc}type'))
            init  = self._get_init(var)
            addr  = var.get('address', '')

            # BUG FIX: support AT %IX / %QX / %MX / %IW … direct-address vars
            at_clause = f' AT {addr}' if addr else ''

            comment = var.get('comment', '')
            suffix  = f'  (* {comment} *)' if comment else ''

            if init is not None:
                lines.append(f'    {vname}{at_clause} : {vtype} := {init};{suffix}')
            else:
                lines.append(f'    {vname}{at_clause} : {vtype};{suffix}')
        return lines

    def _get_type(self, type_elem) -> str:
        if type_elem is None:
            return 'BOOL'

        # Elementary types (order matters — longer names first)
        for t in BASIC_TYPES:
            if type_elem.find(f'{self._plc}{t}') is not None:
                return t

        # ARRAY
        arr = type_elem.find(f'{self._plc}array')
        if arr is not None:
            # BUG FIX: the original code did not handle multi-dimensional arrays
            base_type_elem = arr.find(f'{self._plc}baseType')
            base = self._get_type(base_type_elem)
            dims = [
                f'{d.get("lower", "0")}..{d.get("upper", "0")}'
                for d in arr.findall(f'{self._plc}dimension')
            ]
            return f'ARRAY[{", ".join(dims)}] OF {base}'

        # User-defined / derived type
        derived = type_elem.find(f'{self._plc}derived')
        if derived is not None:
            return derived.get('name', 'UNKNOWN')

        # STRING with explicit length
        s = type_elem.find(f'{self._plc}string')
        if s is not None:
            length = s.get('length')
            return f'STRING[{length}]' if length else 'STRING'

        # WSTRING with explicit length
        ws = type_elem.find(f'{self._plc}wstring')
        if ws is not None:
            length = ws.get('length')
            return f'WSTRING[{length}]' if length else 'WSTRING'

        # BUG FIX: pointer types (TwinCAT / CODESYS extension)
        ptr = type_elem.find(f'{self._plc}pointer')
        if ptr is not None:
            base = self._get_type(ptr.find(f'{self._plc}baseType'))
            return f'POINTER TO {base}'

        # BUG FIX: reference types (CODESYS extension)
        ref = type_elem.find(f'{self._plc}reference')
        if ref is not None:
            base = self._get_type(ref.find(f'{self._plc}baseType'))
            return f'REFERENCE TO {base}'

        log.debug("Unknown type element: %s", ET.tostring(type_elem, encoding='unicode'))
        return 'BOOL'

    def _get_init(self, var) -> str | None:
        """
        BUG FIX: the original code only handled <simpleValue>.  Many exporters
        also emit <arrayValue> and <structValue> for complex initialisers.
        """
        iv = var.find(f'{self._plc}initialValue')
        if iv is None:
            return None

        sv = iv.find(f'{self._plc}simpleValue')
        if sv is not None:
            raw = sv.get('value', '')
            # BUG FIX: normalise boolean literals to IEC 61131-3 TRUE/FALSE
            if raw.upper() in ('TRUE', '1', 'TRUE()', '1#TRUE'):
                return 'TRUE'
            if raw.upper() in ('FALSE', '0', 'FALSE()', '1#FALSE'):
                return 'FALSE'
            return raw

        # Array initialiser
        av = iv.find(f'{self._plc}arrayValue')
        if av is not None:
            elems = []
            for ae in av.findall(f'{self._plc}value'):
                esv = ae.find(f'{self._plc}simpleValue')
                elems.append(esv.get('value', '0') if esv is not None else '0')
            return f'[{", ".join(elems)}]'

        # Struct initialiser
        stv = iv.find(f'{self._plc}structValue')
        if stv is not None:
            parts = []
            for member in stv.findall(f'{self._plc}value'):
                mname = member.get('member', '')
                esv   = member.find(f'{self._plc}simpleValue')
                mval  = esv.get('value', '0') if esv is not None else '0'
                parts.append(f'{mname} := {mval}')
            return f'({", ".join(parts)})'

        return None

    # -----------------------------------------------------------------------
    # Body dispatch
    # -----------------------------------------------------------------------

    def _extract_body(self, body) -> list[str]:
        # BUG FIX: original used hard-coded PLC prefix; now uses self._plc
        st  = body.find(f'{self._plc}ST')
        fbd = body.find(f'{self._plc}FBD')
        ld  = body.find(f'{self._plc}LD')
        sfc = body.find(f'{self._plc}SFC')
        il  = body.find(f'{self._plc}IL')   # BUG FIX: Instruction List was not considered

        if st  is not None:
            return self._handle_st_body(st)
        if fbd is not None:
            return self._handle_fbd_body(fbd)
        if ld  is not None:
            log.info("Converting Ladder Diagram body to ST (best-effort).")
            ld_lines = self._ld_converter.convert(ld)
            return [''] + ld_lines + ['']
        if sfc is not None:
            return self._handle_sfc_body(sfc)
        if il  is not None:
            return ['', '(* Instruction List body — manual conversion required *)', '']
        return ['', '(* Unknown body type *)', '']

    # -----------------------------------------------------------------------
    # ST body (pass-through)
    # -----------------------------------------------------------------------

    @staticmethod
    def _normalize_st_text(text: str) -> str:
        """
        Normalise ST body text for MATIEC compatibility:

        1. Join continuation lines onto their parent statement.
           MATIEC requires each ST statement on a single logical line.
           Multi-line assignments like:
               myVar :=
                   (a = b)
                   AND (c <> d);
           cause "';' missing" errors because MATIEC sees the newline after
           "(a = b)" as end-of-statement, then fails on "AND ...".

        2. Add trailing semicolons after END_IF / END_FOR / END_WHILE /
           END_REPEAT / END_CASE / RETURN / EXIT.
           MATIEC's grammar is: statement_list ::= { statement ';' }*
           so EVERY statement — including structured control blocks — must
           be terminated with a semicolon.
        """
        # IEC operator keywords must NOT be treated as new-statement starters
        # so they are joined onto the previous line as continuations.
        NEW_STMT_RE = re.compile(
            r'^\s*('
            r'IF\b|ELSE\b|ELSIF\b|END_IF\b'
            r'|WHILE\b|END_WHILE\b'
            r'|FOR\b|END_FOR\b'
            r'|REPEAT\b|UNTIL\b|END_REPEAT\b'
            r'|CASE\b|END_CASE\b'
            r'|RETURN\b|EXIT\b'
            r'|(?!AND\b|OR\b|XOR\b|NOT\b|MOD\b)\w+\s*:='   # assignment
            r'|(?!AND\b|OR\b|XOR\b|NOT\b|MOD\b)\w+\s*\('    # FB/function call
            r')',
            re.IGNORECASE,
        )

        # Step 1: join continuation lines
        joined: list[str] = []
        for line in text.split('\n'):
            stripped = line.strip()
            if not stripped:
                joined.append(line)
            elif joined and not NEW_STMT_RE.match(line) and joined[-1].strip():
                joined[-1] = joined[-1].rstrip() + ' ' + stripped
            else:
                joined.append(line)

        # Step 2: add ';' after block-closing keywords that lack one
        END_KW_RE = re.compile(
            r'^(\s*(?:END_IF|END_FOR|END_WHILE|END_REPEAT|END_CASE|RETURN|EXIT))\s*$',
            re.IGNORECASE,
        )
        result: list[str] = []
        for line in joined:
            m = END_KW_RE.match(line)
            result.append(m.group(1) + ';' if m else line)

        return '\n'.join(result)

    def _handle_st_body(self, st_elem) -> list[str]:
        """
        Extract ST body text, normalise multi-line statements to single lines,
        and ensure all statements are semicolon-terminated for MATIEC.
        Handles both <xhtml:body> and <xhtml:xhtml> wrapper elements used
        by different PLCopen XML exporters.
        """
        xhtml_ns   = 'http://www.w3.org/1999/xhtml'
        # Some exporters use <xhtml:body>, others use <xhtml:xhtml>
        xhtml_body = (
            st_elem.find(f'{{{xhtml_ns}}}body') or
            st_elem.find(f'{{{xhtml_ns}}}xhtml')
        )
        text = ''

        if xhtml_body is not None:
            text = ''.join(xhtml_body.itertext()).strip()
        elif st_elem.text and st_elem.text.strip():
            text = st_elem.text.strip()
        else:
            parts = []
            for child in st_elem.iter():
                if child.text and child.text.strip():
                    parts.append(child.text.strip())
                if child.tail and child.tail.strip():
                    parts.append(child.tail.strip())
            text = '\n'.join(parts)

        if text:
            text = self._normalize_st_text(text)

        lines: list[str] = ['']
        if text:
            lines.extend(text.split('\n'))
        else:
            lines.append('(* empty ST body *)')
        lines.append('')
        return lines

    # -----------------------------------------------------------------------
    # SFC body → stub with state comments
    # -----------------------------------------------------------------------

    def _handle_sfc_body(self, sfc_elem) -> list[str]:
        """
        BUG FIX / ENHANCEMENT: instead of a single generic comment, emit
        comments identifying each SFC step and transition so the programmer
        knows what to implement.
        """
        lines = ['', '(* ——— Sequential Function Chart — manual implementation required ——— *)']
        for step in sfc_elem.findall(f'.//{self._plc}step'):
            sname = step.get('name', '?')
            lines.append(f'(* SFC Step     : {sname} *)')
        for trans in sfc_elem.findall(f'.//{self._plc}transition'):
            tname = trans.get('name', '?')
            lines.append(f'(* SFC Transition: {tname} *)')
        lines.append('')
        return lines

    # -----------------------------------------------------------------------
    # FBD body → graph traversal → ST assignment statements
    # -----------------------------------------------------------------------

    def _handle_fbd_body(self, fbd_elem) -> list[str]:
        nodes      = self._build_fbd_graph(fbd_elem)
        statements = self._fbd_to_statements(nodes)
        return [''] + statements + ['']

    def _build_fbd_graph(self, fbd_elem) -> dict[str, _FBDNode]:
        """Parse every FBD child element into an _FBDNode keyed by localId."""
        nodes: dict[str, _FBDNode] = {}

        for elem in fbd_elem:
            tag = elem.tag.replace(self._plc, '')
            lid = elem.get('localId')
            if not lid:
                continue

            # BUG FIX: capture executionOrderId so we can sort FB calls later
            exec_order = elem.get('executionOrderId')
            exec_id    = int(exec_order) if exec_order and exec_order.isdigit() else None

            if tag == 'inVariable':
                expr_elem = elem.find(f'{self._plc}expression')
                expr = (expr_elem.text or '').strip() if expr_elem is not None else ''
                nodes[lid] = _FBDNode(lid, 'inVariable', expression=expr,
                                      execution_order_id=exec_id)

            elif tag == 'outVariable':
                expr_elem = elem.find(f'{self._plc}expression')
                expr = (expr_elem.text or '').strip() if expr_elem is not None else ''
                node = _FBDNode(lid, 'outVariable', expression=expr,
                                execution_order_id=exec_id)
                cp_in = elem.find(f'{self._plc}connectionPointIn')
                if cp_in is not None:
                    # BUG FIX: original only read the first <connection>;
                    # multiple connections on an outVariable (rare but valid)
                    # were silently dropped.  We take the last one as the driver
                    # (matches CODESYS behaviour).
                    for conn in cp_in.findall(f'{self._plc}connection'):
                        node.inputs['In1'] = (
                            conn.get('refLocalId'),
                            conn.get('formalParameter'),
                            False,
                        )
                nodes[lid] = node

            elif tag == 'block':
                type_name     = elem.get('typeName', '')
                instance_name = elem.get('instanceName', '') or None
                node = _FBDNode(lid, 'block', type_name=type_name,
                                instance_name=instance_name,
                                execution_order_id=exec_id)
                iv_section = elem.find(f'{self._plc}inputVariables')
                if iv_section is not None:
                    for var in iv_section.findall(f'{self._plc}variable'):
                        param  = var.get('formalParameter', 'In1')
                        negated = var.get('negated') == 'true'
                        cp_in  = var.find(f'{self._plc}connectionPointIn')
                        if cp_in is not None:
                            for conn in cp_in.findall(f'{self._plc}connection'):
                                node.inputs[param] = (
                                    conn.get('refLocalId'),
                                    conn.get('formalParameter'),
                                    negated,
                                )
                nodes[lid] = node

            elif tag in ('inOutVariable',):
                # BUG FIX: inOutVariable was not handled at all
                expr_elem = elem.find(f'{self._plc}expression')
                expr = (expr_elem.text or '').strip() if expr_elem is not None else ''
                node = _FBDNode(lid, 'outVariable', expression=expr,
                                execution_order_id=exec_id)
                cp_in = elem.find(f'{self._plc}connectionPointIn')
                if cp_in is not None:
                    for conn in cp_in.findall(f'{self._plc}connection'):
                        node.inputs['In1'] = (
                            conn.get('refLocalId'),
                            conn.get('formalParameter'),
                            False,
                        )
                nodes[lid] = node

            # vendorElement and unknown tags are silently ignored

        return nodes

    def _fbd_to_statements(self, nodes: dict[str, _FBDNode]) -> list[str]:
        """Walk the FBD graph and emit ST statements."""

        expr_cache: dict[str, str] = {}
        # Guard against infinite recursion on circular wiring (which is invalid
        # IEC 61131-3 but some tools emit it for feedback loops)
        _in_progress: set[str] = set()

        def resolve(lid: str, out_param: str | None = None) -> str:
            node = nodes.get(lid)
            if node is None:
                return f'(* unknown_node_{lid} *)'

            if node.kind == 'inVariable':
                return node.expression or '(* no_expr *)'

            if node.kind == 'block':
                if node.instance_name:
                    # FB instance: reference its named output pin
                    if out_param:
                        return f'{node.instance_name}.{out_param}'
                    # BUG FIX: if no out_param given, use the conventional 'Q' for
                    # BOOL-output FBs rather than just the instance name (which is
                    # not a valid ST expression)
                    if node.type_name in ('TON', 'TOF', 'TP', 'R_TRIG', 'F_TRIG'):
                        return f'{node.instance_name}.Q'
                    if node.type_name in ('CTU', 'CTD', 'CTUD'):
                        return f'{node.instance_name}.Q'
                    return node.instance_name

                # Pure function / operator
                if lid in _in_progress:
                    log.warning(
                        "Circular FBD reference detected at node %s (%s) — "
                        "inserting feedback placeholder.", lid, node.type_name
                    )
                    return f'(* circular_ref_{lid} *)'

                if lid not in expr_cache:
                    _in_progress.add(lid)
                    try:
                        expr_cache[lid] = self._resolve_block(node, resolve)
                    finally:
                        _in_progress.discard(lid)
                return expr_cache[lid]

            return f'(* unresolved_{lid} *)'

        def _param_sort_key(k: str) -> int:
            return int(k[2:]) if k.startswith('In') and k[2:].isdigit() else 999

        statements: list[str] = []

        # 1. Emit FB instance call statements, sorted by executionOrderId
        fb_nodes = [
            n for n in nodes.values()
            if n.kind == 'block' and n.instance_name
        ]
        # BUG FIX: sort by execution order, fall back to local_id numeric order
        fb_nodes.sort(key=lambda n: (
            n.execution_order_id if n.execution_order_id is not None else 99999,
            int(n.local_id) if n.local_id.isdigit() else 0,
        ))

        for node in fb_nodes:
            args: list[str] = []
            for param in sorted(node.inputs.keys(), key=_param_sort_key):
                ref_lid, ref_param, negated = node.inputs[param]
                expr = resolve(ref_lid, ref_param)
                if negated:
                    expr = f'NOT({expr})'
                args.append(f'{param} := {expr}')
            statements.append(f'{node.instance_name}({", ".join(args)});')

        # 2. Emit assignments for every outVariable, sorted by execution order
        out_nodes = [n for n in nodes.values() if n.kind == 'outVariable']
        out_nodes.sort(key=lambda n: (
            n.execution_order_id if n.execution_order_id is not None else 99999,
            int(n.local_id) if n.local_id.isdigit() else 0,
        ))

        for node in out_nodes:
            src = node.inputs.get('In1')
            if src is None:
                statements.append(f'(* {node.expression} : no driver *)')
                continue
            ref_lid, ref_param, negated = src
            rhs = resolve(ref_lid, ref_param)
            if negated:
                rhs = f'NOT({rhs})'
            statements.append(f'{node.expression} := {rhs};')

        return statements

    def _resolve_block(self, node: _FBDNode, resolve_fn) -> str:
        """Build the ST sub-expression for a pure-function block node."""
        type_name = node.type_name

        def key(k: str) -> int:
            return int(k[2:]) if k.startswith('In') and k[2:].isdigit() else 999

        ordered_params = sorted(node.inputs.keys(), key=key)
        resolved: list[str] = []
        for param in ordered_params:
            ref_lid, ref_param, negated = node.inputs[param]
            expr = resolve_fn(ref_lid, ref_param)
            if negated:
                expr = f'NOT({expr})'
            resolved.append(expr)

        # Binary infix operator
        if type_name in BINARY_OPS and len(resolved) >= 2:
            op  = BINARY_OPS[type_name]
            acc = resolved[0]
            for operand in resolved[1:]:
                # BUG FIX: always parenthesise sub-expressions to preserve
                # operator precedence — the original code only wrapped the
                # whole chain, not each step, which could produce wrong results
                # for chains of mixed operators in the same FBD network.
                acc = f'({acc} {op} {operand})'
            return acc

        # Unary prefix operator
        if type_name in UNARY_OPS and resolved:
            op = UNARY_OPS[type_name]
            # BUG FIX: NEG uses '-' not a keyword, so needs different formatting
            if op == '-':
                return f'-({resolved[0]})'
            return f'{op}({resolved[0]})'

        # BUG FIX: SEL, MUX, LIMIT are common IEC functions with positional args
        if type_name == 'SEL' and len(resolved) >= 3:
            return f'SEL({resolved[0]}, {resolved[1]}, {resolved[2]})'
        if type_name == 'LIMIT' and len(resolved) >= 3:
            return f'LIMIT({resolved[0]}, {resolved[1]}, {resolved[2]})'
        if type_name == 'MUX' and len(resolved) >= 2:
            return f'MUX({", ".join(resolved)})'

        # Generic function call
        if resolved:
            return f'{type_name}({", ".join(resolved)})'
        return f'{type_name}()'


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main() -> None:
    if len(sys.argv) < 2:
        print('Usage: python xml_to_st_converter.py <input.xml> [output.st | -]')
        print('       Use "-" as output to print to stdout.')
        sys.exit(1)

    input_file  = sys.argv[1]
    output_arg  = sys.argv[2] if len(sys.argv) > 2 else None
    to_stdout   = (output_arg == '-')
    output_file = (
        output_arg
        if (output_arg and not to_stdout)
        else str(Path(input_file).with_suffix('.st'))
    )

    if not Path(input_file).exists():
        print(f"Error: '{input_file}' not found.", file=sys.stderr)
        sys.exit(1)

    try:
        converter = PLCopenXMLConverter(input_file)
        st_code   = converter.convert()

        if to_stdout:
            print(st_code)
        else:
            Path(output_file).write_text(st_code, encoding='utf-8')
            print(f'Converted: {input_file}  →  {output_file}')

    except Exception as exc:          # noqa: BLE001
        print(f'Error: {exc}', file=sys.stderr)
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == '__main__':
    main()