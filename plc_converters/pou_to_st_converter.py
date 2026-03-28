"""
POU to Structured Text (ST) Converter
======================================
Converts a proprietary .pou file (Safety Designer and similar tools) to
clean IEC 61131-3 Structured Text.

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
  .st   — Structured Text (pass-through)
  .ld   — Ladder Diagram (stub comment; manual review required)
  .sfc  — Sequential Function Chart (stub comment; manual review required)

Usage
-----
    python pou_to_st_converter.py  input.pou  [output.st]
    python pou_to_st_converter.py  input.pou          # writes <input>.st
"""

from __future__ import annotations

import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Safety-rated operator names → standard IEC 61131-3 equivalents
SAFETY_MAP: dict = {
    'AND_S': 'AND', 'OR_S':  'OR',  'NOT_S': 'NOT', 'XOR_S': 'XOR',
    'ADD_S': 'ADD', 'SUB_S': 'SUB', 'MUL_S': 'MUL', 'DIV_S': 'DIV',
    'GE_S':  'GE',  'LE_S':  'LE',  'GT_S':  'GT',  'LT_S':  'LT',
    'EQ_S':  'EQ',  'NE_S':  'NE',
}

# FBD block type → ST infix binary operator
BINARY_OPS: dict = {
    'AND': 'AND', 'OR':  'OR',  'XOR': 'XOR',
    'GE':  '>=',  'LE':  '<=',  'GT':  '>',   'LT':  '<',
    'EQ':  '=',   'NE':  '<>',
    'ADD': '+',   'SUB': '-',   'MUL': '*',   'DIV': '/', 'MOD': 'MOD',
}

# FBD block type → ST unary prefix operator
UNARY_OPS: dict = {'NOT': 'NOT'}

# Safety-rated type names → standard IEC 61131-3 types (OpenPLC only accepts these)
SAFETY_TYPES: dict = {
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
}

# Variable section keywords, longest-first so bare 'VAR' is checked last
_VAR_KWS: list = [
    'VAR_IN_OUT', 'VAR_INPUT', 'VAR_OUTPUT',
    'VAR_EXTERNAL', 'VAR_TEMP', 'VAR',
]

# Layout-only annotations — silently dropped (carry no semantic information).
# Matches: {LINE(n)}, {Group(n)}, {GroupDefinition(n,'label')},
#           {VariableWorksheet := '...'}, {CodeWorksheet := '...', Type := '...'}
_RE_LAYOUT_ANNOT = re.compile(
    r'\{'
    r'(?:LINE\s*\([^)]*\)'
    r'|Group\s*\([^)]*\)'
    r'|GroupDefinition\s*\([^)]*\)'
    r'|VariableWorksheet\s*:=[^}]*'
    r'|CodeWorksheet\s*:=[^}]*'
    r')'
    r'\}',
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Small utilities
# ---------------------------------------------------------------------------

def _strip_annot(text: str) -> str:
    """Remove layout annotations; preserve semantic ones as (* ... *) comments."""
    # 1. Drop pure layout/structural annotations silently
    cleaned = _RE_LAYOUT_ANNOT.sub('', text)
    # 2. Convert remaining {annotation} (e.g. {Feedback(true)}) to ST comments
    cleaned = re.sub(r'\{([^}]+)\}', r'(* \1 *)', cleaned)
    cleaned = re.sub(r'[ \t]+;', ';', cleaned)   # 'TYPE  ;' → 'TYPE;'
    cleaned = re.sub(r'[ \t]{2,}', ' ', cleaned)  # collapse multiple spaces
    return cleaned.strip()


# ---------------------------------------------------------------------------
# FBD graph node
# ---------------------------------------------------------------------------

class _FBDNode:
    __slots__ = ('local_id', 'kind', 'expression', 'type_name',
                 'instance_name', 'inputs')

    def __init__(self, local_id: str, kind: str, *,
                 expression: str = '',
                 type_name: str = '',
                 instance_name: str = '') -> None:
        self.local_id      = local_id
        self.kind          = kind   # 'inVariable' | 'outVariable' | 'block'
        self.expression    = expression
        self.type_name     = type_name
        self.instance_name = instance_name
        # { param_name: (refLocalId, refFormalParam|None, negated:bool) }
        self.inputs: dict = {}


# ---------------------------------------------------------------------------
# FBD XML → ST logic converter
# ---------------------------------------------------------------------------

class _FBDConverter:
    """Convert raw inline FBD XML (no PLCopen namespace) to ST statements."""

    def convert(self, fbd_xml: str) -> list:
        xml_clean = self._prepare(fbd_xml)
        try:
            root = ET.fromstring(xml_clean)
        except ET.ParseError as exc:
            return [f'(* FBD XML parse error: {exc} *)']
        nodes = self._parse(root)
        return self._emit(nodes)

    # -- prepare XML text --

    @staticmethod
    def _prepare(text: str) -> str:
        """Strip BOM and XML declaration (ET cannot parse encoding="utf-16" strings)."""
        text = text.replace('\ufeff', '')
        text = re.sub(r'<\?xml[^?]*\?>', '', text)
        return text.strip()

    # -- parameter normalisation --

    @staticmethod
    def _norm_param(p: str) -> str:
        """Normalise IN1/IN2 (all-caps) → In1/In2 for consistent dict keys."""
        up = p.upper()
        if up.startswith('IN') and up[2:].isdigit():
            return f'In{up[2:]}'
        return p

    @staticmethod
    def _param_idx(k: str) -> int:
        """Sort key: In1 → 1, In2 → 2, anything else → 999."""
        up = k.upper()
        if up.startswith('IN') and up[2:].isdigit():
            return int(up[2:])
        return 999

    # -- connection helper --

    def _get_conn(self, elem) -> 'ET.Element | None':
        cp = elem.find('connectionPointIn')
        return cp.find('connection') if cp is not None else None

    # -- parse FBD elements into graph nodes --

    def _parse(self, fbd_root) -> dict:
        nodes: dict = {}

        for elem in fbd_root:           # direct children of <FBD> only
            tag = elem.tag.lower()
            lid = elem.get('localId')
            if not lid:
                continue

            if tag == 'invariable':
                ex_elem = elem.find('expression')
                expr = (ex_elem.text or '').strip() if ex_elem is not None else ''
                nodes[lid] = _FBDNode(lid, 'inVariable', expression=expr)

            elif tag == 'outvariable':
                ex_elem = elem.find('expression')
                expr = (ex_elem.text or '').strip() if ex_elem is not None else ''
                node = _FBDNode(lid, 'outVariable', expression=expr)
                conn = self._get_conn(elem)
                if conn is not None:
                    node.inputs['In1'] = (
                        conn.get('refLocalId'),
                        conn.get('formalParameter'),
                        False,
                    )
                nodes[lid] = node

            elif tag == 'block':
                raw_type  = elem.get('typeName', '')
                type_name = SAFETY_MAP.get(raw_type, raw_type)
                inst_name = elem.get('instanceName') or ''
                node = _FBDNode(lid, 'block',
                                type_name=type_name, instance_name=inst_name)
                iv_sec = elem.find('inputVariables')
                if iv_sec is not None:
                    for var in iv_sec.findall('variable'):
                        param   = self._norm_param(var.get('formalParameter', 'In1'))
                        negated = var.get('negated', 'false').lower() == 'true'
                        conn    = self._get_conn(var)
                        if conn is not None:
                            node.inputs[param] = (
                                conn.get('refLocalId'),
                                conn.get('formalParameter'),
                                negated,
                            )
                nodes[lid] = node
            # addData / vendorElement and other unknown tags are intentionally ignored

        return nodes

    # -- emit ST assignment statements --

    def _emit(self, nodes: dict) -> list:
        cache: dict = {}

        def _resolve_block(node: _FBDNode) -> str:
            ordered  = sorted(node.inputs.keys(), key=self._param_idx)
            operands = []
            for p in ordered:
                r_lid, r_param, neg = node.inputs[p]
                ex = resolve(r_lid, r_param)
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

        def resolve(lid: str, out_param=None) -> str:
            node = nodes.get(lid)
            if node is None:
                return f'(* unknown_node_{lid} *)'
            if node.kind == 'inVariable':
                return node.expression
            if node.kind == 'block':
                if node.instance_name:
                    return (f'{node.instance_name}.{out_param}'
                            if out_param else node.instance_name)
                if lid not in cache:
                    cache[lid] = _resolve_block(node)
                return cache[lid]
            return f'(* unresolved_{lid} *)'

        stmts: list = []

        # 1. Function-block instance calls (TON, TOF, CTU, user FBs …)
        for node in nodes.values():
            if node.kind == 'block' and node.instance_name:
                args = []
                for p in sorted(node.inputs.keys(), key=self._param_idx):
                    r_lid, r_param, neg = node.inputs[p]
                    ex = resolve(r_lid, r_param)
                    args.append(f'{p} := {"NOT(" + ex + ")" if neg else ex}')
                stmts.append(f'{node.instance_name}({", ".join(args)});')

        # 2. Output variable assignments
        for node in nodes.values():
            if node.kind != 'outVariable':
                continue
            src = node.inputs.get('In1')
            if src is None:
                stmts.append(f'(* {node.expression} has no driver *)')
                continue
            r_lid, r_param, _ = src
            stmts.append(f'{node.expression} := {resolve(r_lid, r_param)};')

        return stmts


# ---------------------------------------------------------------------------
# Safety type normalisation
# ---------------------------------------------------------------------------

# Build a single regex that matches any safety type/literal as a whole word.
_RE_SAFETY = re.compile(
    r'\b(' + '|'.join(re.escape(k) for k in SAFETY_TYPES) + r')\b'
)


def _normalise_safety_types(text: str) -> str:
    """Replace vendor safety types with standard IEC 61131-3 equivalents.

    Substitution is word-boundary-anchored so e.g. SAFEBOOL inside a
    variable *name* such as 'IsSafeBool_ST' is NOT replaced.
    """
    return _RE_SAFETY.sub(lambda m: SAFETY_TYPES[m.group(1)], text)


# ---------------------------------------------------------------------------
# POU file → ST converter
# ---------------------------------------------------------------------------

class POUConverter:
    """Parse a .pou file and emit clean IEC 61131-3 Structured Text."""

    def __init__(self, filepath: str) -> None:
        self.filepath = filepath
        raw = Path(filepath).read_text(encoding='utf-8', errors='replace')
        self._content = raw.replace('\ufeff', '')   # strip BOM anywhere in file

    # -----------------------------------------------------------------------
    # Public API
    # -----------------------------------------------------------------------

    def convert(self) -> str:
        lines = self._content.splitlines()

        pou_decl, end_kw = self._find_pou_declaration(lines)
        if not pou_decl:
            return '(* ERROR: No POU declaration (FUNCTION_BLOCK/PROGRAM/FUNCTION) found *)'

        var_sections, body_type, body_text = self._parse_sections(lines)

        # Convert body first so we can detect undeclared FBD intermediate variables
        # before emitting the variable-section header.
        body_stmts = self._convert_body(body_type, body_text)
        if body_type == 'fbd':
            self._add_missing_fbd_vars(var_sections, body_stmts)

        out: list = [
            f'(* Generated from : {Path(self.filepath).name} *)',
            '(* Converter      : POU -> ST *)',
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
    # Helper: declare undeclared FBD intermediate variables
    # -----------------------------------------------------------------------

    @staticmethod
    def _add_missing_fbd_vars(var_sections: dict, stmts: list) -> None:
        """Add FBD outVariable targets that have no VAR declaration to the VAR block.

        Intermediate FBD variables (e.g. a computed result wire named 'Feedback')
        appear as outVariable expressions in the FBD but may have no corresponding
        VAR declaration.  MATIEC / OpenPLC rejects undeclared LHS identifiers, so
        we detect them here and inject a declaration.

        Type inference: if another statement assigns the undeclared var to a
        declared variable (e.g. ``B := Feedback;`` where B is SINT), we use that
        type.  Otherwise we default to INT.
        """
        # Build name→type map from all declared variables across all VAR sections
        type_map: dict = {}
        for decls in var_sections.values():
            for d in decls:
                d_clean = re.sub(r'\(\*.*?\*\)', '', d, flags=re.DOTALL).strip()
                m = re.match(
                    r'(\w+)\s*(?:AT\s+%\w+(?:\.\d+)?)?\s*:\s*(\w+)',
                    d_clean, re.IGNORECASE,
                )
                if m:
                    type_map[m.group(1)] = m.group(2).upper()

        # Collect LHS identifiers from generated statements that are not declared
        undeclared: dict = {}   # name → inferred type (None until resolved)
        for stmt in stmts:
            m = re.match(r'^(\w+)\s*:=', stmt.strip())
            if m:
                name = m.group(1)
                if name not in type_map and name not in undeclared:
                    undeclared[name] = None

        if not undeclared:
            return

        # Infer types from simple "DeclaredVar := UndeclaredVar;" statements
        for stmt in stmts:
            m = re.match(r'^(\w+)\s*:=\s*(\w+)\s*;$', stmt.strip())
            if not m:
                continue
            lhs, rhs = m.group(1), m.group(2)
            if rhs in undeclared and undeclared[rhs] is None and lhs in type_map:
                undeclared[rhs] = type_map[lhs]
            if lhs in undeclared and undeclared[lhs] is None and rhs in type_map:
                undeclared[lhs] = type_map[rhs]

        # Fall back to INT for any still-unknown types
        for name in undeclared:
            if undeclared[name] is None:
                undeclared[name] = 'INT'

        # Inject into the plain VAR section
        for name, vtype in undeclared.items():
            var_sections['VAR'].append(f'{name} : {vtype};')

    # -----------------------------------------------------------------------
    # Step 1: locate POU declaration line
    # -----------------------------------------------------------------------

    @staticmethod
    def _find_pou_declaration(lines: list) -> tuple:
        for line in lines:
            clean = _strip_annot(line)
            # FUNCTION_BLOCK must be listed before FUNCTION in the alternation
            m = re.match(
                r'^(FUNCTION_BLOCK|PROGRAM|FUNCTION)\s+(\w+)'
                r'(?:\s*:\s*([\w_]+))?',
                clean, re.IGNORECASE,
            )
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

    _RE_CW = re.compile(
        r'\{\s*CodeWorksheet\s*:=\s*[\'"][^\'"]*[\'"]'
        r'\s*,\s*Type\s*:=\s*[\'"]\.(\w+)[\'"]',
        re.IGNORECASE,
    )

    def _parse_sections(self, lines: list) -> tuple:
        """
        Walk the POU lines and return:
            var_sections  — {keyword: [declaration_string, …]}
            body_type     — 'fbd' | 'ld' | 'sfc' | 'st' | 'unknown'
            body_text     — raw body content as a single string
        """
        var_sections: dict = {kw: [] for kw in _VAR_KWS}
        body_type  = 'unknown'
        body_lines: list = []

        state  = 'scanning'   # 'scanning' | 'in_var' | 'in_body'
        cur_kw = None

        for line in lines:
            stripped = line.strip()

            # ---- CodeWorksheet marker → body starts on the NEXT line ----
            cw = self._RE_CW.search(stripped)
            if cw and state != 'in_body':
                body_type = cw.group(1).lower()   # 'fbd', 'ld', 'sfc', 'st'
                state     = 'in_body'
                continue

            # ---- collecting body lines ----
            if state == 'in_body':
                if re.match(
                    r'^END_(FUNCTION_BLOCK|FUNCTION|PROGRAM)\s*$',
                    stripped, re.IGNORECASE
                ):
                    break
                body_lines.append(line)
                continue

            # ---- inside a variable section ----
            if state == 'in_var':
                if re.match(r'^END_VAR\s*$', stripped, re.IGNORECASE):
                    state  = 'scanning'
                    cur_kw = None
                else:
                    clean = _strip_annot(stripped)
                    if clean:
                        var_sections[cur_kw].append(clean)
                continue

            # ---- scanning: look for a variable section header ----
            clean = _strip_annot(stripped)
            for kw in _VAR_KWS:
                if re.match(rf'^{re.escape(kw)}\b', clean, re.IGNORECASE):
                    cur_kw = kw
                    state  = 'in_var'
                    break

        return var_sections, body_type, '\n'.join(body_lines)

    # -----------------------------------------------------------------------
    # Step 3: convert body to ST
    # -----------------------------------------------------------------------

    def _convert_body(self, body_type: str, body_text: str) -> list:
        if body_type == 'fbd':
            stmts = _FBDConverter().convert(body_text)
            return stmts if stmts else ['(* FBD: no statements generated *)']

        if body_type == 'st':
            lines = []
            for line in body_text.splitlines():
                clean = _strip_annot(line)
                if clean:
                    lines.append(clean)
            return lines if lines else ['(* ST body: empty *)']

        if body_type == 'ld':
            return [
                '(* Ladder Diagram body — automatic ST conversion not supported. *)',
                '(* Review and convert manually from the original .pou file.      *)',
            ]

        if body_type == 'sfc':
            return [
                '(* Sequential Function Chart body — automatic ST conversion not supported. *)',
                '(* Review and convert manually from the original .pou file.                *)',
            ]

        return [f"(* Body type '{body_type}': no automatic conversion available *)"]


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main() -> None:
    if len(sys.argv) < 2:
        print('Usage: python pou_to_st_converter.py input.pou [output.st]')
        print()
        print('Examples:')
        print('  python pou_to_st_converter.py MyBlock.pou')
        print('  python pou_to_st_converter.py MyBlock.pou converted.st')
        sys.exit(1)

    in_path  = sys.argv[1]
    out_path = (sys.argv[2] if len(sys.argv) > 2
                else str(Path(in_path).with_suffix('.st')))

    if not Path(in_path).exists():
        print(f"Error: '{in_path}' not found", file=sys.stderr)
        sys.exit(1)

    try:
        st_code = POUConverter(in_path).convert()

        with open(out_path, 'w', encoding='utf-8') as f:
            f.write(st_code)

        print(f'Converted : {in_path}')
        print(f'Output    : {out_path}')

    except Exception as exc:
        print(f'Error: {exc}', file=sys.stderr)
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == '__main__':
    main()
