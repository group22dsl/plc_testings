"""
PLCopen XML to ST Converter — Full Implementation
==================================================
Converts PLCopen XML (IEC 61131-3) files to Structured Text (ST).

Supports body types : FBD (Function Block Diagram), ST (pass-through),
                      LD and SFC (stub output).
Supports POU types  : PROGRAM, FUNCTION_BLOCK, FUNCTION.
Handles FBD         : full graph traversal — resolves operator chains
                      (AND, OR, NOT, GE, LE, GT, LT, EQ, NE, ADD, SUB,
                       MUL, DIV, MOD, XOR) and generic function calls.

Usage:
    python 1_plcopenxml_converter.py input.xml output.st
    python 1_plcopenxml_converter.py input.xml        # print to console
"""

import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

NS = 'http://www.plcopen.org/xml/tc6_0200'
PLC = f'{{{NS}}}'          # e.g. '{http://...}pou'

# All IEC 61131-3 elementary types (order matters: check longer names first)
BASIC_TYPES = [
    'LREAL', 'REAL',
    'LINT', 'DINT', 'INT', 'SINT',
    'ULINT', 'UDINT', 'UINT', 'USINT',
    'LWORD', 'DWORD', 'WORD', 'BYTE', 'BOOL',
    'TIME', 'DATE', 'TOD', 'DT',
    'WSTRING', 'STRING', 'WCHAR', 'CHAR',
]

# FBD block types that map to ST binary infix operators
BINARY_OPS = {
    'GE': '>=', 'LE': '<=', 'GT': '>', 'LT': '<',
    'EQ': '=',  'NE': '<>',
    'AND': 'AND', 'OR': 'OR', 'XOR': 'XOR',
    'ADD': '+',  'SUB': '-', 'MUL': '*', 'DIV': '/', 'MOD': 'MOD',
}

# FBD block types that map to ST unary prefix operators
UNARY_OPS = {'NOT': 'NOT'}


# ---------------------------------------------------------------------------
# FBD node dataclass (plain object)
# ---------------------------------------------------------------------------

class _FBDNode:
    __slots__ = ('local_id', 'kind', 'expression', 'type_name', 'instance_name', 'inputs')

    def __init__(self, local_id, kind, expression=None, type_name=None, instance_name=None):
        self.local_id      = local_id
        self.kind          = kind          # 'inVariable' | 'outVariable' | 'block'
        self.expression    = expression    # inVariable / outVariable text
        self.type_name     = type_name     # block typeName
        self.instance_name = instance_name # FB instance name (None for pure functions)
        # {formalParameter: (refLocalId, refFormalParameter | None, negated: bool)}
        self.inputs: dict = {}


# ---------------------------------------------------------------------------
# Main converter class
# ---------------------------------------------------------------------------

class PLCopenXMLConverter:
    """Convert a PLCopen XML file to IEC 61131-3 Structured Text."""

    def __init__(self, xml_file):
        self.xml_file = xml_file
        tree = ET.parse(xml_file)
        self.root = tree.getroot()

    # -----------------------------------------------------------------------
    # Public entry point
    # -----------------------------------------------------------------------

    def convert(self) -> str:
        lines = [
            '(*',
            f'  Generated from : {Path(self.xml_file).name}',
            '  Converter      : PLCopen XML → ST',
            '*)', '',
        ]

        pous = self.root.findall(f'.//{PLC}pou')
        pou_info = []
        for pou in pous:
            pou_info.append((pou.get('name'), pou.get('pouType', 'program')))
            lines.extend(self._convert_pou(pou))
            lines.append('')

        # OpenPLC-compatible configuration block
        lines += [
            '(* OpenPLC Configuration *)',
            'CONFIGURATION Config0', '',
            '  RESOURCE Res0 ON PLC',
        ]
        for name, ptype in pou_info:
            if ptype.lower() == 'program':
                lines.append('    TASK task0(INTERVAL := T#20ms, PRIORITY := 0);')
                lines.append(f'    PROGRAM instance0 WITH task0 : {name};')
        lines += ['  END_RESOURCE', '', 'END_CONFIGURATION']

        return '\n'.join(lines)

    # -----------------------------------------------------------------------
    # POU
    # -----------------------------------------------------------------------

    # Map PLCopen XML camelCase pouType values → IEC 61131-3 keywords
    _POU_TYPE_MAP = {
        'program':       'PROGRAM',
        'functionblock': 'FUNCTION_BLOCK',
        'function':      'FUNCTION',
    }

    def _convert_pou(self, pou) -> list:
        lines = []
        name  = pou.get('name')
        ptype = self._POU_TYPE_MAP.get(
            pou.get('pouType', 'program').lower(), 'PROGRAM'
        )

        lines.append(f'{ptype} {name}')

        iface = pou.find(f'{PLC}interface')
        iface_lines = self._extract_interface(iface) if iface is not None else []

        body = pou.find(f'{PLC}body')
        body_lines = self._extract_body(body) if body is not None else []

        # Detect undeclared FBD intermediate variables and inject them into the
        # interface section as a plain VAR block so MATIEC/OpenPLC can compile.
        self._inject_missing_fbd_vars(iface_lines, body_lines)

        lines.extend(iface_lines)
        lines.extend(body_lines)

        lines.append(f'END_{ptype}')
        return lines

    @staticmethod
    def _inject_missing_fbd_vars(iface_lines: list, body_lines: list) -> None:
        """Add FBD outVariable targets absent from the interface to iface_lines.

        FBD wires may use undeclared intermediate names (e.g. 'Feedback') as
        outVariable expressions.  MATIEC rejects undeclared LHS identifiers, so
        we detect them here and append a VAR block to the interface section.

        Type is inferred from simple ``Declared := Undeclared;`` assignments in
        the body; otherwise defaults to INT.
        """
        # Build name→type map from interface declaration lines
        type_map: dict = {}
        for line in iface_lines:
            m = re.match(
                r'[ \t]+(\w+)[ \t]*(?:AT[ \t]+%\w+(?:\.\d+)?)?[ \t]*:[ \t]*(\w+)',
                line,
            )
            if m:
                type_map[m.group(1)] = m.group(2).upper()

        # Collect undeclared LHS identifiers from body statements
        undeclared: dict = {}
        for line in body_lines:
            m = re.match(r'[ \t]*(\w+)[ \t]*:=', line)
            if m:
                name = m.group(1)
                if name not in type_map and name not in undeclared:
                    undeclared[name] = None

        if not undeclared:
            return

        # Infer types from simple "DeclaredVar := UndeclaredVar;" statements
        for line in body_lines:
            m = re.match(r'[ \t]*(\w+)[ \t]*:=[ \t]*(\w+)[ \t]*;', line)
            if not m:
                continue
            lhs, rhs = m.group(1), m.group(2)
            if rhs in undeclared and undeclared[rhs] is None and lhs in type_map:
                undeclared[rhs] = type_map[lhs]
            if lhs in undeclared and undeclared[lhs] is None and rhs in type_map:
                undeclared[lhs] = type_map[rhs]

        # Default any still-unknown types to INT
        for name in undeclared:
            if undeclared[name] is None:
                undeclared[name] = 'INT'

        # Append a new VAR block to the interface lines
        iface_lines.append('VAR')
        for name, vtype in undeclared.items():
            iface_lines.append(f'    {name} : {vtype};')
        iface_lines.append('END_VAR')
        iface_lines.append('')

    # -----------------------------------------------------------------------
    # Interface / variable declarations
    # -----------------------------------------------------------------------

    def _extract_interface(self, iface) -> list:
        """Iterate *all* child sections in document order (handles multiple
        inputVars blocks, which CODESYS sometimes emits)."""
        lines = []
        for child in iface:
            tag = child.tag.replace(PLC, '')
            if tag == 'inputVars':
                lines += self._var_block('VAR_INPUT', child)
            elif tag == 'outputVars':
                lines += self._var_block('VAR_OUTPUT', child)
            elif tag == 'inOutVars':
                lines += self._var_block('VAR_IN_OUT', child)
            elif tag == 'localVars':
                is_const = child.get('constant') == 'true'
                kw = 'VAR CONSTANT' if is_const else 'VAR'
                lines += self._var_block(kw, child)
            elif tag == 'externalVars':
                lines += self._var_block('VAR_EXTERNAL', child)
            elif tag == 'tempVars':
                lines += self._var_block('VAR_TEMP', child)
        return lines

    def _var_block(self, keyword: str, section) -> list:
        variables = section.findall(f'{PLC}variable')
        if not variables:
            return []
        lines = [keyword]
        for var in variables:
            vname = var.get('name')
            vtype = self._get_type(var.find(f'{PLC}type'))
            init  = self._get_init(var)
            if init is not None:
                lines.append(f'    {vname} : {vtype} := {init};')
            else:
                lines.append(f'    {vname} : {vtype};')
        lines += ['END_VAR', '']
        return lines

    def _get_type(self, type_elem) -> str:
        if type_elem is None:
            return 'BOOL'
        for t in BASIC_TYPES:
            if type_elem.find(f'{PLC}{t}') is not None:
                return t
        # ARRAY
        arr = type_elem.find(f'{PLC}array')
        if arr is not None:
            base = self._get_type(arr.find(f'{PLC}baseType'))
            dims = [
                f'{d.get("lower")}..{d.get("upper")}'
                for d in arr.findall(f'{PLC}dimension')
            ]
            return f'ARRAY[{", ".join(dims)}] OF {base}'
        # User-defined / derived
        derived = type_elem.find(f'{PLC}derived')
        if derived is not None:
            return derived.get('name', 'UNKNOWN')
        # STRING with explicit length
        s = type_elem.find(f'{PLC}string')
        if s is not None:
            length = s.get('length')
            return f'STRING[{length}]' if length else 'STRING'
        return 'BOOL'

    def _get_init(self, var):
        iv = var.find(f'{PLC}initialValue')
        if iv is None:
            return None
        sv = iv.find(f'{PLC}simpleValue')
        if sv is not None:
            return sv.get('value')
        return None

    # -----------------------------------------------------------------------
    # Body dispatch
    # -----------------------------------------------------------------------

    def _extract_body(self, body) -> list:
        st  = body.find(f'{PLC}ST')
        fbd = body.find(f'{PLC}FBD')
        ld  = body.find(f'{PLC}LD')
        sfc = body.find(f'{PLC}SFC')

        if st  is not None:
            return self._handle_st_body(st)
        if fbd is not None:
            return self._handle_fbd_body(fbd)
        if ld  is not None:
            return ['', '(* Ladder Diagram body — manual review required *)', '']
        if sfc is not None:
            return ['', '(* Sequential Function Chart body — manual review required *)', '']
        return ['', '(* Unknown body type *)', '']

    # -----------------------------------------------------------------------
    # ST body (pass-through)
    # -----------------------------------------------------------------------

    def _handle_st_body(self, st_elem) -> list:
        # The actual code lives inside an xhtml:body child element
        xhtml_ns = 'http://www.w3.org/1999/xhtml'
        xhtml_body = st_elem.find(f'{{{xhtml_ns}}}body')
        text = ''
        if xhtml_body is not None and xhtml_body.text:
            text = xhtml_body.text.strip()
        elif st_elem.text and st_elem.text.strip():
            text = st_elem.text.strip()
        else:
            # Iterate all descendants for text
            for child in st_elem.iter():
                if child.text and child.text.strip():
                    text = child.text.strip()
                    break
        lines = ['']
        lines.extend(text.split('\n') if text else ['(* empty ST body *)'])
        lines.append('')
        return lines

    # -----------------------------------------------------------------------
    # FBD body → graph traversal → ST assignment statements
    # -----------------------------------------------------------------------

    def _handle_fbd_body(self, fbd_elem) -> list:
        nodes      = self._build_fbd_graph(fbd_elem)
        statements = self._fbd_to_statements(nodes)
        return [''] + statements + ['']

    def _build_fbd_graph(self, fbd_elem) -> dict:
        """Parse every FBD child element into an _FBDNode keyed by localId."""
        nodes = {}

        for elem in fbd_elem:
            tag = elem.tag.replace(PLC, '')
            lid = elem.get('localId')
            if not lid:
                continue

            if tag == 'inVariable':
                expr_elem = elem.find(f'{PLC}expression')
                expr = (expr_elem.text or '').strip() if expr_elem is not None else ''
                nodes[lid] = _FBDNode(lid, 'inVariable', expression=expr)

            elif tag == 'outVariable':
                expr_elem = elem.find(f'{PLC}expression')
                expr = (expr_elem.text or '').strip() if expr_elem is not None else ''
                node = _FBDNode(lid, 'outVariable', expression=expr)
                cp_in = elem.find(f'{PLC}connectionPointIn')
                if cp_in is not None:
                    conn = cp_in.find(f'{PLC}connection')
                    if conn is not None:
                        node.inputs['In1'] = (
                            conn.get('refLocalId'),
                            conn.get('formalParameter'),
                            False,
                        )
                nodes[lid] = node

            elif tag == 'block':
                type_name     = elem.get('typeName', '')
                instance_name = elem.get('instanceName', '') or None
                node = _FBDNode(lid, 'block', type_name=type_name, instance_name=instance_name)
                iv_section = elem.find(f'{PLC}inputVariables')
                if iv_section is not None:
                    for var in iv_section.findall(f'{PLC}variable'):
                        param = var.get('formalParameter', 'In1')
                        negated = var.get('negated') == 'true'
                        cp_in = var.find(f'{PLC}connectionPointIn')
                        if cp_in is not None:
                            conn = cp_in.find(f'{PLC}connection')
                            if conn is not None:
                                node.inputs[param] = (
                                    conn.get('refLocalId'),
                                    conn.get('formalParameter'),
                                    negated,
                                )
                nodes[lid] = node
            # vendorElement and unknown tags are ignored

        return nodes

    def _fbd_to_statements(self, nodes: dict) -> list:
        """Walk the graph and emit ST statements for each FB call and outVariable."""

        expr_cache: dict = {}

        def resolve(lid, out_param=None) -> str:
            """Return the ST expression that a node produces on a given output pin."""
            node = nodes.get(lid)
            if node is None:
                return f'(* unknown_node_{lid} *)'

            if node.kind == 'inVariable':
                return node.expression

            if node.kind == 'block':
                # Function-block instance: reference the named output pin
                if node.instance_name:
                    return f'{node.instance_name}.{out_param}' if out_param else node.instance_name
                # Pure function / operator: build inline expression (cached)
                if lid not in expr_cache:
                    expr_cache[lid] = self._resolve_block(node, resolve)
                return expr_cache[lid]

            return f'(* unresolved_{lid} *)'

        def _param_sort_key(k):
            return int(k[2:]) if k.startswith('In') and k[2:].isdigit() else 999

        statements = []

        # 1. Emit a call statement for every FB instance (TON, TOF, CTU …)
        for node in nodes.values():
            if node.kind != 'block' or not node.instance_name:
                continue
            args = []
            for param in sorted(node.inputs.keys(), key=_param_sort_key):
                ref_lid, ref_param, negated = node.inputs[param]
                expr = resolve(ref_lid, ref_param)
                if negated:
                    expr = f'NOT({expr})'
                args.append(f'{param} := {expr}')
            statements.append(f'{node.instance_name}({(", ".join(args))});')

        # 2. Emit one assignment per outVariable
        for node in nodes.values():
            if node.kind != 'outVariable':
                continue
            src = node.inputs.get('In1')
            if src is None:
                statements.append(f'(* {node.expression} : no driver *)')
                continue
            ref_lid, ref_param, _negated = src
            rhs = resolve(ref_lid, ref_param)
            statements.append(f'{node.expression} := {rhs};')

        return statements

    def _resolve_block(self, node: _FBDNode, resolve_fn) -> str:
        """Build the ST sub-expression for a block node."""
        type_name = node.type_name

        # Sort inputs: In1, In2, In3, … then any named params
        def key(k):
            if k.startswith('In') and k[2:].isdigit():
                return int(k[2:])
            return 999

        ordered_params = sorted(node.inputs.keys(), key=key)
        resolved = []
        for param in ordered_params:
            ref_lid, ref_param, negated = node.inputs[param]
            expr = resolve_fn(ref_lid, ref_param)
            if negated:
                expr = f'NOT({expr})'
            resolved.append(expr)

        # Binary infix operator
        if type_name in BINARY_OPS and len(resolved) >= 2:
            op = BINARY_OPS[type_name]
            expr = resolved[0]
            for operand in resolved[1:]:
                expr = f'({expr} {op} {operand})'
            return expr

        # Unary prefix operator
        if type_name in UNARY_OPS and resolved:
            return f'{UNARY_OPS[type_name]}({resolved[0]})'

        # Generic function / function-block call
        if resolved:
            return f'{type_name}({", ".join(resolved)})'
        return f'{type_name}()'


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main():
    if len(sys.argv) < 2:
        print('Usage: python 1_plcopenxml_converter.py <input.xml> [output.st]')
        sys.exit(1)

    input_file  = sys.argv[1]
    output_file = sys.argv[2] if len(sys.argv) > 2 else str(Path(input_file).with_suffix('.st'))

    if not Path(input_file).exists():
        print(f"Error: '{input_file}' not found")
        sys.exit(1)

    try:
        converter = PLCopenXMLConverter(input_file)
        st_code   = converter.convert()

        with open(output_file, 'w', encoding='utf-8') as f:
            f.write(st_code)
        print(f'Converted: {input_file}  →  {output_file}')

    except Exception as exc:
        print(f'Error: {exc}')
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == '__main__':
    main()
