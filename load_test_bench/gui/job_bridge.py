"""Qt bridge for the job engine - the only file where jobs meets Qt.

Engine callbacks fire on the engine thread; emitting a Signal here queues
delivery onto the GUI thread (CLAUDE.md threading rule).
"""

from PySide6.QtCore import QObject, Signal


class JobEngineBridge(QObject):
    job_changed = Signal(object)  # JobSnapshot
    safety_tripped = Signal(str)  # trip reason

    def __init__(self, executor, parent=None):
        super().__init__(parent)
        executor.add_snapshot_callback(self.job_changed.emit)
