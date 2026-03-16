(*
Pump Control Example
Compatible with OpenPLC / MATIEC
*)

PROGRAM PRG_PumpControl
VAR
    StartButton AT %QX1.0 : BOOL;
    StopButton AT %QX1.1 : BOOL;
    EmergencyStop AT %QX1.2 : BOOL;
    OverloadTrip AT %QX1.3 : BOOL;
    PumpMotor AT %QX0.0 : BOOL;
    Alarm AT %QX0.1 : BOOL;
END_VAR
VAR
    PumpRunLatch    : BOOL;
    StartEdge       : R_TRIG;
    RunTimer        : TON;
    MaxRunTime      : TIME := T#10m;
END_VAR


StartEdge(CLK := StartButton);

IF StartEdge.Q AND NOT EmergencyStop AND NOT OverloadTrip THEN
    PumpRunLatch := TRUE;
END_IF;

IF StopButton OR EmergencyStop OR OverloadTrip THEN
    PumpRunLatch := FALSE;
END_IF;

RunTimer(
    IN := PumpRunLatch,
    PT := MaxRunTime
);

IF RunTimer.Q THEN
    PumpRunLatch := FALSE;
END_IF;

PumpMotor := PumpRunLatch;

Alarm := EmergencyStop OR OverloadTrip;

END_PROGRAM



CONFIGURATION Config0

RESOURCE Res0 ON PLC
TASK MainTask(INTERVAL := T#50ms, PRIORITY := 0);
PROGRAM PumpInstance WITH MainTask : PRG_PumpControl;

END_RESOURCE

END_CONFIGURATION