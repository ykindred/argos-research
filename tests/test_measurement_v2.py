"""The fixture owns its instrumentation; candidate self-report is not evidence."""

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

FIXTURE = Path(__file__).resolve().parents[1] / "examples/synthetic-v2/repo"


@pytest.mark.parametrize("mode", ["baseline", "lying_counter", "set"])
def test_independent_meter_ignores_candidate_report(tmp_path, mode):
    shutil.copytree(FIXTURE, tmp_path / "repo")
    repo = tmp_path / "repo"
    source = (repo / "algorithm.py").read_text()
    if mode == "lying_counter":
        source = source.replace("return len(seen), comparisons", "return len(seen), 0")
    elif mode == "set":
        source = "def count_distinct(values):\n    return len(set(values)), 0\n"
    (repo / "algorithm.py").write_text(source)
    result = json.loads(subprocess.check_output([sys.executable, "-B", "evaluate.py"], cwd=repo))
    assert result["constraints"]["correctness"]
    metrics = result["metrics"]
    if mode in ("baseline", "lying_counter"):
        assert metrics["comparisons"]["value"] == 160000
        assert metrics["hash_calls"]["value"] == 0
    else:
        assert 0 < metrics["comparisons"]["value"] < 160000
        assert metrics["hash_calls"]["value"] == 800


def test_invalid_counts_fail_protected_correctness_cases(tmp_path):
    shutil.copytree(FIXTURE, tmp_path / "repo")
    repo = tmp_path / "repo"
    (repo / "algorithm.py").write_text("def count_distinct(values):\n    return 0, 0\n")
    result = subprocess.run([sys.executable, "-B", "evaluate.py"], cwd=repo, capture_output=True)
    assert result.returncode != 0
