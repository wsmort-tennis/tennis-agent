#!/usr/bin/env python3
"""
Tennisbane-agent
================
Finder ledige tennisbaner i Rundforbi Tennishal, som passer ind i din
iCloud-kalender, og laver:

  * docs/ledige-tennisbaner.ics  – en kalender du kan abonnere på
  * docs/index.html              – en overskuelig tabel
  * en push-besked (ntfy), når en NY prime time-tid dukker op

Agenten booker ALDRIG noget – den læser kun.
Adgangskoder læses fra miljøvariabler (GitHub Secrets), aldrig fra koden:
  ICLOUD_USERNAME      din Apple-ID-mail
  ICLOUD_APP_PASSWORD  en app-specifik adgangskode fra account.apple.com
  NTFY_TOPIC           dit hemmelige ntfy-emne (fx tennis-w7k2p9xq)
"""
from __future__ import annotations

import argparse
import html
import json
import os
import re
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from urllib import robotparser
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import requests
import yaml
from bs4 import BeautifulSoup
from icalendar import Calendar, Event

TZ = ZoneInfo("Europe/Copenhagen")
ROOT = Path(__file__).resolve().parent
DOCS = ROOT / "docs"
STATE_FILE = ROOT / "data" / "state.json"
USER_AGENT = "Tennisbane-agent/1.0 (privat brug; laeser kun baneoversigten fire gange i doegnet)"

MAANEDER = {"jan": 1, "feb": 2, "mar": 3, "apr": 4, "maj": 5, "jun": 6,
            "jul": 7, "aug": 8, "sep": 9, "okt": 10, "nov": 11, "dec": 12}
UGEDAGE = {"man": 0, "tir": 1, "ons": 2, "tor": 3, "fre": 4, "lør": 5, "søn": 6}
DAG_NAVN = ["Man", "Tir", "Ons", "Tor", "Fre", "Lør", "Søn"]
MAANED_NAVN = ["", "jan", "feb", "mar", "apr", "maj", "jun",
               "jul", "aug", "sep", "okt", "nov", "dec"]

# Siden viser fx "Tor 22 okt" efterfulgt af "21:00 - 22:00 21:30 - 22:30 ..."
TOKEN_RE = re.compile(
    r"(?P<dag>\b(?:Man|Tir|Ons|Tor|Fre|L\S{1,2}r|S\S{1,2}n)\w*\.?\s+"
    r"(?P<d>\d{1,2})\.?\s+(?P<m>jan|feb|mar|apr|maj|jun|jul|aug|sep|okt|nov|dec)\w*)"
    r"|(?P<tid>\b(?P<h1>\d{1,2})[:.](?P<m1>\d{2})\s*[-–]\s*(?P<h2>\d{1,2})[:.](?P<m2>\d{2})\b)",
    re.IGNORECASE,
)


# ───────────────────────────── Datamodel ─────────────────────────────
@dataclass
class Slot:
    start: datetime
    end: datetime
    hal: str
    prime: bool = False

    @property
    def minutter(self) -> int:
        return int((self.end - self.start).total_seconds() // 60)

    @property
    def key(self) -> str:
        return f"{self.start.isoformat()}|{self.end.isoformat()}"


@dataclass
class Resultat:
    ledige: list[Slot] = field(default_factory=list)
    kalender_tjekket: bool = False
    antal_paa_siden: int = 0
    antal_efter_filtre: int = 0


# ──────────────────────── 1. Hent ledige baner ────────────────────────
def tjek_robots(url: str) -> None:
    """Stopper agenten, hvis hallens robots.txt ikke tillader siden."""
    p = urlparse(url)
    rp = robotparser.RobotFileParser()
    try:
        r = requests.get(f"{p.scheme}://{p.netloc}/robots.txt",
                         headers={"User-Agent": USER_AGENT}, timeout=20)
        if r.status_code >= 400:
            return  # ingen robots.txt = ingen regler
        rp.parse(r.text.splitlines())
    except requests.RequestException as e:
        print(f"Advarsel: kunne ikke læse robots.txt ({e}). Fortsætter forsigtigt.")
        return
    if not rp.can_fetch(USER_AGENT, url):
        sys.exit(f"robots.txt tillader ikke at hente {url}. Agenten stopper.")


def hent_side(url: str) -> bytes:
    r = requests.get(url, headers={"User-Agent": USER_AGENT, "Accept-Language": "da"},
                     timeout=30)
    r.raise_for_status()
    return r.content


def gaet_aar(dag: int, maaned: int, idag: date) -> date | None:
    """Siden viser ikke årstal. Vælg det år, der ligger tættest på i dag."""
    kandidater = []
    for aar in (idag.year - 1, idag.year, idag.year + 1):
        try:
            kandidater.append(date(aar, maaned, dag))
        except ValueError:
            pass
    return min(kandidater, key=lambda d: abs((d - idag).days)) if kandidater else None


def parse_ledige_tider(side: bytes | str, idag: date, hal: str) -> list[Slot]:
    soup = BeautifulSoup(side, "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    tekst = soup.get_text("\n")

    slots: dict[str, Slot] = {}
    aktuel_dato: date | None = None
    for m in TOKEN_RE.finditer(tekst):
        if m.group("dag"):
            aktuel_dato = gaet_aar(int(m.group("d")), MAANEDER[m.group("m")[:3].lower()], idag)
        elif aktuel_dato is not None:
            h1, m1, h2, m2 = (int(m.group(k)) for k in ("h1", "m1", "h2", "m2"))
            if h1 > 23 or h2 > 24 or m1 > 59 or m2 > 59:
                continue
            start = datetime.combine(aktuel_dato, time(h1, m1), TZ)
            slut = datetime.combine(aktuel_dato, time(0, 0), TZ) + timedelta(hours=h2, minutes=m2)
            if slut <= start:
                slut += timedelta(days=1)
            s = Slot(start, slut, hal)
            slots[s.key] = s
    return sorted(slots.values(), key=lambda s: s.start)


# ─────────────────────────── 2. Filtre ───────────────────────────
def hhmm(t: str) -> int:
    h, m = t.split(":")
    return int(h) * 60 + int(m)


def dagtype(d: date) -> str:
    return "weekend" if d.weekday() >= 5 else "hverdage"


def min_af_dag(s: Slot) -> tuple[int, int]:
    a = s.start.hour * 60 + s.start.minute
    return a, a + s.minutter


def overlapper(a1, a2, b1, b2) -> bool:
    return a1 < b2 and b1 < a2


def opfylder_filtre(s: Slot, f: dict) -> bool:
    if s.minutter < int(f["minimum_varighed_min"]):
        return False
    tilladte = {UGEDAGE[d.strip().lower()] for d in f["ugedage"]}
    if s.start.weekday() not in tilladte:
        return False
    a, b = min_af_dag(s)
    if a < hhmm(f["tidligste_start"]) or b > hhmm(f["seneste_slut"]):
        return False
    for fra, til in (f.get("blokeret") or {}).get(dagtype(s.start.date()), []) or []:
        if overlapper(a, b, hhmm(fra), hhmm(til)):
            return False
    return True


def er_prime(s: Slot, prime: dict) -> bool:
    a, b = min_af_dag(s)
    return any(a >= hhmm(fra) and b <= hhmm(til)
               for fra, til in (prime or {}).get(dagtype(s.start.date()), []) or [])


# ──────────────────── 3. Læs iCloud som optaget/ledig ────────────────────
def til_aware(dt) -> datetime:
    if isinstance(dt, datetime):
        return dt.replace(tzinfo=TZ) if dt.tzinfo is None else dt
    return datetime.combine(dt, time(0), TZ)


def begivenhed_tidsrum(ve, ignorer_heldag: bool):
    """Returnerer KUN (start, slut) – titel, sted og noter bliver aldrig læst."""
    if str(ve.get("TRANSP", "OPAQUE")).upper() == "TRANSPARENT":
        return None  # "Vis som: Ledig" i Apple Kalender
    if str(ve.get("STATUS", "")).upper() == "CANCELLED":
        return None
    ds = ve.get("DTSTART")
    if ds is None:
        return None
    start_raw = ds.dt
    heldag = not isinstance(start_raw, datetime)
    if heldag and ignorer_heldag:
        return None
    start = til_aware(start_raw)
    if ve.get("DTEND") is not None:
        slut = til_aware(ve.get("DTEND").dt)
    elif ve.get("DURATION") is not None:
        slut = start + ve.get("DURATION").dt
    else:
        slut = start + (timedelta(days=1) if heldag else timedelta(0))
    return start, slut


def hent_optaget_tid(kcfg: dict, fra: datetime, til: datetime):
    bruger = os.environ.get("ICLOUD_USERNAME")
    kode = os.environ.get("ICLOUD_APP_PASSWORD")
    if not bruger or not kode:
        print("Advarsel: ICLOUD_USERNAME/ICLOUD_APP_PASSWORD mangler – kalenderen tjekkes IKKE.")
        return None

    import caldav  # importeres kun når der faktisk skal bruges en kalender

    ignorerede = {n.strip().lower() for n in kcfg.get("ignorer_kalendere", [])}
    ignorer_heldag = bool(kcfg.get("ignorer_heldagsbegivenheder", True))
    uden_buffer = {n.strip().lower() for n in kcfg.get("uden_buffer_kalendere", [])}
    optaget = []
    client = caldav.DAVClient(url="https://caldav.icloud.com/", username=bruger,
                              password=kode, features="icloud", timeout=60)
    kalendere = client.principal().calendars()
    if not kalendere:
        sys.exit("Fandt ingen kalendere i iCloud – tjek brugernavn og app-adgangskode.")
    for kal in kalendere:
        try:
            navn = kal.get_display_name() or ""
        except Exception:
            navn = ""
        if navn.strip().lower() in ignorerede:
            continue
        brug_buffer = navn.strip().lower() not in uden_buffer
        try:
            fund = kal.search(start=fra, end=til, event=True, expand=True)
        except Exception as e:  # fx påmindelseslister, som ikke indeholder begivenheder
            print(f"Springer kalender '{navn or '?'}' over ({type(e).__name__}).")
            continue
        for obj in fund:
            for ve in obj.icalendar_instance.walk("VEVENT"):
                tr = begivenhed_tidsrum(ve, ignorer_heldag)
                if tr:
                    optaget.append((tr[0], tr[1], brug_buffer))
    print(f"Læste {len(optaget)} optagede tidsrum fra {len(kalendere)} iCloud-kalendere.")
    return optaget


def har_konflikt(s: Slot, optaget, buffer_min: int) -> bool:
    """optaget = (start, slut, brug_buffer). Kalendere uden buffer (fx Træning)
    må gerne ligge lige op ad tennistiden, men må ikke overlappe."""
    buf = timedelta(minutes=buffer_min)
    for o_start, o_slut, brug_buffer in optaget:
        b = buf if brug_buffer else timedelta(0)
        if overlapper(s.start - b, s.end + b, o_start, o_slut):
            return True
    return False


# ───────────────────── 4. Output: kalenderfeed (ICS) ─────────────────────
def saml_blokke(slots: list[Slot]) -> list[list[Slot]]:
    """Samler overlappende tider (06:00-07:00, 06:30-07:30 ...) til én blok."""
    blokke: list[list[Slot]] = []
    for s in sorted(slots, key=lambda x: x.start):
        if blokke and s.start <= max(x.end for x in blokke[-1]) \
                and s.start.date() == blokke[-1][0].start.date():
            blokke[-1].append(s)
        else:
            blokke.append([s])
    return blokke


def kl(dt: datetime) -> str:
    return dt.astimezone(TZ).strftime("%H:%M")


def dato_tekst(d: date) -> str:
    return f"{DAG_NAVN[d.weekday()]} {d.day}. {MAANED_NAVN[d.month]}"


def lav_ics(slots: list[Slot], cfg: dict, nu: datetime) -> bytes:
    cal = Calendar()
    cal.add("prodid", "-//Tennisbane-agent//DA")
    cal.add("version", "2.0")
    cal.add("calscale", "GREGORIAN")
    cal.add("method", "PUBLISH")
    cal.add("x-wr-calname", "Ledige tennisbaner")
    cal.add("x-wr-caldesc", f"Ledige baner i {cfg['hal']['navn']}, som passer ind i din kalender. Book selv.")
    cal.add("x-wr-timezone", "Europe/Copenhagen")
    cal.add("x-published-ttl", "PT1H")
    cal.add("refresh-interval", timedelta(hours=1), parameters={"VALUE": "DURATION"})

    for blok in saml_blokke(slots):
        start = min(s.start for s in blok)
        slut = max(s.end for s in blok)
        starttider = ", ".join(kl(s.start) for s in blok)
        ev = Event()
        ev.add("uid", f"{start.strftime('%Y%m%dT%H%M')}-{slut.strftime('%H%M')}@tennisbane-agent")
        ev.add("dtstamp", nu.astimezone(timezone.utc))
        ev.add("dtstart", start.astimezone(TZ))
        ev.add("dtend", slut.astimezone(TZ))
        ev.add("summary", cfg.get("kalender", {}).get("titel", "Ledig bane"))
        ev.add("description",
               f"{cfg['hal']['navn']}\n"
               f"Mulige starttider ({blok[0].minutter} min): {starttider}\n\n"
               f"Book selv her: {cfg['hal']['url']}\n"
               f"Opdateret: {nu.strftime('%d.%m.%Y %H:%M')}")
        ev.add("location", cfg["hal"].get("adresse", cfg["hal"]["navn"]))
        ev.add("url", cfg["hal"]["url"])
        ev.add("transp", "TRANSPARENT")  # gør dig ikke "optaget"
        cal.add_component(ev)
    cal.add_missing_timezones()  # dansk tid i stedet for UTC, så Kalender ikke viser "GMT"
    return cal.to_ical()


# ───────────────────── 5. Output: tabel (HTML) ─────────────────────
def lav_html(res: Resultat, cfg: dict, nu: datetime) -> str:
    raekker = []
    for s in res.ledige:
        raekker.append(
            f'<tr class="{"prime" if s.prime else ""}">'
            f"<td>{html.escape(dato_tekst(s.start.date()))}</td>"
            f"<td>{kl(s.start)}–{kl(s.end)}</td>"
            f"<td>{s.minutter} min</td>"
            f"<td>{html.escape(s.hal)}</td>"
            f"<td>{'⭐' if s.prime else ''}</td></tr>")
    kalender_note = ("Tjekket mod din iCloud-kalender med "
                     f"{cfg['filtre']['buffer_min']} min buffer."
                     if res.kalender_tjekket else
                     "<strong>OBS: Kalenderen er ikke tjekket i denne kørsel.</strong>")
    tom = '<tr><td colspan="5">Ingen ledige tider passer lige nu.</td></tr>'
    return f"""<!doctype html>
<html lang="da"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex">
<title>Ledige tennisbaner</title>
<style>
:root{{--bg:#f7f7f4;--fg:#1d1d1b;--mut:#6b6b66;--line:#e2e2dc;--prime:#fff4cc;--acc:#2f6b3a}}
@media (prefers-color-scheme:dark){{:root{{--bg:#151714;--fg:#ecece6;--mut:#9a9a92;--line:#2c2f2a;--prime:#3a3418;--acc:#8fd19e}}}}
body{{background:var(--bg);color:var(--fg);font:16px/1.45 -apple-system,system-ui,sans-serif;margin:0;padding:24px 16px}}
main{{max-width:760px;margin:auto}} h1{{font-size:1.5rem;margin:0 0 4px}}
p{{color:var(--mut);margin:4px 0}} a{{color:var(--acc)}}
label{{display:inline-block;margin:16px 0 8px;cursor:pointer}}
table{{width:100%;border-collapse:collapse}} th,td{{text-align:left;padding:8px 6px;border-bottom:1px solid var(--line)}}
th{{font-size:.8rem;text-transform:uppercase;letter-spacing:.04em;color:var(--mut)}}
tr.prime{{background:var(--prime)}} body.kunprime tbody tr:not(.prime){{display:none}}
</style></head>
<body><main>
<h1>🎾 Ledige tennisbaner</h1>
<p>{html.escape(cfg['hal']['navn'])} · opdateret {nu.strftime('%d.%m.%Y kl. %H:%M')}</p>
<p>{kalender_note} {len(res.ledige)} tider passer ud af {res.antal_paa_siden} ledige på siden.</p>
<p>Agenten booker ikke. <a href="{html.escape(cfg['hal']['url'])}">Book selv hos hallen</a> ·
<a href="ledige-tennisbaner.ics">Kalenderfeed (.ics)</a></p>
<label><input type="checkbox" onchange="document.body.classList.toggle('kunprime',this.checked)"> Vis kun prime time ⭐</label>
<table><thead><tr><th>Dato</th><th>Tid</th><th>Varighed</th><th>Hal</th><th>Prime</th></tr></thead>
<tbody>{''.join(raekker) or tom}</tbody>
</table></main></body></html>
"""


def skriv_github_summary(res: Resultat) -> None:
    sti = os.environ.get("GITHUB_STEP_SUMMARY")
    if not sti:
        return
    linjer = ["## 🎾 Ledige tennisbaner", "",
              "| Dato | Tid | Varighed | Prime |", "|---|---|---|---|"]
    for s in res.ledige:
        linjer.append(f"| {dato_tekst(s.start.date())} | {kl(s.start)}–{kl(s.end)} | "
                      f"{s.minutter} min | {'⭐' if s.prime else ''} |")
    with open(sti, "a", encoding="utf-8") as f:
        f.write("\n".join(linjer) + "\n")


# ───────────────────── 6. Notifikation (ntfy) ─────────────────────
def laes_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def send_notifikation(nye: list[Slot], cfg: dict) -> None:
    emne = os.environ.get("NTFY_TOPIC")
    if not emne:
        print("NTFY_TOPIC mangler – ingen push-besked sendt.")
        return
    server = os.environ.get("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
    linjer = [f"{dato_tekst(s.start.date())} kl. {kl(s.start)}–{kl(s.end)}" for s in nye[:8]]
    if len(nye) > 8:
        linjer.append(f"… og {len(nye) - 8} mere")
    titel = ("Prime time-bane ledig" if len(nye) == 1
             else f"{len(nye)} nye prime time-baner ledige")
    payload = {"topic": emne, "title": titel, "message": "\n".join(linjer),
               "tags": ["tennis"], "priority": 4, "click": cfg["hal"]["url"]}
    try:
        r = requests.post(server, json=payload, timeout=20)
        r.raise_for_status()
        print(f"Push-besked sendt om {len(nye)} tid(er).")
    except requests.RequestException as e:
        print(f"Advarsel: push-besked kunne ikke sendes ({e}).")


# ─────────────────────────── Hovedprogram ───────────────────────────
def koer(cfg: dict, side: bytes | str, nu: datetime, optaget) -> Resultat:
    f = cfg["filtre"]
    hal = cfg["hal"]["navn"]
    alle = parse_ledige_tider(side, nu.date(), hal)
    res = Resultat(antal_paa_siden=len(alle))
    horisont = nu + timedelta(days=int(f["dage_frem"]))
    kandidater = [s for s in alle if nu < s.start <= horisont and opfylder_filtre(s, f)]
    res.antal_efter_filtre = len(kandidater)
    if optaget is None:
        res.ledige = kandidater
    else:
        res.kalender_tjekket = True
        res.ledige = [s for s in kandidater if not har_konflikt(s, optaget, int(f["buffer_min"]))]
    for s in res.ledige:
        s.prime = er_prime(s, cfg.get("prime_time"))
    return res


def main() -> None:
    ap = argparse.ArgumentParser(description="Tennisbane-agent (booker aldrig – foreslår kun)")
    ap.add_argument("--html-fil", help="Test: læs baneoversigten fra en gemt fil i stedet for nettet")
    ap.add_argument("--optaget-fil", help="Test: JSON-liste med [start, slut] i stedet for iCloud")
    ap.add_argument("--nu", help="Test: lad som om klokken er dette (ISO-format)")
    ap.add_argument("--ingen-push", action="store_true", help="Send ingen push-besked")
    args = ap.parse_args()

    cfg = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
    nu = datetime.fromisoformat(args.nu).replace(tzinfo=TZ) if args.nu else datetime.now(TZ)
    url = cfg["hal"]["url"]

    if args.html_fil:
        side = Path(args.html_fil).read_bytes()
    else:
        tjek_robots(url)
        side = hent_side(url)

    horisont = nu + timedelta(days=int(cfg["filtre"]["dage_frem"]))
    if args.optaget_fil:
        raa = json.loads(Path(args.optaget_fil).read_text(encoding="utf-8"))
        optaget = [(til_aware(datetime.fromisoformat(r[0])), til_aware(datetime.fromisoformat(r[1])),
                    bool(r[2]) if len(r) > 2 else True) for r in raa]
    else:
        optaget = hent_optaget_tid(cfg.get("kalender", {}), nu - timedelta(days=1),
                                   horisont + timedelta(days=1))

    res = koer(cfg, side, nu, optaget)
    if res.antal_paa_siden == 0:
        sys.exit("Fandt ingen tider på siden – den har måske ændret udseende. "
                 "Kalenderen og tabellen er IKKE ændret.")

    # Skriv output
    DOCS.mkdir(exist_ok=True)
    (DOCS / ".nojekyll").touch()
    (DOCS / "ledige-tennisbaner.ics").write_bytes(lav_ics(res.ledige, cfg, nu))
    (DOCS / "index.html").write_text(lav_html(res, cfg, nu), encoding="utf-8")
    skriv_github_summary(res)

    # Notifikation om NYE prime time-tider (også tider der bliver ledige ved afbud)
    state = laes_state()
    forrige = set(state.get("sidste_prime", []))
    min_frem = nu + timedelta(hours=float(cfg.get("notifikation", {}).get("min_timer_frem", 0)))
    prime_nu = [s for s in res.ledige if s.prime]
    nye = [s for s in prime_nu if s.key not in forrige and s.start >= min_frem]
    if nye and not args.ingen_push:
        send_notifikation(nye, cfg)
    STATE_FILE.parent.mkdir(exist_ok=True)
    STATE_FILE.write_text(json.dumps({"sidste_prime": sorted(s.key for s in prime_nu),
                                      "opdateret": nu.isoformat()}, indent=1),
                          encoding="utf-8")

    # Log
    print(f"{res.antal_paa_siden} ledige tider på siden → {res.antal_efter_filtre} efter dine filtre "
          f"→ {len(res.ledige)} uden konflikt ({len(prime_nu)} prime time, {len(nye)} nye).")
    for s in res.ledige:
        print(f"  {dato_tekst(s.start.date()):<12} {kl(s.start)}–{kl(s.end)}  {'⭐' if s.prime else ''}")


if __name__ == "__main__":
    main()
