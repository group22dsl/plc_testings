"""
Shared helpers for replaying generated PLC test-case CSVs against the local
IEC 61131-3 ST evaluator (ai/st_model.py).

Used by:
  - coverage_analyzer.py  (statement / branch coverage)
  - mutation_tester.py    (mutation score)

Keeping this logic in one place ensures the coverage and mutation tools
interpret the CSV format (Input_<name> (%addr), Delay_ms, Test_ID columns)
and TON/TOF state stepping between rows in exactly the same way as the
AI augmentation pipeline (ai/ai_test_augmentation.py: validate_and_correct).
"""
import csv
import re


def load_test_csv(csv_path, parser):
    """Load a generated test CSV into a list of rows:
    [{'test_id': str, 'delay_ms': int, 'inputs': {name: value}}, ...]

    Matches columns named 'Input_<name> (...)' to the ST program's declared
    input variable names (parser.inputs).
    """
    rows = []
    input_names = {inp['name'] for inp in parser.inputs}
    with open(csv_path, newline='', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        fieldnames = reader.fieldnames or []
        input_cols = {}
        for col in fieldnames:
            m = re.match(r'Input_(\w+)', col)
            if m and m.group(1) in input_names:
                input_cols[col] = m.group(1)
        delay_col = next((c for c in fieldnames if 'delay' in c.lower()), None)
        id_col = next((c for c in fieldnames if c.lower() in ('test_id', 'test id', 'id')), None)

        for row in reader:
            inputs = {}
            for col, name in input_cols.items():
                raw = str(row.get(col, '0')).strip()
                try:
                    inputs[name] = int(float(raw))
                except ValueError:
                    inputs[name] = 0
            try:
                delay_ms = int(float(row[delay_col])) if delay_col and str(row.get(delay_col, '')).strip() else 100
            except ValueError:
                delay_ms = 100
            rows.append({
                'test_id': row.get(id_col, '') if id_col else '',
                'delay_ms': delay_ms,
                'inputs': inputs,
            })
    return rows


def replay_suite(parser, evaluator, rows):
    """Sequentially evaluate every row's expected outputs, carrying TON Q
    state forward between rows exactly like
    ai/ai_test_augmentation.py: validate_and_correct().

    Returns a list of per-row output dicts (name -> value) aligned with `rows`.
    """
    ton_q_state = {inst: 0 for inst in parser.timer_instances}
    outputs = []
    for row in rows:
        out = evaluator.evaluate_outputs(row['inputs'], row['delay_ms'], prev_ton_q=dict(ton_q_state))
        outputs.append(out)
        for inst_name, inst in parser.timer_instances.items():
            if inst.get('type', 'TON') != 'TON':
                continue
            ton_in = evaluator.timer_in_value(inst_name, row['inputs'])
            if ton_in is None:
                continue
            if not ton_in:
                ton_q_state[inst_name] = 0
            elif row['delay_ms'] >= inst['pt_ms']:
                ton_q_state[inst_name] = 1
    return outputs
