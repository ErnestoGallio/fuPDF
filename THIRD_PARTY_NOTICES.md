# Avvisi sulle licenze di terze parti

Inventario del candidato portabile **fuPDF v0.18.0 per Windows x64**. Le versioni
derivano dai metadati dei pacchetti, dai binari e dalle risorse presenti nel
candidato, con riscontro sui sorgenti versionati. Gli avvisi e i testi qui
raccolti preparano la distribuzione pubblica. La verifica del pacchetto di
rilascio e la disponibilità dei sorgenti corrispondenti restano da completare.

## Componenti inclusi

| Componente | Presenza e licenza |
| --- | --- |
| **PyMuPDF 1.28.2 e MuPDF 1.28.2** | `_extra.pyd`, `_mupdf.pyd`, `mupdfcpp64.dll` e moduli Python nel bundle. PyMuPDF e MuPDF sono distribuiti da Artifex con opzione GNU AGPLv3 oppure licenza commerciale. La distribuzione proposta usa l'opzione AGPLv3. Conservare anche gli avvisi dei componenti e dei font incorporati da MuPDF e quelli applicabili agli output secondo Artifex. [PyMuPDF 1.28.2](https://github.com/pymupdf/PyMuPDF/releases/tag/1.28.2) · [Sorgenti MuPDF 1.28.2](https://github.com/ArtifexSoftware/mupdf/tree/1.28.2) · [Licenza Artifex](https://pymupdf.io/licensing) |
| **PySide6, Shiboken6 e Qt 6.11.2** | Binding e DLL dei moduli Core, Gui, Widgets e Network. Presenti i plugin Windows, stile, ICO, informazioni di rete e backend TLS. I wheel dichiarano opzioni LGPLv3 o GPL, con opzione commerciale separata. L'opzione proposta per i componenti che la offrono è LGPLv3. Includere i testi [LGPLv3](LICENSES/LGPL-3.0-only.txt) e [GPLv3](LICENSES/GPL-3.0-only.txt), gli avvisi di copyright e le attribuzioni dei componenti realmente inclusi. [PySide/Shiboken 6.11.2](https://github.com/pyside/pyside-setup/tree/v6.11.2) · [Qt Base 6.11.2](https://github.com/qt/qtbase/tree/v6.11.2) · [Licenze Qt for Python](https://doc.qt.io/qtforpython-6/licenses.html) · [Licenze Qt 6.11](https://doc.qt.io/qt-6.11/licensing.html) |
| **Traduzioni Qt 6.11.2** | Il candidato contiene 96 cataloghi `qt_*.qm`, `qtbase_*.qm` e `qt_help_*.qm`. Le regole upstream indicano le opzioni per la categoria modulo. Per il pacchetto pubblico mappare i cataloghi effettivi ai sorgenti `.ts` e agli avvisi applicabili, inclusi quelli provenienti da altri moduli. [Regole di licenza Qt Translations 6.11.2](https://github.com/qt/qttranslations/blob/v6.11.2/licenseRule.json) |
| **CPython 3.11.9** | `python311.dll`, `python3.dll`, `base_library.zip` ed estensioni della libreria standard. Conservare i termini PSF, le licenze storiche applicabili e gli avvisi di terze parti del [file di licenza dell'installazione Windows](LICENSES/CPython-3.11.9.txt). [Sorgenti e licenza CPython 3.11.9](https://github.com/python/cpython/blob/v3.11.9/LICENSE) |
| **Librerie native del runtime CPython** | `libssl-3.dll` e `libcrypto-3.dll` sono **OpenSSL 3.0.13**, sotto [Apache 2.0](LICENSES/OpenSSL-3.0.13.txt). `libffi-8.dll` proviene dal runtime Windows, il cui manifest upstream fissa **libffi 3.4.4** ([testo MIT](LICENSES/libffi-3.4.4.txt)). La release CPython usa **bzip2 1.0.8** per `_bz2.pyd` (avviso nel file CPython), **XZ 5.2.5** per `_lzma.pyd` ([termini upstream per componente](LICENSES/XZ-5.2.5.txt)), **Expat 2.6.0** per `pyexpat.pyd` ([testo MIT](LICENSES/Expat-2.6.0.txt)) e **zlib 1.3.1** ([licenza zlib](LICENSES/zlib-1.3.1.txt)). [Manifest PCbuild di CPython 3.11.9](https://github.com/python/cpython/blob/v3.11.9/PCbuild/get_externals.bat) · [Licenza OpenSSL 3.0.13](https://github.com/openssl/openssl/blob/openssl-3.0.13/LICENSE.txt) |
| **Mesa llvmpipe e LLVM** | `opengl32sw.dll` proviene dal wheel Qt. Il binario contiene i marcatori **Mesa 11.2.2** e **LLVM 3.6.2**, coerenti con la distribuzione storica Qt. Il [testo MIT del core Mesa](LICENSES/Mesa-11.2.2-core.txt) e la [licenza LLVM 3.6.2](LICENSES/LLVM-3.6.2.txt), University of Illinois/NCSA con avvisi di terze parti, sono inclusi. Completare il riscontro sui sorgenti, le patch Qt e gli avvisi di Gallium/llvmpipe e degli altri file effettivamente compilati nella DLL. Il testo del core Mesa copre solo il core. [Archivio Mesa 11.2.2](https://archive.mesa3d.org/older-versions/11.x/11.2.2/) · [Build llvmpipe Qt](https://wiki.qt.io/MesaLlvmpipe) · [Licenza LLVM 3.6.2](https://github.com/llvm/llvm-project/blob/llvmorg-3.6.2/llvm/LICENSE.TXT) |
| **Microsoft Visual C++ Runtime** | `VCRUNTIME140*.dll` in versione **14.38.33126.1** nella radice di `_internal` e **14.44.35211.0** nei pacchetti Qt/Shiboken, dove sono presenti anche `MSVCP140*.dll` **14.44.35211.0**. Sono componenti soggetti ai termini Microsoft. Prima di ridistribuire le DLL singole, verificare la provenienza di ciascuna copia, la lista REDIST e l'autorizzazione applicabile al distributore. [Redistribuzione Visual C++](https://learn.microsoft.com/en-us/cpp/windows/redistributing-visual-cpp-files?view=msvc-170) |
| **PyInstaller 6.22.3** | Il bootloader e i moduli di avvio sono presenti in `fuPDF.exe`. La licenza GPLv2 o successiva ha un'eccezione per i bundle generati. Alcuni file upstream sono Apache 2.0. Il [testo del progetto PyInstaller](LICENSES/PyInstaller-6.22.3.txt) è conservato come riferimento. Le licenze delle dipendenze del bundle si applicano anche con l'eccezione. [Licenza PyInstaller 6.22.3](https://pyinstaller.org/en/v6.22.3/license.html) |

La revisione IA usa la libreria standard Python per HTTPS. Il bundle non
contiene un SDK Python OpenAI. PySide6 Addons fa parte delle dipendenze del
wheel PySide6, mentre il candidato contiene i moduli Qt elencati sopra.

Nell'ambiente di build, altgraph 0.17.5, pefile 2024.8.26 e setuptools 65.5.0
hanno testi MIT. Pywin32-ctypes 0.2.3 usa BSD-3-Clause e packaging 26.3 offre
Apache-2.0 oppure BSD-2-Clause. Il testo del wheel `pyinstaller-hooks-contrib`
2026.8 distingue gli hook di build (GPL-2.0-or-later) dagli eventuali hook
runtime distribuiti (Apache-2.0). Nell'archivio ispezionato gli hook di avvio
provengono da PyInstaller. Pytest (MIT) è una dipendenza di test.
[Dipendenze di build](packaging/windows/requirements-build.txt) · [Metadati del progetto](pyproject.toml)

## Sorgenti corrispondenti e Qt sostituibile

GNU AGPLv3 è coerente con l'opzione open source del motore Artifex. Per
distribuire il binario, predisporre il sorgente corrispondente ai sensi
delle sezioni 1 e 6. Deve coprire l'esatto codice fuPDF usato, le dipendenze
coperte distribuite con esso, patch, configurazioni e script necessari per
generare, installare ed eseguire il programma dove applicabile. Pubblicare le
istruzioni per ottenerlo accanto al download, senza costo aggiuntivo, e
verificare che resti disponibile secondo la modalità scelta per il §6.
Un riferimento a una pagina upstream generica lascia da verificare la
corrispondenza effettiva. Il §13 disciplina la compatibilità con componenti
GPLv3 combinati e l'offerta del sorgente agli utenti che interagiscono via
rete con una versione modificata. [GNU AGPLv3](LICENSE)

Per la modalità LGPLv3 verificare gli obblighi del §4 sul pacchetto finale.
Fornire un avviso evidente dell'uso delle librerie, i testi LGPLv3/GPLv3 e
il sorgente corrispondente delle parti LGPL, anche se usate senza modifiche.
Mantenere i diritti di modifica delle parti LGPL e di reverse engineering
necessario a diagnosticare tali modifiche. La verifica della via del
collegamento condiviso (§4(d)(1)) deve dimostrare che il bundle funziona con
librerie modificate compatibili a livello di interfaccia, inclusi binding
e plugin pertinenti. L'esistenza di DLL separate nel bundle PyInstaller
lascia aperta questa verifica perché parte del codice Python è congelata
nell'EXE. Se il meccanismo condiviso risulta insufficiente, il §4(d)(0)
prevede sorgente minimo corrispondente e codice applicativo corrispondente,
in forma e con istruzioni che permettano la ricombinazione o il relink.
Fornire le informazioni di installazione quando richieste dal §4(e).
[Testo LGPLv3](LICENSES/LGPL-3.0-only.txt) · [Obblighi open source Qt](https://www.qt.io/development/open-source-lgpl-obligations)

Per il primo ZIP pubblico restano da verificare gli avvisi dei componenti
inclusi o compilati nei wheel MuPDF e Qt, i cataloghi delle traduzioni,
le patch e le attribuzioni Mesa/LLVM, i permessi Microsoft, la disponibilità
dei sorgenti corrispondenti e la sostituzione o il relink delle librerie Qt.
Il candidato locale contiene i due avvisi dei font. La procedura di creazione
del pacchetto pubblico dovrà includere anche gli altri avvisi, i testi e le
indicazioni sui sorgenti applicabili al bundle definitivo.

## Font e altre risorse

- **Source Sans 3 3.052**, quattro TTF. I metadati identificano Adobe e
  il disegnatore Paul D. Hunt. Mantenere la licenza SIL OFL 1.1, il relativo
  avviso di copyright e il Reserved Font Name «Source».
  [Avviso distribuito](src/pdf_annotator/assets/fonts/SOURCE_SANS_3_LICENSE.md) · [Release Adobe](https://github.com/adobe-fonts/source-sans/releases/tag/3.052R)
- **Courier Prime 3.018**, quattro TTF. I metadati indicano Alan Dague-Greene
  e Quote-Unquote Apps. Preservare l'avviso degli autori del progetto e
  la licenza SIL OFL 1.1.
  [Avviso distribuito](src/pdf_annotator/assets/fonts/COURIER_PRIME_OFL.txt) · [Sorgenti Courier Prime](https://github.com/quoteunquoteapps/CourierPrime)
- Le icone dell'interfaccia disegnate in codice seguono la verifica dei diritti
  sui contributi al progetto. Segoe UI è usato come font di sistema Windows.
  [Icone disegnate nel codice](src/pdf_annotator/app/ui_chrome.py)

### Branding fuPDF

| Risorsa | Provenienza e autorizzazione |
| --- | --- |
| [`fupdf.png`](src/pdf_annotator/assets/branding/fupdf.png) | Logo generato appositamente con ChatGPT, senza immagini di terzi come base, come confermato dal titolare del progetto. Il titolare ne autorizza la distribuzione insieme a fuPDF nei sorgenti e nel pacchetto Windows. |
| [`fupdf.ico`](src/pdf_annotator/assets/branding/fupdf.ico) | Deriva dal PNG tramite lo [script di preparazione](packaging/windows/prepare.py). L'autorizzazione copre questa variante e l'icona incorporata nell'eseguibile. |
