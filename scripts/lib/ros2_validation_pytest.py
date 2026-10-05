"""Exact pytest selection for pipeline subprocesses, including nested CTest pytest.

The control file is created per invocation. No user's pytest configuration or
installed package is modified, and selected/deselected node IDs are retained.
"""
import json
import os
from pathlib import Path

import pytest


@pytest.hookimpl(trylast=True)
def pytest_collection_modifyitems(config, items):
    control = os.environ.get('ROS2_VALIDATION_PYTEST_CONTROL')
    if not control:
        return
    settings = json.loads(Path(control).read_text())
    selected, omitted = [], []
    for item in items:
        # Parameter suffixes identify executions of the same exact test function.
        name = getattr(item, 'originalname', None) or item.name.split('[', 1)[0]
        lint = (name in settings['lint_tests'] or
                any(marker.name in settings['lint_labels'] for marker in item.iter_markers()))
        (omitted if settings['lint_policy'] == 'skip' and lint else selected).append(item)
    items[:] = selected
    config.hook.pytest_deselected(items=omitted)
    evidence = Path(settings['evidence'])
    evidence.mkdir(parents=True, exist_ok=True)
    # Nested pytest commands each have their own process and evidence file.
    (evidence / f'{os.getpid()}.json').write_text(json.dumps({
        'selected': [item.nodeid for item in selected],
        'unselected': [{'nodeid': item.nodeid, 'reason': 'lint-policy: skip'} for item in omitted],
    }, indent=2) + '\n')
    # If only recognized lint was collected, deselection is intentional, not an
    # infrastructure failure. Truly empty collection retains pytest exit 5.
    config._ros2_lint_only = bool(omitted) and not selected


def pytest_sessionfinish(session, exitstatus):
    if exitstatus == pytest.ExitCode.NO_TESTS_COLLECTED and getattr(session.config, '_ros2_lint_only', False):
        session.exitstatus = pytest.ExitCode.OK
