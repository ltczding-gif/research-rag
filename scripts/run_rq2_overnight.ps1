$PYTHON = "C:\Users\Link\AppData\Local\Programs\Python\Python311\python.exe"
$PYTHON_RAG = "C:\Users\Link\.localrag\venv\Scripts\python.exe"

$env:LOCALRAG_BENCHMARK_PYTHON_RAG = $PYTHON_RAG
$RUNNER = Join-Path $PSScriptRoot "..\benchmarks\scripts\run_researchqa_overnight.py"

& $PYTHON $RUNNER @args
exit $LASTEXITCODE
