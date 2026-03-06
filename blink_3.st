PROGRAM PLC_PRG
VAR
    T1 : TON;
END_VAR
VAR
    out AT %QX0.0 : BOOL;
END_VAR

(* Toggle out every 1 second *)
T1(IN := NOT T1.Q, PT := T#1s);
IF T1.Q THEN
    out := NOT out;
END_IF;

END_PROGRAM

CONFIGURATION Config0
    RESOURCE Res0 ON PLC
        TASK MainTask(INTERVAL := T#100ms, PRIORITY := 0);
        PROGRAM Instance0 WITH MainTask : PLC_PRG;
    END_RESOURCE
END_CONFIGURATION
