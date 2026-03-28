#!/usr/bin/env python3
"""
AI-Powered ST Test Case Generator
===================================
Uses OpenAI to analyze a TESTABLE IEC 61131-3 Structured Text (.st) file and
generate comprehensive test cases saved as a CSV compatible with test_generator.py.

Key improvements over the original:
  • Local ST expression evaluator validates and corrects every expected output
    before it hits the CSV — catches SEL, TON, boolean logic errors.
  • Chain-of-thought prompt forces the AI to show its work per output.
  • Automatic retry loop: if the AI produces uncorrectable rows, re-prompts
    with failures highlighted.
  • Clamp helper keeps INT values inside [-32768, 32767] automatically.
  • Sub-scan delay enforcement: delay_ms is always raised to at least
    PLC_SCAN_MS (default 25 ms) so the PLC completes at least one full scan
    before outputs are read. This eliminates stale-output failures.
  • TON state persistence fix: a post-processor inserts a mandatory timer-reset
    row (inputs that make TON_IN=FALSE, delay >= PT) before any test where the
    timer IN expression is TRUE and the timer was previously elapsed, ensuring
    the PLC's ET counter is zeroed before each timing-sensitive test.

Pre-requisite:
    First convert your ST file to a testable form:
        python plc_converters/5_st_to_testable_converter.py program.st
    Feed the resulting <program>_testable.st to this script.

Usage:
    python ai_test_generator.py program_testable.st
    python ai_test_generator.py program_testable.st -o my_tests.csv
    python ai_test_generator.py program_testable.st --num-tests 40
    python ai_test_generator.py program_testable.st --model gpt-4o
    python ai_test_generator.py program_testable.st --plc-scan-ms 20

Environment:
    OPENAI_API_KEY  — your OpenAI API key (required)
"""

import sys
import os
import csv
import json
import argparse
import re
from pathlib import Path
from typing import Optional, List, Dict, Tuple, Any

try:
    from openai import OpenAI
except ImportError:
    print("Error: openai package not installed. Run: pip install openai")
    sys.exit(1)


# ──────────────────────────────────────────────────────────────────────────────
# Constants
# ──────────────────────────────────────────────────────────────────────────────

# Default PLC task scan interval in ms. delay_ms is never set below this value
# so the PLC always completes at least one full scan before outputs are read.
DEFAULT_PLC_SCAN_MS = 25

INT_MIN = -32768
INT_MAX =  32767


# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────

def clamp_int(v: Any) -> int:
    return max(INT_MIN, min(INT_MAX, int(v)))


def bool_int(v: Any) -> int:
    """Normalise any truthy value to 0/1."""
    if isinstance(v, str):
        return 1 if v.strip().upper() in ('1', 'TRUE') else 0
    return 1 if v else 0


# ──────────────────────────────────────────────────────────────────────────────
# ST File Parser
# ──────────────────────────────────────────────────────────────────────────────

class STParser:
    """
    Parses a TESTABLE IEC 61131-3 ST file produced by 5_st_to_testable_converter.py.

    Classification by AT address:
      Inputs  : %QX200.x / %QW200+    (Modbus-writable, driven by test runner)
      Outputs : %QX100.x / %QW100-199 (Modbus-readable, checked by test runner)
    """

    WORD_TYPES = {'INT', 'UINT', 'DINT', 'UDINT', 'WORD', 'DWORD', 'REAL', 'LREAL'}
    FB_TYPES   = {'R_TRIG', 'F_TRIG', 'TON', 'TOF', 'TP', 'CTU', 'CTD', 'CTUD',
                  'SR', 'RS', 'SEMA'}
    SKIP_TYPES = FB_TYPES | {'TIME', 'DATE', 'DT', 'TOD'}

    def __init__(self, st_code: str):
        self.st_code             = st_code
        self.program_name        = ''
        self.inputs:  List[dict] = []
        self.outputs: List[dict] = []
        self.constants: Dict     = {}
        self.has_edge_triggers   = False
        self.statements: List[str]            = []
        self.timer_instances: Dict[str, dict] = {}  # inst_name → {pt_ms, in_expr, type}

    # ── Block helpers ────────────────────────────────────────────────────────

    @staticmethod
    def _extract_block(text: str, keyword: str) -> Optional[str]:
        m = re.search(rf'{keyword}\b(.*?)END_VAR', text, re.DOTALL | re.IGNORECASE)
        return m.group(1) if m else None

    @staticmethod
    def _parse_declarations(block: str) -> List[Tuple[str, str, Optional[str]]]:
        result = []
        for decl in block.split(';'):
            decl = re.sub(r'\(\*.*?\*\)', '', decl.strip(), flags=re.DOTALL).strip()
            if not decl:
                continue
            m = re.match(r'(\w+)\s*(?:AT\s+(%[^\s:]+))?\s*:\s*(\w+)', decl, re.IGNORECASE)
            if m:
                result.append((m.group(1), m.group(3).upper(), m.group(2)))
        return result

    # ── Constant extraction ──────────────────────────────────────────────────

    def _extract_constants(self):
        block = self._extract_block(self.st_code, r'VAR\s+CONSTANT')
        if not block:
            return
        for decl in block.split(';'):
            m = re.match(r'\s*(\w+)\s*:\s*\w+\s*:=\s*([^\s;]+)', decl)
            if not m:
                continue
            raw = m.group(2)
            t = re.match(r'(?:T|TIME)#(\d+(?:\.\d+)?)(ms|s|m|h)', raw, re.IGNORECASE)
            if t:
                val  = float(t.group(1))
                unit = t.group(2).lower()
                ms   = val * {'ms': 1, 's': 1000, 'm': 60_000, 'h': 3_600_000}[unit]
                self.constants[m.group(1)] = ms
            else:
                try:
                    self.constants[m.group(1)] = float(raw)
                except ValueError:
                    self.constants[m.group(1)] = raw

    # ── Timer instance extraction ────────────────────────────────────────────

    def _resolve_pt(self, pt_sym: str) -> float:
        """Resolve a PT argument (symbol or TIME literal) to milliseconds."""
        if pt_sym in self.constants:
            return float(self.constants[pt_sym])
        t = re.match(r'(?:T|TIME)#(\d+(?:\.\d+)?)(ms|s|m|h)', pt_sym, re.IGNORECASE)
        if t:
            val  = float(t.group(1))
            unit = t.group(2).lower()
            return val * {'ms': 1, 's': 1000, 'm': 60_000, 'h': 3_600_000}[unit]
        return 0.0

    def _extract_timer_instances(self):
        """
        Find TON / TOF / TP instance calls and record their IN expression and PT (ms).
        Handles both INST(IN := expr, PT := sym) and INST(IN := expr, PT := TIME#Xms).
        """
        pattern = re.compile(
            r'(\w+)\s*\(\s*IN\s*:=\s*([^,]+),\s*PT\s*:=\s*([^)]+)\)',
            re.IGNORECASE
        )
        # Determine timer types from the VAR block once
        vb = self._extract_block(self.st_code, r'(?<!_)(?<!\w)VAR(?!\s+CONSTANT)(?!_)')

        for m in pattern.finditer(self.st_code):
            inst     = m.group(1).strip()
            in_expr  = m.group(2).strip()
            pt_raw   = m.group(3).strip()
            pt_ms    = self._resolve_pt(pt_raw)

            timer_type = 'TON'
            if vb:
                tm = re.search(rf'\b{re.escape(inst)}\s*:\s*(TON|TOF|TP)\b',
                               vb, re.IGNORECASE)
                if tm:
                    timer_type = tm.group(1).upper()

            self.timer_instances[inst] = {
                'pt_ms'  : pt_ms,
                'in_expr': in_expr,
                'type'   : timer_type,
            }

    # ── Statement extraction ─────────────────────────────────────────────────

    def _extract_statements(self):
        """Pull the program body (between last END_VAR and END_PROGRAM)."""
        m = re.search(r'END_VAR\s*(.*?)\s*END_PROGRAM', self.st_code,
                      re.DOTALL | re.IGNORECASE)
        if not m:
            return
        body = re.sub(r'\(\*.*?\*\)', '', m.group(1), flags=re.DOTALL)
        for stmt in body.split(';'):
            s = stmt.strip()
            if s:
                self.statements.append(s)

    # ── Main parse ───────────────────────────────────────────────────────────

    def parse(self):
        nc = re.sub(r'\(\*.*?\*\)', '', self.st_code, flags=re.DOTALL)
        m  = re.search(r'^\s*PROGRAM\s+(\w+)', nc, re.IGNORECASE | re.MULTILINE)
        self.program_name = m.group(1) if m else 'Unknown'

        self._extract_constants()
        self._extract_timer_instances()
        self._extract_statements()

        self.has_edge_triggers = bool(
            re.search(r'\bR_TRIG\b|\bF_TRIG\b', self.st_code, re.IGNORECASE)
        )

        plain_block = self._extract_block(
            self.st_code, r'(?<!_)(?<!\w)VAR(?!\s+CONSTANT)(?!_)'
        )
        if plain_block:
            for name, vtype, at_addr in self._parse_declarations(plain_block):
                if vtype in self.SKIP_TYPES or not at_addr:
                    continue
                if self._addr_is_output(at_addr):
                    self.outputs.append({'name': name, 'type': vtype, 'address': at_addr})
                else:
                    self.inputs.append({'name': name, 'type': vtype, 'address': at_addr})

    @staticmethod
    def _addr_is_output(at_addr: str) -> bool:
        a = at_addr.upper().strip()
        if a.startswith('%I'):
            return False
        m = re.match(r'%Q[DW](\d+)$', a)
        if m:
            return int(m.group(1)) < 200
        m = re.match(r'%QX(\d+)\.(\d+)$', a)
        if m:
            return (int(m.group(1)) * 8 + int(m.group(2))) < 1600
        return False

    @property
    def has_explicit_io(self) -> bool:
        return bool(self.inputs or self.outputs)


# ──────────────────────────────────────────────────────────────────────────────
# Local Expression Evaluator
# ──────────────────────────────────────────────────────────────────────────────

class STEvaluator:
    """
    Evaluates IEC 61131-3 expressions locally to validate and correct AI outputs.

    Supported:
      Boolean   : AND, OR, NOT, XOR
      Arithmetic: +, -, *, /
      Comparisons: =, <>, <, >, <=, >=
      Functions : SEL, MUX, LIMIT, MAX, MIN, ABS
      Timers    : TON / TOF Q-output evaluated against delay_ms
                  with TON state-persistence awareness via prev_ton_q
    """

    def __init__(self, parser: STParser):
        self.parser = parser
        self._output_exprs: Dict[str, str] = {}   # output_var → rhs expression
        self._ton_outputs:  Dict[str, str] = {}   # output_var → timer inst name
        self._parse_output_exprs()

    def _parse_output_exprs(self):
        for stmt in self.parser.statements:
            m = re.match(r'(\w+)\s*:=\s*(.+)$', stmt, re.IGNORECASE | re.DOTALL)
            if not m:
                continue
            lhs, rhs = m.group(1).strip(), m.group(2).strip()
            mq = re.match(r'(\w+)\.Q$', rhs, re.IGNORECASE)
            if mq:
                self._ton_outputs[lhs] = mq.group(1)
            else:
                self._output_exprs[lhs] = rhs

    def evaluate_outputs(
        self,
        inputs: Dict[str, Any],
        delay_ms: int,
        prev_ton_q: Optional[Dict[str, int]] = None,
    ) -> Dict[str, Optional[int]]:
        """
        Evaluate all outputs for a given set of inputs and delay.

        prev_ton_q: maps timer instance name → previous Q value (0/1).
                    Enables correct modelling of TON state persistence:
                    if IN=TRUE and prev_ton_q=1, Q=TRUE immediately (ET already elapsed).
                    If None, all timers assumed to start from reset.
        """
        env: Dict[str, Any] = {inp['name']: inputs.get(inp['name'], 0)
                                for inp in self.parser.inputs}
        env.update(self.parser.constants)

        if prev_ton_q is None:
            prev_ton_q = {}

        results: Dict[str, Optional[int]] = {}

        for out in self.parser.outputs:
            name = out['name']

            if name in self._ton_outputs:
                inst_name = self._ton_outputs[name]
                inst      = self.parser.timer_instances.get(inst_name)
                if inst is None:
                    results[name] = None
                    continue

                in_val = self._eval_expr(inst['in_expr'], env)
                if in_val is None:
                    results[name] = None
                    continue

                in_active = bool(in_val)
                pt_ms     = inst['pt_ms']
                ttype     = inst.get('type', 'TON')
                prev_q    = bool(prev_ton_q.get(inst_name, 0))

                if ttype == 'TON':
                    # Q=TRUE if IN=TRUE AND (timer was already elapsed OR delay >= PT)
                    results[name] = 1 if (in_active and (prev_q or delay_ms >= pt_ms)) else 0
                elif ttype == 'TOF':
                    results[name] = 1 if in_active else (0 if delay_ms >= pt_ms else 1)
                else:  # TP
                    results[name] = 1 if (in_active and delay_ms >= pt_ms) else 0

            elif name in self._output_exprs:
                val = self._eval_expr(self._output_exprs[name], env)
                if val is None:
                    results[name] = None
                elif out['type'] == 'BOOL':
                    results[name] = bool_int(val)
                else:
                    results[name] = clamp_int(val)
            else:
                results[name] = None

        return results

    def timer_in_value(self, inst_name: str, inputs: Dict[str, Any]) -> Optional[bool]:
        """Evaluate the IN expression of a named timer. Returns None if unsupported."""
        inst = self.parser.timer_instances.get(inst_name)
        if inst is None:
            return None
        env = {inp['name']: inputs.get(inp['name'], 0) for inp in self.parser.inputs}
        env.update(self.parser.constants)
        val = self._eval_expr(inst['in_expr'], env)
        return bool(val) if val is not None else None

    # ── Expression parser ────────────────────────────────────────────────────

    def _eval_expr(self, expr: str, env: Dict[str, Any]) -> Optional[Any]:
        try:
            return self._parse_or(expr.strip(), env)
        except Exception:
            return None

    def _parse_or(self, expr, env):
        parts = self._split_kw(expr, 'OR')
        if len(parts) > 1:
            return int(any(bool(self._parse_xor(p, env)) for p in parts))
        return self._parse_xor(expr, env)

    def _parse_xor(self, expr, env):
        parts = self._split_kw(expr, 'XOR')
        if len(parts) > 1:
            r = bool(self._parse_and(parts[0], env))
            for p in parts[1:]:
                r ^= bool(self._parse_and(p, env))
            return int(r)
        return self._parse_and(expr, env)

    def _parse_and(self, expr, env):
        parts = self._split_kw(expr, 'AND')
        if len(parts) > 1:
            return int(all(bool(self._parse_not(p, env)) for p in parts))
        return self._parse_not(expr, env)

    def _parse_not(self, expr, env):
        expr = expr.strip()
        if re.match(r'^NOT\s*\(', expr, re.IGNORECASE):
            inner = self._paren_inner(expr[3:].strip())
            return int(not bool(self._parse_or(inner, env)))
        if re.match(r'^NOT\s+\w', expr, re.IGNORECASE):
            return int(not bool(self._parse_atom(expr[3:].strip(), env)))
        return self._parse_cmp(expr, env)

    def _parse_cmp(self, expr, env):
        for op in ('<>', '<=', '>=', '<', '>', '='):
            idx = self._find_op(expr, op)
            if idx is not None:
                lhs = self._parse_add(expr[:idx].strip(), env)
                rhs = self._parse_add(expr[idx + len(op):].strip(), env)
                return {
                    '<>': int(lhs != rhs), '<=': int(lhs <= rhs),
                    '>=': int(lhs >= rhs), '<':  int(lhs <  rhs),
                    '>':  int(lhs >  rhs), '=':  int(lhs == rhs),
                }[op]
        return self._parse_add(expr, env)

    def _parse_add(self, expr, env):
        tokens = self._tokenize_add(expr)
        if len(tokens) == 1:
            return self._parse_mul(tokens[0][1], env)
        result = 0
        for sign, tok in tokens:
            v = self._parse_mul(tok, env)
            result = result + v if sign == '+' else result - v
        return result

    def _tokenize_add(self, expr):
        tokens = []; depth = 0; current = ''; sign = '+'
        for c in expr:
            if c == '(':
                depth += 1; current += c
            elif c == ')':
                depth -= 1; current += c
            elif depth == 0 and c in '+-':
                if current.strip():
                    tokens.append((sign, current.strip())); current = ''; sign = c
                else:
                    current += c
            else:
                current += c
        if current.strip():
            tokens.append((sign, current.strip()))
        return tokens or [('+', expr)]

    def _parse_mul(self, expr, env):
        parts = re.split(r'(?<!\*)\*(?!\*)|\/', expr)
        if len(parts) == 1:
            return self._parse_atom(expr.strip(), env)
        ops    = re.findall(r'[*/]', expr)
        result = self._parse_atom(parts[0].strip(), env)
        for op, p in zip(ops, parts[1:]):
            v = self._parse_atom(p.strip(), env)
            result = result * v if op == '*' else (result / v if v else 0)
        return result

    def _parse_atom(self, expr, env):
        expr = expr.strip()
        if not expr:
            raise ValueError('empty')
        if expr.startswith('(') and expr.endswith(')'):
            return self._parse_or(expr[1:-1].strip(), env)

        fm = re.match(r'^(\w+)\s*\((.+)\)$', expr, re.DOTALL)
        if fm:
            fn     = fm.group(1).upper()
            evaled = [self._parse_or(a.strip(), env)
                      for a in self._split_args(fm.group(2))]
            if fn == 'SEL':
                g, i0, i1 = evaled[0], evaled[1], evaled[2]
                return i1 if bool(g) else i0
            if fn == 'MUX':
                k = int(evaled[0])
                return evaled[k + 1] if 1 + k < len(evaled) else 0
            if fn == 'LIMIT':
                return max(evaled[0], min(evaled[2], evaled[1]))
            if fn == 'MAX':  return max(evaled)
            if fn == 'MIN':  return min(evaled)
            if fn == 'ABS':  return abs(evaled[0])

        try:    return int(expr)
        except: pass
        try:    return float(expr)
        except: pass
        if expr.upper() == 'TRUE':  return 1
        if expr.upper() == 'FALSE': return 0

        for k, v in env.items():
            if k.upper() == expr.upper():
                return v
        raise ValueError(f'Unknown symbol: {expr!r}')

    # ── Static helpers ───────────────────────────────────────────────────────

    @staticmethod
    def _split_kw(expr: str, kw: str) -> List[str]:
        parts = []; depth = 0; cur = ''; i = 0
        while i < len(expr):
            if expr[i] == '(':
                depth += 1; cur += expr[i]; i += 1
            elif expr[i] == ')':
                depth -= 1; cur += expr[i]; i += 1
            elif depth == 0:
                m = re.match(rf'\b{kw}\b', expr[i:], re.IGNORECASE)
                if m:
                    parts.append(cur.strip()); cur = ''; i += len(m.group(0))
                else:
                    cur += expr[i]; i += 1
            else:
                cur += expr[i]; i += 1
        parts.append(cur.strip())
        return [p for p in parts if p] if len(parts) > 1 else [expr]

    @staticmethod
    def _paren_inner(expr: str) -> str:
        if not expr.startswith('('):
            return expr
        depth = 0
        for i, c in enumerate(expr):
            if c == '(':   depth += 1
            elif c == ')':
                depth -= 1
                if depth == 0:
                    return expr[1:i]
        return expr[1:-1]

    @staticmethod
    def _find_op(expr: str, op: str) -> Optional[int]:
        depth = 0; i = 0
        while i < len(expr):
            if expr[i] == '(':    depth += 1
            elif expr[i] == ')':  depth -= 1
            elif depth == 0 and expr[i:i + len(op)] == op:
                after  = expr[i + len(op):]
                before = expr[i - 1] if i > 0 else ''
                if op == '<'  and (after.startswith('=') or after.startswith('>')):
                    i += 1; continue
                if op == '>'  and after.startswith('='):
                    i += 1; continue
                if op == '='  and before in ('<', '>', ':'):
                    i += 1; continue
                return i
            i += 1
        return None

    @staticmethod
    def _split_args(s: str) -> List[str]:
        args = []; depth = 0; cur = ''
        for c in s:
            if c == '(':    depth += 1; cur += c
            elif c == ')':  depth -= 1; cur += c
            elif c == ',' and depth == 0:
                args.append(cur.strip()); cur = ''
            else:
                cur += c
        if cur.strip():
            args.append(cur.strip())
        return args


# ──────────────────────────────────────────────────────────────────────────────
# Post-processor — sub-scan delay fix + TON state persistence fix
# ──────────────────────────────────────────────────────────────────────────────

def _make_timer_reset_row(inst_name: str, parser: STParser, plc_scan_ms: int) -> dict:
    """
    Build a timer-reset row with all inputs = 0 (TON_IN becomes FALSE).
    Delay is set to max(plc_scan_ms, PT + plc_scan_ms) to guarantee:
      - At least one PLC scan happens (outputs are fresh).
      - The timer has enough time to reset ET to 0 after IN goes FALSE.
    """
    pt_ms    = parser.timer_instances[inst_name]['pt_ms']
    delay_ms = max(plc_scan_ms, int(pt_ms) + plc_scan_ms)
    return {
        'test_id'         : '__reset__',
        'delay_ms'        : delay_ms,
        'description'     : (f'[AUTO] Reset {inst_name} — '
                             f'drive TON_IN=FALSE so ET resets to 0'),
        'inputs'          : {},   # all inputs = 0
        'expected_outputs': {},   # filled by validate_and_correct
        '_is_reset_row'   : True,
        '_reset_timer'    : inst_name,
    }


def enforce_timing_and_insert_resets(
    test_cases: List[dict],
    parser: STParser,
    evaluator: STEvaluator,
    plc_scan_ms: int,
) -> List[dict]:
    """
    Pass 1 — Sub-scan delay enforcement:
        Any delay_ms < plc_scan_ms is raised to plc_scan_ms.
        Reason: the PLC scans every ~20 ms. If the test runner reads outputs
        before one full scan completes, it gets stale values from the prior test.

    Pass 2 — TON state persistence fix:
        Simulates the TON Q state across the test sequence.
        When a test is about to run with TON_IN=TRUE and the timer was already
        elapsed from a previous test (prev_q=1), the real PLC will assert Q
        immediately — even with a very short delay — because ET is still >= PT
        in PLC memory. The fix: insert an auto-reset row that drives TON_IN=FALSE
        for at least PT ms, forcing ET back to 0 before the actual test runs.

    Returns the new list with resets inserted and IDs renumbered.
    """

    # ── Pass 1: raise sub-scan delays ────────────────────────────────────────
    for tc in test_cases:
        if int(tc.get('delay_ms', 100)) < plc_scan_ms:
            tc['delay_ms'] = plc_scan_ms

    # ── Pass 2: insert timer-reset rows ──────────────────────────────────────
    if not parser.timer_instances:
        _renumber(test_cases)
        return test_cases

    result: List[dict] = []
    ton_q_state: Dict[str, int] = {inst: 0 for inst in parser.timer_instances}

    for tc in test_cases:
        inputs_vals = {k: int(v) for k, v in tc.get('inputs', {}).items()}
        delay_ms    = int(tc.get('delay_ms', 100))

        for inst_name, inst in parser.timer_instances.items():
            if inst.get('type', 'TON') != 'TON':
                continue

            ton_in = evaluator.timer_in_value(inst_name, inputs_vals)
            if ton_in is None:
                continue

            prev_q = ton_q_state[inst_name]

            # If IN=TRUE and timer was previously elapsed, insert a reset row
            # to clear ET before this test executes.
            if ton_in and prev_q:
                reset_row = _make_timer_reset_row(inst_name, parser, plc_scan_ms)
                result.append(reset_row)
                # Simulate the reset: IN=FALSE → Q=0, ET=0
                ton_q_state[inst_name] = 0

        result.append(tc)

        # Advance simulated Q state based on this test's outcome
        for inst_name, inst in parser.timer_instances.items():
            if inst.get('type', 'TON') != 'TON':
                continue
            ton_in = evaluator.timer_in_value(inst_name, inputs_vals)
            if ton_in is None:
                continue
            if not ton_in:
                ton_q_state[inst_name] = 0
            elif delay_ms >= inst['pt_ms']:
                ton_q_state[inst_name] = 1
            # else: IN=TRUE, delay < PT → still counting, Q stays 0

    _renumber(result)
    return result


def _renumber(test_cases: List[dict]) -> None:
    for i, tc in enumerate(test_cases, start=1):
        tc['test_id'] = i


# ──────────────────────────────────────────────────────────────────────────────
# Validation & correction
# ──────────────────────────────────────────────────────────────────────────────

def validate_and_correct(
    test_cases: List[dict],
    parser: STParser,
    evaluator: STEvaluator,
    verbose: bool = True,
) -> Tuple[List[dict], int]:
    """
    For each test case (including auto-reset rows), compute expected outputs
    locally with full TON state tracking, then correct any AI mismatch.

    Returns (corrected_test_cases, num_corrections).
    """
    corrections = 0
    ton_q_state: Dict[str, int] = {inst: 0 for inst in parser.timer_instances}

    for tc in test_cases:
        inputs_vals = {k: int(v) for k, v in tc.get('inputs', {}).items()}
        delay_ms    = int(tc.get('delay_ms', 100))

        local_out = evaluator.evaluate_outputs(
            inputs_vals, delay_ms, prev_ton_q=dict(ton_q_state)
        )

        corrected_this = []

        # For auto-reset rows, just fill in the computed values directly
        if tc.get('_is_reset_row'):
            for out in parser.outputs:
                name  = out['name']
                local = local_out.get(name)
                if local is not None:
                    tc['expected_outputs'][name] = local
        else:
            for out in parser.outputs:
                name  = out['name']
                local = local_out.get(name)
                if local is None:
                    continue   # can't evaluate locally; trust AI

                ai_raw = tc.get('expected_outputs', {}).get(name)
                try:
                    ai_int = int(ai_raw) if ai_raw is not None else None
                except (TypeError, ValueError):
                    ai_int = None

                if ai_int != local:
                    if verbose:
                        print(f"  [CORRECT] Test {tc.get('test_id','?')} '{name}': "
                              f"AI={ai_int} → local={local}")
                    tc.setdefault('expected_outputs', {})[name] = local
                    corrected_this.append(name)
                    corrections += 1

        tc['_corrected_vars'] = corrected_this

        # Advance simulated TON Q state
        for inst_name, inst in parser.timer_instances.items():
            if inst.get('type', 'TON') != 'TON':
                continue
            ton_in = evaluator.timer_in_value(inst_name, inputs_vals)
            if ton_in is None:
                continue
            if not ton_in:
                ton_q_state[inst_name] = 0
            elif delay_ms >= inst['pt_ms']:
                ton_q_state[inst_name] = 1

    return test_cases, corrections


# ──────────────────────────────────────────────────────────────────────────────
# Prompt construction
# ──────────────────────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """\
You are an expert PLC test engineer specializing in IEC 61131-3 Structured Text.
You analyze ST programs and produce comprehensive, correct test cases.

════════════════════════════════════════
IEC 61131-3 Standard Function Semantics  (READ CAREFULLY)
════════════════════════════════════════

SEL(G, IN0, IN1):
  G=0 → returns IN0  (first data arg)
  G=1 → returns IN1  (second data arg)  *** G=1 selects IN1, NOT IN0 ***
  Example: SEL(BYPASS, ACK, RQ) with BYPASS=1 → result = RQ

MUX(K, IN0, IN1, …): returns INk (0-indexed).
LIMIT(MN, IN, MX): clamps IN to [MN, MX].
MAX(a,b,…) / MIN(a,b,…): largest / smallest.

TON (on-delay timer):
  Test runner: (1) writes inputs, (2) waits delay_ms, (3) reads outputs.
  Q = TRUE  iff  IN=TRUE at write time  AND  delay_ms >= PT_in_ms
  Q = FALSE iff  IN=FALSE  OR  delay_ms < PT_in_ms

TOF (off-delay timer):
  Q = TRUE  if IN=TRUE at write time.
  Q = FALSE if IN=FALSE AND delay_ms >= PT_in_ms.

SR (Set-dominant latch): Q1 = S1 OR (NOT RESET1 AND Q1_prev)
RS (Reset-dominant latch): Q1 = NOT R1 AND (S AND Q1_prev)  ← RESET wins

════════════════════════════════════════
Timing constraints (CRITICAL — violations cause real PLC test failures)
════════════════════════════════════════
PLC scan interval: {plc_scan_ms} ms.

RULE 1: delay_ms >= {plc_scan_ms} ms for EVERY test case, no exceptions.
  A shorter delay means the PLC has not completed one scan — outputs will
  be stale from the previous test.

RULE 2: To test "TON does not fire", set inputs so TON_IN=FALSE.
  Do NOT rely on a short delay to prevent the timer from firing.
  Example: TON(IN := ACK AND RQ, PT := 14ms). To test Q=0, set ACK=0 or RQ=0.
  A delay of 10 ms would fail because it is less than one scan cycle.

The script inserts timer-reset rows automatically when needed — you do not
need to add them. Just generate meaningful, diverse test cases.

════════════════════════════════════════
Mandatory chain-of-thought
════════════════════════════════════════
Fill the "reasoning" field by tracing each output step-by-step with the
actual input values substituted in, before writing expected_outputs.

Example:
  HVAC_STATUS_EN = SEL(BYPASS, ACK, RQ) = SEL(1, 1, 0)
    G=1 → result = IN1 = RQ = 0  →  HVAC_STATUS_EN = 0
  HVAC_RN = TON_1.Q: IN=(ACK AND RQ)=(1 AND 0)=FALSE → Q=0

Return a JSON object ONLY — no markdown, no prose.
"""


def _build_prompt(
    parser: STParser,
    num_tests: int,
    plc_scan_ms: int,
    failed_cases: Optional[List[dict]] = None,
) -> str:
    inputs_desc  = "\n".join(f"  {v['name']} ({v['type']}) → {v['address']}"
                              for v in parser.inputs)
    outputs_desc = "\n".join(f"  {v['name']} ({v['type']}) → {v['address']}"
                              for v in parser.outputs)

    constants_desc = ""
    if parser.constants:
        constants_desc = "Constants:\n" + "\n".join(
            f"  {k} = {v}" for k, v in parser.constants.items()
        ) + "\n"

    timer_desc = ""
    if parser.timer_instances:
        lines = [f"  {inst} ({info['type']}): IN = {info['in_expr']!r}, "
                 f"PT = {info['pt_ms']} ms"
                 for inst, info in parser.timer_instances.items()]
        timer_desc = "Timer instances:\n" + "\n".join(lines) + "\n"

    edge_note = ""
    if parser.has_edge_triggers:
        edge_note = (
            "\nEDGE-TRIGGER NOTE: This program uses R_TRIG/F_TRIG.\n"
            "  To activate a latch, generate a 0→1 transition with TWO rows:\n"
            "    Row A: trigger input = 0\n"
            "    Row B: trigger input = 1 (rising edge)\n"
        )

    failed_note = ""
    if failed_cases:
        lines = [
            f"  Test {fc['test_id']} ({fc['description']}): "
            f"inputs={fc['inputs']}, AI={fc['ai_outputs']}, correct={fc['correct_outputs']}"
            for fc in failed_cases[:10]
        ]
        failed_note = (
            "\n\nPREVIOUS ATTEMPT HAD ERRORS — re-generate all tests. "
            "Trace these carefully:\n" + "\n".join(lines) + "\n"
        )

    return f"""Analyze this IEC 61131-3 ST program and generate {num_tests} test cases.

Program: {parser.program_name}

Inputs (do NOT change addresses):
{inputs_desc}

Outputs (do NOT change addresses):
{outputs_desc}

{constants_desc}{timer_desc}{edge_note}
ST Code:
```
{parser.st_code}
```
{failed_note}
Return ONLY this JSON schema:
{{
  "test_cases": [
    {{
      "test_id": 1,
      "delay_ms": 100,
      "description": "<clear description>",
      "reasoning": "<step-by-step trace of every output expression>",
      "inputs":            {{ "<var_name>": <value>, ... }},
      "expected_outputs":  {{ "<var_name>": <value>, ... }}
    }}
  ]
}}

Rules:
- delay_ms >= {plc_scan_ms} for EVERY row — the PLC needs at least one scan.
- Use 0/1 for BOOL; integers for INT.
- All inputs default to 0 unless listed.
- To test TON Q=0: set inputs so TON_IN=FALSE — never rely on a short delay.
- Cover ALL boolean input combinations and numeric boundary values.
- SEL(G, IN0, IN1): G=1 → IN1 (the SECOND argument, not the first).
- Show reasoning before writing expected_outputs.
- Generate exactly {num_tests} test cases.
"""


# ──────────────────────────────────────────────────────────────────────────────
# OpenAI call
# ──────────────────────────────────────────────────────────────────────────────

def call_openai(
    client: OpenAI,
    model: str,
    parser: STParser,
    num_tests: int,
    plc_scan_ms: int,
    failed_cases: Optional[List[dict]] = None,
) -> dict:
    system = SYSTEM_PROMPT.replace('{plc_scan_ms}', str(plc_scan_ms))
    user   = _build_prompt(parser, num_tests, plc_scan_ms, failed_cases)
    print(f"  Sending request to OpenAI ({model}) …")
    response = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": system},
            {"role": "user",   "content": user},
        ],
        response_format={"type": "json_object"},
        temperature=0.1,
    )
    return json.loads(response.choices[0].message.content)


# ──────────────────────────────────────────────────────────────────────────────
# CSV generation
# ──────────────────────────────────────────────────────────────────────────────

def build_headers(inputs: list, outputs: list, include_flag: bool) -> List[str]:
    headers = ['Test_ID', 'Delay_ms', 'Description']
    for v in inputs:
        headers.append(f"Input_{v['name']} ({v['address']})")
    for v in outputs:
        headers.append(f"Expected_{v['name']} ({v['address']})")
    if include_flag:
        headers.append('AutoInserted')
    return headers


def save_csv(
    path: str,
    inputs: list,
    outputs: list,
    test_cases: list,
    include_flag: bool = True,
) -> None:
    headers      = build_headers(inputs, outputs, include_flag)
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
            if include_flag:
                row.append(1 if tc.get('_is_reset_row') else 0)
            writer.writerow(row)


# ──────────────────────────────────────────────────────────────────────────────
# CLI entry point
# ──────────────────────────────────────────────────────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser(
        description='Generate PLC test cases from a TESTABLE ST file using OpenAI.'
    )
    ap.add_argument('st_file',
                    help='Path to the TESTABLE Structured Text (.st) file')
    ap.add_argument('-o', '--output', default=None,
                    help='Output CSV path (default: test_cases_<stem>.csv)')
    ap.add_argument('--num-tests', type=int, default=32,
                    help='Number of AI-generated test cases (default: 32)')
    ap.add_argument('--model', default='gpt-4o',
                    help='OpenAI model (default: gpt-4o)')
    ap.add_argument('--max-retries', type=int, default=2,
                    help='Max AI retry rounds when corrections are found (default: 2)')
    ap.add_argument('--plc-scan-ms', type=int, default=DEFAULT_PLC_SCAN_MS,
                    help=f'PLC task scan interval in ms (default: {DEFAULT_PLC_SCAN_MS}). '
                         'delay_ms will never go below this value.')
    ap.add_argument('--no-flag', action='store_true',
                    help='Omit the AutoInserted flag column from the CSV')
    ap.add_argument('--quiet', action='store_true',
                    help='Suppress per-correction detail output')
    args = ap.parse_args()

    # ── Validate inputs ───────────────────────────────────────────────────────
    st_path = Path(args.st_file)
    if not st_path.exists():
        print(f"Error: ST file not found: {st_path}")
        sys.exit(1)

    api_key = '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'
    if not api_key:
        print("Error: OPENAI_API_KEY environment variable not set.")
        print("  export OPENAI_API_KEY='sk-...'")
        sys.exit(1)

    output_path = (Path(args.output) if args.output
                   else st_path.parent / f"test_cases_{st_path.stem}.csv")

    # ── Parse ST file ─────────────────────────────────────────────────────────
    print(f"Reading:   {st_path}")
    st_code = st_path.read_text(encoding='utf-8')

    parser = STParser(st_code)
    parser.parse()
    evaluator = STEvaluator(parser)

    print(f"Program:   {parser.program_name}")
    if not parser.has_explicit_io:
        print("Error: No I/O variables with AT address bindings found.")
        print("  Run 5_st_to_testable_converter.py first.")
        sys.exit(1)

    print(f"Inputs:    {', '.join(v['name']+' ('+v['address']+')' for v in parser.inputs)}")
    print(f"Outputs:   {', '.join(v['name']+' ('+v['address']+')' for v in parser.outputs)}")
    if parser.constants:
        print(f"Constants: {', '.join(k+'='+str(v) for k, v in parser.constants.items())}")
    for inst, info in parser.timer_instances.items():
        print(f"Timer:     {inst} ({info['type']})  PT={info['pt_ms']} ms  "
              f"IN={info['in_expr']!r}")

    plc_scan_ms = args.plc_scan_ms
    print(f"PLC scan:  {plc_scan_ms} ms  (minimum delay_ms)")

    n_evaluable = len(evaluator._output_exprs) + len(evaluator._ton_outputs)
    print(f"Evaluator: {n_evaluable}/{len(parser.outputs)} outputs can be validated locally")

    # ── AI call + retry loop ──────────────────────────────────────────────────
    client           = OpenAI(api_key=api_key)
    failed_for_retry = None
    test_cases: List[dict] = []
    total_corrections = 0

    for attempt in range(1, args.max_retries + 2):
        print(f"\n[Attempt {attempt}]")
        try:
            ai_result = call_openai(
                client, args.model, parser, args.num_tests,
                plc_scan_ms, failed_for_retry,
            )
        except json.JSONDecodeError as e:
            print(f"Error: OpenAI returned invalid JSON: {e}")
            sys.exit(1)
        except Exception as e:
            print(f"Error calling OpenAI: {e}")
            sys.exit(1)

        raw_cases = ai_result.get('test_cases', [])
        if not raw_cases:
            print("Error: OpenAI returned 0 test cases.")
            sys.exit(1)

        # Renumber AI output sequentially
        for i, tc in enumerate(raw_cases, 1):
            tc['test_id'] = i

        # ── Fix sub-scan delays and insert timer-reset rows ────────────────
        raw_cases = enforce_timing_and_insert_resets(
            raw_cases, parser, evaluator, plc_scan_ms
        )

        # ── Validate and correct expected outputs ──────────────────────────
        raw_cases, corrections = validate_and_correct(
            raw_cases, parser, evaluator, verbose=not args.quiet
        )
        total_corrections += corrections
        test_cases = raw_cases

        still_wrong = [
            {
                'test_id'        : tc['test_id'],
                'description'    : tc['description'],
                'inputs'         : tc.get('inputs', {}),
                'ai_outputs'     : dict(tc.get('expected_outputs', {})),
                'correct_outputs': dict(tc.get('expected_outputs', {})),
            }
            for tc in test_cases
            if tc.get('_corrected_vars') and not tc.get('_is_reset_row')
        ]

        n_resets = sum(1 for tc in test_cases if tc.get('_is_reset_row'))
        print(f"  Corrections: {corrections}   Reset rows inserted: {n_resets}")

        if not still_wrong or attempt > args.max_retries:
            break

        print(f"  {len(still_wrong)} AI values were corrected — retrying …")
        failed_for_retry = still_wrong

    # ── Write CSV ─────────────────────────────────────────────────────────────
    save_csv(str(output_path), parser.inputs, parser.outputs, test_cases,
             include_flag=not args.no_flag)

    n_resets = sum(1 for tc in test_cases if tc.get('_is_reset_row'))
    print(f"\nTotal rows : {len(test_cases)}  "
          f"({len(test_cases) - n_resets} tests + {n_resets} auto-reset rows)")
    print(f"Corrected  : {total_corrections} output values (local evaluator overrides)")
    print(f"Saved      : {output_path}")
    print()
    print("Next steps:")
    print(f"  1. Load '{st_path.name}' on the PLC runtime.")
    print(f"  2. Run:  python3 test_generators/test_generator.py -f {output_path}")


if __name__ == '__main__':
    main()