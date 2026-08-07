#!/usr/bin/env python3
"""
Local Statement / Branch (Condition) Coverage Analyzer for ST test suites
===========================================================================
Computes coverage purely from the local ST evaluator (ai/st_model.py) —
no OpenPLC / Modbus connection required. Intended to compare a human-written
test suite against an AI-augmented combined suite for the same program
(thesis section 9.2 / 9.3).

Metrics reported per CSV:
  - Statement coverage : fraction of top-level ST statements exercised.
    NOTE: the example programs in this study are straight-line ST (no
    IF/CASE branching), so every statement executes every scan regardless
    of inputs -> statement coverage is trivially 100% for any non-empty
    suite (see thesis section 2.4). It is reported for completeness.
  - Decision/condition ("branch") coverage : for every atomic relational
    comparison (e.g. PV_OUT >= TSP) and every atomic boolean operand of an
    AND/OR/XOR/NOT expression found in the program's output/timer-IN
    expressions, whether the supplied test suite drives it to BOTH TRUE
    and FALSE at least once. This is the meaningful coverage metric for
    this class of program.

Usage:
    python3 test_generators/coverage_analyzer.py <st_file> <csv1> [<csv2> ...] \\
        [--save-csv coverage_report.csv]
"""
import sys
import csv
import re
import argparse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'ai'))
from st_model import STParser, STEvaluator  # noqa: E402
from st_suite_utils import load_test_csv  # noqa: E402


def strip_outer_parens(expr):
    expr = expr.strip()
    while expr.startswith('(') and expr.endswith(')'):
        depth = 0
        wraps = True
        for i, c in enumerate(expr):
            if c == '(':
                depth += 1
            elif c == ')':
                depth -= 1
                if depth == 0 and i != len(expr) - 1:
                    wraps = False
                    break
        if wraps:
            expr = expr[1:-1].strip()
        else:
            break
    return expr


def collect_decisions(ev, expr, env, out):
    """Recursively record {expr_text: set(bool outcomes observed)} for every
    atomic relational comparison and boolean operand inside `expr`."""
    expr = strip_outer_parens(expr)
    if not expr:
        return
    for kw in ('OR', 'XOR', 'AND'):
        parts = ev._split_kw(expr, kw)
        if len(parts) > 1:
            for p in parts:
                collect_decisions(ev, p, env, out)
            return
    if re.match(r'^NOT\s*\(', expr, re.IGNORECASE):
        inner = ev._paren_inner(expr[3:].strip())
        collect_decisions(ev, inner, env, out)
        val = ev._eval_expr(expr, env)
        if val is not None:
            out.setdefault(expr, set()).add(bool(val))
        return
    if re.match(r'^NOT\s+\w', expr, re.IGNORECASE):
        val = ev._eval_expr(expr, env)
        if val is not None:
            out.setdefault(expr, set()).add(bool(val))
        return
    for op in ('<>', '<=', '>=', '<', '>', '='):
        if ev._find_op(expr, op) is not None:
            val = ev._eval_expr(expr, env)
            if val is not None:
                out.setdefault(expr, set()).add(bool(val))
            return
    # bare boolean atom (e.g. a BOOL variable used directly as an AND/OR operand)
    val = ev._eval_expr(expr, env)
    if val in (0, 1):
        out.setdefault(expr, set()).add(bool(val))


def analyze(st_path, csv_path):
    st_code = Path(st_path).read_text(encoding='utf-8')
    parser = STParser(st_code)
    parser.parse()
    ev = STEvaluator(parser)
    rows = load_test_csv(csv_path, parser)

    exprs = list(ev._output_exprs.values())
    for inst in parser.timer_instances.values():
        exprs.append(inst['in_expr'])

    decisions = {}
    for row in rows:
        env = {inp['name']: row['inputs'].get(inp['name'], 0) for inp in parser.inputs}
        env.update(parser.constants)
        for expr in exprs:
            collect_decisions(ev, expr, env, decisions)

    n_decisions = len(decisions)
    branch_total = 2 * n_decisions
    branch_covered = sum(min(len(v), 2) for v in decisions.values())
    branch_pct = (100.0 * branch_covered / branch_total) if branch_total else 100.0

    stmt_total = len(parser.statements)
    stmt_covered = stmt_total if rows else 0
    stmt_pct = (100.0 * stmt_covered / stmt_total) if stmt_total else 100.0

    return {
        'program': parser.program_name,
        'csv': str(csv_path),
        'n_tests': len(rows),
        'statement_total': stmt_total,
        'statement_covered': stmt_covered,
        'statement_pct': stmt_pct,
        'decision_total': n_decisions,
        'branch_total': branch_total,
        'branch_covered': branch_covered,
        'branch_pct': branch_pct,
        'decisions': decisions,
    }


def print_report(result):
    print(f"\n=== {result['csv']} ===")
    print(f"Program                   : {result['program']}")
    print(f"Test rows                 : {result['n_tests']}")
    print(f"Statement coverage        : {result['statement_covered']}/{result['statement_total']} "
          f"({result['statement_pct']:.1f}%)")
    print(f"Branch/condition coverage : {result['branch_covered']}/{result['branch_total']} "
          f"({result['branch_pct']:.1f}%)")
    for expr, outcomes in result['decisions'].items():
        status = 'BOTH' if len(outcomes) == 2 else ('TRUE only' if True in outcomes else 'FALSE only')
        flag = '\u2713' if len(outcomes) == 2 else '\u2717'
        print(f"  {flag} {expr:<50s} -> {status}")


def main():
    ap = argparse.ArgumentParser(description='Local statement/branch coverage analyzer for ST test suites')
    ap.add_argument('st_file')
    ap.add_argument('csv_files', nargs='+')
    ap.add_argument('--save-csv', default=None, help='Save a summary comparison table to this CSV path')
    args = ap.parse_args()

    results = [analyze(args.st_file, c) for c in args.csv_files]
    for r in results:
        print_report(r)

    if args.save_csv:
        with open(args.save_csv, 'w', newline='', encoding='utf-8') as f:
            w = csv.writer(f)
            w.writerow(['CSV', 'Tests', 'Statement_Covered', 'Statement_Total', 'Statement_Pct',
                        'Branch_Covered', 'Branch_Total', 'Branch_Pct'])
            for r in results:
                w.writerow([r['csv'], r['n_tests'], r['statement_covered'], r['statement_total'],
                            f"{r['statement_pct']:.1f}", r['branch_covered'], r['branch_total'],
                            f"{r['branch_pct']:.1f}"])
        print(f"\nSaved summary: {args.save_csv}")


if __name__ == '__main__':
    main()
