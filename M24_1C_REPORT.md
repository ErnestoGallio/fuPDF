> Esperimento storico sul flag nativo. La M24.1c definitiva usa la regola
> deterministica descritta in M24_1C_NORMALIZATION_REPORT.md.

# M24.1c — Verifica preliminare TEXT_DEHYPHENATE

## Esito: flag non attivato

Il gate sperimentale non è soddisfatto. Con il binding installato
**PyMuPDF/MuPDF 1.28.2**, l'aggiunta del flag non ricompone nessuna delle
sillabazioni esaminate, nemmeno in un controllo sintetico minimale.
M24.1c non viene considerata completata come funzionalità.

**Unico file aggiunto: questo report.** Nessuna modifica al codice, ai test
persistenti, allo schema JSON, ai documenti normativi o al motore multimodale.
Il working tree conteneva già le modifiche non committate dei precedenti M24.

## Configurazione e metodo

- Estrazione attuale: `base_textpage(page, EXTRACTION_FLAGS)`;
  `EXTRACTION_FLAGS = 67`, senza il bit di dehyphenation.
- Estrazione sperimentale: stessa funzione con
  `EXTRACTION_FLAGS | pymupdf.TEXT_DEHYPHENATE`, valore **83**.
- Verificato anche che `FzStextOptions(83).flags == 83`: il bit viene passato
  alla creazione del dispositivo, non aggiunto tardivamente all'estrazione.
- Confrontati gli output `extractText(sort=False)` prima della sostituzione
  degli accapo. Confrontato anche `Page.get_text("text", flags=..., sort=False)`
  direttamente, senza l'adattatore fuPDF: stesso mancato effetto.
- Tutte le prove PDF sono eseguite sul main thread di un processo di analisi,
  senza salvare o modificare gli originali; fixture sintetica solo in memoria.

La [documentazione ufficiale](https://pymupdf.readthedocs.io/en/latest/vars.html#text-extraction-flags)
descrive TEXT_DEHYPHENATE come rimozione dei trattini di fine riga e unione
delle righe. Il comportamento osservato qui non dimostra quel beneficio.
Non è stata identificata con certezza la causa interna; non si attribuisce
un bug generale alla libreria né si propone un aggiornamento non verificato.

## PDF scolastico: 19 pagine

File verificato: `C:/Users/matte/Desktop/0000.000_000_SezA_UDA1.pdf`.
Controllata anche la copia omonima sotto `IRC vol. 2/1a bozza` per i casi citati.

**Sillabazioni ricostruite dal flag: 0.** Tutte le 19 pagine hanno output
esattamente identico con e senza flag, inclusi whitespace, caratteri e ordine.

| Pagina fisica | Prima | Dopo il flag | Risultato richiesto |
| --- | --- | --- | --- |
| 4 | `ana-\nlizzare` | `ana-\nlizzare` | `analizzare` |
| 5 | `riguar-\ndano` | `riguar-\ndano` | `riguardano` |
| 5 | `chia-\nmante` | `chia-\nmante` | `chiamante` |

Il RAWDICT conferma righe consecutive nello stesso blocco per ciascun caso:
non sono semplicemente porzioni su pagine diverse. Nel JSON M24.1b attuale
restano quindi `ana- lizzare`, `riguar- dano`, `chia- mante`.

Trattini autentici interni: **21 occorrenze controllate**, tra cui `top-down`,
`bottom-up`, `n-esima`, tutte conservate. Nessuna perdita o inversione osservata;
nessuna ricostruzione errata osservata, perché non avviene alcuna ricostruzione.
Questo non prova la capacità del flag di distinguere un composto autentico
da una sillabazione quando entrambi cadono a fine riga.

## Controllo sintetico

Creato in memoria un PDF con `ana-\nlizzare`, composti interni
`top-down bottom-up non-locali` e il composto spezzato `soffitto-\npavimento`.
L'output con flag è identico a quello senza flag. Ulteriori prove con flag
0/16/17/18/19/64/80/83 non cambiano il risultato del controllo minimale.
L'aspettativa iniziale di ottenere `analizzare` sul sintetico è fallita;
il test finale caratterizza esplicitamente il mancato effetto.

## Localizzazione multimodale esistente

Eseguita `review_localization.localize_document(..., base_only=True)` con
operazione di cancellazione, pagina fisica corretta e parola ricomposta,
senza ID di paragrafo e senza un nuovo localizzatore.

| Testo cercato | Esito | Candidati | Geometria | Range sorgente confermato |
| --- | --- | --- | --- | --- |
| `analizzare` | `text_normalized`, applicabile | 1 | 2 quad | `ana-\nlizzare` |
| `riguardano` | `text_normalized`, applicabile | 1 | 2 quad | `riguar-\ndano` |
| `chiamante` | `text_normalized`, applicabile | 1 | 2 quad | `chia-\nmante` |

Le tre prove passano: il matcher esistente trova la parola ricomposta e la
riconduce al range originale su due righe. Nessuna annotazione è stata creata.
Questo risultato è positivo per la localizzazione, ma non risolve il mancato
beneficio dell'estrazione e non certifica ogni possibile caso ambiguo.

## Romanzo: 44 pagine

File: `Due Ragni in un Boccale.pdf` nella directory `Documenti definitivi`.

- Tutte le **44 pagine** hanno testo esattamente identico con e senza flag.
- Nessuna ricostruzione, perdita di caratteri o nuova inversione osservata.
- Pagina 25: mantenuta la sequenza corretta «queste astrazioni e di come in
  loro abbiamo riposto speranze deformi di onnicomprensività».
- Pagine 21–24: identità completa anche di spazi e accapo prima del flattening;
  nessuna alterazione delle spaziature espressive.
- Composti interni controllati: `non-locali` e `soffitto-pavimento`, conservati.

## Test e controlli eseguiti

Comando delle prove mirate: `.venv\Scripts\python.exe -B -`, con script di
asserzioni inline e UTF-8 esplicito. Verifiche finali tutte superate:

1. 19 pagine scolastiche: uguaglianza esatta degli output nativi con/senza flag,
   sia tramite base_textpage sia con Page.get_text diretto.
2. 44 pagine del romanzo: stessi controlli, più sequenza attesa a pagina 25.
3. 21 + 2 occorrenze di composti interni conservate.
4. Tre localizzazioni: un candidato, applicabile, due quad e range sorgente esatto.
5. Controllo sintetico: mancato effetto del flag riprodotto, trattini conservati.
6. `git diff --check`: PASS.

Le asserzioni di caratterizzazione passano; il **gate funzionale di attivazione
NON passa**, perché il numero di ricomposizioni resta zero. Nessuna full suite,
build, dipendenza nuova, chiamata OpenAI, commit o push. Non è stato modificato
il comportamento M24.1b né quello multimodale.

Un eventuale seguito richiede chiarire perché la funzionalità nativa non ha
effetto nella configurazione riprodotta. Nessun algoritmo linguistico o regex
di ricomposizione è stato introdotto per aggirare il problema.
