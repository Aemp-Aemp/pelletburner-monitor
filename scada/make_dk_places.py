"""Builds scada/dk_places.json: Danish towns with coordinates, so the outside-temperature place picker and the
Denmark map work without asking the internet. Run once (needs internet): py scada\\make_dk_places.py
Only town names are sent to Open-Meteo's free geocoding service."""
import json, os, time, urllib.request, urllib.parse

TOWNS = """København,Frederiksberg,Aarhus,Odense,Aalborg,Esbjerg,Randers,Kolding,Horsens,Vejle,Roskilde,Herning,
Hørsholm,Helsingør,Silkeborg,Næstved,Fredericia,Viborg,Køge,Holstebro,Taastrup,Slagelse,Hillerød,Sønderborg,
Svendborg,Hjørring,Holbæk,Frederikshavn,Nørresundby,Ringsted,Haderslev,Skive,Nykøbing Falster,Greve Strand,
Kalundborg,Ballerup,Kongens Lyngby,Hvidovre,Thisted,Nyborg,Aabenraa,Ikast,Grenaa,Varde,Ribe,Tønder,Middelfart,
Assens,Faaborg,Bogense,Kerteminde,Nakskov,Maribo,Vordingborg,Præstø,Faxe,Stege,Rønne,Nexø,Allinge,Svaneke,
Skagen,Brønderslev,Hobro,Mariager,Aars,Løgstør,Nykøbing Mors,Lemvig,Struer,Ringkøbing,Skjern,Hvide Sande,
Grindsted,Billund,Brande,Give,Vejen,Rødding,Gram,Toftlund,Padborg,Gråsten,Nordborg,Ebeltoft,Ry,Skanderborg,Odder,
Hammel,Rønde,Tranebjerg,Byrum,Ærøskøbing,Marstal,Rudkøbing,Hundested,Frederikssund,Frederiksværk,Gilleleje,
Hornbæk,Fredensborg,Birkerød,Farum,Stenløse,Ishøj,Solrød Strand,Sorø,Ringe,Otterup,Haslev,Tølløse,
Nykøbing Sjælland,Asnæs,Jyderup,Korsør,Skælskør,Dianalund,Vinderup,Bjerringbro,Kjellerup,Thyborøn,Hanstholm,
Fjerritslev,Sæby,Hirtshals,Løkken,Hals,Hadsund,Aalestrup,Farsø,Tarm,Nordby,Bramming,Brørup,Børkop,Juelsminde,
Tørring,Hinnerup,Hadsten,Langå,Ølgod,Videbæk,Aulum,Sunds,Karup,Bjerringbro,Nibe,Vodskov,Dronninglund,Brovst,
Aabybro,Pandrup,Blokhus,Vrå,Hurup,Snedsted,Vestervig,Glyngøre,Roslev,Fur,Skals,Ørum,Møldrup,Ans,Them,Galten,
Hørning,Malling,Beder,Lystrup,Hornslet,Kolind,Auning,Ørsted,Allingåbro,Ryomgård,Knebel,Tarm,Vamdrup,Lunderskov,
Christiansfeld,Vojens,Bredebro,Skærbæk,Løgumkloster,Augustenborg,Broager,Rødekro,Tinglev,Kruså,Fårup,Glamsbjerg,
Vissenbjerg,Årslev,Munkebo,Søndersø,Morud,Nørre Aaby,Tommerup,Ullerslev,Gudhjem,Aakirkeby,Hasle,Sakskøbing,
Rødby,Gedser,Stubbekøbing,Nysted,Borre,Fakse Ladeplads,Rødvig,Store Heddinge,Lejre,Hvalsø,Borup,Slangerup,
Kvistgård,Espergærde,Humlebæk,Helsinge,Vejby,Frederiksværk,Tisvildeleje,Kirke Hyllinge,Gørlev,Høng,Fuglebjerg,
Glumsø,Rønnede,Holeby,Dragør,Kastrup,Ølstykke,Værløse,Albertslund,Glostrup,Brøndby Strand,Vallensbæk,Hellerup""".replace("\n", "")

def get(name):
    q = urllib.parse.urlencode({"name": name, "count": 1, "countryCode": "DK", "language": "da", "format": "json"})
    req = urllib.request.Request("https://geocoding-api.open-meteo.com/v1/search?" + q, headers={"User-Agent": "burner-local/1.0"})
    with urllib.request.urlopen(req, timeout=15) as r:
        res = json.loads(r.read().decode("utf-8")).get("results") or []
    return res[0] if res else None

out, seen = [], set()
for n in [x.strip() for x in TOWNS.split(",") if x.strip()]:
    if n in seen:
        continue
    seen.add(n)
    try:
        r = get(n)
    except Exception as e:
        print("skip", n, e)
        continue
    if not r:
        print("not found", n)
        continue
    out.append({"name": r.get("name") or n, "area": r.get("admin1") or "", "lat": round(r["latitude"], 2),
                "lon": round(r["longitude"], 2), "pop": r.get("population") or 0})
    time.sleep(0.15)
out.sort(key=lambda x: -x["pop"])
path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dk_places.json")
with open(path, "w", encoding="utf-8") as fh:
    json.dump(out, fh, ensure_ascii=False)
print(len(out), "towns ->", path)
