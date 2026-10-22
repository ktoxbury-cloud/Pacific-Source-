#!/bin/bash
# Double-click to open the repair scheduler (Mac).
cd "$(dirname "$0")"
python3 -m kodiak_scheduler serve
