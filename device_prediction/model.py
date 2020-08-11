#!/usr/bin/env python3
# vim: ts=4 sw=4 expandtab

import json
import sys
from enum import Enum

s_Reallocated_Sector_Count = '5'
s_Reported_Uncorrectable_Errors = '187'
s_Command_Timeout = '188'
s_Current_Pending_Sector_Count = '197'
s_Offline_Uncorrectable = '198'

class PreditionResult(Enum):
    GOOD = 0
    FAIL = 1

def simple_prediction(smart_data):
    if len(smart_data) == 0:
        return "Invalid prediction input"
    # Get most recent report
    report_date = sorted(smart_data.keys(), reverse=True)[0]
    report = smart_data[report_date]["attr"]
    if s_Reallocated_Sector_Count in report and report[s_Reallocated_Sector_Count]["val_raw"] > 0:
        return PreditionResult.FAIL
    return PreditionResult.GOOD

def main():
    inp_json = sys.stdin.read()
    smart_data = json.loads(inp_json)
    prediction_result = simple_prediction(smart_data)

    print(prediction_result)

if __name__ == '__main__':
    sys.exit(main())

