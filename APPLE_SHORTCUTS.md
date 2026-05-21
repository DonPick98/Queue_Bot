# Upload da iPhone con Apple Shortcuts

Si: puoi mandare foto e video dalla galleria al bot senza aprire Telegram. Il flusso migliore e:

1. condividi una o piu foto/video da Foto su iPhone;
2. tocchi una scorciatoia;
3. la scorciatoia chiama l'API privata del bot;
4. il bot mette tutto in coda, senza pubblicare subito sul canale.

L'API usa questi endpoint gia inclusi nel progetto:

```text
POST /api/queue/photo
POST /api/queue/video
```

## Prima di iniziare

Nel servizio cloud devi avere queste variabili ambiente:

```env
PORT=8080
QUEUE_API_TOKEN=<un-token-lungo-a-tua-scelta>
QUEUE_API_STAGING_CHAT_ID=<tuo-user-id-telegram>
QUEUE_API_DELETE_STAGING=true
QUEUE_API_MAX_MB=50
```

`QUEUE_API_TOKEN` non e il token Telegram del bot: e una password privata solo per l'upload veloce. Usa una stringa lunga e casuale.

Quando Tranger ti da l'URL pubblico dell'app, userai:

```text
https://TUO-DOMINIO/api/queue/photo
https://TUO-DOMINIO/api/queue/video
```

Puoi testare da PC con:

```powershell
curl.exe -X POST "https://TUO-DOMINIO/api/queue/photo" -H "Authorization: Bearer IL_TUO_QUEUE_API_TOKEN" -F "media=@C:\percorso\foto.jpg"
```

## Opzione A: solo Apple Shortcuts

Questa e la via piu semplice se vuoi due scorciatoie separate: una per foto e una per video.

### Scorciatoia "Queue Photo"

1. Apri Comandi Rapidi su iPhone.
2. Crea una nuova scorciatoia chiamata `Queue Photo`.
3. Apri le impostazioni della scorciatoia e abilita `Mostra nel foglio di condivisione`.
4. In `Riceve`, lascia solo `Immagini` e `File`.
5. Aggiungi azione `Ripeti con ogni elemento` usando `Input scorciatoia`.
6. Dentro il repeat, aggiungi azione `URL`:

```text
https://TUO-DOMINIO/api/queue/photo
```

7. Aggiungi azione `Ottieni contenuti dell'URL`.
8. Imposta:

```text
Metodo: POST
Intestazioni:
  Authorization: Bearer IL_TUO_QUEUE_API_TOKEN
Corpo richiesta: Modulo
Campi modulo:
  media = Elemento ripetizione
```

9. Alla fine aggiungi `Mostra notifica` con un testo tipo:

```text
Foto inviata a Queue Bot
```

### Scorciatoia "Queue Video"

Duplica `Queue Photo` e cambia:

```text
Nome: Queue Video
Riceve: Media e File
URL: https://TUO-DOMINIO/api/queue/video
```

Il campo modulo resta:

```text
media = Elemento ripetizione
```

## Opzione B: Scriptable, una sola azione

Se vuoi una sola voce nel foglio di condivisione, usa lo script:

```text
apple_shortcuts/QueueToBot.scriptable.js
```

### Setup Scriptable

1. Apri Scriptable su iPhone.
2. Crea un nuovo script chiamato `QueueToBot`.
3. Incolla il contenuto di `apple_shortcuts/QueueToBot.scriptable.js`.
4. Cambia queste due righe:

```javascript
const BASE_URL = "https://TUO-DOMINIO";
const QUEUE_API_TOKEN = "IL_TUO_QUEUE_API_TOKEN";
```

5. Nelle impostazioni dello script abilita il foglio di condivisione per:

```text
Images
File URLs
```

6. Per video grandi abilita `Run in App`, cosi iOS non interrompe lo script per limiti di memoria.

Da Foto: selezioni media, condividi, scegli Scriptable/QueueToBot. Lo script capisce foto o video dall'estensione; se non riconosce il file, ti chiede di scegliere.

## Caption

Il bot accetta anche un campo `caption`. In Shortcuts puoi aggiungerlo come secondo campo del modulo:

```text
caption = Testo che vuoi
```

Nello script Scriptable puoi attivare la richiesta caption cambiando:

```javascript
const ASK_FOR_CAPTION = true;
```

La stessa caption verra usata per tutti i media selezionati in quel giro.

## Duplicati e messaggi temporanei

L'upload via API non pubblica sul canale. Per ottenere un `file_id` valido, il bot invia prima il media alla chat privata di staging, poi lo salva in coda.

Con:

```env
QUEUE_API_DELETE_STAGING=true
```

quel messaggio temporaneo viene cancellato automaticamente.

La deduplica resta attiva: se mandi una foto o un video gia presenti in coda o gia pubblicati, il bot risponde con `queue_status: duplicate` e non crea un doppione.
