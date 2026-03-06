PROGRAM PLC_PRG
VAR
    (* Binary inputs - 4 bits *)
    B3 AT %IX0.3 : BOOL := FALSE;
    B2 AT %IX0.2 : BOOL := FALSE;
    B1 AT %IX0.1 : BOOL := FALSE;
    B0 AT %IX0.0 : BOOL := FALSE;
END_VAR
VAR
    (* Gray code outputs - 4 bits *)
    D3 AT %QX0.3 : BOOL := FALSE;
    D2 AT %QX0.2 : BOOL := FALSE;
    D1 AT %QX0.1 : BOOL := FALSE;
    D0 AT %QX0.0 : BOOL := FALSE;
END_VAR

(* Binary to Gray Code Conversion *)
(* Algorithm:
   D3 = B3
   D2 = B3 XOR B2
   D1 = B2 XOR B1
   D0 = B1 XOR B0
*)

(* MSB of Gray code is same as MSB of Binary *)
D3 := B3;

(* Other bits are XOR of adjacent Binary bits *)
D2 := B3 XOR B2;
D1 := B2 XOR B1;
D0 := B1 XOR B0;

END_PROGRAM

CONFIGURATION Config0
    RESOURCE Res0 ON PLC
        TASK MainTask(INTERVAL := T#100ms, PRIORITY := 0);
        PROGRAM Instance0 WITH MainTask : PLC_PRG;
    END_RESOURCE
END_CONFIGURATION
