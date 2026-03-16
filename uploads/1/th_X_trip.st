(*
  Generated from: th_X_trip.xml
  Converter: FBD to ST
*)

PROGRAM th_X_trip
VAR
    f_X             AT %QW1   : INT;    (* Analog input  → Modbus HR 1, writable for testing *)
    f_Module_Error  AT %QX1.0 : BOOL;   (* Boolean input → Modbus coil 8, writable for testing *)
    f_Channel_Error AT %QX1.1 : BOOL;   (* Boolean input → Modbus coil 9, writable for testing *)
    tx_X_Logic_Trip AT %QX1.2 : BOOL;   (* Boolean input → Modbus coil 10, writable for testing *)
    tx_X_trip       AT %QX0.0 : BOOL;   (* Output        → Modbus coil 0, readable *)
END_VAR

VAR CONSTANT
    K_X_Min : INT := -55;
    k_X_Max : INT := 125;
END_VAR

(* Logic from FBD *)
tx_X_trip := ((NOT ((f_X >= K_X_Min) AND (f_X <= k_X_Max)) OR f_Module_Error) OR (f_Channel_Error OR tx_X_Logic_Trip));
END_PROGRAM

(* OpenPLC Configuration *)
CONFIGURATION Config0

  RESOURCE Res0 ON PLC
    TASK task0(INTERVAL := T#20ms,PRIORITY := 0);
    PROGRAM instance0 WITH task0 : th_X_trip;
  END_RESOURCE

END_CONFIGURATION