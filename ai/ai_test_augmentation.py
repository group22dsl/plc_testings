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

from st_model import (
    DEFAULT_PLC_SCAN_MS, INT_MIN, INT_MAX, clamp_int, bool_int,
    STParser, STEvaluator,
    enforce_timing_and_insert_resets,
    renumber_test_cases as _renumber,
    save_test_csv as save_csv,
)

# ── ST Parser (moved to st_model.py, shared with test_generators/) ───────────
#
# STParser / STEvaluator, plus the timer/reset post-processing helpers
# (enforce_timing_and_insert_resets, renumber_test_cases, save_test_csv) now
# live in ai/st_model.py so that test_generators/coverage_analyzer.py,
# test_generators/mutation_tester.py and
# test_generators/random_test_augmentation.py can reuse the exact same local
# evaluation/CSV-formatting logic without importing this module (which
# requires the `openai` package).
# ── Post-processor ────────────────────────────────────────────────────────────

def validate_and_correct(test_cases, parser, evaluator, verbose=True, corrections_log=None):
    """Compare each AI-proposed expected output against the local ST
    evaluator's ground truth and overwrite it with the local value on
    mismatch. If `corrections_log` (a list) is supplied, one detailed
    record per mismatch is appended -- this is the raw data source for the
    oracle-error characterization in thesis sections 9.4/9.8 (RQ3/RQ4).
    """
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
                    if corrections_log is not None:
                        construct_type = 'timer (TON/TOF)' if name in evaluator._ton_outputs else 'combinational logic'
                        corrections_log.append({
                            'test_id': tc.get('test_id','?'),
                            'source': tc.get('_source',''),
                            'description': tc.get('description',''),
                            'variable': name,
                            'construct_type': construct_type,
                            'ai_value': ai_int,
                            'corrected_value': local,
                            'inputs': dict(inputs_vals),
                            'delay_ms': delay_ms,
                        })
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

def save_corrections_log(path, corrections_log):
    """Persist the detailed oracle-error log built by validate_and_correct()
    to CSV -- one row per AI expected-value mismatch (thesis RQ3/RQ4 data)."""
    headers = ['Test_ID', 'Source', 'Description', 'Variable', 'Construct_Type',
               'AI_Value', 'Corrected_Value', 'Inputs', 'Delay_ms']
    with open(path, 'w', newline='', encoding='utf-8') as f:
        w = csv.writer(f)
        w.writerow(headers)
        for rec in corrections_log:
            w.writerow([rec['test_id'], rec['source'], rec['description'], rec['variable'],
                        rec['construct_type'], rec['ai_value'], rec['corrected_value'],
                        json.dumps(rec['inputs']), rec['delay_ms']])


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
    ap.add_argument('--num-extra', type=int, default=None,
                    help='Explicit number of AI-generated test cases (Mode B: overrides the automatic '
                         '15-30 rule; Mode A: overrides the fixed default of 25). Use this to size-match '
                         'the AI condition against test_generators/random_test_augmentation.py for a fair '
                         'RQ1 comparison.')
    ap.add_argument('--no-flag', action='store_true')
    ap.add_argument('--quiet', action='store_true')
    args = ap.parse_args()

    st_path = Path(args.st_file)
    if not st_path.exists(): print(f"Error: ST file not found: {st_path}"); sys.exit(1)

    api_key = os.environ.get('OPENAI_API_KEY')
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

        if args.num_extra is not None:
            num_extra = args.num_extra
            print(f"  AI will generate {num_extra} new test cases (explicit --num-extra).")
        else:
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

        corrections_log = []
        for tc in human_raw: tc['_source'] = 'human'
        human_cases = enforce_timing_and_insert_resets(human_raw, parser, evaluator, plc_scan_ms)
        human_cases, corr = validate_and_correct(human_cases, parser, evaluator, verbose=not args.quiet,
                                                  corrections_log=corrections_log)
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
            raw, corr = validate_and_correct(raw, parser, evaluator, verbose=not args.quiet,
                                              corrections_log=corrections_log)
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

        corrections_out = st_path.parent / f"{st_path.stem}_oracle_corrections.csv"
        save_corrections_log(str(corrections_out), corrections_log)

        n_h, n_a, n_t = len(human_cases), len(ai_cases), len(combined)
        a_resets = sum(1 for tc in ai_cases if tc.get('_is_reset_row'))
        print(f"\n{'='*60}")
        print(f"Output 1 — Human formatted : {human_out}")
        print(f"  {n_h} rows ({n_h-h_resets} tests + {h_resets} auto-resets)")
        print(f"Output 2 — Combined        : {combined_out}")
        print(f"  {n_t} rows total ({n_h} human + {n_a} AI, incl. {h_resets+a_resets} auto-resets)")
        print(f"  Total AI corrections: {total_corr}")
        print(f"Output 3 — Oracle corrections log : {corrections_out}  ({len(corrections_log)} rows)")
        print(f"\nNext steps:")
        print(f"  1. Load '{st_path.name}' on the PLC runtime.")
        print(f"  2a. Human only:  python3 test_generators/test_generator.py -f {human_out}")
        print(f"  2b. Full suite:  python3 test_generators/test_generator.py -f {combined_out}")

    # ── MODE A ────────────────────────────────────────────────────────────────
    else:
        out_path = Path(args.output) if args.output else st_path.parent / f"test_cases_{st_path.stem}.csv"
        failed_retry = None; test_cases = []; total_corr = 0
        corrections_log = []
        num_tests_a = args.num_extra if args.num_extra is not None else 25  # fixed default for Mode A (ST-only, no manual CSV)
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
            raw, corr = validate_and_correct(raw, parser, evaluator, verbose=not args.quiet,
                                              corrections_log=corrections_log)
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
        corrections_out = st_path.parent / f"{st_path.stem}_oracle_corrections.csv"
        save_corrections_log(str(corrections_out), corrections_log)
        n_resets = sum(1 for tc in test_cases if tc.get('_is_reset_row'))
        print(f"\nTotal rows : {len(test_cases)} ({len(test_cases)-n_resets} tests + {n_resets} auto-resets)")
        print(f"Corrected  : {total_corr}")
        print(f"Saved      : {out_path}")
        print(f"Oracle corrections log : {corrections_out}  ({len(corrections_log)} rows)")

if __name__ == '__main__':
    main()
