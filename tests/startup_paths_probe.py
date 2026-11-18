"""Subprocess-only observer for the real main() loop; no production test switch."""

import multiprocessing as mp
from pathlib import Path
import sys
from time import monotonic


def run(directory: Path, output: Path, scenario: str) -> None:
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication
    from pdf_annotator.app.main_window import MainWindow
    from pdf_annotator.app.document_session import Lifecycle
    from pdf_annotator.main import main

    named = output / 'Libro città 日本語 con spazi.pdf'
    named.write_bytes((directory / 'normal.pdf').read_bytes())
    choices = {'empty': [], 'single': [str(named)],
        'multiple': [str(named), str(directory / 'alternative.pdf')],
        'relative': [str(named.relative_to(output))],
        'missing': [str(output / 'absent.pdf')], 'invalid': [str(directory / 'invalid.pdf')],
        'mixed': [str(output / 'absent.pdf'), str(directory / 'invalid.pdf'), str(named)]}
    paths = choices[scenario]
    if scenario == 'relative':
        import os
        os.chdir(output)
    expected = 2 if scenario == 'multiple' else 1 if scenario in ('single','relative','mixed') else 0
    expected_errors = 2 if scenario == 'mixed' else 1 if scenario in ('missing','invalid') else 0
    app = QApplication([])
    state = {'window': None, 'workers': [], 'ticks': 0, 'errors': [], 'ok': False}
    original = MainWindow.open_paths
    def observe(window, arguments):
        assert window.isVisible()
        assert not window.manager.sessions
        window.manager.error_occurred.connect(lambda _id, error: state['errors'].append(error.code))
        original(window, arguments)
        # File identity checks/opening stay asynchronous, outside the GUI call.
        assert not any(s.lifecycle == Lifecycle.READY for s in window.manager.sessions.values())
    MainWindow.open_paths = observe
    start = monotonic()
    timer = QTimer()
    def tick():
        state['ticks'] += 1
        window = state['window'] or next((w for w in app.topLevelWidgets() if isinstance(w, MainWindow)), None)
        state['window'] = window
        if window is None:
            return
        sessions = tuple(window.manager.sessions.values())
        state['workers'].extend(s.worker for s in sessions if s.worker and s.worker not in state['workers'])
        ready = len(sessions) == expected and all(s.lifecycle == Lifecycle.READY for s in sessions)
        rendered = not sessions or bool(window.tabs.currentWidget().scheduler.rasters)
        if ready and rendered and len(state['errors']) == expected_errors and state['ticks'] >= 3:
            assert window.tabs.count() == expected
            state['ok'] = True
            timer.stop()
            window.close()
        elif monotonic() - start > 20:
            timer.stop()
            window.manager.request_shutdown(discard_changes=True)
            window.close()
    timer.timeout.connect(tick)
    timer.start(10)
    result = main(paths)
    assert result == 0 and state['ok'], state
    assert state['window'].manager.is_shutdown
    assert all(w.stopped.is_set() and not w.forced_termination for w in state['workers'])
    assert not mp.active_children()
    print('STARTUP_PATHS_PASS', scenario)


if __name__ == '__main__':
    run(Path(sys.argv[1]).resolve(), Path(sys.argv[2]).resolve(), sys.argv[3])
