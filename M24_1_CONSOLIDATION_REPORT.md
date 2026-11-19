# Consolidamento M24.1b / M24.1c

## Correzione UTF-8

L'helper `native()` legge stdout/stderr con `encoding="utf-8"`, ma in
precedenza il figlio stampava JSON Unicode con l'encoding dello stream Windows
ereditato. `tests/m24_text_fixtures.py`, solo nel percorso `--native`, ora
configura esplicitamente stdout e stderr in UTF-8 prima di stampare il JSON.
Nessuna variabile globale dell'applicazione e nessuna modifica di produzione.

Il nuovo test `test_native_json_uses_utf8_despite_inherited_windows_encoding`
simula localmente, mediante monkeypatch ripristinata a fine test,
`PYTHONIOENCODING=cp1252` e `PYTHONUTF8=0`. Verifica la corretta lettura di
`caffè Ω ﬁ 漢字` e delle spaziature interne anche in tale configurazione.

## Verifica

Eseguito senza impostare PYTHONIOENCODING o PYTHONUTF8 nella shell:

```text
.venv\Scripts\python.exe -m pytest tests/test_structured_text.py tests/test_m24_dehyphenation.py -q --tb=short
-> 37 passed in 10.77s
```

`git diff --check` e controllo staged: PASS prima del commit.
La full suite non è stata ripetuta. Il proprietario ha riportato il precedente
risultato 1.143 PASS, 5 FAIL, 1 SKIP e la scomparsa dei cinque FAIL nei moduli
M24 con UTF-8. Questa consegna rende locale e permanente quella correzione.
Nessuna build o chiamata API eseguita.

## Perimetro del consolidamento

Il working tree iniziale contiene esclusivamente le modifiche M24 sviluppate
nei passaggi precedenti. Il consolidamento raccoglie lo stato definitivo:
estrazione nativa per pagina, JSON minimale, normalizzazione delle sillabazioni,
menu File/export asincrono atomico e IPC limitato con controlli interni.
Il vecchio modulo di batching e le euristiche di paragrafo non sono presenti.
I report delle sperimentazioni precedenti rimangono documenti storici identificati
come tali. La versione 0.18.0 e le dipendenze restano invariate.

Nessun PDF di collaudo, JSON estratto, file temporaneo, log, build o segreto
incluso. Nessun lavoro M24.2. Nessun push o tag autorizzato/eseguito.

## File inclusi nel consolidamento — 22

Documentazione:

- `ACCEPTANCE_TESTS.md`
- `ARCHITECTURE.md`
- `PROJECT_SPEC.md`
- `ROADMAP.md`
- `M24_1_REPORT.md`
- `M24_1A_REPORT.md`
- `M24_1B_REPORT.md`
- `M24_1C_REPORT.md`
- `M24_1C_NORMALIZATION_REPORT.md`
- `M24_1_CONSOLIDATION_REPORT.md`

Codice M24:

- `src/pdf_annotator/app/main_window.py`
- `src/pdf_annotator/app/text_export.py`
- `src/pdf_annotator/domain/structured_text.py`
- `src/pdf_annotator/services/structured_text_export.py`
- `src/pdf_annotator/worker/client.py`
- `src/pdf_annotator/worker/document_service.py`
- `src/pdf_annotator/worker/protocol.py`
- `src/pdf_annotator/worker/structured_text.py`

Test e fixture sintetiche:

- `tests/m24_text_fixtures.py`
- `tests/test_structured_text.py`
- `tests/test_m24_dehyphenation.py`
- `tests/test_structured_text_ui.py`

Il commit e il fast-forward sono riportati con hash verificato nella consegna
finale, insieme allo stato dei due branch e del working tree.
