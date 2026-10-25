# Handoff: MyAlarmClock

Uppdaterad 2026-10-25. Läs denna fil först när arbetet återupptas.

## Mål och avgränsning

Bygg en egen klockradio med Raspberry Pi, en lokal webbapp för larm,
Spotify Premium och lokala MP3-filer som reserv. Ingen Home Assistant
eller Google Calendar i första versionen.

Nu finns en fungerande terminalprototyp. Webbapp, återkommande larm och
installation på Raspberry Pi är framtida arbete; de är inte implementerade.

## Nuläge

- `spotify_test.py`: PKCE-inloggning, lista Spotify-enheter, starta spellista,
  startfördröjning, diagnostik, MP3-reserv och larmläge.
- `alarm_control.py`: styrloop, Spotify-/MP3-adaptrar, terminalkommandon,
  snooze, automatisk avstängning och säker felrapportering.
- `test_spotify_test.py` och `test_alarm_control.py`: senast 47 passerande
  offlinetester. Tester använder inga riktiga tokens och spelar ingen musik.
- `SPEC-alarm-control.md`: användarens godkända beteende.
- `tasks/plan.md` och `tasks/todo.md`: implementation och verifieringshistorik.

Vid handoffens början var Git-arbetsträdet rent. Senaste commit var
`5b720f2` (`improve alarm error handling and diagnostics`). Denna handoff
och uppdaterad teststatus skrivs som dokumentationsändringar; ingen commit
eller push görs i detta steg. Kontrollera alltid aktuell Git-status vid återstart.

## Godkänt beteende

- `--alarm` håller skriptet igång och äger larmets livscykel.
- `stop` + Enter pausar Spotify eller avslutar egen MP3-process och avbryter snooze.
- `snooze` + Enter tystar och återupptar efter inställd tid; standard 9 minuter.
- Automatisk avstängning: standard 30 minuter från första bekräftade starten,
  inklusive snoozetid. Snooze flyttar aldrig den slutliga stopptiden.
- Spotify återupptar befintlig uppspelning efter snooze. MP3 startas från början.
- Ctrl+C och stängd terminalindata avslutar med ett försök att tysta larmet.
- Utan `--alarm` lämnar ett lyckat starttest Spotify spelande efter skriptets slut.
- `--diagnose` observerar i 60 sekunder med ungefär 2 sekunders intervall och
  stoppar inte musiken. Kan inte kombineras med `--alarm`.
- MP3-reserven väljs vid Spotify-startfel när `--fallback` angetts. Senare
  Spotify-avbrott aktiverar ingen automatisk reserv. Paus-/återstartsfel
  startar inte heller reserven.

## Verifierat med verkligt ljud av användaren

1. Start av spellista på `Web Player (Chrome)`.
2. `stop` tystar musiken och avslutar larmet.
3. Snooze på 0.1 minuter tystar och återupptar efter 6 sekunder.
4. Automatisk avstängning efter 1 minut inklusive snooze.
5. Samma start, snooze och automatiska avstängning fungerar efter
   7200 sekunders Spotify-inaktivitet.

Tvåtimmarstestet kördes med datorn vaken och Chrome/webbspelaren öppen.
Det verifierar inte start efter datorns viloläge, stängd webbläsare, omstart,
nätverksavbrott eller körning på Raspberry Pi.

Senast lyckade kommando:

```bash
source .venv/bin/activate
python spotify_test.py --alarm \
  --device 'Web Player (Chrome)' \
  --playlist 'https://open.spotify.com/playlist/37i9dQZF1E38FhJg9v3yoF' \
  --delay 7200 \
  --snooze-minutes 0.1 \
  --auto-stop-minutes 1
```

För ett kort test: utelämna `--delay 7200`. Använd inte Spotify på andra
enheter med samma konto under ett inaktivitetstest.

## Tidigare fel och kvarvarande osäkerhet

Tidigare tystnade musiken efter skriptets slut trots att inget stopp skickades.
Ett senare diagnostiktest gav 28 statusprov och 0 API-fel med ökande låtposition.
I ett annat försök gav API:t startbekräftelse utan hörbart ljud, och paus kunde
först bekräftas efter ett nytt pauskommando. Den exakta orsaken till dessa
tidigare problem är inte fastställd; senare lyckade tester är ingen rotorsaksanalys.

Felrapporteringen har förbättrats: fast felorsak, körfas, feltyp och HTTP-status.
Vid verifieringstimeout visar en `ALARM`-rad enbart utvalda bool/null-fält.
`DIAG`-rader innehåller tid, enhet, spelar/pausad, låtposition och säkra API-fel.
API-status är inte bevis på hörbart ljud. Om problemet återkommer, spara
utskriften och notera vad som faktiskt hördes innan beteendet ändras.

## Nästa steg: manuellt MP3-test

Detta är rekommenderad fortsättning; inga nya funktioner behöver byggas först.

1. Kontrollera att `mpv` finns och välj en befintlig lokal MP3-fil med låg volym.
2. Testa filen separat med `--test-fallback --fallback '/absolut/sokvag/reserv.mp3'`.
   Detta fristående läge stoppas med Ctrl+C och har inte larmlägets kommandon.
3. Testa därefter automatisk reserv vid ett avsiktligt Spotify-startfel:

   ```bash
   python spotify_test.py --alarm \
     --device 'TEST-ENHET-SOM-INTE-FINNS' \
     --playlist 'https://open.spotify.com/playlist/37i9dQZF1E38FhJg9v3yoF' \
     --fallback '/absolut/sokvag/reserv.mp3' \
     --snooze-minutes 0.1 \
     --auto-stop-minutes 1
   ```

   Använd ett enhetsnamn som saknas i enhetslistan. Detta testar enhetsvalsfel,
   inte ett verkligt nätverksavbrott. Befintlig Spotify-inloggning krävs fortfarande.
4. Verifiera hörbart MP3-ljud, `stop`, snooze/återstart och automatisk avstängning.
   Kör separat stoppförsök och snooze-/automatiskt stoppförsök. Kontrollera att
   ingen MP3-process eller återstart blir kvar efter avslut.
5. Dokumentera resultaten. Därefter: specificera den lokala webbappen och
   hur larmstyrningen ska köras på Raspberry Pi innan nästa implementation.

## Miljö, hemligheter och kontroller

- Använd `.venv` (Python 3.14.8 vid testerna). Spotipy är låst till 2.25.1.
- `SPOTIPY_CLIENT_ID` måste finnas i terminalmiljön. PKCE använder inget client secret.
- Inloggning: `python spotify_test.py --login`. Enheter: `python spotify_test.py --list-devices`.
  Enhets-ID kan ändras; använd inte gamla loggade ID som beständig konfiguration.
- `.spotify-test/` innehåller privat OAuth-cache och ska inte trackas med Git.
  Läs, kopiera eller logga inte `token.json`. Mappen, `.venv`, Python-cache och
  `spotify-diagnostik.log` är ignorerade av Git.
- Läs projektets instruktioner och relevanta skills före kodändringar. Använd
  Context7 för aktuella externa biblioteks-/API-dokument när sådana behövs.
- Kör offlinekontroller från projektroten:

  ```bash
  PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m unittest -q
  git diff --check
  ```

- Håll dator och terminal igång under larmtest. Processdöd/strömavbrott kan
  förhindra automatisk avstängning. Synkrona Spotify-anrop kan fördröja kommandon.

## Förslag till startmeddelande nästa session

> Läs HANDOFF.md och fortsätt med manuellt test av MP3-reserven, inklusive
> stop, snooze och automatisk avstängning. Spotify har redan klarat
> tvåtimmarstestet på datorn med Chrome öppet. Ingen Home Assistant eller
> Google Calendar i första versionen.
