#!/usr/bin/env python3
"""
Random-Input Test Augmentation Baseline
========================================
Generates a size-matched RANDOM-input augmented test suite, so that it can
be compared against the LLM-augmented suite produced by
ai/ai_test_augmentation.py using exactly the same downstream machinery
(local ST evaluator, timer/reset handling, coverage analyzer, mutation
tester, OpenPLC round-trip).

This gives three test-selection conditions per program, all built on the
same human baseline and the same evaluator/CSV pipeline:

    HUMAN   = human tests only
    RANDOM  = human tests + N randomly-generated tests   (this script)
    LLM     = human tests + N LLM-generated tests         (ai_test_augmentation.py)

Only the *input-selection strategy* differs between RANDOM and LLM -- the
expected outputs for the randomly generated rows are never guessed; they
are computed by the same local IEC 61131-3 evaluator (ai/st_model.py) used
to validate/correct the LLM's proposed oracle values. This makes the random
condition a fair baseline for RQ1 (coverage / mutation-score comparison).
It is deliberately NOT a substitute for RQ3 (LLM oracle-accuracy), since a
random suite never proposes an oracle for validation.

Random-input domain
--------------------
BOOL inputs are sampled uniformly from {0, 1}.

INT-typed inputs are NOT sampled uniformly across the full IEC 61131-3
INT16 range [-32768, 32767]. For programs such as th_X_trip, where the
meaningful thresholds are -55 and 125, sampling uniformly across the full
INT16 range would almost never land near those thresholds, making the
"random" condition trivially weak by construction rather than by design.

Instead, the default per-variable sampling domain is:

    [min(human_test_values) - margin, max(human_test_values) + margin]

i.e. the range actually exercised by the human baseline, padded by a fixed
margin (--domain-margin, default 20). This domain is derived BEFORE looking
at any coverage/mutation results and does NOT use knowledge of the ST
logic's internal thresholds -- only the human-authored test data, which is
assumed to reflect the program's specification. A --domain-config JSON file
can instead supply an explicit, documented per-subject domain, e.g.:

    {"f_X": [-100, 200]}

Every domain actually used is written to a `<output>_domain_used.json`
sidecar file next to the generated CSV for exact reproducibility.

Reproducibility
----------------
Random generation is itself stochastic, so a single random suite should not
be compared against multiple LLM runs. Use --runs N (with --seed-base) to
generate N independently-seeded random suites per program, mirroring N LLM
runs, and record the seeds used (printed, and embedded in output filenames
and the domain sidecar file) in the thesis for exact reproducibility, e.g.:

    Random run 01 -> seed 1001
    Random run 02 -> seed 1002
    ...
    Random run 10 -> seed 1010

Usage
-----
  Size-matched to a specific AI combined CSV (recommended):
    python3 test_generators/random_test_augmentation.py \\
        examples/th_X_trip_testable.st \\
        --human-csv examples/th_X_trip_testable_human_formatted.csv \\
        --match-csv examples/th_X_trip_testable_combined.csv \\
        --seed 1001

  Explicit count, 10 independently-seeded runs (seeds 1001..1010):
    python3 test_generators/random_test_augmentation.py \\
        examples/th_X_trip_testable.st \\
        --human-csv examples/th_X_trip_testable_human_formatted.csv \\
        --num-extra 25 --runs 10 --seed-base 1001
"""

import sys
import csv
import json
import random
import argparse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'ai'))
from st_model import (  # noqa: E402
    DEFAULT_PLC_SCAN_MS, INT_MIN, INT_MAX, clamp_int,
    STParser, STEvaluator,
    enforce_timing_and_insert_resets,
    evaluate_expected_outputs,
    renumber_test_cases,
    save_test_csv,
    load_formatted_test_csv,
)


def count_ai_generated(csv_path):
    """Count rows from an AI-generated combined CSV with Source=ai and
    AutoInserted=0 (i.e. actual AI-authored tests, excluding auto-inserted
    timer-reset rows) -- used to size-match the random condition."""
    with open(csv_path, newline='', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        fieldnames = reader.fieldnames or []
        source_col = next((c for c in fieldnames if c.lower() == 'source'), None)
        auto_col = next((c for c in fieldnames if 'autoinsert' in c.lower()), None)
        count = 0
        for row in reader:
            src = (row.get(source_col, '') if source_col else '').strip().lower()
            is_auto = str(row.get(auto_col, '0')).strip() == '1' if auto_col else False
            if src == 'ai' and not is_auto:
                count += 1
    return count


def resolve_int_domains(parser, human_cases, margin, domain_config):
    """Resolve a [min, max] random-sampling domain for every non-BOOL input.

    Precedence:
      1. Explicit --domain-config override for that variable name.
      2. Range spanned by the human baseline's own values for that
         variable, padded by +/- `margin`, clamped to the INT16 range.
      3. Full INT16 range, ONLY if the human suite provides no signal at
         all for that variable (no defensible domain can be derived).
    """
    domains = {}
    for v in parser.inputs:
        if v['type'] == 'BOOL':
            continue
        name = v['name']
        if domain_config and name in domain_config:
            lo, hi = domain_config[name]
            domains[name] = (clamp_int(lo), clamp_int(hi))
            continue
        values = [tc['inputs'][name] for tc in human_cases
                  if not tc.get('_is_reset_row') and name in tc.get('inputs', {})]
        if values:
            lo, hi = min(values) - margin, max(values) + margin
        else:
            lo, hi = INT_MIN, INT_MAX
        domains[name] = (clamp_int(lo), clamp_int(hi))
    return domains


def generate_random_cases(parser, domains, num_extra, seed, plc_scan_ms):
    """Randomly sample only the INPUTS. Expected outputs are left empty here
    and filled in later purely by evaluate_expected_outputs() -- the random
    generator never guesses an oracle."""
    rng = random.Random(seed)
    cases = []
    for i in range(1, num_extra + 1):
        inputs = {}
        for v in parser.inputs:
            name = v['name']
            if v['type'] == 'BOOL':
                inputs[name] = rng.randint(0, 1)
            else:
                lo, hi = domains[name]
                inputs[name] = rng.randint(lo, hi)
        cases.append({
            'test_id': i,
            'delay_ms': max(100, plc_scan_ms),
            'description': f'[RANDOM seed={seed}] randomly sampled input combination #{i}',
            'inputs': inputs,
            'expected_outputs': {},
            '_is_reset_row': False,
            '_reset_timer': None,
            '_source': 'random',
        })
    return cases


def main():
    ap = argparse.ArgumentParser(
        description='Random-input test augmentation baseline (RQ1 coverage/mutation comparison)')
    ap.add_argument('st_file')
    ap.add_argument('--human-csv', required=True, metavar='CSV',
                    help='Existing *_human_formatted.csv baseline; reused unchanged as the human rows')
    ap.add_argument('--num-extra', type=int, default=None,
                    help='Explicit number of random test cases to generate per run')
    ap.add_argument('--match-csv', default=None, metavar='CSV',
                    help='An AI-generated combined CSV; derive --num-extra automatically as the count '
                         'of Source=ai rows with AutoInserted=0 (exact size matching). Overrides --num-extra.')
    ap.add_argument('--seed', type=int, default=None,
                    help='Explicit single seed for a one-off run (overrides --runs/--seed-base)')
    ap.add_argument('--runs', type=int, default=1,
                    help='Number of independently-seeded random suites to generate (default 1)')
    ap.add_argument('--seed-base', type=int, default=1001,
                    help='Base seed for --runs > 1: run k uses seed seed_base + (k-1) (default 1001)')
    ap.add_argument('-o', '--output', default=None,
                    help='Output combined CSV path (single-run only; default: '
                         '<stem>_random_combined_seed<seed>.csv next to the ST file)')
    ap.add_argument('--plc-scan-ms', type=int, default=DEFAULT_PLC_SCAN_MS)
    ap.add_argument('--domain-margin', type=int, default=20,
                    help='Margin added beyond the human input min/max to build the default INT '
                         'random-sampling domain (default 20; document the chosen value in the thesis)')
    ap.add_argument('--domain-config', default=None, metavar='JSON',
                    help='Optional JSON file {"var_name": [min, max], ...} of explicit per-subject '
                         'domain overrides, documented independently of the human test data')
    ap.add_argument('--no-flag', action='store_true')
    args = ap.parse_args()

    st_path = Path(args.st_file)
    if not st_path.exists():
        print(f"Error: ST file not found: {st_path}"); sys.exit(1)
    human_path = Path(args.human_csv)
    if not human_path.exists():
        print(f"Error: human-formatted CSV not found: {human_path}"); sys.exit(1)

    st_code = st_path.read_text(encoding='utf-8')
    parser = STParser(st_code); parser.parse()
    evaluator = STEvaluator(parser)

    print(f"Program:   {parser.program_name}")
    if not parser.has_explicit_io:
        print("Error: No I/O with AT addresses found. Run the ST-to-testable converter first."); sys.exit(1)
    print(f"Inputs:    {', '.join(v['name']+'('+v['type']+')' for v in parser.inputs)}")
    print(f"Outputs:   {', '.join(v['name'] for v in parser.outputs)}")
    for inst, info in parser.timer_instances.items():
        print(f"Timer:     {inst} ({info['type']})  PT={info['pt_ms']} ms  IN={info['in_expr']!r}")
    print(f"PLC scan:  {args.plc_scan_ms} ms")

    # ── num_extra ─────────────────────────────────────────────────────────
    num_extra = args.num_extra
    if args.match_csv:
        mc_path = Path(args.match_csv)
        if not mc_path.exists():
            print(f"Error: --match-csv not found: {mc_path}"); sys.exit(1)
        derived = count_ai_generated(str(mc_path))
        if num_extra is not None and num_extra != derived:
            print(f"  Note: --num-extra {num_extra} overridden by --match-csv derived count {derived}")
        num_extra = derived
        print(f"  Derived num_extra={num_extra} from AI-generated rows "
              f"(Source=ai, AutoInserted=0) in {mc_path}")
    if num_extra is None:
        print("Error: supply --num-extra N or --match-csv <ai_combined.csv>"); sys.exit(1)
    if num_extra <= 0:
        print(f"Error: num_extra must be > 0 (got {num_extra})"); sys.exit(1)

    # ── domain config ─────────────────────────────────────────────────────
    domain_config = None
    if args.domain_config:
        dc_path = Path(args.domain_config)
        if not dc_path.exists():
            print(f"Error: --domain-config not found: {dc_path}"); sys.exit(1)
        domain_config = json.loads(dc_path.read_text(encoding='utf-8'))

    # ── seeds ─────────────────────────────────────────────────────────────
    if args.seed is not None:
        seeds = [args.seed]
    else:
        seeds = [args.seed_base + i for i in range(args.runs)]
    if len(seeds) > 1 and args.output:
        print("Error: --output only supported for a single run (--runs 1 or explicit --seed)"); sys.exit(1)

    # ── Step 1: load human baseline (unchanged) ──────────────────────────
    print(f"\n[Step 1] Loading human baseline: {human_path}")
    human_cases = load_formatted_test_csv(str(human_path), parser)
    n_human_tests = sum(1 for tc in human_cases if not tc.get('_is_reset_row'))
    n_human_resets = sum(1 for tc in human_cases if tc.get('_is_reset_row'))
    print(f"  Loaded {len(human_cases)} rows ({n_human_tests} tests + {n_human_resets} auto-resets). Reused unchanged.")

    # ── Step 2: resolve the random-sampling domain (once, seed-independent) ─
    domains = resolve_int_domains(parser, human_cases, args.domain_margin, domain_config)
    if domains:
        print(f"\n[Domain] Random INT-input sampling domains (document these in the thesis):")
        for name, (lo, hi) in domains.items():
            src = 'domain-config override' if (domain_config and name in domain_config) \
                else f"human range +/- {args.domain_margin} margin"
            print(f"  {name}: [{lo}, {hi}]  ({src})")

    outputs_written = []
    for run_idx, seed in enumerate(seeds, start=1):
        print(f"\n[Step 3] Run {run_idx}/{len(seeds)} — generating {num_extra} random test cases (seed={seed}) ...")
        random_cases = generate_random_cases(parser, domains, num_extra, seed, args.plc_scan_ms)
        random_cases = enforce_timing_and_insert_resets(random_cases, parser, evaluator, args.plc_scan_ms)
        random_cases = evaluate_expected_outputs(random_cases, parser, evaluator)
        n_resets = sum(1 for tc in random_cases if tc.get('_is_reset_row'))
        print(f"  Generated {len(random_cases)} rows ({len(random_cases)-n_resets} tests + {n_resets} auto-resets).")

        combined = list(human_cases) + list(random_cases)
        renumber_test_cases(combined)

        if args.output:
            out_path = Path(args.output)
        elif len(seeds) > 1:
            out_path = st_path.parent / f"{st_path.stem}_random_combined_run{run_idx:02d}_seed{seed}.csv"
        else:
            out_path = st_path.parent / f"{st_path.stem}_random_combined_seed{seed}.csv"

        save_test_csv(str(out_path), parser.inputs, parser.outputs, combined,
                       include_flag=not args.no_flag, source_tag_col='Source')

        domain_out = out_path.with_name(f"{out_path.stem}_domain_used.json")
        with open(domain_out, 'w', encoding='utf-8') as f:
            json.dump({
                'program': parser.program_name,
                'seed': seed,
                'num_extra': num_extra,
                'domain_margin': args.domain_margin,
                'domain_config_used': bool(domain_config),
                'domains': {k: list(v) for k, v in domains.items()},
            }, f, indent=2)

        n_h, n_r, n_t = len(human_cases), len(random_cases), len(combined)
        print(f"  Output : {out_path}")
        print(f"  {n_t} rows total ({n_h} human + {n_r} random, incl. {n_human_resets+n_resets} auto-resets)")
        print(f"  Domain record : {domain_out}")
        outputs_written.append(out_path)

    print(f"\n{'='*60}")
    print(f"Wrote {len(outputs_written)} random-augmented suite(s) for {parser.program_name}:")
    for p in outputs_written:
        print(f"  {p}")
    print(f"\nNext steps (per suite):")
    print(f"  python3 test_generators/test_generator.py -f <combined_csv>")
    print(f"  python3 test_generators/coverage_analyzer.py {st_path} <human_csv> <combined_csv>")
    print(f"  python3 test_generators/mutation_tester.py {st_path} <human_csv> <combined_csv>")


if __name__ == '__main__':
    main()
