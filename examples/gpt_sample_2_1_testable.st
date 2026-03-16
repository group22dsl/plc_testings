(*
===============================================================================
Program Name : PRG_MotorControl
Author       : Automation Team
Company      : Example Automation AB
Date         : 2026-03-15
Description  : Standard motor control logic with start/stop, overload
               monitoring and runtime timer.

Change Log
-------------------------------------------------------------------------------
Version | Date       | Author | Description
1.0     | 2026-03-15 | Team   | Initial version
===============================================================================
*)

PROGRAM PRG_MotorControl

(*
===============================================================================
Constant Definitions
===============================================================================
*)
VAR CONSTANT
    cRunDelayTime : TIME := T#2s;     (* Delay before motor run confirmation *)
END_VAR

(*
===============================================================================
Input Variables
===============================================================================
*)
VAR
    iStartPB AT %QX1.0 : BOOL;  (* Start Push Button *)
    iStopPB AT %QX1.1 : BOOL;  (* Stop Push Button *)
    iOverload AT %QX1.2 : BOOL;  (* Motor overload signal *)
    qMotorRun AT %QX0.0 : BOOL;  (* Motor Run Command *)
    qMotorFault AT %QX0.1 : BOOL;  (* Fault Indicator *)
END_VAR

(*
===============================================================================
Output Variables
===============================================================================
*)

(*
===============================================================================
Internal Variables
===============================================================================
*)
VAR
    rStartEdge      : R_TRIG;     (* Rising edge detection *)
    tRunConfirm     : TON;        (* Run confirmation timer *)

    bRunRequest     : BOOL;       (* Internal run request *)
END_VAR

(*
===============================================================================
Main Logic
===============================================================================
*)

(* Detect Start Button Rising Edge *)
rStartEdge(CLK := iStartPB);

(* Run Request Latch *)
IF rStartEdge.Q THEN
    bRunRequest := TRUE;
END_IF;

(* Stop condition *)
IF iStopPB OR iOverload THEN
    bRunRequest := FALSE;
END_IF;

(* Fault Detection *)
qMotorFault := iOverload;

(* Run Confirmation Timer *)
tRunConfirm(
    IN := bRunRequest AND NOT qMotorFault,
    PT := cRunDelayTime
);

(* Motor Output *)
qMotorRun := tRunConfirm.Q;

END_PROGRAM

(* OpenPLC Configuration *)
CONFIGURATION Config0

  RESOURCE Res0 ON PLC
    TASK task0(INTERVAL := T#20ms, PRIORITY := 0);
    PROGRAM instance0 WITH task0 : PRG_MotorControl;
  END_RESOURCE

END_CONFIGURATION
