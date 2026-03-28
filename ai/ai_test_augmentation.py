#!/usr/bin/env python3
"""
AI-Powered ST Test Case Generator  (v2 — with hand-written manual CSV support)
===============================================================================
Usage modes
-----------
  Mode A — ST file only (no manual CSV):
      python ai_test_generator_v2.py program_testable.st

  Mode B — ST file + hand-written manual CSV (--manual):
      python ai_test_generator_v2.py program_testable.st --manual base.csv

      Produces TWO output CSVs:
        1. <stem>_human_formatted.csv  — hand-written cases, formatted & validated
        2. <stem>_combined.csv         — hand-written cases + AI-discovered cases

      The number of AI-generated additional cases is determined automatically
      in the range [15, 30] based on how many hand-written rows are supplied.

The hand-written CSV may be raw / unformatted (no header, plain numeric values).
Column order must match the ST file's declared I/O with AT addresses:
  input1, input2, ..., output1, output2, ...

Environment:
    OPENAI_API_KEY  — your OpenAI API key (required)
"""

import sys, os, csv, json, argparse, re
from pathlib import Path
from typing import Optional, List, Dict, Tuple, Any

try:
    from openai import OpenAI
except ImportError:
    print("Error: openai package not installed. Run: pip install openai")
    sys.exit(1)

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
        m = re.search(r'END_VAR\s*(.*?)\s*END_PROGRAM', self.st_code, re.DOTALL|re.IGNORECASE)
        if not m: return
        body = re.sub(r'\(\*.*?\*\)', '', m.group(1), flags=re.DOTALL)
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
                m = re.match(rf'\b{kw}\b', expr[i:], re.IGNORECASE)
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


# ── Post-processor ────────────────────────────────────────────────────────────

def _make_timer_reset_row(inst_name, parser, plc_scan_ms):
    pt_ms = parser.timer_instances[inst_name]['pt_ms']
    return {
        'test_id': '__reset__',
        'delay_ms': max(plc_scan_ms, int(pt_ms)+plc_scan_ms),
        'description': f'[AUTO] Reset {inst_name} — drive TON_IN=FALSE so ET resets to 0',
        'inputs': {}, 'expected_outputs': {},
        '_is_reset_row': True, '_reset_timer': inst_name,
    }

def enforce_timing_and_insert_resets(test_cases, parser, evaluator, plc_scan_ms):
    for tc in test_cases:
        if int(tc.get('delay_ms',100)) < plc_scan_ms: tc['delay_ms'] = plc_scan_ms
    if not parser.timer_instances:
        _renumber(test_cases); return test_cases
    result=[]; ton_q_state={inst:0 for inst in parser.timer_instances}
    for tc in test_cases:
        inputs_vals = {k:int(v) for k,v in tc.get('inputs',{}).items()}
        delay_ms = int(tc.get('delay_ms',100))
        for inst_name,inst in parser.timer_instances.items():
            if inst.get('type','TON')!='TON': continue
            ton_in = evaluator.timer_in_value(inst_name, inputs_vals)
            if ton_in and ton_q_state[inst_name]:
                result.append(_make_timer_reset_row(inst_name, parser, plc_scan_ms))
                ton_q_state[inst_name]=0
        result.append(tc)
        for inst_name,inst in parser.timer_instances.items():
            if inst.get('type','TON')!='TON': continue
            ton_in = evaluator.timer_in_value(inst_name, inputs_vals)
            if ton_in is None: continue
            if not ton_in: ton_q_state[inst_name]=0
            elif delay_ms>=inst['pt_ms']: ton_q_state[inst_name]=1
    _renumber(result); return result

def _renumber(test_cases):
    for i,tc in enumerate(test_cases, start=1): tc['test_id']=i

def validate_and_correct(test_cases, parser, evaluator, verbose=True):
    corrections=0; ton_q_state={inst:0 for inst in parser.timer_instances}
    for tc in test_cases:
        inputs_vals = {k:int(v) for k,v in tc.get('inputs',{}).items()}
        delay_ms = int(tc.get('delay_ms',100))
        local_out = evaluator.evaluate_outputs(inputs_vals, delay_ms, prev_ton_q=dict(ton_q_state))
        corrected_this=[]
        if tc.get('_is_reset_row'):
            for out in parser.outputs:
                local=local_out.get(out['name'])
                if local is not None: tc['expected_outputs'][out['name']]=local
        else:
            for out in parser.outputs:
                name=out['name']; local=local_out.get(name)
                if local is None: continue
                ai_raw=tc.get('expected_outputs',{}).get(name)
                try: ai_int=int(ai_raw) if ai_raw is not None else None
                except: ai_int=None
                if ai_int!=local:
                    if verbose:
                        print(f"  [CORRECT] Test {tc.get('test_id','?')} '{name}': AI={ai_int} -> local={local}")
                    tc.setdefault('expected_outputs',{})[name]=local
                    corrected_this.append(name); corrections+=1
        tc['_corrected_vars']=corrected_this
        for inst_name,inst in parser.timer_instances.items():
            if inst.get('type','TON')!='TON': continue
            ton_in=evaluator.timer_in_value(inst_name, inputs_vals)
            if ton_in is None: continue
            if not ton_in: ton_q_state[inst_name]=0
            elif delay_ms>=inst['pt_ms']: ton_q_state[inst_name]=1
    return test_cases, corrections


# ── Hand-written CSV loading & formatting prompt ──────────────────────────────

def load_raw_base_choice_csv(csv_path):
    rows=[]
    with open(csv_path, newline='', encoding='utf-8') as f:
        for row in csv.reader(f):
            stripped=[c.strip() for c in row]
            if any(stripped): rows.append(stripped)
    return rows

def _build_format_prompt(parser, raw_rows, plc_scan_ms):
    inputs_desc  = "\n".join(f"  col {i}: {v['name']} ({v['type']}) -> {v['address']}" for i,v in enumerate(parser.inputs))
    outputs_desc = "\n".join(f"  col {i+len(parser.inputs)}: {v['name']} ({v['type']}) -> {v['address']}" for i,v in enumerate(parser.outputs))
    constants_desc = ""
    if parser.constants:
        constants_desc = "\nConstants:\n" + "\n".join(f"  {k} = {v}" for k,v in parser.constants.items()) + "\n"
    raw_text = "\n".join(",".join(r) for r in raw_rows)
    return f"""You are a PLC test engineer.
Below is an IEC 61131-3 ST program and a set of raw, unformatted hand-written
base-choice test cases (plain CSV, no header row).

==== ST PROGRAM CONTEXT ====
Program: {parser.program_name}
{constants_desc}
ST Code (for understanding the logic):
```
{parser.st_code}
```

==== I/O SCHEMA (column -> variable mapping) ====
Inputs:
{inputs_desc}

Outputs:
{outputs_desc}

==== RAW BASE-CHOICE ROWS ====
{raw_text}

==== YOUR TASK ====
For EACH raw row, produce one structured test case JSON object using this schema:
{{
  "test_id": <sequential int from 1>,
  "delay_ms": <int, minimum {plc_scan_ms}, default 100>,
  "description": "<specific description of what boundary/scenario this tests>",
  "reasoning": "<brief explanation of why this input combo is meaningful>",
  "inputs":            {{ "<input_var_name>": <int_value>, ... }},
  "expected_outputs":  {{ "<output_var_name>": <int_value>, ... }}
}}

Rules:
- Map columns to variable names using the I/O SCHEMA above.
- delay_ms >= {plc_scan_ms}.
- 0/1 for BOOL variables; plain integers for INT/WORD.
- Write a meaningful description based on the ST logic for each row.
- Return ONLY a JSON object with a "test_cases" array — no markdown, no prose.
- Format exactly the {len(raw_rows)} rows provided, in order. Do NOT invent new rows.
"""

def _build_new_cases_prompts(parser, num_tests, plc_scan_ms, human_cases, failed_cases=None):
    inputs_desc  = "\n".join(f"  {v['name']} ({v['type']}) -> {v['address']}" for v in parser.inputs)
    outputs_desc = "\n".join(f"  {v['name']} ({v['type']}) -> {v['address']}" for v in parser.outputs)
    constants_desc = ""
    if parser.constants:
        constants_desc = "Constants:\n" + "\n".join(f"  {k} = {v}" for k,v in parser.constants.items()) + "\n"
    timer_desc = ""
    if parser.timer_instances:
        timer_desc = "Timer instances:\n" + "\n".join(
            f"  {inst} ({info['type']}): IN = {info['in_expr']!r}, PT = {info['pt_ms']} ms"
            for inst,info in parser.timer_instances.items()) + "\n"
    edge_note = ""
    if parser.has_edge_triggers:
        edge_note = "\nEDGE-TRIGGER NOTE: R_TRIG/F_TRIG present. Use two rows (input=0 then input=1) to fire.\n"
    covered = "\n".join(
        f"  - {tc.get('description','?')}: {', '.join(f'{k}={v}' for k,v in tc.get('inputs',{}).items())}"
        for tc in human_cases if not tc.get('_is_reset_row'))[:3000]
    failed_note = ""
    if failed_cases:
        failed_note = "\n\nPREVIOUS ATTEMPT ERRORS — retrace carefully:\n" + "\n".join(
            f"  Test {fc['test_id']} ({fc['description']}): inputs={fc['inputs']}, "
            f"AI={fc['ai_outputs']}, correct={fc['correct_outputs']}" for fc in failed_cases[:10]) + "\n"

    system = f"""\
You are an expert PLC test engineer specializing in IEC 61131-3 Structured Text.

SEL(G, IN0, IN1): G=0->IN0; G=1->IN1  (G=1 picks the SECOND arg)
MUX(K, IN0, IN1,...): returns INk (0-indexed).
LIMIT(MN, IN, MX): clamps IN to [MN,MX].
TON: Q=TRUE iff IN=TRUE AND delay_ms >= PT. Q=FALSE if IN=FALSE OR delay_ms < PT.
TOF: Q=TRUE if IN=TRUE; Q=FALSE if IN=FALSE AND delay_ms >= PT.
SR: Q1=S1 OR (NOT RESET1 AND Q1_prev). RS: Q1=NOT R1 AND (S AND Q1_prev).

PLC scan interval: {plc_scan_ms} ms. delay_ms >= {plc_scan_ms} for every row — no exceptions.
To test TON Q=0: set inputs so TON_IN=FALSE (never use a short delay).

Return a JSON object ONLY — no markdown, no prose.
"""
    user = f"""Generate {num_tests} NEW test cases NOT already covered by the hand-written tests below.

Program: {parser.program_name}
Inputs:\n{inputs_desc}\nOutputs:\n{outputs_desc}\n{constants_desc}{timer_desc}{edge_note}
ST Code:
```
{parser.st_code}
```

==== ALREADY COVERED (DO NOT DUPLICATE) ====
{covered}
{failed_note}
Return ONLY:
{{
  "test_cases": [
    {{
      "test_id": 1, "delay_ms": 100,
      "description": "<what new scenario this tests>",
      "reasoning": "<step-by-step trace of every output>",
      "inputs": {{}}, "expected_outputs": {{}}
    }}
  ]
}}

Rules: delay_ms>={plc_scan_ms}; 0/1 for BOOL; default inputs=0; avoid duplicates above;
cover untested boundary values and edge cases; show reasoning; generate exactly {num_tests} cases.
"""
    return system, user


# ── OpenAI call ───────────────────────────────────────────────────────────────

def call_openai(client, model, system, user):
    print(f"  Sending request to OpenAI ({model}) ...")
    resp = client.chat.completions.create(
        model=model,
        messages=[{"role":"system","content":system},{"role":"user","content":user}],
        response_format={"type":"json_object"},
        temperature=0.1,
    )
    return json.loads(resp.choices[0].message.content)


# ── CSV helpers ───────────────────────────────────────────────────────────────

def save_csv(path, inputs, outputs, test_cases, include_flag=True, source_tag_col=None):
    headers = ['Test_ID','Delay_ms','Description']
    for v in inputs:  headers.append(f"Input_{v['name']} ({v['address']})")
    for v in outputs: headers.append(f"Expected_{v['name']} ({v['address']})")
    if include_flag: headers.append('AutoInserted')
    if source_tag_col: headers.append(source_tag_col)
    with open(path, 'w', newline='', encoding='utf-8') as f:
        w = csv.writer(f)
        w.writerow(headers)
        for tc in test_cases:
            row = [tc.get('test_id',''), tc.get('delay_ms',100), tc.get('description','')]
            for v in inputs:  row.append(tc.get('inputs',{}).get(v['name'],0))
            for v in outputs: row.append(tc.get('expected_outputs',{}).get(v['name'],0))
            if include_flag: row.append(1 if tc.get('_is_reset_row') else 0)
            if source_tag_col: row.append(tc.get('_source',''))
            w.writerow(row)


# ── Main ──────────────────────────────────────────────────────────────────────

SYSTEM_PROMPT_A = """\
You are an expert PLC test engineer specializing in IEC 61131-3 Structured Text.

SEL(G,IN0,IN1): G=0->IN0; G=1->IN1.  MUX(K,...): returns INk (0-indexed).
LIMIT(MN,IN,MX): clamps.  TON: Q=TRUE iff IN=TRUE AND delay_ms>=PT.
TOF: Q=TRUE if IN=TRUE; Q=FALSE if IN=FALSE AND delay_ms>=PT.
SR: Q1=S1 OR (NOT RESET1 AND Q1_prev).  RS: Q1=NOT R1 AND (S AND Q1_prev).

PLC scan: {plc_scan_ms} ms. delay_ms >= {plc_scan_ms} always.
To test TON Q=0: make TON_IN=FALSE — never rely on short delay.

Return JSON only.
"""

def main():
    ap = argparse.ArgumentParser(description='AI PLC test case generator v2')
    ap.add_argument('st_file')
    ap.add_argument('--manual', default=None, metavar='CSV',
                    help='Raw hand-written CSV (activates Mode B: format + discover new cases)')
    ap.add_argument('-o','--output', default=None)
    ap.add_argument('--model', default='gpt-4o')
    ap.add_argument('--max-retries', type=int, default=2)
    ap.add_argument('--plc-scan-ms', type=int, default=DEFAULT_PLC_SCAN_MS)
    ap.add_argument('--no-flag', action='store_true')
    ap.add_argument('--quiet', action='store_true')
    args = ap.parse_args()

    st_path = Path(args.st_file)
    if not st_path.exists(): print(f"Error: ST file not found: {st_path}"); sys.exit(1)

    api_key = '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'
    if not api_key:
        print("Error: OPENAI_API_KEY not set.\n  export OPENAI_API_KEY='sk-...'"); sys.exit(1)

    plc_scan_ms = args.plc_scan_ms
    client = OpenAI(api_key=api_key)

    print(f"Reading:   {st_path}")
    st_code = st_path.read_text(encoding='utf-8')
    parser = STParser(st_code); parser.parse()
    evaluator = STEvaluator(parser)

    print(f"Program:   {parser.program_name}")
    if not parser.has_explicit_io:
        print("Error: No I/O with AT addresses found. Run 5_st_to_testable_converter.py first."); sys.exit(1)
    print(f"Inputs:    {', '.join(v['name']+'('+v['address']+')' for v in parser.inputs)}")
    print(f"Outputs:   {', '.join(v['name']+'('+v['address']+')' for v in parser.outputs)}")
    if parser.constants: print(f"Constants: {', '.join(k+'='+str(v) for k,v in parser.constants.items())}")
    for inst,info in parser.timer_instances.items():
        print(f"Timer:     {inst} ({info['type']})  PT={info['pt_ms']} ms  IN={info['in_expr']!r}")
    print(f"PLC scan:  {plc_scan_ms} ms")
    n_eval = len(evaluator._output_exprs)+len(evaluator._ton_outputs)
    print(f"Evaluator: {n_eval}/{len(parser.outputs)} outputs locally evaluable")

    # ── MODE B ────────────────────────────────────────────────────────────────
    if args.manual:
        bc_path = Path(args.manual)
        if not bc_path.exists(): print(f"Error: Manual CSV not found: {bc_path}"); sys.exit(1)

        human_out    = st_path.parent / f"{st_path.stem}_human_formatted.csv"
        combined_out = st_path.parent / f"{st_path.stem}_combined.csv"

        # Step 1: format hand-written cases
        print(f"\n[Step 1] Formatting hand-written CSV: {bc_path}")
        raw_rows = load_raw_base_choice_csv(str(bc_path))
        print(f"  Loaded {len(raw_rows)} raw rows.")

        # Auto-determine how many new AI cases to generate (15–30).
        # Scale inversely with the number of hand-written rows so the
        # combined suite stays well-rounded without excessive duplication.
        num_extra = max(15, min(30, 30 - len(raw_rows)))
        print(f"  AI will generate {num_extra} new test cases "
              f"(auto: 30 - {len(raw_rows)} hand-written, clamped to [15, 30]).")

        fmt_result = call_openai(client, args.model,
            "You are a PLC test engineer. Return ONLY valid JSON — no markdown, no prose.",
            _build_format_prompt(parser, raw_rows, plc_scan_ms))

        human_raw = fmt_result.get('test_cases', [])
        if not human_raw: print("Error: 0 formatted cases returned."); sys.exit(1)
        print(f"  Received {len(human_raw)} formatted cases.")

        for tc in human_raw: tc['_source'] = 'human'
        human_cases = enforce_timing_and_insert_resets(human_raw, parser, evaluator, plc_scan_ms)
        human_cases, corr = validate_and_correct(human_cases, parser, evaluator, verbose=not args.quiet)
        h_resets = sum(1 for tc in human_cases if tc.get('_is_reset_row'))
        print(f"  Corrections: {corr}   Auto-resets: {h_resets}")

        save_csv(str(human_out), parser.inputs, parser.outputs, human_cases,
                 include_flag=not args.no_flag, source_tag_col='Source')
        print(f"  Saved (1/2): {human_out}")

        # Step 2: AI discovers new cases
        print(f"\n[Step 2] Discovering {num_extra} new AI test cases ...")
        failed_retry = None; ai_cases = []; total_corr = 0

        for attempt in range(1, args.max_retries+2):
            print(f"  [Attempt {attempt}]")
            sys_new, usr_new = _build_new_cases_prompts(
                parser, num_extra, plc_scan_ms, human_cases, failed_retry)
            ai_result = call_openai(client, args.model, sys_new, usr_new)
            raw = ai_result.get('test_cases', [])
            if not raw: print("Error: 0 new cases returned."); sys.exit(1)
            for tc in raw: tc['_source'] = 'ai'
            raw = enforce_timing_and_insert_resets(raw, parser, evaluator, plc_scan_ms)
            raw, corr = validate_and_correct(raw, parser, evaluator, verbose=not args.quiet)
            total_corr += corr; ai_cases = raw
            a_resets = sum(1 for tc in ai_cases if tc.get('_is_reset_row'))
            print(f"  Corrections: {corr}   Auto-resets: {a_resets}")
            still_wrong = [
                {'test_id':tc['test_id'],'description':tc['description'],
                 'inputs':tc.get('inputs',{}),'ai_outputs':dict(tc.get('expected_outputs',{})),
                 'correct_outputs':dict(tc.get('expected_outputs',{}))}
                for tc in ai_cases if tc.get('_corrected_vars') and not tc.get('_is_reset_row')]
            if not still_wrong or attempt > args.max_retries: break
            print(f"  {len(still_wrong)} corrections — retrying ...")
            failed_retry = still_wrong

        # Step 3: combine
        combined = list(human_cases) + list(ai_cases)
        _renumber(combined)
        save_csv(str(combined_out), parser.inputs, parser.outputs, combined,
                 include_flag=not args.no_flag, source_tag_col='Source')

        n_h, n_a, n_t = len(human_cases), len(ai_cases), len(combined)
        a_resets = sum(1 for tc in ai_cases if tc.get('_is_reset_row'))
        print(f"\n{'='*60}")
        print(f"Output 1 — Human formatted : {human_out}")
        print(f"  {n_h} rows ({n_h-h_resets} tests + {h_resets} auto-resets)")
        print(f"Output 2 — Combined        : {combined_out}")
        print(f"  {n_t} rows total ({n_h} human + {n_a} AI, incl. {h_resets+a_resets} auto-resets)")
        print(f"  Total AI corrections: {total_corr}")
        print(f"\nNext steps:")
        print(f"  1. Load '{st_path.name}' on the PLC runtime.")
        print(f"  2a. Human only:  python3 test_generators/test_generator.py -f {human_out}")
        print(f"  2b. Full suite:  python3 test_generators/test_generator.py -f {combined_out}")

    # ── MODE A ────────────────────────────────────────────────────────────────
    else:
        out_path = Path(args.output) if args.output else st_path.parent / f"test_cases_{st_path.stem}.csv"
        failed_retry = None; test_cases = []; total_corr = 0
        num_tests_a = 25  # fixed default for Mode A (ST-only, no manual CSV)
        sys_a = SYSTEM_PROMPT_A.replace('{plc_scan_ms}', str(plc_scan_ms))

        for attempt in range(1, args.max_retries+2):
            print(f"\n[Attempt {attempt}]")
            inputs_desc  = "\n".join(f"  {v['name']} ({v['type']}) -> {v['address']}" for v in parser.inputs)
            outputs_desc = "\n".join(f"  {v['name']} ({v['type']}) -> {v['address']}" for v in parser.outputs)
            constants_desc = ("Constants:\n"+"\n".join(f"  {k}={v}" for k,v in parser.constants.items())+"\n") if parser.constants else ""
            timer_desc = ("Timer instances:\n"+"\n".join(f"  {inst} ({info['type']}): IN={info['in_expr']!r}, PT={info['pt_ms']} ms" for inst,info in parser.timer_instances.items())+"\n") if parser.timer_instances else ""
            edge_note = ("\nEDGE-TRIGGER NOTE: R_TRIG/F_TRIG present. Use two rows to fire.\n") if parser.has_edge_triggers else ""
            failed_note = ""
            if failed_retry:
                failed_note = "\n\nPREVIOUS ERRORS:\n" + "\n".join(
                    f"  Test {fc['test_id']}: inputs={fc['inputs']}, AI={fc['ai_outputs']}, correct={fc['correct_outputs']}"
                    for fc in failed_retry[:10]) + "\n"
            usr_a = f"""Generate {num_tests_a} test cases for program: {parser.program_name}
Inputs:\n{inputs_desc}\nOutputs:\n{outputs_desc}\n{constants_desc}{timer_desc}{edge_note}
ST Code:\n```\n{parser.st_code}\n```\n{failed_note}
Return ONLY:
{{"test_cases":[{{"test_id":1,"delay_ms":100,"description":"","reasoning":"","inputs":{{}},"expected_outputs":{{}}}}]}}
Rules: delay_ms>={plc_scan_ms}; 0/1 BOOL; default inputs=0; show reasoning; generate exactly {num_tests_a} cases.
"""
            ai_result = call_openai(client, args.model, sys_a, usr_a)
            raw = ai_result.get('test_cases', [])
            if not raw: print("Error: 0 cases returned."); sys.exit(1)
            for i,tc in enumerate(raw,1): tc['test_id']=i
            raw = enforce_timing_and_insert_resets(raw, parser, evaluator, plc_scan_ms)
            raw, corr = validate_and_correct(raw, parser, evaluator, verbose=not args.quiet)
            total_corr += corr; test_cases = raw
            n_resets = sum(1 for tc in test_cases if tc.get('_is_reset_row'))
            print(f"  Corrections: {corr}   Auto-resets: {n_resets}")
            still_wrong = [
                {'test_id':tc['test_id'],'description':tc['description'],
                 'inputs':tc.get('inputs',{}),'ai_outputs':dict(tc.get('expected_outputs',{})),
                 'correct_outputs':dict(tc.get('expected_outputs',{}))}
                for tc in test_cases if tc.get('_corrected_vars') and not tc.get('_is_reset_row')]
            if not still_wrong or attempt > args.max_retries: break
            print(f"  {len(still_wrong)} corrections — retrying ..."); failed_retry = still_wrong

        save_csv(str(out_path), parser.inputs, parser.outputs, test_cases, include_flag=not args.no_flag)
        n_resets = sum(1 for tc in test_cases if tc.get('_is_reset_row'))
        print(f"\nTotal rows : {len(test_cases)} ({len(test_cases)-n_resets} tests + {n_resets} auto-resets)")
        print(f"Corrected  : {total_corr}")
        print(f"Saved      : {out_path}")

if __name__ == '__main__':
    main()
