"""Non-modal controls and monitor for one explicitly selected review session."""

from pathlib import Path

from PySide6.QtCore import QSignalBlocker, QSize, Qt, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (QComboBox, QDialog, QFormLayout, QHBoxLayout, QLabel, QLayout, QLineEdit,
                               QListWidget, QListWidgetItem, QPlainTextEdit, QPushButton, QSpinBox,
                               QToolButton, QVBoxLayout, QWidget)

from ..services.review_contract.metrics import DEFAULT_MODEL, SOL_MODEL, TERRA_MODEL
from ..services.review_contract.prompt_profiles import DEFAULT_PROMPT_PROFILE, PROMPT_PROFILES
from .document_session import Lifecycle
from .review_controller import ReviewController
from .ui_chrome import COLORS, icon


MODEL_NAMES = {SOL_MODEL: "GPT-6.1 Sol", TERRA_MODEL: "GPT-6.1 Terra"}
JOB_STATES = {"completed": "Completata", "partial": "Completata parzialmente",
              "suspended": "Sospesa", "interrupted": "Interrotta", "failed": "Errore"}
DIAGNOSTICS = {
    "authentication_failed": "OpenAI ha rifiutato la chiave API.",
    "quota_exhausted": "Quota o credito OpenAI esaurito.",
    "permission_denied": "Permessi OpenAI insufficienti.",
    "model_unavailable": "Modello OpenAI non disponibile per il progetto.",
    "persistence_failed": "Impossibile aggiornare l’archivio locale.",
    "review_recovery_unavailable": "Impossibile ricostruire il report dai risultati conservati.",
    "worker_unavailable": "Processo PDF non disponibile.",
}
BATCH_STATES = {"failed": "Fallito", "uncertain": "Esito API incerto",
                "cancelled": "Interrotto", "too_large": "Estratto troppo grande"}
CREDENTIAL_STATES = {
    "unchecked": "Stato della chiave non disponibile.",
    "vault": "Chiave configurata in Windows.",
    "environment": "Chiave disponibile dalla variabile di sviluppo OPENAI_API_KEY.",
    "missing": "Configura una chiave API per avviare la revisione.",
    "error": "Chiave API non disponibile.",
}
CREDENTIAL_ERRORS = {
    "invalid_key": "Chiave non valida. Inserisci la chiave senza spazi interni.",
    "vault_write_failed": "Impossibile salvare la chiave in Windows.",
    "vault_delete_failed": "Impossibile eliminare la chiave da Windows.",
}


def _cost(value, unknown: int) -> str:
    if value is None:
        return "non disponibile"
    return f"{value} USD" + (f" (parziale, {unknown} con costo ignoto)" if unknown else "")


class ReviewDialog(QDialog):
    def __init__(self, controller: ReviewController, parent: QWidget) -> None:
        # On Windows an owned window stays above its owner in the z-order.
        # MainWindow retains this widget and closes it during shutdown.
        super().__init__(None, Qt.WindowType.Window | Qt.WindowType.WindowTitleHint
                         | Qt.WindowType.WindowSystemMenuHint | Qt.WindowType.WindowMinimizeButtonHint
                         | Qt.WindowType.WindowCloseButtonHint)
        parent.destroyed.connect(self.deleteLater)
        self.setAttribute(Qt.WidgetAttribute.WA_QuitOnClose, False)
        self.controller = controller
        self.document_id: str | None = None
        self._listed_report: dict | None = None
        self.setWindowTitle("Revisione con IA")
        self.setFont(parent.font())
        self.setModal(False)
        self.setObjectName("reviewDialog")
        self.setStyleSheet(parent.styleSheet() + f"""
            QDialog#reviewDialog {{ background: {COLORS.chrome}; }}
            QComboBox {{ color: {COLORS.primary}; background: {COLORS.surface}; padding: 5px; }}
            QComboBox QAbstractItemView {{ color: {COLORS.primary}; background: {COLORS.surface}; }}
            QPushButton#reviewStart {{ background: {COLORS.active}; }}
        """)
        layout = QVBoxLayout(self)
        # The configuration is hidden in the monitor. Fit the visible
        # sections vertically while retaining interactive width resizing.
        layout.setVerticalSizeConstraint(QLayout.SizeConstraint.SetFixedSize)
        self.document = QLabel(self)
        self.document.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(self.document)
        self.state = QLabel(self)
        self.state.setWordWrap(True)
        layout.addWidget(self.state)

        self.configuration = QWidget(self)
        form = QFormLayout(self.configuration)
        form.setContentsMargins(0, 0, 0, 0)
        self.profile = QComboBox(self.configuration)
        for profile_id, profile in PROMPT_PROFILES.items():
            self.profile.addItem(profile.name, profile_id)
        self.profile.setCurrentIndex(self.profile.findData(DEFAULT_PROMPT_PROFILE))
        form.addRow("Profilo", self.profile)
        self.model = QComboBox(self.configuration)
        for model_id, label in MODEL_NAMES.items():
            self.model.addItem(label, model_id)
        self.model.setCurrentIndex(self.model.findData(DEFAULT_MODEL))
        form.addRow("Modello IA", self.model)
        self.page_mode = QComboBox(self.configuration)
        self.page_mode.addItems(["Intero documento", "Intervallo"])
        form.addRow("Pagine", self.page_mode)
        self.limits = QWidget(self.configuration)
        self.limits.setToolTip("La pagina 1 è la prima pagina del PDF, a prescindere dalla numerazione stampata.")
        ranges = QHBoxLayout(self.limits)
        ranges.setContentsMargins(0, 0, 0, 0)
        self.first_page = QSpinBox(self.limits)
        self.last_page = QSpinBox(self.limits)
        self.first_page.setAccessibleName("Prima pagina fisica")
        self.last_page.setAccessibleName("Ultima pagina fisica")
        first_steps = self._add_page_stepper(ranges, self.first_page)
        ranges.addWidget(QLabel("–", self.limits))
        last_steps = self._add_page_stepper(ranges, self.last_page)
        ranges.addStretch()
        self._steps = {self.first_page: first_steps, self.last_page: last_steps}
        form.addRow("Pagine fisiche", self.limits)
        self.context: dict[str, QPlainTextEdit] = {}
        for key, label, height in (("opera", "Opera", 43), ("destinatari", "Destinatari", 43),
                                   ("indicazioni_aggiuntive", "Indicazioni aggiuntive", 63)):
            field = QPlainTextEdit(self.configuration)
            field.setFixedHeight(height)
            field.setTabChangesFocus(True)
            self.context[key] = field
            form.addRow(label, field)
        self.notice = QLabel(
            "L’avvio invia a OpenAI le pagine fisiche selezionate e le pagine adiacenti di contesto. "
            "L’uso dell’API può comportare un costo.", self.configuration)
        self.notice.setWordWrap(True)
        form.addRow(self.notice)
        layout.addWidget(self.configuration)

        self.progress = QLabel(self)
        self.progress.setWordWrap(True)
        layout.addWidget(self.progress)
        self.summary = QLabel(self)
        self.summary.setWordWrap(True)
        layout.addWidget(self.summary)
        self.batches = QListWidget(self)
        self.batches.setAccessibleName("Batch senza esito")
        self.batches.setMaximumHeight(125)
        layout.addWidget(self.batches)
        self.retry_hint = QLabel("La ripetizione può comportare un nuovo addebito.", self)
        self.retry_hint.setWordWrap(True)
        layout.addWidget(self.retry_hint)

        credential_row = QHBoxLayout()
        self.credentials_button = QPushButton("Chiave API…", self)
        self.credentials_button.setCheckable(True)
        credential_row.addWidget(self.credentials_button)
        self.credential_status = QLabel(self)
        self.credential_status.setWordWrap(True)
        credential_row.addWidget(self.credential_status, 1)
        layout.addLayout(credential_row)
        self.credential_panel = QWidget(self)
        storage = QHBoxLayout(self.credential_panel)
        storage.setContentsMargins(0, 0, 0, 0)
        self.key = QLineEdit(self.credential_panel)
        self.key.setEchoMode(QLineEdit.EchoMode.Password)
        self.key.setPlaceholderText("Nuova chiave OpenAI")
        self.key.setAccessibleName("Nuova chiave OpenAI")
        self.key.setInputMethodHints(Qt.InputMethodHint.ImhHiddenText | Qt.InputMethodHint.ImhSensitiveData
                                     | Qt.InputMethodHint.ImhNoPredictiveText)
        self.store_key = QPushButton("Salva chiave", self.credential_panel)
        self.delete_key = QPushButton("Elimina chiave", self.credential_panel)
        storage.addWidget(self.key, 1)
        storage.addWidget(self.store_key)
        storage.addWidget(self.delete_key)
        layout.addWidget(self.credential_panel)
        self.credential_panel.hide()

        actions = QHBoxLayout()
        self.report_button = QPushButton("Apri archivio", self)
        self.retry_button = QPushButton("Riprova selezionati", self)
        self.resume_button = QPushButton("Riprendi applicazione", self)
        self.stop_button = QPushButton("Interrompi", self)
        self.start_button = QPushButton("Avvia revisione", self)
        self.start_button.setObjectName("reviewStart")
        self.close_button = QPushButton("Chiudi", self)
        actions.addWidget(self.report_button)
        actions.addStretch()
        for button in (self.retry_button, self.resume_button, self.stop_button, self.start_button, self.close_button):
            button.setAutoDefault(False)
            actions.addWidget(button)
        layout.addLayout(actions)
        self.credentials_button.toggled.connect(self._toggle_credentials)
        self.store_key.clicked.connect(self._save_key)
        self.delete_key.clicked.connect(controller.delete_key)
        self.key.textChanged.connect(self._credential_controls)
        self.start_button.clicked.connect(self._start)
        self.stop_button.clicked.connect(lambda: controller.stop(self.document_id))
        self.retry_button.clicked.connect(self._retry)
        self.resume_button.clicked.connect(lambda: controller.resume_application(self.document_id))
        self.report_button.clicked.connect(self._open_archive)
        self.close_button.clicked.connect(self.close)
        self.page_mode.currentIndexChanged.connect(self.refresh)
        self.first_page.valueChanged.connect(lambda: self._page_changed(self.first_page, self.last_page))
        self.last_page.valueChanged.connect(lambda: self._page_changed(self.last_page, self.first_page))
        self.batches.itemChanged.connect(self._retry_controls)
        controller.changed.connect(self.refresh)
        self.refresh()
        self.resize(500, self.sizeHint().height())

    def _add_page_stepper(self, row: QHBoxLayout, spin: QSpinBox) -> tuple[QToolButton, QToolButton]:
        spin.setButtonSymbols(QSpinBox.ButtonSymbols.NoButtons)
        spin.setKeyboardTracking(False)
        row.addWidget(spin)
        buttons = []
        for step, direction, verb in ((-1, "down", "Riduci"), (1, "up", "Aumenta")):
            button = QToolButton(self.limits)
            button.setObjectName("contextStepButton")
            button.setIcon(icon(f"chevron-{direction}"))
            button.setIconSize(QSize(16, 16))
            button.setAccessibleName(f"{verb} {spin.accessibleName().lower()}")
            button.setToolTip(button.accessibleName())
            button.setAutoRepeat(True)
            button.clicked.connect(lambda _checked=False, delta=step: spin.stepBy(delta))
            row.addWidget(button)
            buttons.append(button)
        return buttons[0], buttons[1]

    def _sync_page_steps(self) -> None:
        for spin, (down, up) in self._steps.items():
            down.setEnabled(spin.value() > spin.minimum())
            up.setEnabled(spin.value() < spin.maximum())

    def _page_changed(self, edited: QSpinBox, other: QSpinBox) -> None:
        if self.first_page.value() > self.last_page.value():
            other.setValue(edited.value())
        self._sync_page_steps()

    def show_document(self, document_id: str) -> None:
        if self.document_id != document_id:
            self.document_id = document_id
            self._listed_report = None
            self.batches.clear()
            self.page_mode.setCurrentIndex(0)
            self.profile.setCurrentIndex(self.profile.findData(DEFAULT_PROMPT_PROFILE))
            self.model.setCurrentIndex(self.model.findData(DEFAULT_MODEL))
            for field in self.context.values():
                field.clear()
            session = self.controller.manager.sessions.get(document_id)
            count = max(1, session.page_count) if session is not None else 1
            with QSignalBlocker(self.first_page), QSignalBlocker(self.last_page):
                for spin in (self.first_page, self.last_page):
                    spin.setRange(1, count)
                self.first_page.setValue(1)
                self.last_page.setValue(count)
            self._sync_page_steps()
        self.controller.refresh_credentials()
        self.refresh()
        if self.isMinimized():
            self.showNormal()
        else:
            self.show()
        self.raise_()
        self.activateWindow()

    def hideEvent(self, event) -> None:
        self.key.clear()
        self.credentials_button.setChecked(False)
        super().hideEvent(event)

    def _toggle_credentials(self, visible: bool) -> None:
        self.credential_panel.setVisible(visible)
        if visible:
            self.key.setFocus()
        else:
            self.key.clear()

    def _save_key(self) -> None:
        value = self.key.text()
        self.key.clear()
        self.controller.save_key(value)

    def _credential_controls(self) -> None:
        enabled = not self.controller.closing and not self.controller.credential_busy
        self.key.setEnabled(enabled)
        self.store_key.setEnabled(enabled and bool(self.key.text()))
        self.delete_key.setEnabled(enabled and self.controller.credential_state == "vault")

    def _start(self) -> None:
        if not self.start_button.isEnabled():
            return
        self.first_page.interpretText()
        self.last_page.interpretText()
        whole = self.page_mode.currentIndex() == 0
        first = 0 if whole else self.first_page.value() - 1
        last = self.last_page.maximum() - 1 if whole else self.last_page.value() - 1
        editorial = {name: field.toPlainText() for name, field in self.context.items() if field.toPlainText().strip()}
        self.controller.start(self.document_id, profile=self.profile.currentData(), first=first, last=last,
                              context=editorial, model=self.model.currentData())

    def _selected_batches(self) -> tuple[str, ...]:
        return tuple(self.batches.item(index).data(Qt.ItemDataRole.UserRole)
                     for index in range(self.batches.count())
                     if self.batches.item(index).checkState() == Qt.CheckState.Checked)

    def _retry(self) -> None:
        if self.retry_button.isEnabled():
            self.controller.retry(self.document_id, self._selected_batches())

    def _open_archive(self) -> None:
        entry = self.controller.entries.get(self.document_id)
        if entry is not None and entry.report is not None:
            if not QDesktopServices.openUrl(QUrl.fromLocalFile(str(entry.job.store.directory))):
                self.state.setText("Impossibile aprire l’archivio locale.")

    def _retry_controls(self) -> None:
        can_retry = self.controller.can_retry(self.document_id) and not self.controller.closing
        has_choices = any(self.batches.item(index).data(Qt.ItemDataRole.UserRole)
                          for index in range(self.batches.count()))
        self.retry_button.setVisible(can_retry and has_choices)
        self.retry_hint.setVisible(can_retry and has_choices)
        self.retry_button.setEnabled(can_retry and bool(self._selected_batches())
                                     and not self.controller.credential_busy
                                     and self.controller.credential_state in ("vault", "environment"))

    def _update_batches(self, report: dict | None) -> None:
        if report is self._listed_report:
            return
        self._listed_report = report
        self.batches.clear()
        for batch in (report or {}).get("batches", ()):
            status = batch["status"]
            if status == "completed":
                continue
            first, last = batch["target_pages"][0] + 1, batch["target_pages"][-1] + 1
            pages = f"Pagina {first}" if first == last else f"Pagine {first}–{last}"
            row = QListWidgetItem(f"{pages} · {BATCH_STATES.get(status, 'Senza esito')}", self.batches)
            if status != "too_large":
                row.setData(Qt.ItemDataRole.UserRole, batch["batch_id"])
                row.setFlags(row.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                row.setCheckState(Qt.CheckState.Unchecked)

    def refresh(self) -> None:
        controller = self.controller
        session = controller.manager.sessions.get(self.document_id)
        entry = controller.entries.get(self.document_id)
        preparing = controller.preparing(self.document_id)
        report = entry.report if entry is not None and not preparing else None
        ready = session is not None and session.lifecycle == Lifecycle.READY
        running = controller.busy(self.document_id)
        limits = self.page_mode.currentIndex() == 1
        self.document.setText(Path(session.canonical_path).name if session is not None else
                              entry.title if entry is not None else "Documento chiuso")
        self.configuration.setVisible(ready and not running)
        self.limits.setVisible(limits)
        self.configuration.layout().labelForField(self.limits).setVisible(limits)
        self.start_button.setVisible(ready and not running)
        self.start_button.setEnabled(ready and not session.saving and not controller.closing
                                     and not controller.credential_busy
                                     and controller.credential_state in ("vault", "environment"))
        self.stop_button.setVisible(running)
        self.stop_button.setEnabled(not (entry and entry.stopping) and not controller.closing)
        self.resume_button.setVisible(controller.can_resume_application(self.document_id))
        self.report_button.setVisible(report is not None and "cycle" in report)
        state = controller.errors.get(self.document_id, "")
        if not state:
            if running:
                state = ("Interruzione in corso…" if entry and entry.stopping else
                         "Preparazione…" if preparing else
                         "Applicazione delle annotazioni…" if entry and entry.phase == "application" else
                         "Analisi in corso…" if entry and entry.progress is not None else "Preparazione…")
            elif report is not None:
                state = JOB_STATES.get(report["status"], "Errore")
                error = report.get("error")
                if error:
                    state += " · " + DIAGNOSTICS.get(error, "La revisione richiede attenzione.")
                application = report.get("application", {})
                if application.get("status") in ("interrupted", "failed"):
                    state += (" · Applicazione interrotta" if application["status"] == "interrupted"
                              else " · Applicazione incompleta")
                    if application.get("error") in ("REVIEW_SOURCE_CHANGED", "REVIEW_SNAPSHOT_CHANGED",
                                                     "REVIEW_SNAPSHOT_UNKNOWN", "session_invalid"):
                        state += " · Il documento o lo snapshot non è più valido per questa revisione."
                elif application.get("status") == "deferred" and error in (
                        "persistence_failed", "review_recovery_unavailable"):
                    state += " · Applicazione non avviata"
            elif not ready:
                state = "La scheda non è disponibile."
        self.state.setText(state)
        self.state.setVisible(bool(state))
        progress = entry.progress if entry is not None and not preparing else None
        self.progress.setVisible(progress is not None or running)
        if progress is not None:
            details = (f"Modello del job {MODEL_NAMES[entry.job.settings.model]}\n"
                       f"Pagine analizzate {progress.pages_analyzed}/{progress.pages_total} · "
                       f"Batch riusciti {progress.batches_completed}/{progress.batches_total}\n"
                       f"Batch senza esito {progress.batches_failed} · Correzioni {progress.corrections}\n"
                       f"Costo stimato {_cost(progress.cost_usd, progress.unknown_cost_attempts)}")
            if running and progress.rate_waiting_batches and not entry.stopping:
                details += f"\nBatch in attesa per limiti API {progress.rate_waiting_batches}"
            self.progress.setText(details)
        elif running:
            pages = controller.requested_pages(self.document_id)
            self.progress.setText(f"Pagine richieste {pages if pages is not None else 'non disponibili'}\n"
                                  "Avanzamento e costo stimato non disponibili.")
        app = (report or {}).get("application")
        self.summary.setVisible(app is not None)
        if app is not None:
            self.summary.setText(
                f"Pagine non analizzate {app.get('pages_not_analyzed', '—')} · "
                f"Applicate {app.get('applied', '—')} · Già applicate {app.get('already_applied', '—')}\n"
                f"Scartate {app.get('discarded', '—')} · Errori {app.get('errors', '—')} · "
                f"Incerte {app.get('uncertain', '—')}")
        self._update_batches(report)
        self.batches.setVisible(self.batches.count() > 0)
        self._retry_controls()
        credentials = CREDENTIAL_STATES[controller.credential_state]
        if controller.credential_busy:
            credentials = "Operazione sulla chiave in corso…"
        elif controller.credential_error:
            credentials = CREDENTIAL_ERRORS.get(controller.credential_error, "Impossibile accedere alle credenziali Windows.")
        self.credential_status.setText(credentials)
        self._credential_controls()
