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
echo "=== analysis endpoint tests (unit always; integ needs :8010; live needs RUN_LLM=1) ==="
BARRA_API="${BARRA_API:-http://127.0.0.1:8010}" $PY test_analysis.py; r4=$?
echo
echo "=== desk-limit tests (unit always; integ needs :8010) ==="
BARRA_API="${BARRA_API:-http://127.0.0.1:8010}" $PY test_limits.py; r5=$?
echo
echo "=== data-quality tests (unit always; integ needs :8010) ==="
BARRA_API="${BARRA_API:-http://127.0.0.1:8010}" $PY test_dq.py; r6=$?
echo
echo "=== VaR backtest tests (unit always; integ needs :8010) ==="
BARRA_API="${BARRA_API:-http://127.0.0.1:8010}" $PY test_backtest.py; r7=$?
echo
echo "=== trends endpoint tests (integ needs :8010) ==="
BARRA_API="${BARRA_API:-http://127.0.0.1:8010}" $PY test_trends.py; r8=$?
echo
echo "=== in-UI docs tests (unit builds the report; integ needs the UI on :8502) ==="
BARRA_UI="${BARRA_UI:-http://127.0.0.1:8502}" $PY test_docs.py; r9=$?
echo
echo "=== stress tests (custom & reverse; integ needs :8010) ==="
BARRA_API="${BARRA_API:-http://127.0.0.1:8010}" $PY test_stress.py; r10=$?
echo
echo "=== drawdown tests (unit always; integ needs :8010) ==="
BARRA_API="${BARRA_API:-http://127.0.0.1:8010}" $PY test_drawdown.py; r11=$?
echo
echo "=== pre-trade what-if tests (integ needs :8010) ==="
BARRA_API="${BARRA_API:-http://127.0.0.1:8010}" $PY test_whatif.py; r12=$?
echo
echo "=== universe membership tests (unit always; integ needs :8010 + built artifact) ==="
BARRA_API="${BARRA_API:-http://127.0.0.1:8010}" $PY test_universe.py; r13=$?
echo
echo "=== universe funnel tests (unit always; integ needs :8010 + built artifact) ==="
BARRA_API="${BARRA_API:-http://127.0.0.1:8010}" $PY test_funnel.py; r14=$?
echo
[ $r1 -eq 0 ] && [ $r2 -eq 0 ] && [ $r3 -eq 0 ] && [ $r4 -eq 0 ] && [ $r5 -eq 0 ] && [ $r6 -eq 0 ] && [ $r7 -eq 0 ] && [ $r8 -eq 0 ] && [ $r9 -eq 0 ] && [ $r10 -eq 0 ] && [ $r11 -eq 0 ] && [ $r12 -eq 0 ] && [ $r13 -eq 0 ] && [ $r14 -eq 0 ] && echo "ALL GREEN" || echo "FAILURES (repo=$r1 ui=$r2 measures=$r3 analysis=$r4 limits=$r5 dq=$r6 backtest=$r7 trends=$r8 docs=$r9 stress=$r10 drawdown=$r11 whatif=$r12 universe=$r13 funnel=$r14)"
exit $(( r1 || r2 || r3 || r4 || r5 || r6 || r7 || r8 || r9 || r10 || r11 || r12 || r13 || r14 ))
