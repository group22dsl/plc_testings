#!/usr/bin/env python3
"""
Local Mutation Testing for IEC 61131-3 ST Output/Timer Expressions
=====================================================================
Generates single-fault mutants of every output/timer-IN expression in the
ST program using three classic operators:

  - ROR (Relational Operator Replacement): >= -> {>, <=},  <= -> {<, >=},
    > -> {>=, <},  < -> {<=, >},  = -> {<>},  <> -> {=}
  - LOR (Logical Operator Replacement): AND -> OR,  OR -> AND
  - AOR (Arithmetic Operator Replacement): binary + -> -,  binary - -> +,
    * -> //,  // -> *  (division uses '//' because that is the only
    division form the local evaluator's _parse_mul supports)

For each mutant, the local STEvaluator (ai/st_model.py) re-evaluates every
row of a given CSV test suite (with the same TON/TOF state stepping used
by the AI augmentation pipeline) and compares outputs against the
un-mutated program. A mutant is KILLED if at least one test row produces a
different output value; otherwise it SURVIVES.

    Mutation score = killed_mutants / total_mutants

This lets a human-written suite and an AI-augmented combined suite be
compared for fault-detection effectiveness (thesis section 9.5) without
needing to redeploy each mutant to OpenPLC.

Usage:
    python3 test_generators/mutation_tester.py <st_file> <csv1> [<csv2> ...] \\
        [--save-csv mutation_report.csv] [--verbose]
"""
import sys
import csv
import re
import argparse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'ai'))
from st_model import STParser, STEvaluator  # noqa: E402
from st_suite_utils import load_test_csv, replay_suite  # noqa: E402

ROR_MAP = {
    '>=': ['>', '<='],
    '<=': ['<', '>='],
    '>':  ['>=', '<'],
    '<':  ['<=', '>'],
    '=':  ['<>'],
    '<>': ['='],
}
# Order matters: check 2-char operators before their 1-char prefixes.
ROR_OPS_ORDERED = ['>=', '<=', '<>', '>', '<', '=']

AOR_MAP = {
    '+':  ['-'],
    '-':  ['+'],
    '*':  ['//'],
    '//': ['*'],
}


def _find_all_ops(expr, op):
    """Find start indices of every standalone occurrence of relational
    operator `op` in expr, skipping matches that are part of a longer
    operator (e.g. skip '=' inside '<=' / '>=' / '<>')."""
    idxs = []
    start = 0
    while True:
        idx = expr.find(op, start)
        if idx == -1:
            break
        before = expr[idx - 1] if idx > 0 else ''
        after = expr[idx + len(op):idx + len(op) + 1]
        if op == '=' and before in ('<', '>'):
            start = idx + 1
            continue
        if op in ('<', '>') and after == '=':
            start = idx + 1
            continue
        idxs.append(idx)
        start = idx + len(op)
    return idxs


_UNARY_CONTEXT_CHARS = ('', '(', ',', '+', '-', '*', '/', '<', '>', '=')
_UNARY_CONTEXT_KEYWORDS = re.compile(r'(?:\bAND\b|\bOR\b|\bNOT\b)\s*$', re.IGNORECASE)


def _find_all_binary_pm(expr):
    """Find (idx, op) for every BINARY +/- occurrence in expr (i.e. excluding
    unary sign uses such as a leading '-5' or '(-TSP)'), by checking whether
    the nearest preceding non-space character is itself an operator/opening
    paren/keyword (in which case the +/- is unary and must not be mutated)."""
    idxs = []
    for i, c in enumerate(expr):
        if c not in '+-':
            continue
        j = i - 1
        while j >= 0 and expr[j] == ' ':
            j -= 1
        prev = expr[j] if j >= 0 else ''
        if prev in _UNARY_CONTEXT_CHARS:
            continue
        if _UNARY_CONTEXT_KEYWORDS.search(expr[:i]):
            continue
        idxs.append((i, c))
    return idxs


def _find_all_mul_ops(expr):
    """Find (idx, op) for every standalone '*' (not part of '**') and every
    '//' in expr, mirroring STEvaluator._parse_mul's own split regex so
    mutants stay within the local evaluator's supported grammar."""
    idxs = []
    i, n = 0, len(expr)
    while i < n:
        if expr[i:i+2] == '//':
            idxs.append((i, '//'))
            i += 2
            continue
        if expr[i] == '*':
            prev = expr[i-1] if i > 0 else ''
            nxt = expr[i+1] if i+1 < n else ''
            if prev != '*' and nxt != '*':
                idxs.append((i, '*'))
        i += 1
    return idxs


def generate_mutants(parser):
    """Return a list of mutant dicts:
    {'loc_type': 'output'|'timer_in', 'loc_name': str,
     'operator': str, 'original': str, 'mutant': str}
    """
    mutants = []
    ev = STEvaluator(parser)
    targets = [('output', name, expr) for name, expr in ev._output_exprs.items()]
    targets += [('timer_in', inst_name, inst['in_expr']) for inst_name, inst in parser.timer_instances.items()]

    for loc_type, loc_name, expr in targets:
        # --- ROR: relational operator replacement ---
        for op in ROR_OPS_ORDERED:
            for idx in _find_all_ops(expr, op):
                for repl in ROR_MAP[op]:
                    mutant_expr = expr[:idx] + repl + expr[idx + len(op):]
                    mutants.append({'loc_type': loc_type, 'loc_name': loc_name,
                                     'operator': f'ROR:{op}->{repl}', 'original': expr,
                                     'mutant': mutant_expr})
        # --- LOR: logical operator replacement ---
        for kw, repl in (('AND', 'OR'), ('OR', 'AND')):
            for m in re.finditer(rf'\b{kw}\b', expr, re.IGNORECASE):
                mutant_expr = expr[:m.start()] + repl + expr[m.end():]
                mutants.append({'loc_type': loc_type, 'loc_name': loc_name,
                                 'operator': f'LOR:{kw}->{repl}', 'original': expr,
                                 'mutant': mutant_expr})
        # --- AOR: arithmetic operator replacement ---
        for idx, op in _find_all_binary_pm(expr):
            for repl in AOR_MAP[op]:
                mutant_expr = expr[:idx] + repl + expr[idx + len(op):]
                mutants.append({'loc_type': loc_type, 'loc_name': loc_name,
                                 'operator': f'AOR:{op}->{repl}', 'original': expr,
                                 'mutant': mutant_expr})
        for idx, op in _find_all_mul_ops(expr):
            for repl in AOR_MAP[op]:
                mutant_expr = expr[:idx] + repl + expr[idx + len(op):]
                mutants.append({'loc_type': loc_type, 'loc_name': loc_name,
                                 'operator': f'AOR:{op}->{repl}', 'original': expr,
                                 'mutant': mutant_expr})
    return mutants


def apply_mutant_and_eval(parser, mutant, rows):
    """Temporarily patch the target expression, replay the suite, restore."""
    ev = STEvaluator(parser)
    if mutant['loc_type'] == 'output':
        original = ev._output_exprs[mutant['loc_name']]
        ev._output_exprs[mutant['loc_name']] = mutant['mutant']
        try:
            return replay_suite(parser, ev, rows)
        finally:
            ev._output_exprs[mutant['loc_name']] = original
    else:
        original = parser.timer_instances[mutant['loc_name']]['in_expr']
        parser.timer_instances[mutant['loc_name']]['in_expr'] = mutant['mutant']
        try:
            return replay_suite(parser, ev, rows)
        finally:
            parser.timer_instances[mutant['loc_name']]['in_expr'] = original


def run_mutation_analysis(st_path, csv_path):
    st_code = Path(st_path).read_text(encoding='utf-8')
    parser = STParser(st_code)
    parser.parse()
    baseline_ev = STEvaluator(parser)
    rows = load_test_csv(csv_path, parser)
    baseline_outputs = replay_suite(parser, baseline_ev, rows)

    mutants = generate_mutants(parser)
    killed = 0
    details = []
    for mutant in mutants:
        mutant_outputs = apply_mutant_and_eval(parser, mutant, rows)
        is_killed = any(
            any(mutant_outputs[i].get(k) != baseline_outputs[i].get(k) for k in baseline_outputs[i])
            for i in range(len(rows))
        )
        if is_killed:
            killed += 1
        details.append({**mutant, 'killed': is_killed})

    score = (100.0 * killed / len(mutants)) if mutants else 0.0
    return {
        'csv': str(csv_path),
        'program': parser.program_name,
        'n_tests': len(rows),
        'total_mutants': len(mutants),
        'killed_mutants': killed,
        'mutation_score_pct': score,
        'details': details,
    }


def print_report(result, verbose=False):
    print(f"\n=== {result['csv']} ===")
    print(f"Program           : {result['program']}")
    print(f"Test rows         : {result['n_tests']}")
    print(f"Mutants generated : {result['total_mutants']}")
    print(f"Mutants killed    : {result['killed_mutants']}")
    print(f"Mutation score    : {result['mutation_score_pct']:.1f}%")
    if verbose:
        for d in result['details']:
            status = 'KILLED' if d['killed'] else 'SURVIVED'
            print(f"  [{status:8s}] {d['loc_name']:<20s} {d['operator']:<14s} "
                  f"'{d['original']}' -> '{d['mutant']}'")


def main():
    ap = argparse.ArgumentParser(description='Local mutation-score analyzer for ST test suites')
    ap.add_argument('st_file')
    ap.add_argument('csv_files', nargs='+')
    ap.add_argument('--save-csv', default=None, help='Save a summary comparison table to this CSV path')
    ap.add_argument('--verbose', action='store_true', help='List every mutant and its killed/survived status')
    args = ap.parse_args()

    results = [run_mutation_analysis(args.st_file, c) for c in args.csv_files]
    for r in results:
        print_report(r, verbose=args.verbose)

    if args.save_csv:
        with open(args.save_csv, 'w', newline='', encoding='utf-8') as f:
            w = csv.writer(f)
            w.writerow(['CSV', 'Tests', 'Total_Mutants', 'Killed_Mutants', 'Mutation_Score_Pct'])
            for r in results:
                w.writerow([r['csv'], r['n_tests'], r['total_mutants'], r['killed_mutants'],
                            f"{r['mutation_score_pct']:.1f}"])
        print(f"\nSaved summary: {args.save_csv}")


if __name__ == '__main__':
    main()
