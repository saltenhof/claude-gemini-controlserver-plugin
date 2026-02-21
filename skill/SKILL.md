---
name: gemini-pool-review
description: Sends a prompt (optionally with files) to Google Gemini via the Session Pool Service. Handles acquire/send/release automatically.
allowed-tools: Read, Bash, mcp__gemini-pool__gemini_acquire, mcp__gemini-pool__gemini_send, mcp__gemini-pool__gemini_release, mcp__gemini-pool__gemini_pool_status, mcp__gemini-pool__gemini_health, mcp__gemini-pool__gemini_pool_reset
argument-hint: "<prompt> [--file <path>] [--files <p1>,<p2>,...] [--images <p1>,<p2>,...] [--continue] [--owner <name>]"
---

# Gemini Review via Session Pool — Execution Plan

Du sendest einen Prompt (optional mit Dateien) an Google Gemini ueber den Session Pool Service.
Der Pool verwaltet mehrere Browser-Tabs. Du musst einen Slot acquiren, nutzen, und wieder
freigeben. Arbeite diesen Plan Schritt fuer Schritt ab.

## Kontext

- **Pool Service**: REST API auf `http://localhost:9200` (muss separat laufen)
- **MCP-Server**: `gemini-pool` (Thin Client, uebersetzt MCP-Tools in HTTP-Requests)
- **Kein API-Key noetig**: Browser-Automation mit persistentem Chrome-Profil
- **Verfuegbare MCP-Tools**:
  - `gemini_health()` — Lebendigkeitstest des Pool Service
  - `gemini_acquire(owner)` — Slot anfordern, liefert slot_id + lease_token
  - `gemini_send(slot_id, token, message, merge_paths?, file_paths?)` — Nachricht senden
  - `gemini_release(slot_id, token)` — Slot freigeben
  - `gemini_pool_status()` — Pool-Uebersicht (Slots, Queue, System)
  - `gemini_pool_reset()` — Kompletter Pool-Reset (Notfall)

## Argument-Parsing

Argumente: `$ARGUMENTS`

Zerlege `$ARGUMENTS` in:

1. **Prompt** (PFLICHT):
   - Der Text, der an Gemini gesendet wird
   - Alles was nicht zu einem Flag gehoert ist der Prompt
   - Beispiel: `"Reviewe dieses Konzept auf Schwaechen"`

2. **`--file <path>`** (optional):
   - Einzelne Textdatei, deren Inhalt IN den Prompt eingebettet wird
   - Wird gelesen und an den Prompt-Text angehaengt (wie bisher)
   - Beispiel: `--file T:\codebase\project\concept.md`

3. **`--files <path1>,<path2>,...`** (optional):
   - Mehrere Textdateien die zusammengefuegt (gemergt) als EIN Upload gesendet werden
   - Kommasepariert, absolute Pfade
   - Beispiel: `--files T:\docs\kap1.md,T:\docs\kap2.md,T:\docs\kap3.md`

4. **`--images <path1>,<path2>,...`** (optional):
   - Dateien die einzeln an Gemini gesendet werden (Bilder, PDFs, etc.)
   - Kommasepariert, absolute Pfade, max 9 (bzw. 8 wenn auch --files angegeben)
   - Beispiel: `--images T:\screenshots\ui.png,T:\docs\spec.pdf`

5. **`--continue`** (optional):
   - Statt einen neuen Slot zu acquiren: im bestehenden Slot weitermachen
   - Nur moeglich wenn bereits ein Slot aus einem vorherigen Aufruf aktiv ist
   - Nutzt die gespeicherten slot_id und token aus dem letzten Acquire

6. **`--owner <name>`** (optional):
   - Owner-Name fuer den Slot (Default: "gemini-review")
   - Beispiel: `--owner sub-agent-review`

## Execution Plan

### Phase 0: Pool Service pruefen

1. Rufe `gemini_health()` auf.
2. **Bei Erfolg** ("ok"): Weiter zu Phase 1.
3. **Bei Fehler**:
   - Dem Nutzer mitteilen: **"Der Gemini Pool Service ist nicht erreichbar."**
   - Hinweis: `start.cmd` in `T:\codebase\claude-gemini-controlserver-plugin` ausfuehren.
   - **STOP**.

### Phase 1: Argumente parsen und validieren

1. Extrahiere **Prompt**, **Flags** und **Dateipfade** aus `$ARGUMENTS`.
2. Validierung:
   - Prompt darf nicht leer sein. Falls leer: Fehlermeldung und STOP.
   - Falls `--file`: Pruefe ob Datei existiert (via Read-Tool).
   - Falls `--files`: Pruefe ob alle Pfade existieren.
   - Falls `--images`: Pruefe ob alle Pfade existieren, max 9 (bzw. 8 mit --files).

### Phase 2: Nachricht und Dateien vorbereiten

1. **Prompt zusammenbauen**:
   - Basistext ist der Prompt aus den Argumenten.
   - Falls `--file <path>`: Datei lesen und an Prompt anhaengen:
     ```
     {prompt}

     ---
     Dateiinhalt ({dateiname}):
     ---

     {dateiinhalt}
     ```

2. **Datei-Listen vorbereiten**:
   - `merge_paths`: Liste aus `--files` (oder leer)
   - `file_paths`: Liste aus `--images` (oder leer)

3. **Laengencheck**: Falls Prompt + eingebetteter Dateiinhalt > 50.000 Zeichen:
   - Nutzer warnen und Bestaetigung einholen.

### Phase 3: Slot acquiren

1. Falls `--continue` und ein aktiver Slot bekannt ist:
   - Ueberspringe Acquire, nutze bestehende slot_id und token.
   - Weiter zu Phase 4.

2. Sonst: Rufe `gemini_acquire(owner)` auf.
   - Owner = Wert aus `--owner` oder Default `"gemini-review"`.

3. **Ergebnis verarbeiten**:
   - **"Slot acquired"**: Extrahiere `slot_id` und `lease_token`. MERKE BEIDE WERTE.
   - **"Queued"**: Warte die geschaetzte Zeit, dann erneut `gemini_acquire` mit gleichem Owner.
     Wiederhole bis Slot zugeteilt oder max 5 Versuche.
   - **"Rejected"**: Pool voll. Nutzer informieren, `gemini_pool_status()` aufrufen fuer Details. STOP.

### Phase 4: An Gemini senden

1. Rufe `gemini_send` auf mit:
   - `slot_id`: Der gemerkter Slot-ID
   - `token`: Das gemerkte Lease-Token
   - `message`: Der zusammengebaute Prompt aus Phase 2
   - `merge_paths`: Die Textdatei-Liste aus Phase 2 (oder weglassen)
   - `file_paths`: Die Einzeldatei-Liste aus Phase 2 (oder weglassen)

2. **Bei Erfolg**: Weiter zu Phase 5.

3. **Bei Fehler**: Fehlerbehandlung (siehe unten).

### Phase 5: Antwort praesentieren und Slot freigeben

1. Pruefe die Antwort auf Encoding-Artefakte (`Ã¤`, `Ã¶`, `Ã¼`).
   Falls vorhanden: Hinweis an Nutzer.

2. Gib die Antwort aus:
   ```
   --- Gemini-Antwort ---

   {antwort}

   --- Ende Gemini-Antwort ---
   ```

3. **Slot freigeben** (PFLICHT, ausser bei `--continue`-Intention fuer Folgefragen):
   - Rufe `gemini_release(slot_id, token)` auf.
   - Falls der Nutzer wahrscheinlich Folgefragen hat (z.B. Review-Kontext):
     Frage den Nutzer: "Soll ich den Gemini-Slot offen halten fuer Folgefragen?"
     - Ja → Slot behalten, slot_id und token merken fuer naechsten Aufruf
     - Nein → Sofort releasen

## Fehlerbehandlung

### Bei gemini_send Fehler

**Stufe 1: Status pruefen**
- `gemini_pool_status()` aufrufen und analysieren:
  - `login: expired` → Nutzer informieren: "Google-Login abgelaufen. Im Chrome-Fenster einloggen."
  - `chrome: dead` → `gemini_pool_reset()` aufrufen, dann Retry.
  - Slot in ERROR → Slot war kaputt, neuen Slot acquiren.

**Stufe 2: Retry**
- `gemini_release(slot_id, token)` versuchen (egal ob Fehler).
- Neuen Slot acquiren via `gemini_acquire(owner)`.
- `gemini_send` erneut versuchen (max 1 Retry).

**Stufe 3: Eskalation**
- `gemini_pool_reset()` als letztes Mittel.
- Bei erneutem Fehler: Nutzer informieren mit konkreten naechsten Schritten.

### Bei gemini_acquire "queued"

- Warte `estimated_wait_s` Sekunden (via `Bash: sleep N`).
- Rufe `gemini_acquire` erneut auf mit GLEICHEM Owner.
- Wiederhole max 5x. Danach: Nutzer informieren, Pool ist ueberlastet.

### Bei lease_expired (410)

- Slot wurde wegen Inaktivitaet freigegeben (>5 Min ohne Aktion).
- Neuen Slot acquiren und Nachricht erneut senden.

## WICHTIG — Token-Handling

- `slot_id` und `lease_token` kommen AUS dem Ergebnis von `gemini_acquire`.
- Du MUSST dir beide Werte merken und bei JEDEM `gemini_send` und `gemini_release` mitgeben.
- Die Werte NICHT raten oder erfinden. Immer aus dem Acquire-Ergebnis nehmen.
- Bei Reattach (gleicher Owner nochmal acquire): Du bekommst denselben Slot + Token zurueck.

## Kontext-Limits (STRIKT)

Gemini hat pro Konversation (= pro Slot-Acquire) zwei harte Limits:

### Nachrichten-Limit: Max 20 Sends pro Kontext

- Zaehle jeden `gemini_send`-Aufruf innerhalb eines Acquires mit.
- Nach 20 Sends: Slot releasen, neu acquiren (= neuer Chat).
- Bei geplanten laengeren Sessions (z.B. kapitelweises Review):
  Vorab aufteilen in Bloecke von max 20 Nachrichten.

### Dateigroessen-Limit: Max 700 KB kumuliert pro Kontext

- Zaehle die Gesamtgroesse aller Dateien die innerhalb eines Acquires
  hochgeladen werden (merge_paths + file_paths + --file Inhalt).
- Das Limit ist **kumulativ ueber alle Sends** innerhalb eines Kontexts:
  - Send 1: 200 KB Datei → verbraucht: 200 KB, Rest: 500 KB
  - Send 2: 200 KB Datei → verbraucht: 400 KB, Rest: 300 KB
  - Send 3: 200 KB Datei → verbraucht: 600 KB, Rest: 100 KB
  - Send 4 mit 200 KB Datei: STOP → erst Release + neues Acquire noetig
- Sends OHNE Dateien zaehlen nicht gegen das Datei-Limit.
- Vor jedem Send mit Dateien: Dateigroessen pruefen (via Bash: `wc -c < datei`
  oder Read + Zeichenzaehlung). Summe der neuen Dateien + bisheriger Verbrauch
  darf 700 KB (716.800 Bytes) nicht ueberschreiten.
- Bei Ueberschreitung: Slot releasen, neu acquiren, dann senden.

### Kontext-Reset durchfuehren

Wenn eines der Limits erreicht ist:
1. `gemini_release(slot_id, token)` — aktuellen Slot freigeben
2. `gemini_acquire(owner)` — neuen Slot holen (= neuer Chat)
3. Zaehler zuruecksetzen (Nachrichten = 0, Dateien-KB = 0)
4. Weiterarbeiten im neuen Kontext

## Wichtige Regeln

1. **Immer Release**: JEDER Acquire MUSS mit einem Release enden. Ausnahme: Nutzer will explizit weiter chatten.
2. **Keine sensiblen Dateien**: Keine Credentials, .env, API-Keys an Gemini senden. Bei Verdacht warnen.
3. **Laengenlimit**: Bei Dateien ueber 50.000 Zeichen Nutzer warnen.
4. **Neuer Chat pro Aufruf**: Jeder Acquire startet einen neuen Chat (Slot wird nach Release navigiert).
5. **Projektunabhaengig**: Dieser Skill ist nicht an ein bestimmtes Projekt gebunden.
6. **Pool Service muss laufen**: Der Skill setzt voraus, dass `start.cmd` bereits ausgefuehrt wurde.
7. **Kontext-Limits einhalten**: Max 20 Nachrichten UND max 700 KB Dateien pro Kontext (siehe oben).
