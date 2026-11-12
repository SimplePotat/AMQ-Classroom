#!/usr/bin/env python3
"""Run retained upstream selection/filter units with the classroom dependencies.

Worker imports are stubbed; these units exercise only the real view's local
selection/filter algorithms. Live MAL/AnimeThemes integration is separate.
"""
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
from unittest.mock import Mock, patch


def main():
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    with tempfile.TemporaryDirectory(prefix="classroom-legacy-units-") as directory:
        os.environ.update(CLASSROOM_DATA_DIR=directory, CLASSROOM_PREWARM="0",
                          DJANGO_SETTINGS_MODULE="classroom.settings")
        import django
        django.setup()
        from django.test.runner import DiscoverRunner
        with patch.dict(sys.modules, {"quiz.tasks": SimpleNamespace(
                GetUserThemesTask=Mock(), find_cached_lyrics=Mock(return_value=None))}):
            return DiscoverRunner(verbosity=1).run_tests([
                "quiz.tests.test_views.ChooseThemeTestCase",
                "quiz.tests.test_views.ApplyThemesFiltersTestCase",
            ])


if __name__ == "__main__":
    raise SystemExit(bool(main()))
