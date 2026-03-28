"""
POU to Structured Text (ST) Converter
======================================
Converts a proprietary .pou file (Safety Designer, Codesys, TwinCAT, OpenPLC,
Siemens, Beckhoff, Schneider and any IEC 61131-3-compliant vendor) to clean
IEC 61131-3 Structured Text.

Vendor annotation handling:
  - Layout/structural annotations ({LINE}, {Group}, {GroupDefinition},
    {VariableWorksheet}, {CodeWorksheet}) are silently removed.
  - Semantic/safety annotations ({Feedback}, {SafetyClass}, etc.) are
    preserved as IEC 61131-3 comments (* ... *) so the metadata is not lost.
Multiple variable-section blocks of the same kind (e.g. several
VAR_INPUT {Group(…)} entries) are merged into one.

Body types handled
------------------
  .fbd  — Function Block Diagram (inline FBD XML) → full ST conversion
  .st   — Structured Text (pass-through with annotation cleanup)
  .il   — Instruction List → best-effort ST conversion
  .ld   — Ladder Diagram (stub comment; manual review required)
  .sfc  — Sequential Function Chart (stub comment; manual review required)
  .cfc  — Continuous Function Chart (treated like FBD where possible)

POU types handled
-----------------
  FUNCTION_BLOCK, PROGRAM, FUNCTION, ACTION, TYPE (struct/enum/alias)

Usage
-----
    python pou_to_st_converter.py  input.pou  [output.st]
    python pou_to_st_converter.py  input.pou          # writes <input>.st
    python pou_to_st_converter.py  --batch  dir/      # converts all *.pou in a folder
    python pou_to_st_converter.py  --encoding utf-16  input.pou  # explicit encoding
"""

from __future__ import annotations

import argparse
import logging
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    format='%(levelname)s: %(message)s',
    level=logging.WARNING,
)
log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Safety-rated operator names → standard IEC 61131-3 equivalents
SAFETY_MAP: Dict[str, str] = {
    'AND_S': 'AND', 'OR_S':  'OR',  'NOT_S': 'NOT', 'XOR_S': 'XOR',
    'ADD_S': 'ADD', 'SUB_S': 'SUB', 'MUL_S': 'MUL', 'DIV_S': 'DIV',
    'GE_S':  'GE',  'LE_S':  'LE',  'GT_S':  'GT',  'LT_S':  'LT',
    'EQ_S':  'EQ',  'NE_S':  'NE',
    # Siemens / Pilz S-variants
    'ANDSAFE': 'AND', 'ORSAFE': 'OR', 'NOTSAFE': 'NOT',
}

# FBD block type → ST infix binary operator
BINARY_OPS: Dict[str, str] = {
    'AND': 'AND', 'OR':  'OR',  'XOR': 'XOR',
    'GE':  '>=',  'LE':  '<=',  'GT':  '>',   'LT':  '<',
    'EQ':  '=',   'NE':  '<>',
    'ADD': '+',   'SUB': '-',   'MUL': '*',   'DIV': '/', 'MOD': 'MOD',
    # Vendor aliases
    'BOOL_AND': 'AND', 'BOOL_OR': 'OR', 'BOOL_XOR': 'XOR',
}

# FBD block type → ST unary prefix operator
UNARY_OPS: Dict[str, str] = {
    'NOT': 'NOT',
    'NEG': '-',   # arithmetic negation
    'ABS': 'ABS',
}

# Safety-rated type names → standard IEC 61131-3 types
SAFETY_TYPES: Dict[str, str] = {
    'SAFEBOOL':  'BOOL',
    'SAFEINT':   'INT',
    'SAFEDINT':  'DINT',
    'SAFEUDINT': 'UDINT',
    'SAFEUINT':  'UINT',
    'SAFEBYTE':  'BYTE',
    'SAFEWORD':  'WORD',
    'SAFEDWORD': 'DWORD',
    'SAFELREAL': 'LREAL',
    'SAFEREAL':  'REAL',
    'SAFELINT':  'LINT',
    'SAFEULINT': 'ULINT',
    'SAFESINT':  'SINT',
    'SAFEUSINT': 'USINT',
    # Safety literals
    'SAFEFALSE': 'FALSE',
    'SAFETRUE':  'TRUE',
    # Pilz/Sick extended safety types
    'SAFE_BOOL': 'BOOL',
    'SAFE_INT':  'INT',
    'SAFE_DINT': 'DINT',
    'SAFE_REAL': 'REAL',
}

# Variable section keywords, longest-first so bare 'VAR' is checked last
_VAR_KWS: List[str] = [
    'VAR_IN_OUT', 'VAR_INPUT', 'VAR_OUTPUT',
    'VAR_EXTERNAL', 'VAR_GLOBAL', 'VAR_ACCESS',
    'VAR_TEMP', 'VAR_STAT', 'VAR',
]

# IL (Instruction List) opcode → ST infix/prefix operator or function
_IL_OPS: Dict[str, str] = {
    'LD':   '__LD__',   # load accumulator  → handled by emitter
    'ST':   '__ST__',   # store accumulator → handled by emitter
    'STN':  '__STN__',  # store negated
    'S':    '__S__',    # set
    'R':    '__R__',    # reset
    'AND':  'AND',   'ANDN': '__ANDN__',
    'OR':   'OR',    'ORN':  '__ORN__',
    'XOR':  'XOR',   'XORN': '__XORN__',
    'NOT':  '__NOT__',
    'ADD':  '+',  'SUB': '-',  'MUL': '*',  'DIV': '/', 'MOD': 'MOD',
    'GT':   '>',  'GE':  '>=', 'EQ':  '=',  'NE': '<>', 'LT': '<', 'LE': '<=',
    'JMP':  '__JMP__',  'JMPC': '__JMPC__', 'JMPN': '__JMPN__',
    'CAL':  '__CAL__',  'CALC': '__CALC__', 'CALN': '__CALN__',
    'RET':  '__RET__',  'RETC': '__RETC__',
}

# PLCopen XML namespace URIs (multiple vendor variants)
_PLCOPEN_NS: Tuple[str, ...] = (
    'http://www.plcopen.org/xml/tc6_0201',
    'http://www.plcopen.org/xml/tc6.xsd',
    'http://www.plcopen.org/xml/tc6_0200',
    '',  # no-namespace fallback
)

# Layout-only annotations — silently dropped
_RE_LAYOUT_ANNOT = re.compile(
    r'\{'
    r'(?:LINE\s*\([^)]*\)'
    r'|Group\s*\([^)]*\)'
    r'|GroupDefinition\s*\([^)]*\)'
    r'|VariableWorksheet\s*:=[^}]*'
    r'|CodeWorksheet\s*:=[^}]*'
    r'|Position\s*\([^)]*\)'        # some vendors emit position hints
    r'|Color\s*\([^)]*\)'           # color hints
    r'|Size\s*\([^)]*\)'            # size hints
    r'|Font\s*\([^)]*\)'            # font annotations
    r'|Zoom\s*\([^)]*\)'            # zoom annotations
    r'|ID\s*\([^)]*\)'              # internal ID hints
    r')'
    r'\}',
    re.IGNORECASE,
)

# CodeWorksheet marker — detects body type in proprietary POU header
_RE_CW = re.compile(
    r'\{\s*CodeWorksheet\s*:=\s*[\'"][^\'"]*[\'"]'
    r'\s*,\s*Type\s*:=\s*[\'"]\.(\w+)[\'"]',
    re.IGNORECASE,
)

# POU declaration line pattern
_RE_POU_DECL = re.compile(
    r'^(FUNCTION_BLOCK|PROGRAM|FUNCTION|ACTION|TYPE)\s+(\w+)'
    r'(?:\s*:\s*([\w_]+))?',
    re.IGNORECASE,
)

# END_VAR line (tolerates trailing whitespace / comments)
_RE_END_VAR = re.compile(r'^END_VAR\b', re.IGNORECASE)

# END_POU line
_RE_END_POU = re.compile(
    r'^END_(FUNCTION_BLOCK|FUNCTION|PROGRAM|ACTION|TYPE)\b',
    re.IGNORECASE,
)

# Detect PLCopen XML root document
_RE_PLCOPEN_ROOT = re.compile(
    r'<project\b[^>]*xmlns',
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Small utilities
# ---------------------------------------------------------------------------

def _strip_annot(text: str) -> str:
    """Remove layout annotations; preserve semantic ones as (* ... *) comments."""
    cleaned = _RE_LAYOUT_ANNOT.sub('', text)
    # Convert remaining {annotation} → IEC 61131-3 comment
    cleaned = re.sub(r'\{([^}]+)\}', r'(* \1 *)', cleaned)
    cleaned = re.sub(r'[ \t]+;', ';', cleaned)    # 'TYPE  ;' → 'TYPE;'
    cleaned = re.sub(r'[ \t]{2,}', ' ', cleaned)  # collapse multiple spaces
    return cleaned.strip()


def _detect_encoding(raw_bytes: bytes) -> str:
    """Best-effort encoding detection from BOM or XML declaration."""
    if raw_bytes[:3] == b'\xef\xbb\xbf':
        return 'utf-8-sig'
    if raw_bytes[:2] in (b'\xff\xfe', b'\xfe\xff'):
        return 'utf-16'
    if raw_bytes[:4] in (b'\xff\xfe\x00\x00', b'\x00\x00\xfe\xff'):
        return 'utf-32'
    # Look for XML declaration
    head = raw_bytes[:200]
    m = re.search(rb'encoding=["\']([^"\']+)["\']', head, re.IGNORECASE)
    if m:
        return m.group(1).decode('ascii', errors='replace')
    return 'utf-8'


def _read_pou_file(filepath: str, forced_encoding: Optional[str] = None) -> str:
    """Read POU file, handling BOM, encoding declarations, and CRLF."""
    raw = Path(filepath).read_bytes()
    enc = forced_encoding or _detect_encoding(raw)
    try:
        text = raw.decode(enc, errors='replace')
    except (LookupError, UnicodeDecodeError):
        log.warning("Encoding '%s' failed for %s; falling back to utf-8", enc, filepath)
        text = raw.decode('utf-8', errors='replace')
    # Normalise line endings
    text = text.replace('\r\n', '\n').replace('\r', '\n')
    # Strip BOM anywhere
    return text.replace('\ufeff', '')


# ---------------------------------------------------------------------------
# FBD graph node
# ---------------------------------------------------------------------------

class _FBDNode:
    __slots__ = ('local_id', 'kind', 'expression', 'type_name',
                 'instance_name', 'inputs', 'output_param')

    def __init__(self, local_id: str, kind: str, *,
                 expression: str = '',
                 type_name: str = '',
                 instance_name: str = '',
                 output_param: str = '') -> None:
        self.local_id      = local_id
        self.kind          = kind        # 'inVariable' | 'outVariable' | 'block' | 'connector' | 'return'
        self.expression    = expression
        self.type_name     = type_name
        self.instance_name = instance_name
        self.output_param  = output_param  # which output pin this node drives (for multi-output FBs)
        # { param_name: (refLocalId, refFormalParam|None, negated:bool) }
        self.inputs: Dict[str, Tuple[str, Optional[str], bool]] = {}


# ---------------------------------------------------------------------------
# Infinite-loop guard for FBD cyclic graph resolution
# ---------------------------------------------------------------------------

_MAX_DEPTH = 128


class _CycleError(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# FBD XML → ST logic converter
# ---------------------------------------------------------------------------

class _FBDConverter:
    """Convert raw inline FBD XML (any PLCopen or proprietary namespace) to ST."""

    def convert(self, fbd_xml: str) -> List[str]:
        xml_clean = self._prepare(fbd_xml)
        if not xml_clean:
            return ['(* FBD: empty body *)']

        # Try namespace-aware parse first, then strip-namespace fallback
        root = self._parse_xml(xml_clean)
        if root is None:
            return ['(* FBD XML parse error: could not parse after all attempts *)']

        nodes = self._parse(root)
        return self._emit(nodes)

    # -- prepare XML text --

    @staticmethod
    def _prepare(text: str) -> str:
        """Strip BOM, encoding declarations, and normalise whitespace."""
        text = text.replace('\ufeff', '')
        # Remove XML declaration (ET cannot handle encoding="utf-16" strings)
        text = re.sub(r'<\?xml[^?]*\?>', '', text)
        # Some vendors wrap FBD in a CDATA section
        cdata = re.search(r'<!\[CDATA\[(.*?)]]>', text, re.DOTALL)
        if cdata:
            text = cdata.group(1)
        return text.strip()

    @staticmethod
    def _strip_ns(xml_text: str) -> str:
        """Strip all XML namespace declarations and prefixes for uniform parsing."""
        # Remove xmlns declarations
        text = re.sub(r'\s+xmlns(?::\w+)?=["\'][^"\']*["\']', '', xml_text)
        # Remove namespace prefixes from tags and attributes (e.g. tc6:block → block)
        text = re.sub(r'<(/?)[\w]+:', r'<\1', text)
        return text

    def _parse_xml(self, xml_text: str) -> Optional[ET.Element]:
        """Try multiple parse strategies; return root element or None."""
        # Strategy 1: plain parse (works for no-namespace FBD)
        for attempt in (xml_text, self._strip_ns(xml_text)):
            try:
                root = ET.fromstring(attempt)
                # Descend to the <FBD> or <body> element if needed
                return self._find_fbd_root(root)
            except ET.ParseError:
                continue
        # Strategy 2: wrap in a synthetic root to handle fragments
        wrapped = f'<_root_>{xml_text}</_root_>'
        try:
            root = ET.fromstring(self._strip_ns(wrapped))
            return self._find_fbd_root(root)
        except ET.ParseError as exc:
            log.error("FBD XML parse failed: %s", exc)
            return None

    @staticmethod
    def _find_fbd_root(root: ET.Element) -> ET.Element:
        """Return the <FBD> (or equivalent body) element regardless of nesting."""
        tag_lower = root.tag.lower().split('}')[-1]  # strip namespace URI part
        if tag_lower in ('fbd', 'cfc'):
            return root
        # Search one level down (body > FBD pattern used by PLCopen XML)
        for child in root.iter():
            t = child.tag.lower().split('}')[-1]
            if t in ('fbd', 'cfc'):
                return child
        return root  # fall back to whatever we got

    # -- parameter normalisation --

    @staticmethod
    def _norm_param(p: str) -> str:
        """Normalise IN1/IN2 (all-caps) → In1/In2 for consistent dict keys."""
        up = p.upper()
        if up.startswith('IN') and up[2:].isdigit():
            return f'In{up[2:]}'
        # Also handle 'INPUT1', 'INPUT2' patterns from some vendors
        if up.startswith('INPUT') and up[5:].isdigit():
            return f'In{up[5:]}'
        return p

    @staticmethod
    def _param_idx(k: str) -> int:
        """Sort key: In1 → 1, In2 → 2, anything else → 999."""
        up = k.upper()
        for prefix in ('IN', 'INPUT'):
            suffix = up[len(prefix):]
            if up.startswith(prefix) and suffix.isdigit():
                return int(suffix)
        return 999

    # -- connection helper --

    def _get_conn(self, elem: ET.Element) -> Optional[ET.Element]:
        """Find first <connection> inside <connectionPointIn>, handling namespaces."""
        for child in elem:
            if child.tag.lower().split('}')[-1] == 'connectionpointin':
                for conn in child:
                    if conn.tag.lower().split('}')[-1] == 'connection':
                        return conn
                return None
        return None

    def _get_all_conns(self, elem: ET.Element) -> List[ET.Element]:
        """Return all <connection> elements inside <connectionPointIn>."""
        conns = []
        for child in elem:
            if child.tag.lower().split('}')[-1] == 'connectionpointin':
                for conn in child:
                    if conn.tag.lower().split('}')[-1] == 'connection':
                        conns.append(conn)
        return conns

    @staticmethod
    def _bare(tag: str) -> str:
        """Return tag name stripped of any namespace prefix/URI."""
        return tag.lower().split('}')[-1]

    # -- parse FBD elements into graph nodes --

    def _parse(self, fbd_root: ET.Element) -> Dict[str, _FBDNode]:
        nodes: Dict[str, _FBDNode] = {}

        for elem in fbd_root:
            tag = self._bare(elem.tag)
            lid = elem.get('localId')
            if not lid:
                continue

            if tag == 'invariable':
                expr = self._get_expr(elem)
                nodes[lid] = _FBDNode(lid, 'inVariable', expression=expr)

            elif tag == 'outvariable':
                expr = self._get_expr(elem)
                node = _FBDNode(lid, 'outVariable', expression=expr)
                conn = self._get_conn(elem)
                if conn is not None:
                    node.inputs['In1'] = (
                        conn.get('refLocalId', ''),
                        conn.get('formalParameter'),
                        False,
                    )
                nodes[lid] = node

            elif tag in ('block', 'functionblock'):
                raw_type  = elem.get('typeName', '') or elem.get('type', '')
                type_name = SAFETY_MAP.get(raw_type, raw_type)
                inst_name = elem.get('instanceName', '') or elem.get('name', '')
                node = _FBDNode(lid, 'block',
                                type_name=type_name, instance_name=inst_name)
                self._parse_block_inputs(elem, node)
                nodes[lid] = node

            elif tag == 'contact':
                # Ladder-in-FBD contact (some vendors embed LD contacts in FBD)
                expr = elem.get('localId', lid)
                negated = elem.get('negated', 'false').lower() == 'true'
                node = _FBDNode(lid, 'inVariable', expression=('NOT_CONTACT' if negated else expr))
                nodes[lid] = node

            elif tag == 'coil':
                # Ladder-in-FBD coil
                expr = self._get_expr(elem) or lid
                node = _FBDNode(lid, 'outVariable', expression=expr)
                conn = self._get_conn(elem)
                if conn is not None:
                    node.inputs['In1'] = (
                        conn.get('refLocalId', ''),
                        conn.get('formalParameter'),
                        False,
                    )
                nodes[lid] = node

            elif tag == 'connector':
                # Named connector (jump-style wiring) — treat as passthrough inVariable
                name = elem.get('name', f'_conn_{lid}')
                nodes[lid] = _FBDNode(lid, 'connector', expression=name)

            elif tag == 'continuation':
                # Named continuation (receives from connector) — passthrough
                name = elem.get('name', f'_cont_{lid}')
                nodes[lid] = _FBDNode(lid, 'inVariable', expression=name)

            # addData / vendorElement / position / label and other layout tags ignored

        return nodes

    def _parse_block_inputs(self, elem: ET.Element, node: _FBDNode) -> None:
        """Extract all input variable connections from a block element."""
        for child in elem:
            if self._bare(child.tag) in ('inputvariables', 'variables'):
                for var in child:
                    if self._bare(var.tag) not in ('variable', 'inputvariable'):
                        continue
                    param   = self._norm_param(var.get('formalParameter', 'In1'))
                    negated = var.get('negated', 'false').lower() == 'true'
                    conns   = self._get_all_conns(var)
                    if conns:
                        # multi-connection (e.g. ADD with 3+ inputs)
                        for idx, conn in enumerate(conns, 1):
                            key = param if idx == 1 else f'{param}_{idx}'
                            node.inputs[key] = (
                                conn.get('refLocalId', ''),
                                conn.get('formalParameter'),
                                negated,
                            )
                break

    @staticmethod
    def _get_expr(elem: ET.Element) -> str:
        """Extract <expression> text from a variable element."""
        for child in elem:
            if child.tag.lower().split('}')[-1] == 'expression':
                return (child.text or '').strip()
        return ''

    # -- emit ST assignment statements --

    def _emit(self, nodes: Dict[str, _FBDNode]) -> List[str]:
        cache: Dict[str, str] = {}
        resolving: Set[str] = set()  # cycle guard

        def _resolve_block(node: _FBDNode, depth: int) -> str:
            if depth > _MAX_DEPTH:
                raise _CycleError(f'Cyclic FBD graph at node {node.local_id}')
            ordered  = sorted(node.inputs.keys(), key=self._param_idx)
            operands = []
            for p in ordered:
                r_lid, r_param, neg = node.inputs[p]
                ex = resolve(r_lid, r_param, depth + 1)
                operands.append(f'NOT({ex})' if neg else ex)

            t = node.type_name
            if t in BINARY_OPS and len(operands) >= 2:
                op     = BINARY_OPS[t]
                result = operands[0]
                for o in operands[1:]:
                    result = f'({result} {op} {o})'
                return result
            if t in UNARY_OPS and operands:
                return f'{UNARY_OPS[t]}({operands[0]})'
            if operands:
                return f'{t}({", ".join(operands)})'
            return f'{t}()'

        def resolve(lid: str, out_param: Optional[str] = None,
                    depth: int = 0) -> str:
            if lid in resolving:
                return f'(* circular_ref_{lid} *)'
            node = nodes.get(lid)
            if node is None:
                return f'(* unknown_node_{lid} *)'
            if node.kind == 'inVariable':
                return node.expression or f'(* empty_inVar_{lid} *)'
            if node.kind == 'connector':
                return node.expression
            if node.kind == 'block':
                if node.instance_name:
                    # Named FB instance: reference its output pin
                    return (f'{node.instance_name}.{out_param}'
                            if out_param else node.instance_name)
                # Inline operator block (AND, OR, ADD …)
                if lid not in cache:
                    resolving.add(lid)
                    try:
                        cache[lid] = _resolve_block(node, depth)
                    except _CycleError as exc:
                        cache[lid] = f'(* {exc} *)'
                    finally:
                        resolving.discard(lid)
                return cache[lid]
            return f'(* unresolved_{lid} *)'

        stmts: List[str] = []
        warnings: List[str] = []

        # 1. Named function-block instance calls (TON, TOF, CTU, user FBs …)
        #    Emit in order of localId (preserves diagram left-to-right roughly)
        for lid in sorted(nodes.keys(), key=lambda x: int(x) if x.isdigit() else 0):
            node = nodes[lid]
            if node.kind == 'block' and node.instance_name:
                args = []
                for p in sorted(node.inputs.keys(), key=self._param_idx):
                    r_lid, r_param, neg = node.inputs[p]
                    if not r_lid:
                        continue
                    ex = resolve(r_lid, r_param)
                    args.append(f'{p} := {"NOT(" + ex + ")" if neg else ex}')
                stmts.append(f'{node.instance_name}({", ".join(args)});')

        # 2. Output variable assignments
        for lid in sorted(nodes.keys(), key=lambda x: int(x) if x.isdigit() else 0):
            node = nodes[lid]
            if node.kind != 'outVariable':
                continue
            src = node.inputs.get('In1')
            if src is None:
                warnings.append(f'(* WARNING: {node.expression} has no driver *)')
                continue
            r_lid, r_param, negated = src
            if not r_lid:
                warnings.append(f'(* WARNING: {node.expression} driver refLocalId is empty *)')
                continue
            rhs = resolve(r_lid, r_param)
            if negated:
                rhs = f'NOT({rhs})'
            stmts.append(f'{node.expression} := {rhs};')

        return warnings + stmts if warnings else stmts


# ---------------------------------------------------------------------------
# IL (Instruction List) → ST converter  (best-effort)
# ---------------------------------------------------------------------------

class _ILConverter:
    """Best-effort Instruction List → Structured Text converter.

    Handles the most common IL patterns:
      LD / ST / AND / OR / NOT / ADD / SUB / MUL / DIV / comparison operators.
    Branches (JMP/CAL) are emitted as comments for manual review.
    """

    _RE_IL_LINE = re.compile(
        r'^\s*(?:(\w+)\s*:\s*)?'   # optional label
        r'(\w+)\s*'                 # opcode
        r'(?:([^\s(][^;]*))?',      # optional operand
        re.IGNORECASE,
    )

    def convert(self, il_text: str) -> List[str]:
        stmts: List[str] = [
            '(* IL→ST auto-conversion; review carefully *)',
        ]
        accumulator = 'TRUE'  # symbolic accumulator
        for raw_line in il_text.splitlines():
            line = raw_line.strip()
            if not line or line.startswith('(*'):
                if line:
                    stmts.append(line)
                continue
            m = self._RE_IL_LINE.match(line)
            if not m:
                stmts.append(f'(* IL: {line} *)')
                continue
            label   = m.group(1) or ''
            opcode  = (m.group(2) or '').upper()
            operand = (m.group(3) or '').strip().rstrip(';').strip()

            prefix = f'{label}: ' if label else ''

            if opcode == 'LD':
                accumulator = operand
            elif opcode == 'LDN':
                accumulator = f'NOT({operand})'
            elif opcode == 'ST':
                stmts.append(f'{prefix}{operand} := {accumulator};')
            elif opcode == 'STN':
                stmts.append(f'{prefix}{operand} := NOT({accumulator});')
            elif opcode == 'S':
                stmts.append(f'{prefix}IF {accumulator} THEN {operand} := TRUE; END_IF;')
            elif opcode == 'R':
                stmts.append(f'{prefix}IF {accumulator} THEN {operand} := FALSE; END_IF;')
            elif opcode in ('AND', 'OR', 'XOR'):
                op = BINARY_OPS[opcode]
                accumulator = f'({accumulator} {op} {operand})'
            elif opcode in ('ANDN', 'ORN', 'XORN'):
                base = opcode[:-1]
                op = BINARY_OPS[base]
                accumulator = f'({accumulator} {op} NOT({operand}))'
            elif opcode == 'NOT':
                accumulator = f'NOT({accumulator})'
            elif opcode == 'ADD':
                accumulator = f'({accumulator} + {operand})'
            elif opcode == 'SUB':
                accumulator = f'({accumulator} - {operand})'
            elif opcode == 'MUL':
                accumulator = f'({accumulator} * {operand})'
            elif opcode == 'DIV':
                accumulator = f'({accumulator} / {operand})'
            elif opcode == 'MOD':
                accumulator = f'({accumulator} MOD {operand})'
            elif opcode in ('GT', 'GE', 'EQ', 'NE', 'LT', 'LE'):
                op = BINARY_OPS[opcode]
                accumulator = f'({accumulator} {op} {operand})'
            elif opcode in ('JMP', 'JMPC', 'JMPN', 'CAL', 'CALC', 'CALN', 'RET', 'RETC'):
                stmts.append(f'(* IL control flow: {line} — manual review required *)')
            else:
                # Unknown opcode — emit as comment
                stmts.append(f'(* IL: {line} *)')

        return stmts


# ---------------------------------------------------------------------------
# PLCopen XML project file parser  (wraps one or many POUs)
# ---------------------------------------------------------------------------

class _PLCopenParser:
    """Extract individual POU ST/FBD bodies from a PLCopen XML project file.

    Returns a list of (pou_name, pou_type, body_type, body_text, var_sections).
    """

    def parse(self, xml_text: str) -> List[dict]:
        xml_clean = re.sub(r'<\?xml[^?]*\?>', '', xml_text).strip()
        ns_stripped = re.sub(r'\s+xmlns(?::\w+)?=["\'][^"\']*["\']', '', xml_clean)
        ns_stripped = re.sub(r'<(/?)[\w]+:', r'<\1', ns_stripped)
        try:
            root = ET.fromstring(ns_stripped)
        except ET.ParseError as exc:
            log.error("PLCopen XML parse error: %s", exc)
            return []
        pous = []
        for pou in root.iter('pou'):
            entry = self._parse_pou(pou)
            if entry:
                pous.append(entry)
        return pous

    def _parse_pou(self, pou: ET.Element) -> Optional[dict]:
        name = pou.get('name', 'UNKNOWN')
        pou_type = pou.get('pouType', 'FUNCTION_BLOCK').upper()

        var_sections: Dict[str, List[str]] = {kw: [] for kw in _VAR_KWS}
        # Parse variable declarations from <interface>
        iface = pou.find('interface')
        if iface is not None:
            for vsec in iface:
                kw = vsec.tag.upper()
                if kw not in var_sections:
                    kw = 'VAR'
                for var in vsec.findall('variable'):
                    vname = var.get('name', '')
                    vtype_elem = var.find('type')
                    vtype = self._get_type_text(vtype_elem) if vtype_elem is not None else 'INT'
                    init_elem = var.find('initialValue')
                    init = f' := {init_elem.text}' if init_elem is not None and init_elem.text else ''
                    var_sections[kw].append(f'{vname} : {vtype}{init};')

        body_type = 'unknown'
        body_text = ''
        body_elem = pou.find('body')
        if body_elem is not None:
            for child in body_elem:
                tag = child.tag.lower()
                if tag == 'st':
                    body_type = 'st'
                    xhtml = child.find('xhtml')
                    body_text = (xhtml.text or '') if xhtml is not None else (child.text or '')
                elif tag == 'fbd':
                    body_type = 'fbd'
                    body_text = ET.tostring(child, encoding='unicode')
                elif tag == 'il':
                    body_type = 'il'
                    xhtml = child.find('xhtml')
                    body_text = (xhtml.text or '') if xhtml is not None else (child.text or '')
                elif tag == 'ld':
                    body_type = 'ld'
                elif tag == 'sfc':
                    body_type = 'sfc'

        return {
            'name': name,
            'pou_type': pou_type,
            'body_type': body_type,
            'body_text': body_text,
            'var_sections': var_sections,
            'return_type': pou.get('returnType', ''),
        }

    @staticmethod
    def _get_type_text(type_elem: ET.Element) -> str:
        """Extract type string from PLCopen <type> element."""
        for child in type_elem:
            tag = child.tag.lower()
            if tag in ('derived', 'userdefined'):
                return child.get('name', 'INT')
            if tag == 'array':
                # e.g. <array><baseType><INT/></baseType><dimension lower="0" upper="9"/></array>
                bt = child.find('baseType')
                dim = child.find('dimension')
                base = list(bt)[0].tag.upper() if bt is not None and len(bt) else 'INT'
                if dim is not None:
                    lo = dim.get('lower', '0')
                    hi = dim.get('upper', '0')
                    return f'ARRAY[{lo}..{hi}] OF {base}'
                return f'ARRAY[*] OF {base}'
            # Simple types: <INT/>, <BOOL/>, etc.
            return child.tag.upper()
        return type_elem.text.strip() if type_elem.text else 'INT'


# ---------------------------------------------------------------------------
# Safety type normalisation
# ---------------------------------------------------------------------------

_RE_SAFETY = re.compile(
    r'\b(' + '|'.join(re.escape(k) for k in sorted(SAFETY_TYPES, key=len, reverse=True)) + r')\b',
    re.IGNORECASE,
)


def _normalise_safety_types(text: str) -> str:
    """Replace vendor safety types with standard IEC 61131-3 equivalents.

    Word-boundary anchored so e.g. SAFEBOOL inside 'IsSafeBool_ST' is NOT replaced.
    Case-insensitive so SAFEBOOL / SafeBool / safebool are all caught.
    """
    def _sub(m: re.Match) -> str:
        return SAFETY_TYPES.get(m.group(1).upper(), m.group(1))
    return _RE_SAFETY.sub(_sub, text)


# ---------------------------------------------------------------------------
# POU file → ST converter  (proprietary text-based .pou format)
# ---------------------------------------------------------------------------

class POUConverter:
    """Parse a proprietary text .pou file or a PLCopen XML project and emit
    clean IEC 61131-3 Structured Text.

    Supports:
      - Safety Designer / Pilz PNOZmulti proprietary .pou format
      - Codesys / TwinCAT / OpenPLC PLCopen XML (.xml / .pou with XML content)
      - Bare ST files passed through
    """

    def __init__(self, filepath: str,
                 forced_encoding: Optional[str] = None) -> None:
        self.filepath = filepath
        self._content = _read_pou_file(filepath, forced_encoding)

    # -----------------------------------------------------------------------
    # Public API
    # -----------------------------------------------------------------------

    def convert(self) -> str:
        content = self._content.strip()

        # Detect PLCopen XML project file and handle accordingly
        if _RE_PLCOPEN_ROOT.search(content[:500]):
            return self._convert_plcopen_xml(content)

        # Detect bare XML that could be a single PLCopen POU
        if content.startswith('<') and '<pou' in content.lower():
            result = self._convert_plcopen_xml(content)
            if '(* ERROR' not in result:
                return result

        lines = content.splitlines()

        pou_decl, end_kw = self._find_pou_declaration(lines)
        if not pou_decl:
            # May be a bare ST file — try pass-through
            if any(re.match(r'^\s*\w+\s*:=', l) for l in lines):
                log.warning("No POU declaration found; treating as bare ST body.")
                cleaned = '\n'.join(
                    c for l in lines if (c := _strip_annot(l.strip()))
                )
                return _normalise_safety_types(cleaned)
            return '(* ERROR: No POU declaration (FUNCTION_BLOCK/PROGRAM/FUNCTION/ACTION/TYPE) found *)'

        var_sections, body_type, body_text = self._parse_sections(lines)

        body_stmts = self._convert_body(body_type, body_text)
        if body_type == 'fbd':
            self._add_missing_fbd_vars(var_sections, body_stmts)

        out: List[str] = [
            f'(* Generated from : {Path(self.filepath).name} *)',
            '(* Converter      : POU -> ST                  *)',
            '',
            pou_decl,
        ]

        # Variable declarations — emit only non-empty sections, in standard order
        for kw in _VAR_KWS:
            decls = var_sections.get(kw, [])
            if decls:
                out.append(kw)
                for d in decls:
                    out.append(f'    {d}')
                out += ['END_VAR', '']

        # Body
        out.extend(body_stmts)

        out.append('')
        out.append(end_kw)
        return _normalise_safety_types('\n'.join(out))

    # -----------------------------------------------------------------------
    # PLCopen XML project → ST
    # -----------------------------------------------------------------------

    def _convert_plcopen_xml(self, xml_text: str) -> str:
        parser = _PLCopenParser()
        pous = parser.parse(xml_text)
        if not pous:
            return '(* ERROR: No POUs found in PLCopen XML *)'

        blocks: List[str] = [
            f'(* Generated from : {Path(self.filepath).name} *)',
            '(* Converter      : PLCopen XML -> ST           *)',
        ]
        for entry in pous:
            blocks.append('')
            blocks.append(self._render_plcopen_pou(entry))

        return _normalise_safety_types('\n'.join(blocks))

    def _render_plcopen_pou(self, entry: dict) -> str:
        name       = entry['name']
        pou_type   = entry['pou_type']
        ret_type   = entry.get('return_type', '')
        body_type  = entry['body_type']
        body_text  = entry['body_text']
        var_sections = entry['var_sections']

        decl    = f'{pou_type} {name}'
        if ret_type:
            decl += f' : {ret_type}'
        end_kw  = f'END_{pou_type}'

        body_stmts = self._convert_body(body_type, body_text)
        if body_type == 'fbd':
            self._add_missing_fbd_vars(var_sections, body_stmts)

        out: List[str] = [decl]
        for kw in _VAR_KWS:
            decls = var_sections.get(kw, [])
            if decls:
                out.append(kw)
                for d in decls:
                    out.append(f'    {d}')
                out += ['END_VAR', '']
        out.extend(body_stmts)
        out.append('')
        out.append(end_kw)
        return '\n'.join(out)

    # -----------------------------------------------------------------------
    # Helper: declare undeclared FBD intermediate variables
    # -----------------------------------------------------------------------

    @staticmethod
    def _add_missing_fbd_vars(var_sections: Dict[str, List[str]],
                              stmts: List[str]) -> None:
        """Inject VAR declarations for FBD outVariable targets with no declaration.

        Type inference priority:
          1. RHS type from a declared variable in a simple A := B assignment
          2. LHS type from a declared variable in a simple A := B assignment
          3. Default: BOOL (most FBD wires are boolean)
        """
        type_map: Dict[str, str] = {}
        for decls in var_sections.values():
            for d in decls:
                d_clean = re.sub(r'\(\*.*?\*\)', '', d, flags=re.DOTALL).strip()
                m = re.match(
                    r'(\w+)\s*(?:AT\s+%\w+(?:\.\d+)?)?\s*:\s*([\w_]+)',
                    d_clean, re.IGNORECASE,
                )
                if m:
                    type_map[m.group(1)] = m.group(2).upper()

        undeclared: Dict[str, Optional[str]] = {}
        for stmt in stmts:
            m = re.match(r'^(\w+)\s*:=', stmt.strip())
            if m:
                name = m.group(1)
                if name not in type_map and name not in undeclared:
                    undeclared[name] = None

        if not undeclared:
            return

        # Infer from simple assignment statements
        for stmt in stmts:
            m = re.match(r'^(\w+)\s*:=\s*(\w+)\s*;$', stmt.strip())
            if not m:
                continue
            lhs, rhs = m.group(1), m.group(2)
            if rhs in undeclared and undeclared[rhs] is None and lhs in type_map:
                undeclared[rhs] = type_map[lhs]
            if lhs in undeclared and undeclared[lhs] is None and rhs in type_map:
                undeclared[lhs] = type_map[rhs]

        # Default to BOOL (most common for FBD wires)
        for name in undeclared:
            if undeclared[name] is None:
                undeclared[name] = 'BOOL'
                log.warning(
                    "Undeclared FBD variable '%s' defaulted to BOOL. "
                    "Verify the correct type.", name
                )

        var_sections.setdefault('VAR', [])
        for name, vtype in sorted(undeclared.items()):
            var_sections['VAR'].append(f'{name} : {vtype};')

    # -----------------------------------------------------------------------
    # Step 1: locate POU declaration line
    # -----------------------------------------------------------------------

    @staticmethod
    def _find_pou_declaration(lines: List[str]) -> Tuple[str, str]:
        for line in lines:
            clean = _strip_annot(line)
            m = _RE_POU_DECL.match(clean)
            if m:
                ptype    = m.group(1).upper()
                pname    = m.group(2)
                ret_type = m.group(3)
                decl     = f'{ptype} {pname}'
                if ret_type:
                    decl += f' : {ret_type}'
                return decl, f'END_{ptype}'
        return '', ''

    # -----------------------------------------------------------------------
    # Step 2: parse variable sections and body
    # -----------------------------------------------------------------------

    def _parse_sections(self, lines: List[str]) -> Tuple[Dict[str, List[str]], str, str]:
        """Walk POU lines → (var_sections, body_type, body_text)."""
        var_sections: Dict[str, List[str]] = {kw: [] for kw in _VAR_KWS}
        body_type   = 'unknown'
        body_lines: List[str] = []

        state  = 'scanning'   # 'scanning' | 'in_var' | 'in_body'
        cur_kw: Optional[str] = None

        for line in lines:
            stripped = line.strip()

            # ---- CodeWorksheet marker → body starts on the NEXT line ----
            cw = _RE_CW.search(stripped)
            if cw and state != 'in_body':
                body_type = cw.group(1).lower()
                state     = 'in_body'
                continue

            # ---- collecting body lines ----
            if state == 'in_body':
                if _RE_END_POU.match(stripped):
                    break
                body_lines.append(line)
                continue

            # ---- inside a variable section ----
            if state == 'in_var':
                if _RE_END_VAR.match(stripped):
                    state  = 'scanning'
                    cur_kw = None
                else:
                    clean = _strip_annot(stripped)
                    if clean:
                        # Handle multi-line declarations gracefully
                        var_sections[cur_kw].append(clean)
                continue

            # ---- scanning: look for a variable section header ----
            clean = _strip_annot(stripped)

            # Check for inline body indicators (bare ST after the POU header)
            if body_type == 'unknown' and re.match(
                r'^(IF|WHILE|FOR|REPEAT|CASE|;|\w+\s*:=)',
                clean, re.IGNORECASE
            ):
                # Looks like a bare ST body with no CodeWorksheet marker
                if state == 'scanning' and not any(var_sections.values()):
                    body_type = 'st'
                    state = 'in_body'
                    body_lines.append(line)
                    continue

            for kw in _VAR_KWS:
                if re.match(rf'^{re.escape(kw)}\b', clean, re.IGNORECASE):
                    cur_kw = kw
                    state  = 'in_var'
                    break

        # If we never found a CodeWorksheet marker but have body lines, guess ST
        if body_type == 'unknown' and body_lines:
            body_type = 'st'

        return var_sections, body_type, '\n'.join(body_lines)

    # -----------------------------------------------------------------------
    # Step 3: convert body to ST
    # -----------------------------------------------------------------------

    def _convert_body(self, body_type: str, body_text: str) -> List[str]:
        if body_type == 'fbd':
            stmts = _FBDConverter().convert(body_text)
            return stmts if stmts else ['(* FBD: no statements generated *)']

        if body_type in ('st', 'unknown'):
            lines = []
            for line in body_text.splitlines():
                clean = _strip_annot(line)
                if clean:
                    lines.append(clean)
            return lines if lines else ['(* ST body: empty *)']

        if body_type == 'il':
            return _ILConverter().convert(body_text)

        if body_type in ('cfc',):
            # CFC is structurally similar to FBD; attempt FBD conversion
            stmts = _FBDConverter().convert(body_text)
            header = ['(* CFC body — treated as FBD; verify output carefully *)']
            return header + (stmts if stmts else ['(* CFC: no statements generated *)'])

        if body_type == 'ld':
            return [
                '(* ============================================================= *)',
                '(* Ladder Diagram body — automatic ST conversion not supported.  *)',
                '(* Review and convert manually from the original .pou file.      *)',
                '(* ============================================================= *)',
            ]

        if body_type == 'sfc':
            return [
                '(* ============================================================= *)',
                '(* Sequential Function Chart — automatic ST conversion not       *)',
                '(* supported. Review and convert manually.                        *)',
                '(* ============================================================= *)',
            ]

        return [f"(* Body type '{body_type}': no automatic conversion available — manual review required *)"]


# ---------------------------------------------------------------------------
# Batch conversion helper
# ---------------------------------------------------------------------------

def convert_directory(directory: str,
                      forced_encoding: Optional[str] = None,
                      recurse: bool = False) -> None:
    """Convert all .pou files in a directory (optionally recursive)."""
    base = Path(directory)
    pattern = '**/*.pou' if recurse else '*.pou'
    files = list(base.glob(pattern))
    if not files:
        print(f'No .pou files found in {directory}')
        return

    ok = 0
    fail = 0
    for pou_path in sorted(files):
        out_path = pou_path.with_suffix('.st')
        try:
            st_code = POUConverter(str(pou_path), forced_encoding).convert()
            out_path.write_text(st_code, encoding='utf-8')
            print(f'  OK  {pou_path.name} → {out_path.name}')
            ok += 1
        except Exception as exc:
            print(f'  ERR {pou_path.name}: {exc}', file=sys.stderr)
            fail += 1

    print(f'\nDone: {ok} converted, {fail} failed.')


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description='Convert POU files to IEC 61131-3 Structured Text.',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python pou_to_st_converter.py MyBlock.pou
  python pou_to_st_converter.py MyBlock.pou converted.st
  python pou_to_st_converter.py --batch project_dir/
  python pou_to_st_converter.py --batch project_dir/ --recurse
  python pou_to_st_converter.py --encoding utf-16 MyBlock.pou
  python pou_to_st_converter.py --verbose MyBlock.pou
""",
    )
    p.add_argument('input', nargs='?', help='Input .pou file (or directory with --batch)')
    p.add_argument('output', nargs='?', help='Output .st file (optional)')
    p.add_argument('--batch', action='store_true',
                   help='Convert all .pou files in the given directory')
    p.add_argument('--recurse', action='store_true',
                   help='Recurse into subdirectories (used with --batch)')
    p.add_argument('--encoding', default=None,
                   help='Force input file encoding (e.g. utf-8, utf-16, latin-1)')
    p.add_argument('--verbose', action='store_true',
                   help='Show debug-level log messages')
    return p


def main() -> None:
    parser = _build_parser()
    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    if not args.input:
        parser.print_help()
        sys.exit(1)

    if args.batch:
        convert_directory(args.input, args.encoding, args.recurse)
        return

    in_path = args.input
    if not Path(in_path).exists():
        print(f"Error: '{in_path}' not found", file=sys.stderr)
        sys.exit(1)

    out_path = args.output or str(Path(in_path).with_suffix('.st'))

    try:
        st_code = POUConverter(in_path, args.encoding).convert()
        Path(out_path).write_text(st_code, encoding='utf-8')
        print(f'Converted : {in_path}')
        print(f'Output    : {out_path}')
    except Exception as exc:
        print(f'Error: {exc}', file=sys.stderr)
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == '__main__':
    main()
