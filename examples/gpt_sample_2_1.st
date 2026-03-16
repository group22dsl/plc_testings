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
VAR_INPUT
    iStartPB        : BOOL;   (* Start Push Button *)
    iStopPB         : BOOL;   (* Stop Push Button *)
    iOverload       : BOOL;   (* Motor overload signal *)
END_VAR


(*
===============================================================================
Output Variables
===============================================================================
*)
VAR_OUTPUT
    qMotorRun       : BOOL;   (* Motor Run Command *)
    qMotorFault     : BOOL;   (* Fault Indicator *)
END_VAR


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