PROGRAM PLC_PRG
VAR
    T_Red : TON;
    T_Yellow : TON;
    T_Green : TON;
    Red_Light : BOOL := TRUE;
    Yellow_Light : BOOL := FALSE;
    Green_Light : BOOL := FALSE;
    State : INT := 0;
END_VAR

(* Simple Traffic Light Controller *)
(* Red: 5s, Green: 5s, Yellow: 2s *)

CASE State OF
    0: (* Red Light State *)
        Red_Light := TRUE;
        Yellow_Light := FALSE;
        Green_Light := FALSE;
        T_Red(IN := TRUE, PT := T#5s);
        IF T_Red.Q THEN
            T_Red(IN := FALSE);
            State := 1;
        END_IF;
    
    1: (* Green Light State *)
        Red_Light := FALSE;
        Yellow_Light := FALSE;
        Green_Light := TRUE;
        T_Green(IN := TRUE, PT := T#5s);
        IF T_Green.Q THEN
            T_Green(IN := FALSE);
            State := 2;
        END_IF;
    
    2: (* Yellow Light State *)
        Red_Light := FALSE;
        Yellow_Light := TRUE;
        Green_Light := FALSE;
        T_Yellow(IN := TRUE, PT := T#2s);
        IF T_Yellow.Q THEN
            T_Yellow(IN := FALSE);
            State := 0;
        END_IF;
END_CASE;

END_PROGRAM

CONFIGURATION Config0
    RESOURCE Res0 ON PLC
        TASK MainTask(INTERVAL := T#100ms, PRIORITY := 0);
        PROGRAM Instance0 WITH MainTask : PLC_PRG;
    END_RESOURCE
END_CONFIGURATION

