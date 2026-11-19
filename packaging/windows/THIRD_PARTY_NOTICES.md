# Avvisi di terze parti della candidata Windows x64

Questo inventario si applica alla candidata Python 3.14.10 compilata
dall'esportazione del commit pubblico identificato da RELEASE-SOURCE.json
e RELEASE-ASSETS.json. Queste ricevute e SHA256SUMS.txt identificano
l'input e gli ZIP della singola compilazione, senza riferimenti circolari
al commit nei documenti tracciati. M22.7 ha già prodotto e verificato
una prima coppia di ZIP, distinta dalle candidate successive.
Non vengono distribuiti CRT Microsoft app-local o installer Microsoft.
Il titolare riferisce PASS su Windows 11 pulito per avvio, prerequisito
Visual C++ x64 ufficiale, rendering, annotazioni, salvataggio, riapertura
e finestra Revisione con IA. La versione installata del runtime in quella
prova non è stata comunicata. Gli ZIP M22.7 restano immutati.
La [matrice M22.8](https://github.com/ErnestoGallio/fuPDF/blob/main/tests/manual/windows_redistribution.md#verifica-m228-degli-obblighi-effettivi)
distingue obblighi soddisfatti e dubbi residui, senza imporre prove forensi
non richieste dalle licenze.

| Componente nel pacchetto | Versione e licenza |
| --- | --- |
| fuPDF | GNU AGPLv3. Testo integrale in `LICENSE`. |
| PyMuPDF, MuPDF | 1.28.2, opzione GNU AGPLv3. I testi `LICENSES/PyMuPDF-1.28.2.txt` e `LICENSES/MuPDF-1.28.2.txt` provengono rispettivamente dal wheel e dalla tarball Artifex. Preservare gli avvisi Artifex e quelli dei componenti e font incorporati in MuPDF. |
| Qt per Python e Qt | PySide6 Essentials, Shiboken6 e Qt 6.11.2. Il pacchetto usa Qt Core, Gui, Widgets e Network, i plugin Windows, ICO, stile, informazione di rete e TLS. Sono incluse le licenze GNU LGPLv3 e GPLv3. Applicare la LGPLv3 dove offerta. Copyright (C) The Qt Company Ltd. and other contributors, come indicato nelle DLL. I 96 cataloghi Qt, QtBase e Qt Help seguono l'opzione LGPLv3 per i file di modulo di QtTranslations. Resta aperta la mappatura delle librerie incorporate in QtBase. |
| CPython | 3.14.10 x64, `python314.dll`, `python3.dll`, estensioni e libreria standard. Il file `LICENSES/CPython-3.14.10.txt` è quello fornito dalla distribuzione PSF e include termini storici e avvisi di terze parti, fra cui bzip2 1.0.8. |
| Dipendenze native CPython | OpenSSL 3.5.9 (Apache 2.0), libffi 3.4.4 (MIT), XZ 5.2.5 (termini per componente), Expat 2.9.0 (MIT), zlib-ng 2.2.4 (zlib), Zstandard 1.5.7 (opzione BSD), mpdecimal 2.5.1 (BSD). OpenSSL è verificato nei PE. Le altre versioni derivano dal manifest Windows o dai sorgenti vendorizzati del tag CPython. |
| Mesa e LLVM | Il file Qt `opengl32sw.dll` riporta marcatori Mesa 11.2.2 e LLVM 3.6.2. Sono inclusi il testo MIT del core Mesa, gli avvisi sorgente VMware di llvmpipe e WGL e il testo LLVM/NCSA. Le sezioni PE corrispondono alla build Qt Win64 pubblicata. Resta il dubbio documentale sulla copertura degli ulteriori avvisi applicabili; MIT/NCSA non richiedono in generale le patch esatte. |
| Microsoft Visual C++ Runtime | La candidata M22.7 usa il Visual C++ Redistributable x64 v14 ufficiale più recente come prerequisito di sistema. Le copie app-local e l'installer sono esclusi. Le versioni **14.51.36247.0** (PSF) e **14.44.35211.0** (wheel Qt/Shiboken) riguardano la precedente variante app-local. |
| PyInstaller | 6.22.3, bootloader e codice di avvio. Il testo incluso documenta GPLv2 o successiva con eccezione per il bundle e i file Apache 2.0. |
| Font | Source Sans 3 3.052 e Courier Prime 3.018, SIL OFL 1.1, con gli avvisi in `_internal/pdf_annotator/assets/fonts`. |

## Attribuzioni dei componenti incorporati

I messaggi e le stringhe dei binari confermano riferimenti specifici a Brotli,
FreeType, HarfBuzz, jbig2dec, Little CMS, Leptonica, OpenJPEG e Tesseract
nel motore MuPDF. I testi delle versioni vendorizzate sono raccolti in
LICENSES/MuPDF-1.28.2-vendored-notices.txt. Per QtBase sono stati riscontrati
PCRE2 in QtCore e FreeType, HarfBuzz e libpng in QtGui. I relativi testi
vendor, inclusi gli avvisi ausiliari dei sorgenti FreeType e PCRE2, sono in
LICENSES/QtBase-6.11.2-vendored-notices.txt. Le raccolte conservano le
attribuzioni presenti negli archivi upstream verificati. Restano da
confermare opzioni di compilazione e copertura completa per gli altri
componenti effettivamente incorporati. Per i font MuPDF sono stati
identificati byte per byte 177 blob. Il file
LICENSES/MuPDF-1.28.2-font-notices.txt riunisce il NOTICE Droid, i testi
OFL di Noto, Charis SIL e URW e i copyright estratti dai font SFNT
corrispondenti. L'inventario per file e SHA-256 è in
packaging/windows/mupdf-embedded-fonts.json. I font che non hanno un
riscontro byte per byte sono indicati a parte. Il mancato riscontro
non stabilisce se siano presenti in forma trasformata. Gli avvisi noti di Mesa,
llvmpipe e LLVM sono inclusi. La mappatura completa delle unità della build
storica Mesa resta aperta.

## Riferimenti per i sorgenti corrispondenti

- **fuPDF** — il commit pubblico e il tree della candidata sono registrati
  in RELEASE-SOURCE.json; usare RELEASE-ASSETS.json per il legame con gli
  asset. Il riferimento storico M22.7 è il commit
  `07a992d46f8b8c0d1dd5b3c737e0922b85578a8a`, tree
  `5979e440fca7ee40dc30417dcbf26444c448e1fb`.
  Lo ZIP sorgenti contiene l'export esatto con pin, spec e script di build,
  otto archivi upstream e RELEASE-SOURCE.json. RELEASE-ASSETS.json abbina
  i due ZIP per SHA-256. La futura pubblicazione deve offrire sorgenti
  e binario con accesso equivalente senza costo aggiuntivo per il sorgente.
- **PyMuPDF/MuPDF** — consultare [setup.py 1.28.2](https://github.com/pymupdf/PyMuPDF/blob/1.28.2/setup.py),
  il [rilascio PyPI](https://pypi.org/project/PyMuPDF/1.28.2/#files) e il
  [sorgente MuPDF](https://mupdf.com/downloads/archive/mupdf-1.28.2-source.tar.gz)
  selezionato dal build upstream. Verificare il wheel Windows x64 ABI3
  (`ebd244918798502d7b4504c90410d1711a4d7675a32584ca30f1bab419ecbffe`) e lo
  sdist (`5e0be7908a715aa20333caddd73f1d6f01e4cd0c26e869fa2dd0b7f344da2249`).
  SHA-256 misurato sulla tarball Artifex scaricata dall'URL dichiarato
  dal wheel (`44075a84e329db55b9bef5f342a70fd26d69e48ad1d33cb89d9664581c641156`).
  Associare `_extra.pyd`, `_mupdf.pyd` e `mupdfcpp64.dll` ai file di wheel.
  Conservare la configurazione di build, il sorgente MuPDF con le dipendenze
  incorporate e le eventuali patch effettive. Versione e ricetta upstream
  sono note. Il file `_build.py` del wheel riporta il commit PyMuPDF
  `12786d12b2962a1d77fd13b103162eddf749644c` (tag 1.28.2), SWIG 4.4.1 e
  l'URL della tarball MuPDF. Il tag MuPDF 1.28.2 punta a
  `fe374accd98a43174a328fa7980d7675e06d5b0d`. La configurazione completa
  del wheel pubblicato e le attribuzioni
  del motore incorporato non sono state ricostruite da questi soli file.
- **Qt e binding** — conservare le versioni corrispondenti di
  [pyside-setup v6.11.2](https://github.com/pyside/pyside-setup/tree/v6.11.2),
  [qtbase v6.11.2](https://github.com/qt/qtbase/tree/v6.11.2) e
  [qttranslations v6.11.2](https://github.com/qt/qttranslations/tree/v6.11.2),
  con i sorgenti degli altri moduli richiamati dai cataloghi trattenuti. Usare
  i wheel [Essentials](https://pypi.org/project/PySide6-Essentials/6.11.2/#files)
  (`c8a29def77032773a30879f7f24415b5395ad08592d147c170824ef4c735dfc1`)
  e [Shiboken](https://pypi.org/project/shiboken6/6.11.2/#files)
  (`6ab0eba1c904455df621f9a6df3ca2bb896bab8670572d2bc4e37804ae91f19a`).
  Per ricostruire il gruppo coerente catturare revisioni, patch, impostazioni
  CMake, Qt SDK usato da `--qtpaths`, toolchain MSVC, ABI CPython, plugin e
  cataloghi. Il binding riporta Qt x64 shared release, MSVC 2022 e ABI3
  di base CPython 3.10. I tag documentano la versione. La configurazione esatta
  usata per le DLL precompilate resta da confermare. Seguire la
  [licenza Qt](https://doc.qt.io/qt-6.11/licensing.html),
  gli [obblighi LGPL](https://www.qt.io/development/open-source-lgpl-obligations)
  e le [regole dei cataloghi](https://github.com/qt/qttranslations/blob/v6.11.2/licenseRule.json).
- **CPython e librerie native** — [tag v3.14.10](https://github.com/python/cpython/tree/v3.14.10),
  [PCbuild](https://github.com/python/cpython/blob/v3.14.10/PCbuild/get_externals.bat),
  [Expat vendorizzato](https://github.com/python/cpython/tree/v3.14.10/Modules/expat)
  e [libmpdec vendorizzato](https://github.com/python/cpython/tree/v3.14.10/Modules/_decimal/libmpdec).
  I testi aggiunti provengono dai tag indicati o dal sorgente CPython.
- **Mesa e LLVM** — [sorgente Mesa 11.2.2](https://archive.mesa3d.org/older-versions/11.x/11.2.2/),
  [licenza LLVM 3.6.2](https://github.com/llvm/llvm-project/blob/llvmorg-3.6.2/llvm/LICENSE.TXT)
  e [ricetta llvmpipe Qt](https://wiki.qt.io/MesaLlvmpipe). Questi riferimenti
  sostengono la provenienza; l'obbligo delle licenze permissive identificate
  è conservare gli avvisi applicabili, non ricostruire la patch list storica.

Gli archivi M22.7 sono già acquisiti e verificati. Per ripetere la
preparazione, scaricarli dalle fonti indicate e confrontare
`Get-FileHash -Algorithm SHA256` con i digest ufficiali disponibili. Registrare
anche commit, hash degli archivi sorgente, patch e istruzioni di build.
Confrontare i file estratti dai wheel con gli hash dell'inventario binario
interno e pubblicare le informazioni necessarie senza percorsi locali. I
riferimenti qui raccolti non costituiscono ancora un'offerta del sorgente
corrispondente ai sensi delle licenze.

La candidata M22.7 non ridistribuisce DLL Visual C++ né l'installer:
non occorre quindi un titolo di redistribuzione app-local per file assenti.
Il destinatario ottiene l'ultimo runtime x64 v14 supportato direttamente
da Microsoft. [Istruzioni](WINDOWS-PORTABLE.md).

La sostituzione del gruppo dinamico Qt/PySide6/Shiboken 6.11.2 con quello
ufficiale 6.11.3 ha superato la prova M22.6e senza ricompilare l'EXE.
[Istruzioni e diritti di modifica e debug](WINDOWS-PORTABLE.md#licenze-sorgenti-e-qt).
Il meccanismo condiviso non sostituisce l'obbligo separato di fornire
il sorgente delle librerie LGPL.

Questo software si basa in parte sul lavoro del FreeType Team.
L'attribuzione esplicita risponde al §2 della FreeType License, già
riprodotta nelle raccolte degli avvisi MuPDF e Qt.

AGPLv3 §1 richiede il sorgente necessario a generare, installare, eseguire
e modificare il programma, inclusi gli script pertinenti. LGPLv3 §4
disciplina la sostituzione. Non sono richiesti di per sé SBOM ufficiale,
log originale del produttore o una ricompilazione con hash identico.
Eventuali modifiche o script necessari mancanti resterebbero obbligatori;
l'assenza di un log non dimostra da sola una lacuna del sorgente.
Le licenze permissive Mesa/LLVM identificate richiedono gli avvisi
applicabili, non in generale l'offerta di sorgenti e patch. I dubbi sulla
copertura degli avvisi sono classificati nella matrice M22.8.
