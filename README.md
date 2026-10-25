# TAP ⇄ WAV per Commodore 64

Programma Python con interfaccia grafica per:

1. **TAP → WAV / audio**: convertire un file `.tap` in WAV oppure riprodurlo sull'uscita audio del PC, per registrarlo su una cassetta reale.
2. **WAV → TAP**: decodificare una registrazione audio e ricavarne un file `.tap`.
3. **Verifica**: controllare se una registrazione (file WAV o ingresso audio del PC) è coerente con il TAP sorgente.

Il file del programma è `tap2wav_gui.py`.

> Nota: il programma è stato provato con segnali simulati. Non è stato provato su un C64, una Datasette o un registratore reali: se qualcosa non carica, vedi la sezione «Problemi comuni».

---

## Requisiti

- Python 3.8 o superiore
- `numpy` (obbligatorio)
- `tkinter` (per l'interfaccia grafica)
- `sounddevice` + PortAudio (solo per riproduzione e registrazione dal PC; la conversione da e verso file funziona anche senza)

### Installazione

**Linux (Debian/Ubuntu)**
```
sudo apt install python3-tk libportaudio2
pip install numpy sounddevice
```

**Windows / macOS**
```
pip install numpy sounddevice
```
Su Windows e macOS `tkinter` è incluso nell'installazione standard di Python.

## Avvio

```
python3 tap2wav_gui.py
```

Test rapido delle funzioni di base, senza interfaccia grafica:
```
python3 tap2wav_gui.py --selftest
```

---

## Scheda 1 – TAP → WAV / Audio

Scegli il file TAP: sotto compaiono versione, numero di impulsi, durata e standard (PAL/NTSC).

### Parametri audio

| Parametro | Descrizione |
|---|---|
| Frequenza di campionamento | 96000 Hz consigliati. Per i turbo loader evita valori bassi: gli impulsi corti perdono precisione. |
| Clock del C64 | `auto` legge lo standard dal TAP (PAL se non indicato). Si può forzare PAL o NTSC. |
| Forma d'onda | `quadra` (default), `seno` o `triangolare`. Seno e triangolare sono utili con registratori che tagliano le alte frequenze. |
| Volume (%) | Livello del segnale. Applicato al WAV e alla riproduzione. |
| Inverti polarità | Capovolge il segnale. Da provare se il nastro non carica. |
| Canali | mono, stereo, solo sinistro, solo destro. |
| Formato WAV | 16 bit, 8 bit o 32 bit float. |
| Silenzio iniziale / finale | Secondi di silenzio prima e dopo il segnale. |

### Azioni

- **Salva come WAV…**: genera il file WAV.
- **Riproduci sull'uscita audio**: sceglie il dispositivo di uscita dall'elenco (↻ per aggiornarlo). Il campo «Inizia da (s)» permette di partire da un punto del nastro.
- **Stop**: ferma la riproduzione.
- **Tono pilota di calibrazione (30 s)**: riproduce un tono continuo per regolare il livello di registrazione.

### Per registrare su cassetta

1. Collega l'uscita audio del PC all'ingresso (line-in/aux) del registratore.
2. Riproduci il tono pilota e regola il livello di registrazione senza saturare.
3. Disattiva Dolby, riduzione del rumore e AGC/limiter del registratore: deformano l'onda.
4. Riproduci il TAP e registra.

---

## Scheda 2 – WAV → TAP

Scegli il WAV e i parametri di decodifica, poi «Decodifica e salva TAP…».

| Parametro | Descrizione |
|---|---|
| Canale audio | `auto` (sceglie il canale con più segnale), sinistro, destro, somma. |
| Fronte di misura | `discesa` (default, come il C64), `salita`, oppure `entrambi (TAP v2)` per ottenere un TAP v2 a mezze onde. |
| Soglia / isteresi (% del picco) | Più alta = più immune al rumore; più bassa = segue segnali deboli. |
| Offset DC: finestra mobile (ms) | `0` toglie solo la media globale (consigliato). Valori maggiori servono con segnali con deriva del livello. |
| Inverti polarità | Capovolge il segnale prima della decodifica. |
| Clock del C64 | PAL o NTSC (in `auto` usa PAL). |
| Versione TAP in uscita | 1 (default) oppure 0. |

Alla fine il programma mostra durata, fronti rilevati, impulsi trovati e i valori più frequenti (byte TAP, ad esempio `$30`, `$42`, `$56`): utile per capire a colpo d'occhio se la decodifica ha senso.

---

## Scheda 3 – Verifica WAV vs TAP

Confronta una registrazione con il TAP sorgente.

1. Scegli il **TAP sorgente**.
2. Scegli la **sorgente da verificare**:
   - **File WAV**: indica il file registrato.
   - **Registrazione dall'ingresso audio del PC**: scegli ingresso, frequenza, canali e durata massima; premi **● Registra**, fai partire la cassetta dall'inizio, poi **■ Stop e verifica**.
3. Regola, se serve, decodifica e confronto, poi **✔ Verifica coerenza**.

### Parametri di confronto

| Parametro | Descrizione |
|---|---|
| Tolleranza sulla durata (%) | Scarto massimo accettato per ogni impulso, dopo aver compensato la velocità del nastro. |
| Impulsi massimi per risincronizzare | Quanti impulsi persi o aggiunti il confronto prova a recuperare. |
| Compensa la velocità del nastro | Segue anche le variazioni di velocità (wow/flutter). |
| Rileva la polarità automaticamente | Prova entrambe le polarità e usa quella coerente. |
| Eventi mostrati nel rapporto | Quanti errori elencare. |

### Il rapporto

Contiene:

- un **verdetto**: coerente, quasi coerente o non coerente;
- il conteggio di impulsi corrispondenti, sostituiti, mancanti ed extra;
- la **velocità del nastro** rispetto al nominale e la sua oscillazione;
- lo scarto di durata (medio e 99° percentile);
- l'elenco dei primi errori con posizione nel tempo;
- un **grafico** dello scarto lungo il nastro (in rosso i punti con errori).

Una differenza nella lunghezza del tono pilota viene segnalata come informazione e non conta come errore. Il rapporto si salva con «Salva rapporto…», la registrazione dal PC con «Salva registrazione…» (WAV a 16 bit).

Durante la registrazione una barra mostra il livello: i picchi ideali sono tra il 50% e il 90%. La registrazione resta in memoria (a 96 kHz mono circa 0,4 GB ogni 15 minuti, il doppio in stereo).

---

## Preset

Ogni pannello ha un elenco di preset già pronti. Con **Salva…** puoi salvare i tuoi (compaiono con ★), con **Elimina** rimuoverli; i predefiniti non si possono eliminare.

Le impostazioni dell'ultima sessione e i preset personali sono salvati in:

```
~/.tap2wav_gui.json
```

Cancellando il file si torna alle impostazioni iniziali.

---

## Problemi comuni

**Il nastro non carica sul C64**
- Prova «Inverti polarità» nella scheda 1.
- Usa una frequenza più alta (96000 o 192000 Hz).
- Verifica il livello di registrazione e che Dolby/AGC siano disattivati.
- Registra e riverifica nella scheda 3 per vedere dove compaiono gli errori.

**La verifica dice «non coerente» anche se il nastro sembra buono**
- Controlla che il canale sia quello giusto e la soglia adatta al livello del segnale.
- Aumenta la tolleranza con il preset «Tollerante».
- Se il WAV non c'entra col TAP, il programma lo segnala dicendo che non riesce ad allineare i due segnali.

**Errore su audio o sounddevice**
- Installa `sounddevice` e, su Linux, `libportaudio2`.
- Se il dispositivo non accetta la frequenza scelta, prova 48000 Hz o un altro dispositivo.

**Troppo pochi o nessun impulso in decodifica**
- Abbassa la soglia di isteresi, controlla il canale e il livello della registrazione.

---

## Limiti

- Formati WAV letti: PCM a 8, 16, 24 e 32 bit e float a 32 e 64 bit.
- Le pause molto lunghe tra i blocchi vengono gestite, ma la rimozione dell'offset DC a finestra mobile può alterarne i tempi (per questo è disattivata di default).
- Perdite lunghe in mezzo ai dati vengono contate come errori e possono portare a un esito «non coerente».
- Seno e triangolare a frequenze di campionamento basse riducono la precisione sugli impulsi più corti.
