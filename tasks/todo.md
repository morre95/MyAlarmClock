# Larmstyrning

- [x] 1. Styrloop (`alarm_control.py`, `test_alarm_control.py`).
  Acceptans: stoppa före start/under snooze, återuppta efter snooze,
  oförändrad stopptid inklusive snooze, EOF/Ctrl+C städar upp.
  Verifiera: fokuserade `unittest` med simulerad tid. Beroenden: inga.
- [x] 2. Ljudkällor (samma två filer).
  Acceptans: Spotify styr rätt enhet och verifierar paus; MP3-processen
  avslutas och kan startas igen; fel startar ingen oavsiktlig reserv.
  Verifiera: tester av API-/processgränser utan nätverk eller ljud.
  Beroenden: 1.
- [x] Kontrollpunkt: styrloop och ljudkällor fungerar tillsammans offline.
- [x] 3. CLI och användarhjälp (`spotify_test.py`, `test_spotify_test.py`).
  Acceptans: `--alarm` och validerade tidsval fungerar, gammalt test- och
  diagnostikläge behåller beteendet. Verifiera: hela `unittest`-sviten,
  CLI med fejkad transport, hjälptext och `git diff --check`. Beroenden: 2.
- [x] 4. Granska och dokumentera verifieringen.
  Acceptans: felvägar/sekretess granskade; ändrad timerprioritet upptäcks av
  tester; specifikation och denna lista uppdaterade. Beroenden: 3.

Verifierat 2026-10-25: alla 43 offlinetester passerar, inklusive riktig
Spotipy-klient med fejkad HTTP-transport, riktig stdin-pipe med timer,
buffrade terminalkommandon och start/stopp av en riktig ljudlös barnprocess.
En separat modell granskade korrekthet, läsbarhet, arkitektur, säkerhet och
prestanda. Den upptäckte utebliven MP3-processkontroll; felet reproducerades
med fallerande tester och rättades. Inga kvarvarande obligatoriska fynd.
En mutationskontroll inverterade stopptidsjämförelsen i en tillfällig modul
i minnet; beteendetestet upptäckte felet. Ingen källfil ändrades av kontrollen.
`git diff --check` passerar. Ingen verklig musik, tokenfil eller befintlig
diagnostiklogg användes i verifieringen. Manuellt ljudtest är nästa steg.
