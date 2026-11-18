# M19 — Fixture riproducibili

La fixture pytest di sessione `m19_samples` genera i tre PDF M19 sotto
`tmp_path_factory`, eseguendo il generatore esistente in un processo separato.
I test M19.1 e M19.2 ricevono i percorsi temporanei tramite dependency injection;
il processo GUI non importa PyMuPDF. Il generatore non importa moduli di test.
Le copie modificate dai singoli test restano sotto `tmp_path`.

Nessun PDF in `output/pdf/m19/` o `output/pdf/m192/` e' richiesto per pytest.
I PDF, manifest e inspection generati manualmente restano artefatti locali esclusi
da Git. I report storici conservano le evidenze e gli hash delle loro esecuzioni.

Per rigenerare esplicitamente il campione per Acrobat (da una directory nuova):

```powershell
.\.venv\Scripts\python.exe -m experiments.m19.motivated_corrections --output-dir output/pdf/m19/manual-new
```

Il generatore rifiuta una directory gia' esistente. Per mantenere gli output
manuali sotto il percorso ignorato usare, per esempio, `output/pdf/m19/manual-new`.
La generazione manuale e' distinta dall'esecuzione automatica delle fixture.
Il vecchio campione non viene sovrascritto. UUID e timestamp possono differire:
i test verificano contenuti, geometrie, profili nativi e relazioni IRT/RT,
non l'identita' dei byte fra esecuzioni.

Il consolidamento registra il collaudo Acrobat e la full suite positivi riferiti
dall'utente dopo i report originali. Non aggiorna automaticamente i gate Adobe
ne' introduce una nuova prova InDesign. La release Windows resta v0.17.1.

## Verifica di riproducibilita' del consolidamento

Eseguiti da una copia isolata di src, tests, experiments e pyproject.toml,
senza cartella output ne' PDF preesistenti (PYTHONPATH sulla copia isolata):

```powershell
python -m pytest -q tests/test_annotation_replies_m191.py tests/test_motivated_corrections_m192.py --basetemp=temp-pytest --tb=short
# 80 passed in 93.76s
python -m pytest -q experiments/m19/test_motivated_corrections.py --basetemp=temp-probe --tb=short
# 11 passed in 1.18s
```

Un confronto read-only separato fra i tre PDF rigenerati e quelli originali
ha verificato stesso contenuto base, geometrie, profili, appearance, metadati
semantici e grafo IRT/RT, normalizzando UUID e date. Entrambe le generazioni
superano il validator nativo M19. Nessuna differenza sostanziale riscontrata.
Full suite non rieseguita; gli output precedenti e la stabile sono preservati.
