(*
  Generated from: th_X_trip.xml
  Converter: FBD to ST
*)

PROGRAM th_X_trip
VAR_INPUT
    f_X : INT;
    f_Module_Error : BOOL;
    f_Channel_Error : BOOL;
    tx_X_Logic_Trip : BOOL;
END_VAR

VAR_OUTPUT
    tx_X_trip : BOOL;
END_VAR

VAR CONSTANT
    K_X_Min : INT := -55;
    k_X_Max : INT := 125;
END_VAR

(* Logic from FBD *)
tx_X_trip := ((NOT ((f_X >= K_X_Min) AND (f_X <= k_X_Max)) OR f_Module_Error) OR (f_Channel_Error OR tx_X_Logic_Trip));
END_PROGRAM
