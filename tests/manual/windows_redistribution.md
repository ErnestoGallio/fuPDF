# Qualifica di redistribuzione Windows x64

Ambito della prova M22.6e: bundle Windowed Python 3.14.10 della candidata
M22.6c, con Qt/PySide6/Shiboken 6.11.2. Le prove usano copie diagnostiche
fuori dal checkout, avviate sullo stesso host. I binari e l'icona originali
restano intatti.

## Matrice di conformità

| Componente | Obbligo | Evidenza | Intervento | Verifica residua | Blocca lo ZIP? |
| --- | --- | --- | --- | --- | --- |
| fuPDF, PyMuPDF/MuPDF | AGPLv3 §§1, 4–6. Testi e avvisi, accesso equivalente al sorgente corrispondente del binario offerto. | Archivi sorgente verificati, wheel e commit PyMuPDF riscontrati. Il `config.h` consegnato nel wheel coincide byte per byte con quello della tarball MuPDF. | Staging dei sorgenti upstream e procedura per abbinare il sorgente fuPDF al futuro binario. | Chiudere opzioni di build effettive, eventuali patch ulteriori e lista delle librerie e dei font compilati con relativi avvisi. Offrire il sorgente del commit effettivamente compilato. | Sì, fino a un'offerta completa e agli avvisi applicabili. |
| Qt per Python, Shiboken, QtBase | Per il ramo LGPLv3, avvisi e licenze, sorgenti delle librerie, diritti di modifica e debug e una modalità del §4(d). | Import ed export normali e differiti compatibili con il gruppo ufficiale Qt/PySide6/Shiboken 6.11.3. Caricamento effettivo dai percorsi portabili. | Apertura, worker, Salva con nome e riapertura superati con il gruppo alternativo senza ricompilare l'EXE. Archivi QtBase/PySide acquisiti; attribuzione Qt riscontrata nei PE. | Mappare terze parti e configurazione del wheel 6.11.2, distribuire le istruzioni di sostituzione e i sorgenti pertinenti. Verificare che la configurazione finale non imponga blocchi alla sostituzione. | Sì, finché sorgenti e avvisi non sono completi. La prova di sostituzione non è più soltanto statica. |
| Cataloghi Qt | Conservare licenza e sorgenti delle traduzioni distribuite. | 32 `qtbase_*`, 28 `qt_help_*`, 36 `qt_*`. Il file `licenseRule.json` di QtTranslations 6.11.2 contempla LGPLv3 per questi file di modulo. Gli aggregatori contengono riferimenti a `qtbase` e `qtmultimedia`; i cataloghi Qt Multimedia non sono presenti nel bundle. | Verificati conteggi e dipendenze dai binari `.qm`; messi in staging QtTranslations e QtTools. | Includere i sorgenti delle traduzioni nella stessa offerta Qt, con le istruzioni per `lrelease`. | Parte dell'offerta Qt; nessun obbligo da inferire per un catalogo Qt Multimedia solo referenziato. |
| Mesa/LLVM | Conservare gli avvisi per il codice distribuito e verificare eventuali termini ulteriori delle unità incluse. | Tutte le sezioni PE di `opengl32sw.dll` corrispondono alle distribuzioni Qt Win64 Mesa 11.2.2 pubblicate, con hash del file intero diversi. LLVM 3.6.2 e archivi upstream confermati. | Aggiunti avvisi sorgente VMware per llvmpipe e WGL; conservati MIT del core e licenza LLVM/NCSA. | Completare copertura delle attribuzioni delle unità effettive. Le patch esatte Qt della build storica restano da individuare. | La copertura degli avvisi resta da chiudere. Una ricostruzione identica non è richiesta dalle licenze permissive identificate. |
| Microsoft VC++ app-local | Titolo di redistribuzione applicabile alle copie incluse e rispetto delle condizioni REDIST. | Provenienza già riscontrata dai wheel Qt/Shiboken e dal runtime PSF. I termini Community autorizzano il Distributable Code entro le condizioni della licenza, incluso il passaggio delle tutele. Le licenze d'uso del runtime Microsoft 2015–2022 e 2026 non autorizzano di per sé la redistribuzione. | Consiglio operativo: usare il prerequisito ufficiale separato finché il titolo app-local non è documentato. | Per app-local, confermare titolo del distributore, provenienza REDIST o autorizzazione trasferita e rapporto fra termini Microsoft e AGPL. | Sì per il bundle attuale. Nessuna DLL Microsoft da ridistribuire con l'app nella variante a prerequisito. |
| CRT come prerequisito | Il destinatario installa il runtime originale da Microsoft se necessario. | Copia senza le dieci DLL. Import normali e differiti risolti. GUI e worker hanno usato `System32` 14.44.35211.0. Apertura e Save As superati. | Quantificata l'alternativa, con installer ufficiale x64 **14.52.37029.0**, 18.863.624 byte, Authenticode valido, SHA-256 `9c02c9ed120f671b900e6b9471d890e3b2414117a7e53f4ae2e855466fa2b7c9`. Installer acquisito per verifica, mai eseguito. | Su Windows x64 pulito verificare avvio dopo installazione ufficiale, comportamento senza prerequisito e ambito Windows dichiarato. Usare come base il redistributable supportato, senza dedurre dal test il minimo richiesto. | Nessun problema di ridistribuzione Microsoft nella variante senza copie app-local. Qualifica tecnica finale ancora necessaria. |

L'opzione con prerequisito riduce la cartella applicativa di **1.940.792
byte** (1,85 MiB). Su sistemi già provvisti del runtime, fuPDF resta
avviabile dopo estrazione. Su sistemi privi del runtime occorrono un
download separato di circa 18 MiB e una singola installazione di sistema,
con eventuale elevazione. L'installer va scaricato da Microsoft e non
incluso nello ZIP. Le cinque DLL di nome distinto omesse nella prova
sono `MSVCP140.dll`, `MSVCP140_1.dll`, `MSVCP140_2.dll`,
`VCRUNTIME140.dll` e `VCRUNTIME140_1.dll`.

## Ripetere il collaudo Qt

Seguire la [procedura di sostituzione](windows_packaging.md#collaudo-di-sostituzione-qt)
su una copia scrivibile dell'intero bundle. Per ripetere il risultato
usare i wheel Windows x64 ABI3 ufficiali
[PySide6 Essentials 6.11.3](https://pypi.org/project/PySide6-Essentials/6.11.3/#files)
e [Shiboken6 6.11.3](https://pypi.org/project/shiboken6/6.11.3/#files),
rispettivamente SHA-256
`ea61687d2c27f8cc7a851c259481be1001dbffa3bebacea4491d9905e7a5d0cf` e
`1d38919492d08417b7579ad077bc81110d05183d21eb21d66316bc556140a8a3`.
Sostituire, mantenendo i percorsi, i file Qt, binding, Shiboken, plugin e
cataloghi presenti nella copia, lasciando fuori dalla sostituzione il CRT.
Sono stati sostituiti 115 file, di cui 19 con byte diversi. I 96 cataloghi
erano identici. I moduli Python `__init__.py` e `_config.py` congelati
restavano quelli del bundle. Le differenze rispetto al donatore
riguardavano solo metadati di versione e data.

Gli import PE normali e ritardati dei binari della copia sono stati
risolti contro gli export del gruppo. Lo smoke ha verificato le DLL
Core/Gui/Widgets 6.11.3 caricate dalla copia, il worker, Salva con nome
e l'uguaglianza del raster e delle annotazioni del PDF salvato. Per
usare una build modificata partire dal gruppo coerente e ripetere la
stessa verifica. La firma Authenticode di fuPDF non dovrebbe essere usata
per vincolare le DLL a hash immutabili. Le policy Windows dell'utente
restano una verifica distinta.

## Variante M22.6f con CRT di sistema

Il comando seguente genera la variante in una directory di output separata.

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File packaging/windows/build.ps1 -Mode Windowed -DistributionCandidate -SystemVCRuntime
```

La variante esclude solo le cinque DLL di nome distinto verificate in M22.6e,
in qualunque directory del bundle. Il controllo finale rifiuta anche copie
rinominate riconoscibili nei metadati PE. La **14.44.35211.0** è la versione usata per lo smoke storico.
La distribuzione richiede il Redistributable x64 v14 supportato più recente.
La versione del download Microsoft riscontrata in M22.6g è **14.52.37029.0**.
La candidata include [le istruzioni di avvio e verifica del runtime](../../packaging/windows/WINDOWS-PORTABLE.md) senza installer Microsoft.

## Preparare i sorgenti della futura release

L'[inventario JSON](../../packaging/windows/source-archives.json) pinna
otto archivi upstream, con URL, SHA-256 e origine del digest. Scaricarli
dalle fonti indicate, preservando nomi e byte, poi comporre una cartella
di staging verificata con:

```powershell
& .\.venv\Scripts\python.exe -I -B packaging/windows/stage_source_archives.py --cache tmp/m226c-sources --cache tmp/m226e --output tmp/source-offer-dependencies
```

Il comando è offline, rifiuta file mancanti o con hash diversi e produce
`SHA256SUMS`. La sottocartella risultante è materiale di riferimento e
non include modifiche future né il sorgente del commit applicativo.

- **PyMuPDF/MuPDF**. Estrarre sdist e tarball. Verificare che lo
  SHA-256 di `include/mupdf/fitz/config.h` sia
  `134195a6dec13aa8e217882cd78b3610c8966f465d393be4bd9a373dfad429b4`.
  Nel prompt strumenti MSVC x64 usare il `setup.py` 1.28.2, SWIG
  4.4.1 e `PYMUPDF_SETUP_MUPDF_BUILD` puntato alla directory MuPDF
  verificata. Con il Python di build desiderato usare
  `python -m pip wheel --no-deps . --wheel-dir wheelhouse` dallo sdist
  estratto. Registrare toolchain, variabili, patch e configurazione
  effettiva. La coincidenza di `config.h` non esclude macro aggiunte
  dalla toolchain o opzioni fuori da quel file.
- **Qt**. Configurare QtBase shared release x64 con la toolchain MSVC
  compatibile. Registrare `CMakeCache.txt`, manifest dei componenti e
  rapporto di configurazione. Usare `pyside-setup` 6.11.2 con
  `--qtpaths` riferito a quell'installazione, gli script di wheel
  della versione e CPython ABI3. Per i cataloghi usare le sorgenti
  QtTranslations e il tool `lrelease` dello stesso rilascio. I sorgenti
  QtBase contengono 76 file `qt_attribution.json`, utili per mappare gli
  avvisi contro le feature della build effettiva. Conservare patch e
  script realmente usati.
- **Mesa/LLVM**. Gli archivi sono riscontri della versione upstream.
  Il file Qt ha SHA-256
  `34b444c016289b560662ff896deceb7f4b2c0723aed3d319ae167c9186ce42b3`.
  La variante storica Qt `signed_sha256` ha sezioni PE identiche ma
  DLL con SHA-256 `b04de4541863bc7d8879040a78889c4849c1b1da2784c4630f734c146c2998ce`.
  Usare i riferimenti storici Win64 e la procedura Qt per individuare
  patch e unità. Nessuna patch è dichiarata identica senza un riscontro.

Per l'offerta finale aggiungere allo staging l'archivio generato con
`git archive` dal commit effettivamente usato dalla build fuPDF, le
patch, gli avvisi e le istruzioni necessarie. Pubblicare il relativo
archivio sorgente, o più asset distinti, dalla stessa pagina dello ZIP.
Identificare commit e SHA-256 dei file. Fornire il download dei sorgenti
senza sovrapprezzo e mantenerlo disponibile insieme al binario. La
conformità si valuta sul sorgente effettivamente corrispondente, senza
richiedere in generale binari riproducibili byte per byte.

- [AGPLv3](../../LICENSE) e [LGPLv3](../../LICENSES/LGPL-3.0-only.txt)
- [Licenze Qt](https://doc.qt.io/qt-6.11/licensing.html), [attribuzioni Qt](https://doc.qt.io/qt-6.11/licenses-used-in-qt.html) e [regole QtTranslations](https://github.com/qt/qttranslations/blob/v6.11.2/licenseRule.json)
- [PyMuPDF setup](https://github.com/pymupdf/PyMuPDF/blob/1.28.2/setup.py) e [Qt llvmpipe](https://download.qt.io/development_releases/prebuilt/llvmpipe/windows/)
- [Visual Studio Community 2026](https://visualstudio.microsoft.com/license-terms/vs2026-ga-community/), [REDIST 2022](https://learn.microsoft.com/en-us/visualstudio/releases/2022/redistribution), [REDIST 2026](https://learn.microsoft.com/en-us/visualstudio/releases/2026/redistribution), [EULA runtime 2015–2022](https://visualstudio.microsoft.com/license-terms/vs2022-cruntime/), [EULA runtime 2026](https://visualstudio.microsoft.com/license-terms/vs2026-ga-visualcpp-v14-redist-runtime/) e [download ufficiale](https://learn.microsoft.com/en-us/cpp/windows/latest-supported-vc-redist)


## Verifica conclusiva M22.6g

Gli ZIP M22.6f esistenti restano invariati. I loro SHA-256 coincidono
con il manifesto locale. Lo ZIP sorgenti contiene 311 file applicativi,
otto archivi upstream e la corrispondenza per hash dei 45 file nativi
con il binario. La fotografia applicativa comprende 11 file del tree
pubblico modificati e 16 file aggiunti. Il commit pubblico di base
non identifica quindi da solo il sorgente della candidata.

| Requisito | Stato | Evidenza disponibile | Intervento indispensabile | Impedisce la distribuzione? |
| --- | --- | --- | --- | --- |
| Prerequisito VC++ x64 | Risolto come requisito documentato | CPython 3.14.10 dichiara MSC 1944. CPython, OpenSSL, Qt e Shiboken mostrano linker PE 14.44 e le relative tabelle Rich contengono anche build 35227 o 35229. MuPDF usa linker 14.29 e libffi 14.34. Il bootloader 14.51 non importa direttamente le DLL VC++ v14. Il 14.51.36247.0 in dotazione PSF è la versione del redistributable copiato, come distingue la ricetta CPython del tag. Microsoft richiede un runtime almeno altrettanto recente della toolchain usata. | Richiedere l'ultimo VC++ v14 x64 supportato. Il download ufficiale verificato è **14.52.37029.0**. Sostituire le istruzioni vecchie alla prossima build. Lo smoke 14.44.35211.0 resta evidenza storica. | Blocco operativo per il pacchetto con istruzioni obsolete. La variante senza app-local non richiede autorizzazione a ridistribuire quelle DLL. |
| Sorgente fuPDF del commit di release | Risolto il meccanismo, commit di release ancora da fissare | Gli ZIP esistenti sono abbinati per hash. L'export da Git produce commit, tree e SHA-256 dell'archivio. | Compilare dal futuro archivio estratto e offrire ricevuta, sorgente esatto e hash degli asset sulla stessa pagina del binario. | Sì, obbligo AGPL per il sorgente effettivamente distribuito. |
| Font MuPDF | Risolti i 177 font identificati. Verifica residua per altri blob | Riscontro byte per byte di 157 Noto, 15 URW, quattro Charis SIL e un Droid. Aggiunti NOTICE e OFL ufficiali e metadati di copyright dei font SFNT riscontrati. | Per Source Han Serif, Droid Full e i tre Noto senza corrispondenza raw, verificare la lista delle risorse effettivamente trattenute da libresources/libmupdf e se il processo le trasforma. Solo se presenti, includere gli avvisi applicabili. | Gli avvisi aggiunti vanno inclusi nella nuova candidata. L'eventuale blocco residuo riguarda risorse realmente distribuite e prive di avvisi. |
| PyMuPDF/MuPDF, sorgente e unità native | Non risolto | Wheel, sdist, tarball MuPDF, revisioni e config.h verificati. Le stringhe e il progetto Windows riscontrano Tesseract/Leptonica e gli altri componenti già elencati negli avvisi. Il progetto libresources.vcxproj descrive input, senza provare quali oggetti sopravvivano al link. | Acquisire log e configurazione della build del wheel per mupdfcpp64.dll, con eventuali patch e lista degli oggetti di libmupdf-third/libresources. In alternativa, ricostruire questi binari da una ricetta verificata e conservare opzioni, sorgenti, patch e avvisi di quella build. | Sì finché non si può offrire il sorgente corrispondente e gli avvisi delle unità trattenute. L'uguaglianza bit per bit tra due ricompilazioni non è un obbligo generale. |
| Qt/PySide6/Shiboken 6.11.2 | Non risolto per la build dei wheel | Tarball QtBase/PySide/QtTranslations/QtTools e hash verificati. Import e stringhe identificano PCRE2 in QtCore, libpng e HarfBuzz in QtGui e caricamento runtime di libssl-3/libcrypto-3 nel backend OpenSSL. Schannel usa le API Windows. Il wheel fornisce la configurazione CMake del binding, senza la configurazione QtBase usata per le sue DLL. La sostituzione del gruppo con 6.11.3 è già provata. | Acquisire il rapporto di configurazione o SBOM riferibile al Qt x64 del wheel, con feature e scelta bundled/system per QtCore e QtGui, e opzioni di build dei binding e plugin. Completare i relativi avvisi o usare build open source proprie con configurazione registrata. Offrire i sorgenti e le istruzioni già provate. | Sì per sorgenti e avvisi LGPL incompleti. Il collaudo di sostituibilità è un'evidenza tecnica già acquisita. |
| Mesa/LLVM in opengl32sw.dll | Avvisi noti completati. Copertura delle unità storiche non risolta | Sezioni PE coincidenti con il prebuilt Qt Win64. LLVM 3.6.2 LICENSE.TXT confrontato con la copia inclusa. Il target libgl-gdi collega WGL, ws_gdi, GLAPI, Mesa, compiler, Gallium e GLSL. Aggiunto l'avviso VMware del winsys GDI, oltre agli avvisi WGL e llvmpipe già inclusi. | Acquisire lista delle unità compilate/link map o attestazione degli avvisi di quella DLL, in particolare per glapi, compiler e glsl oltre ai blocchi già attribuiti. In alternativa usare un binario equivalente con attribuzioni documentate e collaudarlo. La patch list esatta è utile come prova, ma la ricostruzione bit per bit non è richiesta dai termini permissivi identificati. | Sì finché la copertura degli avvisi del binario non è dimostrata. Nessun blocco separato per la mancata ricostruzione identica. |
| Windows pulito e firma | Non risolto | Smoke riuscito solo su host esistente e EXE M22.6f non firmato. | Qualificare Windows x64 pulito con l'installer ufficiale e ottenere una firma attendibile per la distribuzione prevista. | Blocco operativo e di fiducia, distinto dalle licenze. |

Il confronto tecnico non prova diritti app-local per le DLL Microsoft,
e per questa variante non è necessario ottenerli. Se i fornitori non
possono documentare sorgenti o avvisi dei binari esistenti, serve materiale
upstream o una ricostruzione tracciata. Un parere legale può chiarire un
termine o un'autorizzazione ambigua, senza sostituire le evidenze tecniche.

- [Criterio di compatibilità Microsoft](https://learn.microsoft.com/en-us/cpp/porting/binary-compat-2015-2017)
- [Download VC++ x64](https://learn.microsoft.com/en-us/cpp/windows/latest-supported-vc-redist)
- [Ricetta CRT di CPython 3.14.10](https://github.com/python/cpython/blob/v3.14.10/PCbuild/pyproject.props)
- [Inventario dei font MuPDF](../../packaging/windows/mupdf-embedded-fonts.json) e [offerta dei sorgenti](../../packaging/windows/SOURCE-OFFER.md)
