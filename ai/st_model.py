#!/usr/bin/env python3
"""
Shared IEC 61131-3 Structured Text parsing/evaluation model.
=============================================================
Extracted from ai_test_augmentation.py so that the coverage analyzer and
mutation tester (test_generators/) can reuse the exact same local ST
evaluator used to validate/correct AI-generated oracle values, without
requiring the `openai` package to be installed.

Contains:
  - STParser    : parses PROGRAM I/O, VAR CONSTANT values, TON/TOF/TP
                  instances and top-level statements from ST source.
  - STEvaluator : a small recursive-descent expression evaluator that can
                  compute expected output values (and, for the coverage/
                  mutation tools, individual sub-expression truth values)
                  from a set of input values and an elapsed delay.
"""

import csv
import re
from typing import Dict, List

DEFAULT_PLC_SCAN_MS = 25
INT_MIN, INT_MAX = -32768, 32767


def clamp_int(v):
    return max(INT_MIN, min(INT_MAX, int(v)))


def bool_int(v):
    if isinstance(v, str):
        return 1 if v.strip().upper() in ('1', 'TRUE') else 0
    return 1 if v else 0


# ── ST Parser ─────────────────────────────────────────────────────────────────

class STParser:
    FB_TYPES   = {'R_TRIG','F_TRIG','TON','TOF','TP','CTU','CTD','CTUD','SR','RS','SEMA'}
    SKIP_TYPES = FB_TYPES | {'TIME','DATE','DT','TOD'}

    def __init__(self, st_code):
        self.st_code = st_code
        self.program_name = ''
        self.inputs: List[dict] = []
        self.outputs: List[dict] = []
        self.constants: Dict = {}
        self.has_edge_triggers = False
        self.statements: List[str] = []
        self.timer_instances: Dict[str, dict] = {}

    @staticmethod
    def _extract_block(text, keyword):
        m = re.search(rf'{keyword}\b(.*?)END_VAR', text, re.DOTALL|re.IGNORECASE)
        return m.group(1) if m else None

    @staticmethod
    def _parse_declarations(block):
        result = []
        for decl in block.split(';'):
            decl = re.sub(r'\(\*.*?\*\)', '', decl.strip(), flags=re.DOTALL).strip()
            if not decl: continue
            m = re.match(r'(\w+)\s*(?:AT\s+(%[^\s:]+))?\s*:\s*(\w+)', decl, re.IGNORECASE)
            if m:
                result.append((m.group(1), m.group(3).upper(), m.group(2)))
        return result

    def _extract_constants(self):
        block = self._extract_block(self.st_code, r'VAR\s+CONSTANT')
        if not block: return
        for decl in block.split(';'):
            m = re.match(r'\s*(\w+)\s*:\s*\w+\s*:=\s*([^\s;]+)', decl)
            if not m: continue
            raw = m.group(2)
            t = re.match(r'(?:T|TIME)#(\d+(?:\.\d+)?)(ms|s|m|h)', raw, re.IGNORECASE)
            if t:
                val = float(t.group(1))
                ms = val * {'ms':1,'s':1000,'m':60000,'h':3600000}[t.group(2).lower()]
                self.constants[m.group(1)] = ms
            else:
                try: self.constants[m.group(1)] = float(raw)
                except: self.constants[m.group(1)] = raw

    def _resolve_pt(self, pt_sym):
        if pt_sym in self.constants: return float(self.constants[pt_sym])
        t = re.match(r'(?:T|TIME)#(\d+(?:\.\d+)?)(ms|s|m|h)', pt_sym, re.IGNORECASE)
        if t:
            return float(t.group(1)) * {'ms':1,'s':1000,'m':60000,'h':3600000}[t.group(2).lower()]
        return 0.0

    def _extract_timer_instances(self):
        pattern = re.compile(r'(\w+)\s*\(\s*IN\s*:=\s*([^,]+),\s*PT\s*:=\s*([^)]+)\)', re.IGNORECASE)
        vb = self._extract_block(self.st_code, r'(?<!_)(?<!\w)VAR(?!\s+CONSTANT)(?!_)')
        for m in pattern.finditer(self.st_code):
            inst, in_expr, pt_raw = m.group(1).strip(), m.group(2).strip(), m.group(3).strip()
            timer_type = 'TON'
            if vb:
                tm = re.search(rf'\b{re.escape(inst)}\s*:\s*(TON|TOF|TP)\b', vb, re.IGNORECASE)
                if tm: timer_type = tm.group(1).upper()
            self.timer_instances[inst] = {'pt_ms': self._resolve_pt(pt_raw), 'in_expr': in_expr, 'type': timer_type}

    def _extract_statements(self):
        m_end = re.search(r'END_PROGRAM', self.st_code, re.IGNORECASE)
        if not m_end: return
        head = self.st_code[:m_end.start()]
        last_var = None
        for mv in re.finditer(r'END_VAR', head, re.IGNORECASE):
            last_var = mv
        if last_var is None: return
        body = re.sub(r'\(\*.*?\*\)', '', head[last_var.end():], flags=re.DOTALL)
        for stmt in body.split(';'):
            s = stmt.strip()
            if s: self.statements.append(s)

    def parse(self):
        nc = re.sub(r'\(\*.*?\*\)', '', self.st_code, flags=re.DOTALL)
        m = re.search(r'^\s*PROGRAM\s+(\w+)', nc, re.IGNORECASE|re.MULTILINE)
        self.program_name = m.group(1) if m else 'Unknown'
        self._extract_constants()
        self._extract_timer_instances()
        self._extract_statements()
        self.has_edge_triggers = bool(re.search(r'\bR_TRIG\b|\bF_TRIG\b', self.st_code, re.IGNORECASE))
        plain_block = self._extract_block(self.st_code, r'(?<!_)(?<!\w)VAR(?!\s+CONSTANT)(?!_)')
        if plain_block:
            for name, vtype, at_addr in self._parse_declarations(plain_block):
                if vtype in self.SKIP_TYPES or not at_addr: continue
                if self._addr_is_output(at_addr):
                    self.outputs.append({'name': name, 'type': vtype, 'address': at_addr})
                else:
                    self.inputs.append({'name': name, 'type': vtype, 'address': at_addr})

    @staticmethod
    def _addr_is_output(at_addr):
        a = at_addr.upper().strip()
        if a.startswith('%I'): return False
        m = re.match(r'%Q[DW](\d+)$', a)
        if m: return int(m.group(1)) < 200
        m = re.match(r'%QX(\d+)\.(\d+)$', a)
        if m: return (int(m.group(1)) * 8 + int(m.group(2))) < 1600
        return False

    @property
    def has_explicit_io(self):
        return bool(self.inputs or self.outputs)


# ── Evaluator ─────────────────────────────────────────────────────────────────

class STEvaluator:
    def __init__(self, parser):
        self.parser = parser
        self._output_exprs: Dict[str,str] = {}
        self._ton_outputs:  Dict[str,str] = {}
        self._parse_output_exprs()

    def _parse_output_exprs(self):
        for stmt in self.parser.statements:
            m = re.match(r'(\w+)\s*:=\s*(.+)$', stmt, re.IGNORECASE|re.DOTALL)
            if not m: continue
            lhs, rhs = m.group(1).strip(), m.group(2).strip()
            mq = re.match(r'(\w+)\.Q$', rhs, re.IGNORECASE)
            if mq: self._ton_outputs[lhs] = mq.group(1)
            else:  self._output_exprs[lhs] = rhs

    def evaluate_outputs(self, inputs, delay_ms, prev_ton_q=None):
        env = {inp['name']: inputs.get(inp['name'], 0) for inp in self.parser.inputs}
        env.update(self.parser.constants)
        if prev_ton_q is None: prev_ton_q = {}
        results = {}
        for out in self.parser.outputs:
            name = out['name']
            if name in self._ton_outputs:
                inst_name = self._ton_outputs[name]
                inst = self.parser.timer_instances.get(inst_name)
                if inst is None: results[name] = None; continue
                in_val = self._eval_expr(inst['in_expr'], env)
                if in_val is None: results[name] = None; continue
                in_active, pt_ms = bool(in_val), inst['pt_ms']
                ttype, prev_q = inst.get('type','TON'), bool(prev_ton_q.get(inst_name,0))
                if ttype == 'TON':
                    results[name] = 1 if (in_active and (prev_q or delay_ms >= pt_ms)) else 0
                elif ttype == 'TOF':
                    results[name] = 1 if in_active else (0 if delay_ms >= pt_ms else 1)
                else:
                    results[name] = 1 if (in_active and delay_ms >= pt_ms) else 0
            elif name in self._output_exprs:
                val = self._eval_expr(self._output_exprs[name], env)
                if val is None: results[name] = None
                elif out['type'] == 'BOOL': results[name] = bool_int(val)
                else: results[name] = clamp_int(val)
            else:
                results[name] = None
        return results

    def timer_in_value(self, inst_name, inputs):
        inst = self.parser.timer_instances.get(inst_name)
        if inst is None: return None
        env = {inp['name']: inputs.get(inp['name'],0) for inp in self.parser.inputs}
        env.update(self.parser.constants)
        val = self._eval_expr(inst['in_expr'], env)
        return bool(val) if val is not None else None

    def _eval_expr(self, expr, env):
        try: return self._parse_or(expr.strip(), env)
        except: return None

    def _parse_or(self, expr, env):
        parts = self._split_kw(expr, 'OR')
        if len(parts) > 1: return int(any(bool(self._parse_xor(p, env)) for p in parts))
        return self._parse_xor(expr, env)

    def _parse_xor(self, expr, env):
        parts = self._split_kw(expr, 'XOR')
        if len(parts) > 1:
            r = bool(self._parse_and(parts[0], env))
            for p in parts[1:]: r ^= bool(self._parse_and(p, env))
            return int(r)
        return self._parse_and(expr, env)

    def _parse_and(self, expr, env):
        parts = self._split_kw(expr, 'AND')
        if len(parts) > 1: return int(all(bool(self._parse_not(p, env)) for p in parts))
        return self._parse_not(expr, env)

    def _parse_not(self, expr, env):
        expr = expr.strip()
        if re.match(r'^NOT\s*\(', expr, re.IGNORECASE):
            return int(not bool(self._parse_or(self._paren_inner(expr[3:].strip()), env)))
        if re.match(r'^NOT\s+\w', expr, re.IGNORECASE):
            return int(not bool(self._parse_atom(expr[3:].strip(), env)))
        return self._parse_cmp(expr, env)

    def _parse_cmp(self, expr, env):
        for op in ('<>','<=','>=','<','>','='):
            idx = self._find_op(expr, op)
            if idx is not None:
                lhs = self._parse_add(expr[:idx].strip(), env)
                rhs = self._parse_add(expr[idx+len(op):].strip(), env)
                return {'<>':int(lhs!=rhs),'<=':int(lhs<=rhs),'>=':int(lhs>=rhs),
                        '<':int(lhs<rhs),'>':int(lhs>rhs),'=':int(lhs==rhs)}[op]
        return self._parse_add(expr, env)

    def _parse_add(self, expr, env):
        tokens = self._tokenize_add(expr)
        if len(tokens) == 1: return self._parse_mul(tokens[0][1], env)
        result = 0
        for sign, tok in tokens:
            v = self._parse_mul(tok, env)
            result = result + v if sign == '+' else result - v
        return result

    def _tokenize_add(self, expr):
        tokens=[]; depth=0; current=''; sign='+'
        for c in expr:
            if c=='(': depth+=1; current+=c
            elif c==')': depth-=1; current+=c
            elif depth==0 and c in '+-':
                if current.strip(): tokens.append((sign,current.strip())); current=''; sign=c
                else: current+=c
            else: current+=c
        if current.strip(): tokens.append((sign,current.strip()))
        return tokens or [('+',expr)]

    def _parse_mul(self, expr, env):
        parts = re.split(r'(?<!\*)\*(?!\*)|\/\/', expr)
        if len(parts)==1: return self._parse_atom(expr.strip(), env)
        ops = re.findall(r'[*/]', expr)
        result = self._parse_atom(parts[0].strip(), env)
        for op,p in zip(ops,parts[1:]):
            v = self._parse_atom(p.strip(), env)
            result = result*v if op=='*' else (result/v if v else 0)
        return result

    def _parse_atom(self, expr, env):
        expr = expr.strip()
        if not expr: raise ValueError('empty')
        if expr.startswith('(') and expr.endswith(')'): return self._parse_or(expr[1:-1].strip(), env)
        fm = re.match(r'^(\w+)\s*\((.+)\)$', expr, re.DOTALL)
        if fm:
            fn = fm.group(1).upper()
            evaled = [self._parse_or(a.strip(), env) for a in self._split_args(fm.group(2))]
            if fn=='SEL': return evaled[2] if bool(evaled[0]) else evaled[1]
            if fn=='MUX': k=int(evaled[0]); return evaled[k+1] if 1+k<len(evaled) else 0
            if fn=='LIMIT': return max(evaled[0], min(evaled[2], evaled[1]))
            if fn=='MAX': return max(evaled)
            if fn=='MIN': return min(evaled)
            if fn=='ABS': return abs(evaled[0])
        try: return int(expr)
        except: pass
        try: return float(expr)
        except: pass
        if expr.upper()=='TRUE': return 1
        if expr.upper()=='FALSE': return 0
        for k,v in env.items():
            if k.upper()==expr.upper(): return v
        raise ValueError(f'Unknown symbol: {expr!r}')

    @staticmethod
    def _split_kw(expr, kw):
        parts=[]; depth=0; cur=''; i=0
        while i<len(expr):
            if expr[i]=='(': depth+=1; cur+=expr[i]; i+=1
            elif expr[i]==')': depth-=1; cur+=expr[i]; i+=1
            elif depth==0:
                left_ok = (i==0) or not (expr[i-1].isalnum() or expr[i-1]=='_')
                m = re.match(rf'{kw}\b', expr[i:], re.IGNORECASE) if left_ok else None
                if m: parts.append(cur.strip()); cur=''; i+=len(m.group(0))
                else: cur+=expr[i]; i+=1
            else: cur+=expr[i]; i+=1
        parts.append(cur.strip())
        return [p for p in parts if p] if len(parts)>1 else [expr]


    @staticmethod
    def _paren_inner(expr):
        if not expr.startswith('('): return expr
        depth=0
        for i,c in enumerate(expr):
            if c=='(': depth+=1
            elif c==')':
                depth-=1
                if depth==0: return expr[1:i]
        return expr[1:-1]

    @staticmethod
    def _find_op(expr, op):
        depth=0; i=0
        while i<len(expr):
            if expr[i]=='(': depth+=1
            elif expr[i]==')': depth-=1
            elif depth==0 and expr[i:i+len(op)]==op:
                after=expr[i+len(op):]
                before=expr[i-1] if i>0 else ''
                if op=='<' and (after.startswith('=') or after.startswith('>')): i+=1; continue
                if op=='>' and after.startswith('='): i+=1; continue
                if op=='=' and before in ('<','>',':'): i+=1; continue
                return i
            i+=1
        return None


# ── Shared test-case post-processing (timers/resets, CSV I/O) ────────────────
#
# Extracted from ai_test_augmentation.py so that BOTH experimental conditions
# -- LLM-generated tests (ai/ai_test_augmentation.py) and the random-input
# baseline (test_generators/random_test_augmentation.py) -- run through the
# exact same timer/reset handling and CSV formatting. Only the input-selection
# strategy should differ between conditions; everything downstream of "pick
# the inputs" must be identical or the comparison is confounded.

def make_timer_reset_row(inst_name, parser, plc_scan_ms):
    pt_ms = parser.timer_instances[inst_name]['pt_ms']
    return {
        'test_id': '__reset__',
        'delay_ms': max(plc_scan_ms, int(pt_ms) + plc_scan_ms),
        'description': f'[AUTO] Reset {inst_name} — drive TON_IN=FALSE so ET resets to 0',
        'inputs': {}, 'expected_outputs': {},
        '_is_reset_row': True, '_reset_timer': inst_name,
    }


def renumber_test_cases(test_cases):
    for i, tc in enumerate(test_cases, start=1):
        tc['test_id'] = i


def enforce_timing_and_insert_resets(test_cases, parser, evaluator, plc_scan_ms):
    for tc in test_cases:
        if int(tc.get('delay_ms', 100)) < plc_scan_ms: tc['delay_ms'] = plc_scan_ms
    if not parser.timer_instances:
        renumber_test_cases(test_cases); return test_cases
    result = []; ton_q_state = {inst: 0 for inst in parser.timer_instances}
    for tc in test_cases:
        inputs_vals = {k: int(v) for k, v in tc.get('inputs', {}).items()}
        delay_ms = int(tc.get('delay_ms', 100))
        for inst_name, inst in parser.timer_instances.items():
            if inst.get('type', 'TON') != 'TON': continue
            ton_in = evaluator.timer_in_value(inst_name, inputs_vals)
            if ton_in and ton_q_state[inst_name]:
                result.append(make_timer_reset_row(inst_name, parser, plc_scan_ms))
                ton_q_state[inst_name] = 0
        result.append(tc)
        for inst_name, inst in parser.timer_instances.items():
            if inst.get('type', 'TON') != 'TON': continue
            ton_in = evaluator.timer_in_value(inst_name, inputs_vals)
            if ton_in is None: continue
            if not ton_in: ton_q_state[inst_name] = 0
            elif delay_ms >= inst['pt_ms']: ton_q_state[inst_name] = 1
    renumber_test_cases(result); return result


def evaluate_expected_outputs(test_cases, parser, evaluator):
    """Assign expected_outputs to every test case purely from the local ST
    evaluator (ground truth), carrying TON Q state forward between rows in
    the same way validate_and_correct() does. Unlike validate_and_correct(),
    this does NOT compare against (or log corrections to) any externally
    proposed oracle -- there is no LLM value to validate here. This is the
    expected-output source for the random-baseline condition: only the
    *inputs* are randomly generated, the oracle always comes from this
    evaluator."""
    ton_q_state = {inst: 0 for inst in parser.timer_instances}
    for tc in test_cases:
        inputs_vals = {k: int(v) for k, v in tc.get('inputs', {}).items()}
        delay_ms = int(tc.get('delay_ms', 100))
        local_out = evaluator.evaluate_outputs(inputs_vals, delay_ms, prev_ton_q=dict(ton_q_state))
        tc['expected_outputs'] = {name: val for name, val in local_out.items() if val is not None}
        for inst_name, inst in parser.timer_instances.items():
            if inst.get('type', 'TON') != 'TON': continue
            ton_in = evaluator.timer_in_value(inst_name, inputs_vals)
            if ton_in is None: continue
            if not ton_in: ton_q_state[inst_name] = 0
            elif delay_ms >= inst['pt_ms']: ton_q_state[inst_name] = 1
    return test_cases


def save_test_csv(path, inputs, outputs, test_cases, include_flag=True, source_tag_col=None):
    headers = ['Test_ID', 'Delay_ms', 'Description']
    for v in inputs:  headers.append(f"Input_{v['name']} ({v['address']})")
    for v in outputs: headers.append(f"Expected_{v['name']} ({v['address']})")
    if include_flag: headers.append('AutoInserted')
    if source_tag_col: headers.append(source_tag_col)
    with open(path, 'w', newline='', encoding='utf-8') as f:
        w = csv.writer(f)
        w.writerow(headers)
        for tc in test_cases:
            row = [tc.get('test_id', ''), tc.get('delay_ms', 100), tc.get('description', '')]
            for v in inputs:  row.append(tc.get('inputs', {}).get(v['name'], 0))
            for v in outputs: row.append(tc.get('expected_outputs', {}).get(v['name'], 0))
            if include_flag: row.append(1 if tc.get('_is_reset_row') else 0)
            if source_tag_col: row.append(tc.get('_source', ''))
            w.writerow(row)


def load_formatted_test_csv(csv_path, parser):
    """Load a fully-formatted test CSV (as produced by save_test_csv(), e.g.
    the *_human_formatted.csv files) back into the internal test-case dict
    schema, preserving expected outputs, description, the AutoInserted flag
    and the Source tag. Used to reuse an existing human baseline UNCHANGED
    when building an augmented suite (AI or random), so that all conditions
    share exactly the same human rows."""
    input_names = {v['name'] for v in parser.inputs}
    output_names = {v['name'] for v in parser.outputs}
    cases = []
    with open(csv_path, newline='', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        fieldnames = reader.fieldnames or []
        input_cols, output_cols = {}, {}
        for col in fieldnames:
            m = re.match(r'Input_(\w+)', col)
            if m and m.group(1) in input_names: input_cols[col] = m.group(1)
            m = re.match(r'Expected_(\w+)', col)
            if m and m.group(1) in output_names: output_cols[col] = m.group(1)
        delay_col = next((c for c in fieldnames if 'delay' in c.lower()), None)
        desc_col = next((c for c in fieldnames if 'description' in c.lower()), None)
        auto_col = next((c for c in fieldnames if 'autoinsert' in c.lower()), None)
        source_col = next((c for c in fieldnames if c.lower() == 'source'), None)
        for row in reader:
            inputs, outputs = {}, {}
            for col, name in input_cols.items():
                try: inputs[name] = int(float(str(row.get(col, '0')).strip()))
                except ValueError: inputs[name] = 0
            for col, name in output_cols.items():
                try: outputs[name] = int(float(str(row.get(col, '0')).strip()))
                except ValueError: outputs[name] = 0
            try:
                delay_ms = int(float(row[delay_col])) if delay_col and str(row.get(delay_col, '')).strip() else 100
            except ValueError:
                delay_ms = 100
            is_reset = bool(auto_col) and str(row.get(auto_col, '0')).strip() == '1'
            cases.append({
                'test_id': row.get('Test_ID', ''),
                'delay_ms': delay_ms,
                'description': row.get(desc_col, '') if desc_col else '',
                'inputs': inputs,
                'expected_outputs': outputs,
                '_is_reset_row': is_reset,
                '_reset_timer': None,
                '_source': row.get(source_col, 'human') if source_col else 'human',
            })
    return cases

    @staticmethod
    def _split_args(s):
        args=[]; depth=0; cur=''
        for c in s:
            if c=='(': depth+=1; cur+=c
            elif c==')': depth-=1; cur+=c
            elif c==',' and depth==0: args.append(cur.strip()); cur=''
            else: cur+=c
        if cur.strip(): args.append(cur.strip())
        return args
