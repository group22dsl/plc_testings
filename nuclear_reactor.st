PROGRAM X_Trip_Program
  VAR_INPUT
    f_X AT %IW0 : INT;
    f_Module_Error AT %IX0.0 : BOOL;
    f_Channel_Error AT %IX0.1 : BOOL;
    th_X_Logic_Trip AT %IX0.2 : BOOL;
  END_VAR

  VAR_OUTPUT
    th_X_Trip AT %QX0.0 : BOOL;
  END_VAR

  VAR CONSTANT
    k_X_Min : INT := -55;
    k_X_Max : INT := 125;
  END_VAR

  VAR
    Is_In_Range : BOOL;
  END_VAR

  (* Logic Step 1: Check if the temperature is within the safe range.
    This corresponds to the GE (Greater or Equal), LE (Less or Equal), 
    and AND blocks in the diagram.
  *)
  Is_In_Range := (f_X >= k_X_Min) AND (f_X <= k_X_Max);

  (* Logic Step 2: Determine the trip condition.
    The NOT block inverses the range check (triggering if out of range).
    The OR blocks combine the out-of-range condition with the error and logic trip signals.
  *)
  th_X_Trip := (NOT Is_In_Range) OR f_Module_Error OR f_Channel_Error OR th_X_Logic_Trip;

END_PROGRAM

CONFIGURATION Config0
  RESOURCE Res0 ON PLC
    TASK task0(INTERVAL := T#20ms, PRIORITY := 0);
    PROGRAM instance0 WITH task0 : X_Trip_Program;
  END_RESOURCE
END_CONFIGURATION