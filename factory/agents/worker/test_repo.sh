#!/bin/sh
set -eu
for suite in gates dispatcher runtime acceptance; do
  python3 -m unittest discover -s "factory/$suite" -p 'test_*.py'
done
python3 -m unittest discover -s factory/agents/planning -p 'test_*.py'
python3 -m unittest discover -s factory/agents/worker -p 'test_*.py'
