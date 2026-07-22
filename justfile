# aTorch DL24P Control — dev tasks (run `just` to list)

# List available recipes
default:
    @just --list

# Install/update dependencies (including dev extras)
sync:
    uv sync --extra dev

# Run the Test Bench app (device control + testing)
run:
    uv run python -m load_test_bench.main

# Run the Test Viewer (standalone, no device needed)
viewer:
    uv run python -m load_test_bench.viewer

# Run all tests (pass extra pytest args, e.g. `just test -v`)
test *args:
    uv run --extra dev pytest {{args}}

# Run a single test file, e.g. `just test-file test_protocol`
test-file name:
    uv run --extra dev pytest tests/{{ if name =~ '\.py$' { name } else { name + ".py" } }} -v

# Build the standalone app bundle (PyInstaller)
build:
    uv run python build.py

# Tail the device communication debug log
log:
    tail -f debug.log
