"""Builds scada/de_places.json: German towns with coordinates (same as make_dk_places.py, for Germany), so the
outside-temperature picker and the Germany map work without asking the internet. Run once (needs internet):
py scada\\make_de_places.py   Only town names are sent to Open-Meteo's free geocoding service."""
import json, os, time, urllib.request, urllib.parse

TOWNS = """Berlin,Hamburg,München,Köln,Frankfurt am Main,Stuttgart,Düsseldorf,Leipzig,Dortmund,Essen,Bremen,Dresden,
Hannover,Nürnberg,Duisburg,Bochum,Wuppertal,Bielefeld,Bonn,Münster,Mannheim,Karlsruhe,Augsburg,Wiesbaden,
Mönchengladbach,Gelsenkirchen,Aachen,Braunschweig,Kiel,Chemnitz,Halle (Saale),Magdeburg,Freiburg im Breisgau,
Krefeld,Mainz,Lübeck,Erfurt,Oberhausen,Rostock,Kassel,Hagen,Potsdam,Saarbrücken,Hamm,Ludwigshafen am Rhein,
Oldenburg,Osnabrück,Leverkusen,Darmstadt,Heidelberg,Solingen,Regensburg,Paderborn,Neuss,Ingolstadt,
Offenbach am Main,Fürth,Ulm,Heilbronn,Pforzheim,Würzburg,Wolfsburg,Göttingen,Reutlingen,Erlangen,Bremerhaven,
Koblenz,Trier,Recklinghausen,Jena,Salzgitter,Siegen,Gütersloh,Hildesheim,Hanau,Kaiserslautern,Cottbus,Schwerin,
Gera,Ludwigsburg,Düren,Zwickau,Flensburg,Villingen-Schwenningen,Konstanz,Worms,Minden,Neumünster,Dessau-Roßlau,
Norderstedt,Delmenhorst,Wilhelmshaven,Rheine,Bamberg,Landshut,Bayreuth,Celle,Lüneburg,Plauen,Neubrandenburg,
Stralsund,Greifswald,Wismar,Husum,Schleswig,Rendsburg,Heide,Itzehoe,Cuxhaven,Stade,Emden,Leer,Aurich,Lingen,
Nordhorn,Meppen,Cloppenburg,Vechta,Verden,Uelzen,Soltau,Gifhorn,Goslar,Nordhausen,Eisenach,Fulda,Gießen,Marburg,
Wetzlar,Limburg an der Lahn,Bad Kreuznach,Idar-Oberstein,Zweibrücken,Pirmasens,Landau in der Pfalz,Speyer,
Baden-Baden,Offenburg,Lörrach,Waldshut-Tiengen,Ravensburg,Friedrichshafen,Kempten,Memmingen,Kaufbeuren,
Garmisch-Partenkirchen,Rosenheim,Traunstein,Passau,Deggendorf,Straubing,Weiden in der Oberpfalz,Amberg,Hof,
Coburg,Schweinfurt,Aschaffenburg,Ansbach,Nördlingen,Görlitz,Bautzen,Freiberg,Annaberg-Buchholz,Suhl,Weimar,Gotha,
Mühlhausen,Halberstadt,Wernigerode,Stendal,Salzwedel,Wittenberge,Neuruppin,Eberswalde,Frankfurt (Oder),Prenzlau,
Pasewalk,Anklam,Bergen auf Rügen,Güstrow,Parchim,Ludwigslust,Waren,Westerland,Burg auf Fehmarn,Eckernförde,
Kappeln,Niebüll,Bad Segeberg,Ratzeburg,Mölln,Bad Oldesloe,Elmshorn,Pinneberg,Heiligenhafen,Plön,Eutin,Preetz,
Glückstadt,Brunsbüttel,Tönning,Friedrichstadt,Harrislee,Süderbrarup,Lauenburg,Geesthacht,Buxtehude,Bremervörde,
Zeven,Rotenburg (Wümme),Osterholz-Scharmbeck,Nienburg,Diepholz,Sulingen,Bad Bentheim,Papenburg,Norden,Jever,
Varel,Brake,Nordenham,Oberstdorf,Berchtesgaden,Weißenburg in Bayern,Neumarkt in der Oberpfalz,Cham,Freyung,
Altötting,Mühldorf am Inn,Erding,Freising,Dachau,Landsberg am Lech,Weilheim in Oberbayern,Bad Tölz,Lindau,
Tuttlingen,Rottweil,Balingen,Sigmaringen,Biberach an der Riß,Aalen,Schwäbisch Hall,Crailsheim,Bad Mergentheim,
Tauberbischofsheim,Mosbach,Wertheim,Lohr am Main,Kitzingen,Bad Kissingen,Meiningen,Ilmenau,Saalfeld,Greiz,Altenburg,
Döbeln,Torgau,Riesa,Meißen,Pirna,Zittau,Hoyerswerda,Senftenberg,Lübben,Luckenwalde,Brandenburg an der Havel,
Rathenow,Perleberg,Bitterfeld-Wolfen,Wittenberg,Bernburg,Köthen,Merseburg,Naumburg,Sangerhausen,Quedlinburg,
Osterode am Harz,Northeim,Holzminden,Höxter,Detmold,Lemgo,Herford,Arnsberg,Meschede,Brilon,Olpe,Lüdenscheid,
Bad Berleburg,Korbach,Bad Hersfeld,Eschwege,Alsfeld,Lauterbach,Friedberg,Bad Homburg,Montabaur,Altenkirchen,
Mayen,Cochem,Bernkastel-Kues,Bitburg,Prüm,Daun,Wittlich,Simmern,Kusel,Homburg,Saarlouis,Merzig,Neunkirchen""".replace("\n", "")

def get(name):
    q = urllib.parse.urlencode({"name": name, "count": 1, "countryCode": "DE", "language": "de", "format": "json"})
    req = urllib.request.Request("https://geocoding-api.open-meteo.com/v1/search?" + q, headers={"User-Agent": "burner-local/1.0"})
    for i in range(3):
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                res = json.loads(r.read().decode("utf-8")).get("results") or []
            return res[0] if res else None
        except Exception:
            time.sleep(1.5)
    return None

out, seen = [], set()
for n in [x.strip() for x in TOWNS.split(",") if x.strip()]:
    if n in seen:
        continue
    seen.add(n)
    r = get(n)
    if not r:
        print("not found", n)
        continue
    k = (r.get("name"), round(r["latitude"], 2), round(r["longitude"], 2))
    if any((x["name"], x["lat"], x["lon"]) == k for x in out):
        continue
    out.append({"name": r.get("name") or n, "area": r.get("admin1") or "", "lat": k[1], "lon": k[2], "pop": r.get("population") or 0})
    time.sleep(0.15)
out.sort(key=lambda x: -x["pop"])
path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "de_places.json")
with open(path, "w", encoding="utf-8") as fh:
    json.dump(out, fh, ensure_ascii=False)
print(len(out), "towns ->", path)
