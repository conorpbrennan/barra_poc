#!/usr/bin/env bash
# Run the Flex Agg ++ test suite with the project venv.
#   ./run_tests.sh
# Repo tests run standalone; UI tests need risk_api on :8010 (else they SKIP).
set -u
cd "$(dirname "$0")"
PY=../barra/bin/python
echo "=== views_repo unit tests ==="
$PY test_views_repo.py; r1=$?
echo
echo "=== pivot app UI tests (AppTest) ==="
BARRA_API="${BARRA_API:-http://127.0.0.1:8010}" $PY test_pivot_app.py; r2=$?
echo
echo "=== risk measure (cube) tests ==="
BARRA_API="${BARRA_API:-http://127.0.0.1:8010}" $PY test_risk_measures.py; r3=$?
echo
[ $r1 -eq 0 ] && [ $r2 -eq 0 ] && [ $r3 -eq 0 ] && echo "ALL GREEN" || echo "FAILURES (repo=$r1 ui=$r2 measures=$r3)"
exit $(( r1 || r2 || r3 ))
