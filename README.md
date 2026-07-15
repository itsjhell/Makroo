# Makroo 🖱⏺

Autoclicker + registratore di macro per Windows, tutto in un programma.

## Avvio

Doppio click su **`Avvia Makroo.bat`** (oppure `python makroo.py` da terminale).

Requisiti: Python 3 con la libreria `pynput` (`pip install pynput` — già installata se hai usato questa cartella con Claude).

## Modalità Autoclicker 🖱

- **Intervallo** tra i click in millisecondi, secondi, minuti o ore
- **Tasto** sinistro o destro, click **singolo o doppio**
- **Ripetizioni**: numero fisso di click oppure loop infinito
- **Posizione**: cursore attuale oppure coordinate fisse (col pulsante *Scegli punto…* basta cliccare sullo schermo per fissarle)
- **Hotkey** avvia/ferma personalizzabili — default **F6** (stessa hotkey = comportamento start/stop alternato, stile OP Autoclicker)

## Modalità Macro ⏺

- Registra tastiera + click del mouse; spunta l'opzione per registrare **anche i movimenti** del mouse
- Riproduci la registrazione **N volte o in loop infinito**
- **Velocità di riproduzione**: 0.25x, 0.5x, 1x, 2x, 4x o personalizzata
- **Salva** la macro su file `.json` e **ricaricala** quando vuoi
- **Hotkey** personalizzabili — default **F7** (registra/ferma) e **F8** (riproduci/ferma)

## Note

- Le hotkey funzionano globalmente, anche quando Makroo non è in primo piano, e possono essere anche **pulsanti del mouse** (es. i tasti laterali Mouse 4/5).
- La spunta **"Sempre in primo piano"** tiene la finestra sopra le altre.
- Le hotkey del programma **non vengono registrate** nelle macro, e i click generati da Makroo non fanno scattare le hotkey né vengono ri-registrati.
- Tutte le impostazioni (hotkey comprese) vengono salvate in `makroo_config.json` alla chiusura e ricaricate al prossimo avvio.
- Se una riproduzione viene fermata a metà, i tasti rimasti premuti vengono rilasciati automaticamente (niente tasti "incastrati").
