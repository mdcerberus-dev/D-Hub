"""D-Hub Beta 1.2 - ein Hub fuer Steam, Epic, GOG und mehr. Nur Python 3 noetig, keine Pakete."""
import os, sys, json, re, glob, time, threading, subprocess, shutil, webbrowser
import urllib.request, urllib.parse, sqlite3, tempfile, sysconfig, html
import xml.etree.ElementTree as ET
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler

HERE = os.path.dirname(sys.executable) if getattr(sys, "frozen", False) else os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "hub_data.json")
PORT = 8766
VERSION = "1.2"
IS_WIN = sys.platform == "win32"
if IS_WIN:
    import winreg

try:
    store = json.load(open(DATA, encoding="utf-8"))
except Exception:
    store = {}
store.setdefault("prices", {})
store.setdefault("settings", {"view": "cover", "limit": 0})
store.setdefault("listprices", {})
store["settings"].setdefault("demo", False)
if store.get("pricev") != 3:
    store["listprices"] = {k: v for k, v in store["listprices"].items() if v.get("v") is not None}
    store["pricev"] = 3


def save():
    json.dump(store, open(DATA, "w", encoding="utf-8"), indent=1)


def norm(t):
    return re.sub(r"[^a-z0-9]", "", t.lower())


# ---------- Scanner ----------
def steam_root():
    if not IS_WIN:
        return None
    for hive, path, name in [(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam", "SteamPath"),
                             (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Valve\Steam", "InstallPath"),
                             (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Valve\Steam", "InstallPath")]:
        try:
            p = os.path.normpath(winreg.QueryValueEx(winreg.OpenKey(hive, path), name)[0])
            if os.path.isdir(p):
                return p
        except OSError:
            pass
    for p in (r"C:\Program Files (x86)\Steam", r"C:\Program Files\Steam"):
        if os.path.isdir(p):
            return p
    return None


def steam_games():
    out = []
    if not IS_WIN:
        return out
    root = steam_root()
    if not root:
        return out
    libs = {root}
    try:
        txt = open(os.path.join(root, "steamapps", "libraryfolders.vdf"), encoding="utf-8").read()
        libs |= {p.replace("\\\\", "\\") for p in re.findall(r'"path"\s+"([^"]+)"', txt)}
    except OSError:
        pass
    for lib in libs:
        for f in glob.glob(os.path.join(lib, "steamapps", "appmanifest_*.acf")):
            try:
                t = open(f, encoding="utf-8", errors="ignore").read()
                g = lambda k: (re.search(r'"%s"\s+"([^"]*)"' % k, t) or [None, ""])[1]
                name, appid = g("name"), g("appid")
                if not name or appid == "228980" or name.startswith(("Steamworks", "Proton", "Steam Linux")):
                    continue
                out.append(dict(store="Steam", sid=appid, title=name, size=int(g("SizeOnDisk") or 0),
                                installed=True, launch="steam://rungameid/" + appid))
            except Exception:
                pass
    return out


def ubisoft_games():
    out = []
    if not IS_WIN:
        return out
    try:
        k = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Ubisoft\Launcher\Installs")
    except OSError:
        return out
    i = 0
    while True:
        try:
            sub = winreg.EnumKey(k, i)
        except OSError:
            break
        i += 1
        try:
            d = winreg.QueryValueEx(winreg.OpenKey(k, sub), "InstallDir")[0]
            out.append(dict(store="Ubisoft", sid=sub, title=os.path.basename(d.rstrip("/\\")), size=0, installed=True,
                            launch="uplay://launch/%s/0" % sub))
        except OSError:
            pass
    return out


def epic_games():
    out = []
    d = os.path.join(os.environ.get("ProgramData", r"C:\ProgramData"), "Epic", "EpicGamesLauncher", "Data", "Manifests")
    for f in glob.glob(os.path.join(d, "*.item")):
        try:
            m = json.load(open(f, encoding="utf-8"))
            if not m.get("DisplayName") or m.get("bIsIncompleteInstall"):
                continue
            uri = "com.epicgames.launcher://apps/%s%%3A%s%%3A%s?action=launch&silent=true" % (
                m["CatalogNamespace"], m["CatalogItemId"], m["AppName"])
            out.append(dict(store="Epic", sid=m["AppName"], title=m["DisplayName"], size=m.get("InstallSize", 0),
                            installed=True, launch=uri))
        except Exception:
            pass
    return out


def gog_games():
    out = []
    if not IS_WIN:
        return out
    try:
        k = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\GOG.com\Games")
    except OSError:
        return out
    i = 0
    while True:
        try:
            sub = winreg.EnumKey(k, i)
        except OSError:
            break
        i += 1
        try:
            s = winreg.OpenKey(k, sub)
            v = lambda n: winreg.QueryValueEx(s, n)[0]
            out.append(dict(store="GOG", sid=sub, title=v("gameName"), size=0, installed=True,
                            launch="exe:" + v("exe") + "|" + v("path")))
        except OSError:
            pass
    return out


def find_legendary():
    p = shutil.which("legendary")
    if p:
        return p
    if os.path.exists(os.path.join(HERE, "legendary.exe")):
        return os.path.join(HERE, "legendary.exe")
    for scheme in (None, "nt_user"):
        try:
            d = sysconfig.get_path("scripts", scheme) if scheme else sysconfig.get_path("scripts")
            c = os.path.join(d, "legendary.exe")
            if os.path.exists(c):
                return c
        except Exception:
            pass
    return None


PRIVATE = "Steam: Keine Spieleliste erhalten. Stelle unter Steam > Profil bearbeiten > Datenschutz die Spieldetails auf Öffentlich."
GALAXY_DB = os.path.join(os.environ.get("ProgramData", r"C:\ProgramData"), "GOG.com", "Galaxy", "storage", "galaxy-2.0.db")


def owned_entry(st, sid, title):
    return dict(store=st, sid=str(sid), title=title, size=0, installed=False, launch="")


def steam_id():
    root = steam_root()
    if not root:
        return None
    try:
        t = open(os.path.join(root, "config", "loginusers.vdf"), encoding="utf-8", errors="ignore").read()
    except OSError:
        return None
    ids = re.findall(r'"(\d{17})"\s*\{([^}]*)\}', t)
    for i, body in ids:
        if re.search(r'"MostRecent"\s+"1"', body):
            return i
    return ids[0][0] if ids else None


def steam_owned():
    """Alle Steam-Spiele des Kontos: per Web-API (mit Key) oder oeffentlichem Profil (ohne Key)."""
    sid = store["settings"].get("steamid") or steam_id()
    if not sid:
        raise RuntimeError("Steam: Steam-ID nicht gefunden. Bitte in den Einstellungen eintragen.")
    key = store["settings"].get("steamkey")
    if key:
        d = fetch_json("https://api.steampowered.com/IPlayerService/GetOwnedGames/v1/?key=%s&steamid=%s&include_appinfo=1&include_played_free_games=1" % (key, sid))
        games = d.get("response", {}).get("games")
        if games is None:
            raise RuntimeError(PRIVATE)
        items = [(g["appid"], g.get("name", "")) for g in games]
    else:
        req = urllib.request.Request("https://steamcommunity.com/profiles/%s/games?tab=all&xml=1" % sid, headers={"User-Agent": "D-Hub/0.4"})
        txt = urllib.request.urlopen(req, timeout=25).read().decode("utf-8", "replace")
        if "<games>" not in txt:
            raise RuntimeError(PRIVATE)
        items = []
        for blk in re.findall(r"<game>(.*?)</game>", txt, re.S):  # bewusst kein XML-Parser: Steams XML ist oft fehlerhaft
            a = re.search(r"<appID>(\d+)</appID>", blk)
            n = re.search(r"<name>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</name>", blk, re.S)
            if a and n:
                items.append((a[1], html.unescape(n[1]).strip()))
    return [owned_entry("Steam", a, n) for a, n in items if a and n and not n.startswith(("Steamworks", "Proton", "Steam Linux"))]


def epic_owned():
    """Alle Epic-Spiele des Kontos ueber Legendary (einmalige Anmeldung noetig)."""
    lg = find_legendary()
    if not lg:
        raise RuntimeError("Epic: Legendary ist nicht installiert. Klicke auf Epic verbinden.")
    r = subprocess.run([lg, "list", "--json"], capture_output=True, text=True, timeout=180,
                       creationflags=0x08000000 if IS_WIN else 0)
    try:
        data = json.loads(r.stdout)
    except Exception:
        raise RuntimeError("Epic: Legendary ist nicht angemeldet. Klicke auf Epic anmelden.")
    return [owned_entry("Epic", g["app_name"], g["app_title"]) for g in data]


def galaxy_owned():
    """GOG-Besitz (und alles, was in Galaxy verbunden ist) aus der lokalen Galaxy-Datenbank."""
    if not os.path.exists(GALAXY_DB):
        raise FileNotFoundError("GOG: Galaxy-Datenbank nicht gefunden. Installiere GOG Galaxy und melde dich einmal an.")
    tmp = os.path.join(tempfile.gettempdir(), "dhub_galaxy.db")
    shutil.copy2(GALAXY_DB, tmp)
    con = sqlite3.connect(tmp)
    try:
        try:
            owned = {r[0] for r in con.execute("SELECT releaseKey FROM OwnedGames")}
        except sqlite3.Error:
            owned = None
        titles = {}
        for key, typ, val in con.execute(
                "SELECT gp.releaseKey, t.type, gp.value FROM GamePieces gp JOIN GamePieceTypes t ON t.id = gp.gamePieceTypeId "
                "WHERE t.type IN ('originalTitle', 'title')"):
            try:
                t = json.loads(val)["title"]
            except Exception:
                continue
            if typ == "originalTitle" or key not in titles:
                titles[key] = t
    finally:
        con.close()
    names = {"gog": "GOG", "steam": "Steam", "epic": "Epic", "origin": "EA", "uplay": "Ubisoft", "battlenet": "Battle.net", "xboxone": "Xbox", "amazon": "Amazon"}
    out = []
    for key, t in titles.items():
        pre, _, sid = key.partition("_")
        if pre in names and (owned is None or key in owned):
            out.append(owned_entry(names[pre], sid, t))
    if not out:
        raise RuntimeError("GOG: Galaxy hat keine Spiele gespeichert. Melde dich in Galaxy an und warte kurz.")
    return out


def refresh_owned():
    if LIB["busy"] or store["settings"].get("demo"):
        return
    LIB["busy"] = True
    try:
        owned = store.setdefault("owned", {})
        jobs = []
        if "Steam" in LIB["launchers"]:
            jobs.append(("Steam", steam_owned))
        if "Epic" in LIB["launchers"]:
            jobs.append(("Epic", epic_owned))
        if os.path.exists(GALAXY_DB):
            jobs.append(("GOG", galaxy_owned))
        elif "GOG" in LIB["launchers"]:
            LIB["errs"]["GOG"] = "GOG: Für deine ganze Bibliothek wird GOG Galaxy (angemeldet) gebraucht."
        for name, fn in jobs:
            try:
                owned[name] = fn()
                LIB["errs"].pop(name, None)
            except Exception as e:
                msg = str(e)
                LIB["errs"][name] = msg if msg[:5] in ("Steam", "Epic:", "GOG: ") else "%s: %s" % (type(e).__name__, e)
        have_owned = {g["store"] for lst in owned.values() for g in lst}
        for st in ("Ubisoft", "EA", "Battle.net"):
            if st in LIB["launchers"] and st not in have_owned:
                LIB["errs"][st] = ("%s: Deine ganze Bibliothek kann D-Hub nur über GOG Galaxy lesen. Installiere es (kostenlos), melde dich an "
                                   "und verbinde unter Integrationen %s. Danach in D-Hub auf Neu einlesen klicken." % (st, st))
            else:
                LIB["errs"].pop(st, None)
        save()
        scan()
    finally:
        LIB["busy"] = False


def kick():
    threading.Thread(target=refresh_owned, daemon=True).start()


KF_URL = "https://kumpelfunk.lerun3-funk.workers.dev"


def open_window(url):
    edge = next((e for e in (shutil.which("msedge"), r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe") if e and os.path.exists(e)), None)
    if edge:
        subprocess.Popen([edge, "--app=" + url])
    else:
        webbrowser.open(url)


def dl_legendary():
    try:
        tmp = os.path.join(HERE, "legendary.part")
        urllib.request.urlretrieve("https://github.com/derrod/legendary/releases/latest/download/legendary.exe", tmp)
        os.replace(tmp, os.path.join(HERE, "legendary.exe"))
        LIB["errs"]["Epic"] = "Epic: Legendary ist geladen. Klicke jetzt auf Epic anmelden."
    except Exception as e:
        LIB["errs"]["Epic"] = "Epic: Legendary konnte nicht geladen werden (%s: %s)." % (type(e).__name__, e)


def connect(st):
    if st == "KumpelFunk":
        open_window(KF_URL)
        return
    urls = {"Steam": "https://steamcommunity.com/my/edit/settings", "Galaxy": "https://www.gog.com/galaxy"}
    if st in urls:
        webbrowser.open(urls[st])
        return
    if st != "Epic" or not IS_WIN:
        return
    lg = find_legendary()
    if not lg:
        LIB["errs"]["Epic"] = "Epic: Legendary wird geladen …"
        threading.Thread(target=dl_legendary, daemon=True).start()
        return
    subprocess.Popen(["cmd", "/k", lg, "auth"], creationflags=0x10)


DEMO = [dict(store=s, sid=str(i), title=t, size=int(z * 1e9), installed=i % 3 != 0, launch="")
        for i, (s, t, z) in enumerate([
            ("Steam", "Hades", 15), ("Steam", "The Witcher 3", 50), ("GOG", "The Witcher 3", 48),
            ("Epic", "Alan Wake 2", 90), ("Steam", "Stardew Valley", 1), ("GOG", "Disco Elysium", 20),
            ("Epic", "Control", 42), ("Steam", "Portal 2", 12), ("GOG", "Cyberpunk 2077", 70),
            ("Steam", "Hollow Knight", 9), ("Epic", "Rocket League", 20), ("GOG", "Baldur's Gate 3", 150)])]

def reg(hive, path):
    try:
        winreg.CloseKey(winreg.OpenKey(hive, path))
        return True
    except OSError:
        return False


def uninstall_names():
    """Alle installierten Programme aus der Windows-Programmliste (kleingeschrieben)."""
    names = []
    if not IS_WIN:
        return names
    for hive, view in [(winreg.HKEY_LOCAL_MACHINE, winreg.KEY_WOW64_64KEY),
                       (winreg.HKEY_LOCAL_MACHINE, winreg.KEY_WOW64_32KEY), (winreg.HKEY_CURRENT_USER, 0)]:
        try:
            k = winreg.OpenKey(hive, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall", 0, winreg.KEY_READ | view)
        except OSError:
            continue
        i = 0
        while True:
            try:
                sub = winreg.EnumKey(k, i)
            except OSError:
                break
            i += 1
            try:
                names.append(winreg.QueryValueEx(winreg.OpenKey(k, sub), "DisplayName")[0].lower())
            except OSError:
                pass
    return names


def detect_launchers(found):
    """Welche Launcher sind wirklich installiert? Programmliste, Registry, Standardordner und gefundene Spiele."""
    names = set(found)
    if IS_WIN:
        pf = [os.environ.get("ProgramFiles", r"C:\Program Files"), os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")]
        H = winreg.HKEY_LOCAL_MACHINE
        folder = lambda *p: any(os.path.isdir(os.path.join(x, *p)) for x in pf)
        un = uninstall_names()
        has = lambda *keys: any(k in n for n in un for k in keys)
        checks = {
            "Steam": steam_root() is not None,
            "Epic": has("epic games launcher") or reg(H, r"SOFTWARE\WOW6432Node\Epic Games\EpicGamesLauncher") or folder("Epic Games", "Launcher"),
            "GOG": has("gog galaxy") or reg(H, r"SOFTWARE\WOW6432Node\GOG.com\GalaxyClient") or folder("GOG Galaxy"),
            "EA": has("ea app", "ea desktop", "origin") and (has("ea app", "ea desktop") or folder("Origin")) or folder("Electronic Arts", "EA Desktop"),
            "Ubisoft": has("ubisoft connect", "uplay") or reg(H, r"SOFTWARE\WOW6432Node\Ubisoft\Launcher") or folder("Ubisoft", "Ubisoft Game Launcher"),
            "Battle.net": has("battle.net") or reg(H, r"SOFTWARE\WOW6432Node\Blizzard Entertainment\Battle.net") or folder("Battle.net"),
        }
        names |= {k for k, v in checks.items() if v}
    return [n for n in ["Steam", "Epic", "GOG", "EA", "Ubisoft", "Battle.net"] if n in names]


# ---------- Listenpreise (Steam-Shop, oeffentlich, in EUR) ----------
def fetch_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": "D-Hub/0.4"})
    return json.loads(urllib.request.urlopen(req, timeout=15).read().decode("utf-8"))


_RATE = {}


def usd_eur():
    if "r" not in _RATE:
        try:
            _RATE["r"] = fetch_json("https://api.frankfurter.app/latest?from=USD&to=EUR")["rates"]["EUR"]
        except Exception:
            _RATE["r"] = 0.92
    return _RATE["r"]


def p_steam(g):
    st = next((v for v in g["versions"] if v["store"] == "Steam"), None)
    if st and not LIB["demo"]:
        d = fetch_json("https://store.steampowered.com/api/appdetails?appids=%s&cc=de&filters=price_overview,basic" % st["sid"]).get(st["sid"], {})
        if d.get("success"):
            data = d["data"]
            if data.get("is_free"):
                return 0.0, "Steam"
            po = data.get("price_overview")
            if po:
                return po["initial"] / 100, "Steam"
    r = fetch_json("https://store.steampowered.com/api/storesearch/?term=%s&cc=de&l=german" % urllib.parse.quote(g["title"]))
    c = [it for it in r.get("items", []) if it.get("price") and norm(it.get("name", "")).startswith(g["id"])]
    if c:
        it = min(c, key=lambda i: len(norm(i["name"])))
        p = it["price"].get("initial") or it["price"].get("final")
        return p / 100, "Steam-Katalog" if norm(it["name"]) == g["id"] else "Steam-Katalog (Näherung)"
    return None


def p_gog(g):
    r = fetch_json("https://catalog.gog.com/v1/catalog?limit=8&query=like%%3A%s&productType=in%%3Agame%%2Cpack&countryCode=DE&locale=de-DE&currencyCode=EUR" % urllib.parse.quote(g["title"]))
    c = [p for p in r.get("products", []) if norm(p.get("title", "")).startswith(g["id"])]
    if not c:
        return None
    p = min(c, key=lambda p: len(p["title"]))
    pr = p.get("price") or {}
    amt = (pr.get("baseMoney") or {}).get("amount")
    if amt is None:
        m = re.search(r"\d+[.,]\d+", pr.get("base", "") or "")
        amt = m[0].replace(",", ".") if m else None
    if amt is None:
        return None
    return float(amt), "GOG-Katalog" if norm(p["title"]) == g["id"] else "GOG-Katalog (Näherung)"


def p_cheapshark(g):
    """Online-UVP aus dem Preisvergleich CheapShark (deckt u.a. Epic, GOG, Steam, Ubisoft ab), USD -> EUR."""
    r = fetch_json("https://www.cheapshark.com/api/1.0/deals?pageSize=30&title=%s" % urllib.parse.quote(g["title"]))
    if not isinstance(r, list):
        return None
    ex = [float(d["normalPrice"]) for d in r if norm(d.get("title", "")) == g["id"]]
    exact = bool(ex)
    if not ex:
        near = [d for d in r if norm(d.get("title", "")).startswith(g["id"])]
        if not near:
            return None
        t = min(near, key=lambda d: len(d["title"]))["title"]
        ex = [float(d["normalPrice"]) for d in near if d["title"] == t]
    p = sorted(ex)[len(ex) // 2] * usd_eur()
    return round(p, 2), "Online-UVP (CheapShark, USD in EUR%s)" % ("" if exact else ", Näherung")


def list_price(g):
    fails, last = 0, None
    for fn in (p_steam, p_gog, p_cheapshark):
        try:
            r = fn(g)
            if r:
                return r
        except Exception as e:
            fails, last = fails + 1, e
    if fails == 3:
        raise last
    return None, ""


def price_worker():
    while True:
        if not store["settings"].get("autoprices", True):
            time.sleep(5)
            continue
        todo = [g for g in LIB["games"] if g["id"] not in store["listprices"] or
                (store["listprices"][g["id"]]["v"] is None and time.time() - store["listprices"][g["id"]]["t"] > 86400)]
        failed = False
        for g in todo:
            try:
                v, src = list_price(g)
            except Exception as e:
                LIB["perr"] = "%s: %s" % (type(e).__name__, e)
                failed = True
                break
            store["listprices"][g["id"]] = dict(v=v, src=src, t=time.time())
            LIB["perr"] = ""
            time.sleep(1.2)
        if todo:
            save()
        time.sleep(60 if failed else 10)


LIB = dict(games=[], demo=False, launchers=[], perr="", diag=[], errs={}, busy=False)


def diagnose(real):
    return ["System: " + ("Windows" if IS_WIN else sys.platform + " (Launcher-Erkennung funktioniert nur unter Windows)"),
            "Steam-Ordner: " + (steam_root() or "nicht gefunden"),
            "Gefundene Spiele: Steam %d, Epic %d, GOG %d, Ubisoft %d" % tuple(sum(g["store"] == n for g in real) for n in ("Steam", "Epic", "GOG", "Ubisoft")),
            "Legendary: " + (find_legendary() or "nicht gefunden")]


def scan():
    inst = steam_games() + epic_games() + gog_games() + ubisoft_games()
    real = list(inst)
    have = {(g["store"], g["sid"]) for g in real}
    for lst in store.get("owned", {}).values():
        for g in lst:
            if (g["store"], g["sid"]) not in have:
                have.add((g["store"], g["sid"]))
                real.append(g)
    demo = bool(store["settings"].get("demo"))
    raw = DEMO if demo else real
    merged = {}
    for g in raw:
        e = merged.setdefault(norm(g["title"]), dict(id=norm(g["title"]), title=g["title"], versions=[]))
        e["versions"].append(g)
    LIB["games"], LIB["demo"] = list(merged.values()), demo
    LIB["launchers"] = ["Steam", "Epic", "GOG"] if demo else detect_launchers({g["store"] for g in inst})
    LIB["diag"] = diagnose(real)


# ---------- Downloads ----------
downloads = []


def run_dl(d):
    lg = find_legendary()
    if lg and d["store"] == "Epic" and not LIB["demo"]:
        p = subprocess.Popen([lg, "install", d["sid"], "--yes"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             text=True, creationflags=0x08000000 if IS_WIN else 0)
        for line in p.stdout:
            m = re.search(r"Progress:\s*([\d.]+)%", line)
            s = re.search(r"Download:\s*([\d.]+)\s*MiB/s", line)
            if m:
                d["pct"] = float(m[1])
            if s:
                d["speed"] = float(s[1])
            if d["state"] != "running":
                p.terminate()
                break
        p.wait()
        if d["state"] == "running":
            d["state"] = "done" if p.returncode == 0 else "error"
            d["pct"] = 100 if p.returncode == 0 else d["pct"]
    else:  # Test-Modus: simulierter Download
        while d["state"] == "running" and d["pct"] < 100:
            lim = store["settings"].get("limit") or 0
            d["speed"] = min(30.0, lim) if lim else 30.0
            d["pct"] = min(100, d["pct"] + d["speed"] * 0.5 / 20)
            time.sleep(0.5)
        if d["state"] == "running":
            d["state"] = "done"
    d["speed"] = 0


def worker():
    while True:
        time.sleep(0.5)
        if not any(d["state"] == "running" for d in downloads):
            q = [d for d in downloads if d["state"] == "queued"]
            if q:
                q[0]["state"] = "running"
                threading.Thread(target=run_dl, args=(q[0],), daemon=True).start()


def launch(gid, st, install=False):
    for g in LIB["games"]:
        if g["id"] == gid:
            for v in g["versions"]:
                if v["store"] != st:
                    continue
                l = v["launch"]
                if v["installed"]:
                    if l.startswith("exe:"):
                        exe, cwd = l[4:].split("|")
                        subprocess.Popen([exe], cwd=cwd)
                    elif IS_WIN and l:
                        os.startfile(l)
                    else:
                        print("Demo-Start:", g["title"], st)
                elif install and IS_WIN:
                    os.startfile(("steam://install/" if st == "Steam" else "goggalaxy://openGameView/") + v["sid"])


DEFAULT_REPO = ""  # wird beim Bau auf GitHub automatisch mit deinem Repository gefuellt
UPD = dict(state="idle", latest="", notes="", err="", url="", checked=0, apply_at=0, reason="", msg="", paused=False, skip="")
START = time.time()
RX_REPO = re.compile(r"^[A-Za-z0-9_.-]{1,80}/[A-Za-z0-9_.-]{1,80}$")


def vt(v):
    return tuple(int(x) for x in re.findall(r"\d+", v or ""))


def upd_repo():
    r = (store["settings"].get("updrepo") or DEFAULT_REPO or "").strip()
    return r if RX_REPO.match(r) else ""


def upd_public():
    return dict(UPD, now=time.time(), repo=upd_repo(), current=VERSION, frozen=bool(getattr(sys, "frozen", False)))


def inbox_add(kind, text, sender="D-Hub", mid=None, ts=None):
    box = store.setdefault("inbox", [])
    mid = mid or "%s-%s" % (kind, time.time())
    if any(m["id"] == mid for m in box):
        return
    box.append({"id": mid, "kind": kind, "from": str(sender)[:60], "text": str(text)[:2000], "ts": ts or time.time(), "read": False})
    del box[:-200]
    save()


def inbox_public():
    return list(reversed(store.get("inbox", [])[-60:]))


def unread_count():
    return sum(not m.get("read") for m in store.get("inbox", []))


def clean_name(n):
    n = str(n or "").strip().lower()
    return n if re.fullmatch(r"[a-z0-9_-]{1,40}", n) else ""


def kf_set(p):
    """Profil von KumpelFunk uebernehmen (Name und Freunde). Alles wird geprueft und gekuerzt."""
    if not isinstance(p, dict):
        return
    name = clean_name(p.get("name"))
    if not name:
        return
    lst = p.get("friends")
    fr, seen = [], set()
    for f in (lst if isinstance(lst, list) else [])[:500]:
        if isinstance(f, str):
            f = {"name": f}
        if not isinstance(f, dict):
            continue
        n = clean_name(f.get("name"))
        if not n or n in seen or n == name:
            continue
        seen.add(n)
        st = f.get("status") if f.get("status") in ("online", "dnd", "offline") else "offline"
        fr.append(dict(name=n, status=st, dhub=bool(f.get("dhub")), playing=str(f.get("playing") or "")[:80]))
    old = store.get("kf") or {}
    changed = (old.get("name"), old.get("friends")) != (name, fr)
    store["kf"] = dict(name=name, friends=fr, ts=time.time())
    if old.get("name") != name:
        inbox_add("system", "Mit KumpelFunk verbunden als %s. %d Freunde übernommen." % (name, len(fr)), mid="kf-" + name)
    elif changed:
        save()


def fetch_control(repo):
    """Steuerdatei des Herausgebers (update.json im Repository): min_version, force, paused, message."""
    try:
        c = fetch_json("https://raw.githubusercontent.com/%s/main/update.json?_=%d" % (repo, int(time.time())))
        return c if isinstance(c, dict) else {}
    except Exception:
        return {}


def check_update(manual=False, first=False):
    repo = upd_repo()
    if not repo:
        UPD.update(state="idle", err="Kein Update-Repository eingetragen." if manual else "")
        return
    UPD.update(state="checking", err="")
    try:
        r = fetch_json("https://api.github.com/repos/%s/releases/latest" % repo)
        want = "D-Hub.exe" if getattr(sys, "frozen", False) else "dhub.py"
        url = next((a["browser_download_url"] for a in r.get("assets", []) if a.get("name") == want), "")
        UPD.update(latest=str(r.get("tag_name", "")).lstrip("vV"), notes=(r.get("body") or "")[:1500], url=url, checked=time.time())
        newer = vt(UPD["latest"]) > vt(VERSION)
        UPD["state"] = "available" if newer and url else "idle"
        if newer and not url:
            UPD["err"] = "Die neue Version hat keine passende Datei (%s)." % want
        ctl = fetch_control(repo)
        UPD["paused"] = bool(ctl.get("paused"))
        UPD["msg"] = str(ctl.get("message") or "")[:300]
        if UPD["paused"]:
            UPD.update(state="idle", apply_at=0, err="Updates sind vom Herausgeber vorübergehend pausiert.")
            return
        if newer and url:
            inbox_add("system", "Update %s ist verfügbar." % UPD["latest"], mid="upd-" + UPD["latest"])
            must = bool(ctl.get("force")) and vt(VERSION) < vt(str(ctl.get("min_version") or ""))
            if not UPD["apply_at"] and (must or UPD["skip"] != UPD["latest"]):
                reason = "force" if must else ("auto" if first and store["settings"].get("autoinstall", True) else "")
                if reason:
                    UPD.update(apply_at=time.time() + (20 if must else 12), reason=reason)
                    if must:
                        inbox_add("system", "Wichtiges Update %s wird installiert.%s" % (UPD["latest"], (" " + UPD["msg"]) if UPD["msg"] else ""), mid="force-" + UPD["latest"])
    except Exception as e:
        UPD.update(state="idle", err=("Update-Prüfung fehlgeschlagen: %s: %s" % (type(e).__name__, e)) if manual else "")


def apply_loop():
    """Setzt geplante Updates (automatisch oder vom Herausgeber erzwungen) um, aber nie waehrend eines Downloads."""
    while True:
        time.sleep(1)
        at = UPD.get("apply_at")
        if not at or time.time() < at or UPD["state"] == "checking":
            continue
        if UPD["state"] != "available":
            UPD["apply_at"] = 0
            continue
        if any(d["state"] in ("running", "queued") for d in downloads):
            UPD["apply_at"] = time.time() + 30
            continue
        apply_update()


def fetch_news():
    repo = upd_repo()
    if not repo:
        return
    try:
        items = fetch_json("https://raw.githubusercontent.com/%s/main/news.json" % repo)
        for n in (items if isinstance(items, list) else [])[:20]:
            if isinstance(n, dict) and n.get("id"):
                inbox_add("news", "%s\n%s" % (str(n.get("title", ""))[:120], str(n.get("text", ""))[:1500]), sender="Neuigkeiten", mid="news-" + str(n["id"])[:40])
    except Exception:
        pass


def update_loop():
    time.sleep(8)
    first = True
    while True:
        if store["settings"].get("autoupdate", True):
            check_update(first=first)
            fetch_news()
            first = False
        time.sleep(3600)


def restart():
    if IS_WIN:
        if getattr(sys, "frozen", False):
            run = '"%s"' % sys.executable
        else:
            pw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
            run = '"%s" "%s"' % (pw if os.path.exists(pw) else sys.executable, os.path.abspath(__file__))
        bat = os.path.join(tempfile.gettempdir(), "dhub_restart.bat")
        with open(bat, "w", encoding="mbcs", errors="replace") as f:
            f.write('@echo off\r\nping -n 3 127.0.0.1 >nul\r\ncd /d "%s"\r\nstart "" %s\r\ndel "%%~f0"\r\n' % (HERE, run))
        env = {k: v for k, v in os.environ.items() if k != "_MEIPASS2"}
        env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
        subprocess.Popen([bat], creationflags=0x08000000 | 0x00000008, close_fds=True, env=env)
    threading.Thread(target=lambda: (time.sleep(1), os._exit(0)), daemon=True).start()


def apply_update():
    repo = upd_repo()
    if UPD["state"] != "available" or not UPD["url"]:
        return
    if not UPD["url"].startswith("https://github.com/%s/releases/download/" % repo):
        UPD.update(state="idle", err="Die Download-Adresse passt nicht zum Repository. Abgebrochen.")
        return
    UPD.update(state="downloading", err="", apply_at=0)
    frozen = getattr(sys, "frozen", False)
    target = sys.executable if frozen else os.path.abspath(__file__)
    tmp = target + ".new"
    try:
        req = urllib.request.Request(UPD["url"], headers={"User-Agent": "D-Hub/" + VERSION})
        data = urllib.request.urlopen(req, timeout=180).read()
        if frozen:
            if data[:2] != b"MZ" or len(data) < 1000000:
                raise ValueError("Der Download ist keine gültige .exe")
        else:
            src = data.decode("utf-8-sig")
            compile(src, "dhub.py", "exec")
            if "\nVERSION = " not in src:
                raise ValueError("Der Download ist keine D-Hub-Datei")
            data = src.encode("utf-8")
        with open(tmp, "wb") as f:
            f.write(data)
        if frozen:
            old = target + ".old"
            try:
                os.remove(old)
            except OSError:
                pass
            os.replace(target, old)
            try:
                os.replace(tmp, target)
            except Exception:
                os.replace(old, target)
                raise
        else:
            shutil.copy2(target, target + ".bak")
            os.replace(tmp, target)
        UPD["state"] = "restarting"
        restart()
    except Exception as e:
        try:
            os.remove(tmp)
        except OSError:
            pass
        UPD.update(state="available", err="Update fehlgeschlagen: %s: %s" % (type(e).__name__, e))


RUNKEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
WIN_PAGES = {"mic": "ms-settings:privacy-microphone", "cam": "ms-settings:privacy-webcam", "notif": "ms-settings:notifications",
             "startup": "ms-settings:startupapps", "security": "windowsdefender://network/"}
LAST, BYE = [time.time()], [0.0]


def consent(name):
    if not IS_WIN:
        return None
    try:
        k = winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\CapabilityAccessManager\ConsentStore" + "\\" + name)
        return winreg.QueryValueEx(k, "Value")[0]
    except OSError:
        return None


def autostart_get():
    if not IS_WIN:
        return False
    try:
        winreg.QueryValueEx(winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUNKEY), "D-Hub")
        return True
    except OSError:
        return False


def autostart_set(on):
    if not IS_WIN:
        return
    k = winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUNKEY, 0, winreg.KEY_SET_VALUE)
    if on:
        if getattr(sys, "frozen", False):
            cmd = '"%s"' % sys.executable
        else:
            pw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
            cmd = '"%s" "%s"' % (pw if os.path.exists(pw) else sys.executable, os.path.abspath(__file__))
        winreg.SetValueEx(k, "D-Hub", 0, winreg.REG_SZ, cmd)
    else:
        try:
            winreg.DeleteValue(k, "D-Hub")
        except OSError:
            pass


def edge_path():
    return next((e for e in (shutil.which("msedge"), r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe") if e and os.path.exists(e)), None)


def do_action(b):
    n = b.get("name")
    if n == "winpage" and IS_WIN and b.get("page") in WIN_PAGES:
        os.startfile(WIN_PAGES[b["page"]])
    elif n == "autostart":
        autostart_set(bool(b.get("on")))
    elif n == "edgemic" and edge_path():
        subprocess.Popen([edge_path(), "edge://settings/content/microphone"])
    elif n == "opendata" and IS_WIN:
        os.startfile(HERE)
    elif n == "clearprices":
        store["listprices"] = {}
        save()
    elif n == "checkupdate":
        threading.Thread(target=check_update, args=(True,), daemon=True).start()
    elif n == "cancelapply":
        UPD.update(apply_at=0, skip=UPD.get("latest", ""))
    elif n == "applyupdate":
        threading.Thread(target=apply_update, daemon=True).start()
    elif n == "markread":
        for m in store.get("inbox", []):
            m["read"] = True
        save()
    elif n == "clearinbox":
        store["inbox"] = []
        save()
    elif n == "kfprofile":
        kf_set(b.get("profile"))
    elif n == "kfmsg":
        mid = ("kf-msg-" + str(b["id"])[:40]) if b.get("id") else None
        if str(b.get("text") or "").strip():
            inbox_add("msg", str(b["text"]).strip(), sender=clean_name(b.get("from")) or "KumpelFunk", mid=mid)
    elif n == "outmsg":
        if str(b.get("text") or "").strip() and clean_name(b.get("to")):
            inbox_add("out", str(b["text"]).strip(), sender="Du an " + clean_name(b["to"]))
            for m in store["inbox"]:
                m["read"] = True if m["kind"] == "out" else m["read"]


def watcher():
    """Beendet D-Hub, wenn das Fenster geschlossen wurde (und keine Downloads mehr laufen)."""
    while True:
        time.sleep(1)
        busy = any(d["state"] in ("running", "queued") for d in downloads)
        if BYE[0] and LAST[0] <= BYE[0] and time.time() - BYE[0] > 4 and not busy:
            os._exit(0)


def state():
    pending = 0
    for g in LIB["games"]:
        lp = store["listprices"].get(g["id"])
        manual = store["prices"].get(g["id"])
        pending += lp is None
        g["manual"] = manual
        g["price"] = manual if manual is not None else (lp or {}).get("v")
        g["psrc"] = "Manuell" if manual is not None else (lp or {}).get("src", "")
    return dict(games=LIB["games"], demo=LIB["demo"], launchers=LIB["launchers"], pending=pending, version=VERSION, diag=LIB["diag"], perr=LIB["perr"],
                downloads=downloads, settings={k: v for k, v in store["settings"].items() if k != "steamkey"},
                errs=LIB["errs"], busy=LIB["busy"], upd=upd_public(), inbox=inbox_public(), unread=unread_count(), kf=store.get("kf") or {}, perms=dict(mic=consent("microphone"), cam=consent("webcam")), autostart=autostart_get(), galaxy=os.path.exists(GALAXY_DB), haskey=bool(store["settings"].get("steamkey")), legendary=bool(find_legendary()))


PAGE = r'''<!DOCTYPE html>
<html lang="de"><head><meta charset="utf-8"><title>D-Hub</title>
<link rel="icon" href="/logo.png">
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
:root{--bg:#050605;--panel:#0e120c;--line:#1f2c19;--tx:#ececec;--dim:#8d9889;--ac:#5cff0c;--warn:#ffb454;
--Steam:#66c0f4;--GOG:#b88cff;--Epic:#dcdcdc;--EA:#ff7a7a;--Ubisoft:#5aa9ff;--Battlenet:#3fb6ff}
*{box-sizing:border-box}
body{margin:0;background:radial-gradient(900px 420px at 50% -15%,rgba(92,255,12,.10) 0,transparent 65%),var(--bg);color:var(--tx);
 font:14px/1.45 "Segoe UI Variable Text","Segoe UI",system-ui,sans-serif;display:flex;flex-direction:column;height:100vh}
header{display:flex;align-items:center;gap:16px;padding:12px 24px;border-bottom:1px solid var(--line);flex-wrap:wrap;background:rgba(5,6,5,.88);backdrop-filter:blur(8px)}
.brand small{font:500 12px "Segoe UI",sans-serif;color:var(--dim);margin-left:6px;letter-spacing:0}
.brand{display:flex;align-items:center;gap:10px;font:italic 800 25px/1 "Segoe UI Variable Display","Segoe UI",sans-serif;letter-spacing:1px}
.brand b{color:var(--ac);font-weight:800}
.brand img{height:42px;width:42px;object-fit:contain;mix-blend-mode:lighten}
nav{display:flex;gap:4px;flex:1;flex-wrap:wrap}
nav button{background:none;border:0;color:var(--dim);padding:8px 11px;font:inherit;font-size:14.5px;cursor:pointer;border-radius:8px;transition:background .15s,color .15s}
nav button:hover{color:var(--tx);background:var(--panel)}nav button.on{color:#041100;background:var(--ac);font-weight:600}
#tools{display:flex;gap:8px;align-items:center}
input,select{background:var(--panel);border:1px solid var(--line);color:var(--tx);padding:7px 11px;border-radius:8px;font:inherit}
input:focus-visible,button:focus-visible,select:focus-visible,.card:focus-visible{outline:2px solid var(--ac);outline-offset:2px}
.btn{background:var(--panel);border:1px solid var(--line);color:var(--tx);padding:6px 13px;border-radius:8px;cursor:pointer;font:inherit}
.btn:hover{border-color:var(--ac)}.btn.pri{background:var(--ac);color:#041100;border-color:var(--ac);font-weight:600}
main{flex:1;overflow:auto;padding:22px 24px}
h2{margin:0 0 4px;font-size:22px}.sub{color:var(--dim);margin-bottom:20px}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(var(--cw,170px),1fr));gap:22px 18px;padding:6px}
.card{cursor:pointer;position:relative;transition:transform .22s cubic-bezier(.2,.8,.2,1)}
.card:hover,.card:focus-visible{transform:translateY(-7px) scale(1.05);z-index:3}
.art{aspect-ratio:2/3;border-radius:10px;position:relative;overflow:hidden;display:flex;align-items:flex-end;padding:12px;font-weight:700;font-size:16px;line-height:1.2;
 background:linear-gradient(160deg,hsl(var(--h) 45% 34%),hsl(calc(var(--h) + 40) 50% 14%));box-shadow:0 2px 8px rgba(0,0,0,.35);transition:box-shadow .22s}
.card:hover .art,.card:focus-visible .art{box-shadow:0 16px 32px rgba(0,0,0,.55),0 0 0 2px var(--ac),0 0 26px rgba(92,255,12,.30)}
.art img{position:absolute;inset:0;width:100%;height:100%;object-fit:cover}
.play{position:absolute;left:0;right:0;bottom:0;padding:34px 12px 12px;background:linear-gradient(transparent,rgba(8,10,18,.92));font-weight:600;z-index:2;
 opacity:0;transform:translateY(10px);transition:opacity .2s,transform .2s}
.card:hover .play,.card:focus-visible .play{opacity:1;transform:none}
.art span{transition:opacity .2s}.card:hover .art span,.card:focus-visible .art span{opacity:0}
.card.off .art img,.card.off .art{filter:saturate(.45) brightness(.8)}
.badges{position:absolute;top:8px;left:8px;display:flex;gap:4px;z-index:2;flex-wrap:wrap}
.b{font-style:normal;font-size:11px;font-weight:600;padding:1px 8px;border-radius:10px;background:rgba(10,12,20,.82);color:var(--c,var(--dim))}
.Steam{--c:var(--Steam)}.GOG{--c:var(--GOG)}.Epic{--c:var(--Epic)}.EA{--c:var(--EA)}.Ubisoft{--c:var(--Ubisoft)}.Battlenet{--c:var(--Battlenet)}
.name{margin-top:9px;font-weight:600;overflow:hidden;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical}
.meta{color:var(--dim);font-size:12px}
table{width:100%;border-collapse:collapse}th{text-align:left;color:var(--dim);font-weight:500;padding:9px 12px;border-bottom:1px solid var(--line)}
td{padding:9px 12px;border-bottom:1px solid var(--line)}tr:hover td{background:var(--panel)}
.panel{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:18px;margin-bottom:16px;max-width:820px}
.row{display:flex;align-items:center;gap:14px;padding:11px 0;border-bottom:1px solid var(--line)}.row:last-child{border:0}
.bar{height:6px;background:var(--line);border-radius:3px;overflow:hidden;margin-top:7px}.bar i{display:block;height:100%;background:var(--ac);transition:width .4s}
.stat{font-size:38px;font-weight:700;letter-spacing:-1px}.dim{color:var(--dim)}
#kfwrap .bar{display:flex;align-items:center;gap:12px;padding:6px 24px;color:var(--dim);font-size:13px;border-bottom:1px solid var(--line)}
#kf{flex:1;width:100%;border:0;background:var(--bg)}
body.light .btn.pri,body.light nav button.on{color:#fff}
*{scrollbar-color:#25361d transparent}
.b.ok{--c:#7ddc8a}.b.warn{--c:var(--warn)}.b.mute{--c:var(--dim)}
body.light{--ac:#2a9d00;--bg:#f4f2ee;--panel:#fff;--line:#dcd7ca;--tx:#1d2030;--dim:#5f667c;background:var(--bg)}
body.light header{background:rgba(244,242,238,.92)}
#kfwrap{display:flex;flex:1;min-height:0;flex-direction:column}
#kfwrap.hide{position:fixed;left:-12000px;top:0;width:1100px;height:720px;flex:none}
#hact{display:flex;gap:6px;align-items:center}
.hbtn{position:relative;background:none;border:1px solid transparent;color:var(--dim);width:42px;height:42px;border-radius:11px;cursor:pointer;display:grid;place-items:center}
.hbtn:hover,.hbtn.on{color:var(--tx);background:var(--panel);border-color:var(--line)}
.hbtn svg{width:23px;height:23px;stroke:currentColor;fill:none;stroke-width:1.8;stroke-linecap:round;stroke-linejoin:round}
.dot{position:absolute;top:1px;right:0;min-width:18px;height:18px;border-radius:9px;background:var(--ac);color:#041100;font:700 11px/18px "Segoe UI",sans-serif;text-align:center;padding:0 4px}
.dot[hidden]{display:none}
.pop{position:fixed;top:68px;right:16px;width:min(390px,calc(100vw - 32px));max-height:calc(100vh - 110px);background:var(--panel);border:1px solid var(--line);border-radius:14px;box-shadow:0 24px 60px rgba(0,0,0,.65);z-index:20;display:none;flex-direction:column;overflow:hidden}
.pop.on{display:flex}
.pop h3{margin:0;padding:13px 16px;font-size:15px;border-bottom:1px solid var(--line);display:flex;align-items:center;gap:8px}
.pop .body{overflow:auto;flex:1;min-height:80px}
.msg{padding:11px 16px;border-bottom:1px solid var(--line)}
.msg.new{border-left:3px solid var(--ac);background:rgba(92,255,12,.05)}
.msg small{color:var(--dim)}.msg p{margin:3px 0 0;white-space:pre-wrap;word-break:break-word}
.compose{display:flex;gap:8px;padding:10px 12px;border-top:1px solid var(--line);align-items:center}
.compose input{flex:1;min-width:0}.compose select{max-width:120px}
.fr{display:flex;align-items:center;gap:11px;padding:8px 16px;cursor:pointer}
.fr:hover{background:rgba(255,255,255,.05)}
.av{width:36px;height:36px;border-radius:50%;display:grid;place-items:center;font-weight:700;color:#fff;background:hsl(var(--h) 45% 36%);position:relative;flex:none}
.av i{position:absolute;right:-2px;bottom:-2px;width:12px;height:12px;border-radius:50%;border:2px solid var(--panel);background:#555}
.av i.online{background:#5cff0c}.av i.dnd{background:#ff5c5c}
.sect{padding:12px 16px 4px;color:var(--dim);font-size:12px;text-transform:uppercase;letter-spacing:.7px}
textarea.code{width:100%;height:140px;font:12px/1.4 Consolas,monospace;background:var(--bg);color:var(--tx);border:1px solid var(--line);border-radius:8px;padding:8px;margin-top:8px}
pre.notes{white-space:pre-wrap;background:var(--bg);border:1px solid var(--line);border-radius:8px;padding:10px;color:var(--dim);max-height:140px;overflow:auto}
#lost{display:none;position:fixed;inset:0;background:rgba(5,6,5,.95);z-index:50;align-items:center;justify-content:center;text-align:center;padding:30px}
#splash{position:fixed;inset:0;z-index:100;background:#000;display:flex;flex-direction:column;align-items:center;justify-content:center;cursor:pointer;animation:spOut .5s ease 2.35s forwards}
@keyframes spOut{to{opacity:0;visibility:hidden;transform:scale(1.05)}}
.sp-logo{position:relative;width:min(34vmin,260px);aspect-ratio:1;animation:spGlow 1.7s ease-in-out .9s 1}
.sp-logo img{display:block;width:100%;height:100%;object-fit:cover;border-radius:50%;animation:spLogo 1s cubic-bezier(.2,.9,.2,1) both}
@keyframes spLogo{from{opacity:0;transform:scale(.5) rotate(-10deg);filter:blur(12px)}to{opacity:1;transform:none;filter:blur(0)}}
@keyframes spGlow{0%,100%{filter:drop-shadow(0 0 0 rgba(92,255,12,0))}50%{filter:drop-shadow(0 0 28px rgba(92,255,12,.8))}}
.sp-logo::before,.sp-logo::after{content:"";position:absolute;inset:0;border-radius:50%;border:2px solid #5cff0c;opacity:0;animation:spPulse 1.5s ease-out .7s both}
.sp-logo::after{animation-delay:1.15s}
@keyframes spPulse{from{opacity:.75;transform:scale(.9)}to{opacity:0;transform:scale(1.75)}}
.sp-tag{margin-top:28px;font:600 14px/1 "Segoe UI Variable Text","Segoe UI",sans-serif;text-transform:uppercase;color:#8d9889;white-space:nowrap;opacity:0;animation:spTag 1s cubic-bezier(.2,.9,.2,1) 1.1s both}
.sp-tag b{color:#5cff0c;font-weight:600}
@keyframes spTag{from{opacity:0;letter-spacing:14px}to{opacity:1;letter-spacing:5px}}
.sp-bar{margin-top:26px;width:190px;height:3px;background:#1a2614;border-radius:2px;overflow:hidden}
.sp-bar i{display:block;height:100%;background:#5cff0c;box-shadow:0 0 10px #5cff0c;transform-origin:left;animation:spBar 1.3s ease-in-out .85s both}
@keyframes spBar{from{transform:scaleX(0)}to{transform:scaleX(1)}}
@media(prefers-reduced-motion:reduce){#splash,#splash *{animation:none!important}#splash{animation:spOut .3s ease .7s forwards!important}.sp-logo::before,.sp-logo::after{display:none}}
#ubanner{display:none;align-items:center;gap:12px;padding:10px 24px;background:rgba(92,255,12,.09);border-bottom:1px solid var(--ac)}
.empty{max-width:520px;margin:40px 0;color:var(--dim)}
footer{padding:9px 24px;border-top:1px solid var(--line);color:var(--dim);font-size:13px}
#toast{position:fixed;bottom:48px;left:50%;transform:translateX(-50%);background:var(--panel);border:1px solid var(--ac);padding:9px 18px;border-radius:10px;opacity:0;transition:opacity .2s;pointer-events:none;z-index:9}
#toast.on{opacity:1}
@media(prefers-reduced-motion:reduce){*{transition:none!important}.card:hover{transform:none}}
</style></head><body>
<div id="splash" aria-hidden="true"><div class="sp-logo"><img src="/logo.png" alt=""></div>
 <div class="sp-tag">Alle Spiele. <b>Ein Ort.</b></div>
 <div class="sp-bar"><i></i></div></div>
<header>
 <div class="brand"><img src="/logo.png" alt="" onerror="this.remove()"><span>D-<b>HUB</b></span><small id="ver"></small></div>
 <nav></nav>
 <div id="tools"><select id="flt"><option value="all">Alle Spiele</option><option value="inst">Installiert</option><option value="not">Nicht installiert</option></select><input id="q" type="search" placeholder="Spiele suchen"><button class="btn" id="vw"></button></div>
<div id="hact">
 <button class="btn pri" id="updbtn" style="display:none"></button>
 <button class="hbtn" id="frbtn" title="Freunde" aria-label="Freunde"><svg viewBox="0 0 24 24"><circle cx="9" cy="8" r="3.5"/><path d="M2.5 20c.6-3.6 3.2-5.5 6.5-5.5s5.9 1.9 6.5 5.5"/><circle cx="17.5" cy="9" r="2.6"/><path d="M17 14.3c2.5.1 4.2 1.6 4.7 4.2"/></svg><span class="dot" id="frdot" hidden></span></button>
 <button class="hbtn" id="mlbtn" title="Nachrichten" aria-label="Nachrichten"><svg viewBox="0 0 24 24"><rect x="3" y="5" width="18" height="14" rx="2.5"/><path d="m3.5 7 8.5 6.5L20.5 7"/></svg><span class="dot" id="mldot" hidden></span></button>
</div>
</header>
<div class="pop" id="mlpop"><h3>Nachrichten<span style="flex:1"></span><button class="btn" onclick="api('action',{name:'clearinbox'}).then(()=>updChrome(true))">Leeren</button></h3><div class="body" id="mlbody"></div><div class="compose" id="mlcomp"></div></div>
<div class="pop" id="frpop"><h3>Freunde</h3><div class="body" id="frbody"></div></div>
<div id="lost"><div><b style="font-size:18px">D-Hub wurde beendet oder startet neu.</b><p class="dim">Nach einem Update öffnet sich automatisch ein neues Fenster.<br>Du kannst dieses Fenster schließen.</p></div></div>
<div id="ubanner"></div>
<main></main>
<div id="kfwrap" class="hide"><div class="bar"><span style="flex:1">KumpelFunk von Lerun3. Das Gespräch läuft weiter, wenn du den Reiter wechselst.</span>
 <button class="btn" onclick="api('connect',{store:'KumpelFunk'})">In eigenem Fenster öffnen</button></div>
 <iframe id="kf" title="KumpelFunk" allow="microphone; camera; display-capture; autoplay; clipboard-write; fullscreen"
  sandbox="allow-scripts allow-same-origin allow-forms allow-popups allow-modals allow-downloads"></iframe></div><div id="toast"></div>
<footer id="status">Bereit</footer>
<script>
const KF_URL='https://kumpelfunk.lerun3-funk.workers.dev';
let S=null,tab='Alle',q='',navKey=null,flt='all',stab='Allgemein';
const $=s=>document.querySelector(s), SCANNED=['Steam','Epic','GOG','Ubisoft','EA','Battle.net'];
const esc=s=>String(s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const sc=s=>s.replace(/\W/g,'');
const hue=t=>[...t].reduce((a,c)=>(a*31+c.charCodeAt(0))>>>0,7)%360;
const gb=b=>b?(b/1e9).toFixed(1)+' GB':'–';
const eur=n=>n.toLocaleString('de-DE',{style:'currency',currency:'EUR'});
const isLib=()=>tab=='Alle'||S.launchers.includes(tab);
async function api(p,b){const r=await fetch('/api/'+p,b?{method:'POST',body:JSON.stringify(b)}:{});S=await r.json();return S}
function toast(t){const e=$('#toast');e.textContent=t;tr(e);e.classList.add('on');setTimeout(()=>e.classList.remove('on'),2400)}
const stores=g=>[...new Set(g.versions.map(v=>v.store))];
const badges=g=>stores(g).map(s=>`<i class="b ${sc(s)}">${s}</i>`).join('');
const installed=g=>g.versions.some(v=>v.installed);

function visible(){return S.games.filter(g=>(tab=='Alle'||g.versions.some(v=>v.store==tab))&&g.title.toLowerCase().includes(q)&&(flt=='all'||(flt=='inst')==installed(g)))
 .sort((a,b)=>a.title.localeCompare(b.title))}

function play(id){
 const g=S.games.find(x=>x.id==id), order=['GOG','Steam','Epic'];
 const inst=g.versions.filter(v=>v.installed).sort((a,b)=>order.indexOf(a.store)-order.indexOf(b.store))[0];
 if(inst){api('launch',{id,store:inst.store});toast('Startet über '+inst.store+' …');return}
 const ep=g.versions.find(v=>v.store=='Epic'&&S.legendary);
 if(ep){api('dl',{action:'add',title:g.title,store:'Epic',sid:ep.sid});toast('Installation eingereiht');return}
 const o=g.versions.find(v=>v.store=='Steam'||v.store=='GOG');
 if(o){api('launch',{id,store:o.store,install:true});toast('Öffnet '+o.store+' zur Installation …')}
 else toast('Installieren ist für diesen Store noch nicht möglich')
}

function banner(){
 const es=tab=='Alle'?Object.entries(S.errs):(S.errs[tab]?[[tab,S.errs[tab]]]:[]);
 return es.map(([s,m])=>{
  const b=s=='Steam'?`<button class="btn" onclick="api('connect',{store:'Steam'})">Steam-Datenschutz öffnen</button>`
   :s=='Epic'?`<button class="btn" onclick="api('connect',{store:'Epic'});toast('Legendary wird geladen oder die Anmeldung öffnet sich')">${S.legendary?'Epic anmelden':'Epic verbinden'}</button>`
   :`<button class="btn" onclick="api('connect',{store:'Galaxy'})">GOG Galaxy holen</button>`;
  return `<div class="panel" style="border-color:var(--warn)"><div class="row"><span style="flex:1">${esc(m)}</span>${b}<button class="btn" onclick="api('rescan',{});toast('Lädt neu …')">Neu einlesen</button></div></div>`}).join('')}
function libView(){
 const gs=visible(), n=gs.filter(installed).length;
 let h=`<h2>${tab=='Alle'?'Alle Spiele':tab}</h2><div class="sub">${gs.length} Spiele · ${n} installiert</div>`+banner();
 if(!gs.length) return h+(tab!='Alle'&&!SCANNED.includes(tab)
  ?`<div class="empty">D-Hub hat ${tab} auf diesem PC gefunden. Das Einlesen der ${tab}-Spiele kommt in einer der nächsten Versionen.</div>`
  :tab=='Alle'&&!S.games.length
  ?`<div class="empty"><b>D-Hub hat auf diesem PC noch keine Spiele gefunden.</b><br>Unter Einstellungen findest du die Diagnose, was geprüft wurde.<br><br><button class="btn" onclick="api('settings',{demo:true}).then(render)">Demo-Daten ansehen</button></div>`
  :'<div class="empty">Keine Treffer.</div>');
 if(S.settings.view=='list') return h+`<table><tr><th>Titel</th><th>Stores</th><th>Größe</th><th>Status</th><th></th></tr>`+
  gs.map(g=>`<tr><td>${esc(g.title)}</td><td>${badges(g)}</td><td>${gb(Math.max(...g.versions.map(v=>v.size)))}</td>
  <td>${installed(g)?'Installiert':'Nicht installiert'}</td><td><button class="btn pri" onclick="play('${g.id}')">${installed(g)?'Spielen':'Installieren'}</button></td></tr>`).join('')+'</table>';
 return h+'<div class="grid">'+gs.map(g=>{
  const st=g.versions.find(v=>v.store=='Steam');
  const img=st&&!S.demo?`<img src="https://cdn.cloudflare.steamstatic.com/steam/apps/${st.sid}/library_600x900.jpg" loading="lazy" alt="" onerror="this.remove()">`:'';
  return `<div class="card ${installed(g)?'':'off'}" tabindex="0" onclick="play('${g.id}')" onkeydown="if(event.key=='Enter')play('${g.id}')">
  <div class="art" style="--h:${hue(g.title)}"><div class="badges">${badges(g)}</div><span>${esc(g.title)}</span>${img}
  <div class="play">${installed(g)?'Spielen':'Installieren'}</div></div>
  <div class="name">${esc(g.title)}</div><div class="meta">${installed(g)?gb(Math.max(...g.versions.map(v=>v.size))):'Nicht installiert'}</div></div>`}).join('')+'</div>'}

function dlView(){
 const ds=S.downloads, run=d=>d.state=='running';
 return `<h2>Downloads</h2><div class="sub">Alles an einem Ort, ohne dass du einen Launcher öffnest.</div>
 <div class="panel"><div class="row"><div style="flex:1"><b>Geschwindigkeit</b><div class="dim">Maximal ${S.settings.limit?S.settings.limit+' MB/s':'unbegrenzt'} (0 = unbegrenzt)</div></div>
  <input type="number" min="0" style="width:90px" value="${S.settings.limit}" onchange="api('settings',{limit:+this.value});toast('Gespeichert')"> MB/s</div></div>
 <div class="panel">`+(ds.length?ds.map(d=>`<div class="row"><div style="flex:1"><b>${esc(d.title)}</b> <i class="b ${sc(d.store)}">${d.store}</i>
  <div class="bar"><i style="width:${d.pct}%"></i></div>
  <div class="dim">${{queued:'Wartet',running:'Lädt',paused:'Pausiert',done:'Fertig',cancelled:'Abgebrochen',error:'Fehler'}[d.state]} · ${d.pct.toFixed(1)} %${run(d)?' · '+d.speed.toFixed(1)+' MB/s':''}</div></div>
  ${d.state=='running'||d.state=='queued'?`<button class="btn" onclick="api('dl',{action:'pause',id:'${d.id}'})">Pausieren</button>`:''}
  ${d.state=='paused'?`<button class="btn" onclick="api('dl',{action:'resume',id:'${d.id}'})">Fortsetzen</button>`:''}
  ${['queued','running','paused'].includes(d.state)?`<button class="btn" onclick="api('dl',{action:'cancel',id:'${d.id}'})">Abbrechen</button>`:''}</div>`).join(''):'<div class="dim">Keine Downloads. Klicke ein nicht installiertes Epic-Spiel an oder starte einen Test-Download.</div>')+
 `</div><button class="btn" onclick="api('dl',{action:'add',title:'Test-Spiel '+(S.downloads.length+1),store:'Epic',sid:'test'})">Test-Download starten</button>
  <button class="btn" onclick="api('dl',{action:'clear'})">Beendete entfernen</button>`}

function valView(){
 const gs=[...S.games].sort((a,b)=>(b.price||0)-(a.price||0)), sum=gs.reduce((a,g)=>a+(g.price||0),0), no=gs.filter(g=>g.price==null).length;
 const per=S.launchers.map(s=>[s,S.games.filter(g=>g.versions.some(v=>v.store==s)).reduce((a,g)=>a+(g.price||0),0)]).filter(x=>x[1]>0);
 return `<h2>Wert deiner Bibliothek</h2><div class="sub">Basis sind Online-Listenpreise ohne Rabatt: zuerst Steam, dann GOG, dann der Preisvergleich CheapShark (auch für Epic und Ubisoft, Umrechnung von USD). Eigene Preise überschreiben alles.</div>
 <div class="panel"><div class="stat">${eur(sum)}</div><div class="dim">${S.games.length} Spiele, jedes nur einmal gezählt${no?' · '+no+' ohne Preis':''}${S.pending?' · Preise werden geladen ('+S.pending+' offen)':''}</div>${S.perr?'<div class="meta" style="color:var(--warn);margin-top:8px">Preisabfrage fehlgeschlagen: '+esc(S.perr)+'</div>':''}
 ${per.length?'<div style="margin-top:12px">'+per.map(([s,v])=>`<i class="b ${sc(s)}">${s}</i> ${eur(v)} &nbsp; `).join('')+'</div>':''}</div>
 <div class="panel"><table><tr><th>Spiel</th><th>Stores</th><th>Preis</th><th>Eigener Preis (€)</th></tr>`+gs.map(g=>`<tr><td>${esc(g.title)}</td><td>${badges(g)}</td>
 <td>${g.price==null?'<span class="dim">–</span>':eur(g.price)}<div class="meta">${esc(g.psrc||'')}</div></td>
 <td><input type="number" min="0" step="0.01" style="width:100px" value="${g.manual??''}" placeholder="${g.price??''}" onchange="api('price',{id:'${g.id}',price:this.value===''?null:+this.value}).then(render)"></td></tr>`).join('')+'</table></div>'}

function setView(){
 const c=s=>S.games.filter(g=>g.versions.some(v=>v.store==s)).length, L=S.launchers, has=x=>L.includes(x), P=S.perms||{}, E=S.errs, T=S.settings;
 const chip=k=>({ok:'<i class="b ok">Bereit</i>',todo:'<i class="b warn">Aktion nötig</i>',na:'<i class="b mute">Nicht erkannt</i>'}[k]||'');
 const row=(l,ctl)=>`<div class="row"><span style="flex:1">${l}</span>${ctl}</div>`;
 const prow=(l,k,bt,fn)=>row(l,chip(k)+`<button class="btn" onclick="${fn}">${bt}</button>`);
 const sel=(k,o,cur)=>`<select onchange="api('settings',{${k}:this.value}).then(render)">${o.map(([v,l])=>`<option value="${v}" ${v==cur?'selected':''}>${l}</option>`).join('')}</select>`;
 const chk=(on,fn)=>`<input type="checkbox" ${on?'checked':''} onchange="${fn}">`;
 const act=(n,l)=>`<button class="btn" onclick="api('action',{name:'${n}'}).then(render)">${l}</button>`;
 const win=n=>`api('action',{name:'winpage',page:'${n}'})`, st=v=>v=='Allow'?'ok':v=='Deny'?'todo':'na';
 const gal=['Ubisoft','EA','Battle.net'].filter(has);
 let h=`<h2>Einstellungen</h2><div class="sub">D-Hub Beta ${S.version}</div><div style="display:flex;gap:6px;margin-bottom:18px;flex-wrap:wrap">`+
  ['Allgemein','Berechtigungen','Konten','Updates','Erweitert'].map(x=>`<button class="btn ${x==stab?'pri':''}" onclick="stab='${x}';render()">${x}</button>`).join('')+'</div>';
 if(stab=='Updates') return h+updView();
 if(stab=='Allgemein') return h+'<div class="panel">'+
  row('Sprache',sel('lang',[['de','Deutsch'],['en','English']],T.lang||'de'))+
  row('Design',sel('theme',[['dark','Dunkel'],['light','Hell']],T.theme||'dark'))+
  row('Cover-Größe',sel('cover',[['small','Klein'],['medium','Mittel'],['large','Groß']],T.cover||'medium'))+
  row('Standardansicht',sel('view',[['cover','Cover'],['list','Liste']],T.view))+
  row('Start-Reiter',sel('starttab',['Alle',...L,'Downloads','Wert','KumpelFunk'].map(x=>[x,x]),T.starttab||'Alle'))+
  row('Mit Windows starten',chk(S.autostart,"api('action',{name:'autostart',on:this.checked}).then(render)"))+
  row('Preise automatisch laden',chk(T.autoprices!==false,"api('settings',{autoprices:this.checked})"))+row('KumpelFunk beim Start verbinden',chk(T.kfauto!==false,"api('settings',{kfauto:this.checked})"))+row('Updates automatisch suchen',chk(T.autoupdate!==false,"api('settings',{autoupdate:this.checked})"))+row('Updates automatisch installieren (beim Start)',chk(T.autoinstall!==false,"api('settings',{autoinstall:this.checked})"))+row('Startanimation zeigen',chk(T.splash!==false,"api('settings',{splash:this.checked});try{localStorage.setItem('dhub_nosplash',this.checked?'':'1')}catch(e){}"))+'</div>';
 if(stab=='Berechtigungen') return h+'<div class="panel"><b>Windows</b><p class="dim">Diese Schalter gehören Windows. D-Hub kann sie nicht für dich umlegen, öffnet aber die passende Seite.</p>'+
  prow('Mikrofon (für KumpelFunk)',st(P.mic),'Öffnen',win('mic'))+prow('Kamera',st(P.cam),'Öffnen',win('cam'))+
  prow('Benachrichtigungen','','Öffnen',win('notif'))+prow('Autostart-Apps','','Öffnen',win('startup'))+prow('Windows-Sicherheit und Firewall','','Öffnen',win('security'))+
  '</div><div class="panel"><b>Anbieter</b><p class="dim">Hier siehst du, wo ein Anbieter noch eine Freigabe von dir braucht.</p>'+
  prow('Steam: Spieldetails öffentlich',!has('Steam')?'na':E.Steam?'todo':'ok','Öffnen',"api('connect',{store:'Steam'})")+
  prow('Epic: Anmeldung über Legendary',!has('Epic')?'na':(E.Epic||!S.legendary)?'todo':'ok',S.legendary?'Anmelden':'Verbinden',"api('connect',{store:'Epic'});toast('Legendary wird geladen oder die Anmeldung öffnet sich')")+
  prow('GOG Galaxy (für GOG, Ubisoft, EA, Battle.net)',S.galaxy?'ok':(has('GOG')||gal.length)?'todo':'na','Galaxy holen',"api('connect',{store:'Galaxy'})")+
  prow('KumpelFunk: Mikrofon im Browser','','Öffnen',"api('action',{name:'edgemic'})")+'</div>';
 if(stab=='Konten') return h+'<div class="panel"><b>Erkannte Launcher</b>'+
  (L.map(s=>row(`<i class="b ${sc(s)}">${s}</i> ${c(s)} Spiele${E[s]?`<div class="meta" style="color:var(--warn)">${esc(E[s])}</div>`:''}`,
    s=='Epic'&&E.Epic?`<button class="btn" onclick="api('connect',{store:'Epic'})">${S.legendary?'Epic anmelden':'Epic verbinden'}</button>`:'')).join('')||'<div class="dim">Keine Launcher gefunden.</div>')+
  `<p class="dim">Für Ubisoft, EA und Battle.net braucht D-Hub GOG Galaxy als Brücke.</p><button class="btn" onclick="api('rescan',{});toast('Lädt neu …')">Neu einlesen</button></div>
 ${kfPanel()}<div class="panel"><b>Steam-Bibliothek</b><p class="dim">D-Hub liest deine Spiele über dein Steam-Profil. Die Spieldetails dort müssen auf Öffentlich stehen. Ein API-Key (steamcommunity.com/dev/apikey) ist optional.</p>
 <div class="row"><span style="width:90px">API-Key</span><input type="password" id="skey" placeholder="${S.haskey?'gespeichert':'optional'}" style="flex:1"></div>
 <div class="row"><span style="width:90px">Steam-ID</span><input id="sid" placeholder="${T.steamid||'automatisch erkannt'}" style="flex:1"></div>
 <button class="btn pri" onclick="saveSteam()">Speichern und neu laden</button></div>`;
 return h+'<div class="panel">'+row('Demo-Daten anzeigen',chk(S.demo,"api('settings',{demo:this.checked}).then(render)"))+
  row('Datenordner öffnen',act('opendata','Öffnen'))+row('Preis-Cache leeren',act('clearprices','Leeren'))+'</div>'+
  `<div class="panel"><b>Diagnose</b>${S.diag.map(d=>`<div class="meta">${esc(d)}</div>`).join('')}</div>`;
}
function render(){
 applyPrefs();
 const key=S.launchers.join(',');
 if(key!==navKey){navKey=key;$('nav').innerHTML=['Alle',...S.launchers,'Downloads','Wert','KumpelFunk','Einstellungen'].map(x=>`<button data-t="${x}">${x}</button>`).join('');
  document.querySelectorAll('nav button').forEach(b=>b.onclick=()=>{tab=b.dataset.t;render()})}
 if(!['Alle','Downloads','Wert','KumpelFunk','Einstellungen',...S.launchers].includes(tab))tab='Alle';
 document.querySelectorAll('nav button').forEach(b=>b.classList.toggle('on',b.dataset.t==tab));
 $('#ver').textContent='Beta '+S.version;
 $('#tools').style.display=isLib()?'flex':'none';
 $('#vw').textContent=S.settings.view=='cover'?'Als Liste':'Als Cover';
 const kf=tab=='KumpelFunk';
 $('#kfwrap').classList.toggle('hide',!kf);$('main').style.display=kf?'none':'';
 if(kf){if(!$('#kf').getAttribute('src'))$('#kf').src=KF_URL}
 else $('main').innerHTML=isLib()?libView():tab=='Downloads'?dlView():tab=='Wert'?valView():setView();
 trAll();
}
function saveSteam(){const b={},k=$('#skey').value.trim(),i=$('#sid').value.trim();if(k)b.steamkey=k;if(i)b.steamid=i;
 if(Object.keys(b).length){api('settings',b);toast('Gespeichert, lädt neu …')}else{api('rescan',{});toast('Lädt neu …')}}
function status(){setTimeout(()=>tr($('#status')),0);const d=S.downloads.find(x=>x.state=='running');
 $('#status').textContent=S.upd&&S.upd.state=='downloading'?'Update wird geladen …':d?`Lädt ${d.title} · ${d.pct.toFixed(0)} % · ${d.speed.toFixed(1)} MB/s`:S.busy?'Besitz wird bei den Stores abgefragt …':S.pending?`Preise werden geladen … (${S.pending} offen)`:'Bereit'}

const EN={'Alle':'All','Alle Spiele':'All games','Installiert':'Installed','Nicht installiert':'Not installed','Als Liste':'As list','Als Cover':'As cover','Spielen':'Play','Installieren':'Install','Titel':'Title','Größe':'Size','Keine Treffer.':'No matches.','Wert':'Value','Einstellungen':'Settings','Alles an einem Ort, ohne dass du einen Launcher öffnest.':'Everything in one place, no launcher needed.','Geschwindigkeit':'Speed','Pausieren':'Pause','Fortsetzen':'Resume','Abbrechen':'Cancel','Test-Download starten':'Start test download','Beendete entfernen':'Clear finished','Wartet':'Waiting','Lädt':'Downloading','Pausiert':'Paused','Fertig':'Done','Abgebrochen':'Cancelled','Fehler':'Error','Wert deiner Bibliothek':'Value of your library','Spiel':'Game','Preis':'Price','Eigener Preis (€)':'Your price (€)','Allgemein':'General','Berechtigungen':'Permissions','Konten':'Accounts','Erweitert':'Advanced','Sprache':'Language','Design':'Theme','Dunkel':'Dark','Hell':'Light','Cover-Größe':'Cover size','Klein':'Small','Mittel':'Medium','Groß':'Large','Standardansicht':'Default view','Liste':'List','Start-Reiter':'Start tab','Mit Windows starten':'Start with Windows','Preise automatisch laden':'Load prices automatically','Bereit':'Ready','Aktion nötig':'Action needed','Nicht erkannt':'Not detected','Öffnen':'Open','Mikrofon (für KumpelFunk)':'Microphone (for KumpelFunk)','Kamera':'Camera','Benachrichtigungen':'Notifications','Autostart-Apps':'Startup apps','Windows-Sicherheit und Firewall':'Windows Security and firewall','Anbieter':'Providers','Anmelden':'Sign in','Verbinden':'Connect','Neu einlesen':'Rescan','Erkannte Launcher':'Detected launchers','Steam-Bibliothek':'Steam library','Speichern und neu laden':'Save and reload','Demo-Daten anzeigen':'Show demo data','Diagnose':'Diagnostics','Datenordner öffnen':'Open data folder','Preis-Cache leeren':'Clear price cache','Leeren':'Clear','Spiele suchen':'Search games','Besitz wird bei den Stores abgefragt …':'Checking your stores …','Keine Launcher gefunden.':'No launchers found.','Steam: Spieldetails öffentlich':'Steam: game details public','Epic: Anmeldung über Legendary':'Epic: sign-in via Legendary','Eigener Preis':'Your price','Stores':'Stores','Nicht installiert ':'Not installed'};
const RX=[[/^(\d+) Spiele · (\d+) installiert$/,'$1 games · $2 installed'],[/^(\d+) Spiele$/,'$1 games'],[/^(Wartet|Lädt|Pausiert|Fertig|Abgebrochen|Fehler)( · .*)$/,(m,a,b)=>(EN[a]||a)+b],[/^Bereit$/,'Ready']];
function tr(root){if(!S||S.settings.lang!='en'||!root)return;
 const w=document.createTreeWalker(root,NodeFilter.SHOW_TEXT);let n;
 while(n=w.nextNode()){const t=n.nodeValue.trim();if(!t)continue;let r=EN[t];
  if(!r)for(const [rx,to] of RX)if(rx.test(t)){r=t.replace(rx,to);break}
  if(r)n.nodeValue=n.nodeValue.replace(t,()=>r)}
 root.querySelectorAll('[placeholder]').forEach(e=>{if(EN[e.placeholder])e.placeholder=EN[e.placeholder]})}
const trAll=()=>['header','main','footer','#toast'].forEach(s=>tr($(s)));
function applyPrefs(){document.body.classList.toggle('light',S.settings.theme=='light');
 document.documentElement.style.setProperty('--cw',{small:'130px',large:'220px'}[S.settings.cover]||'170px');document.documentElement.lang=S.settings.lang=='en'?'en':'de'}
addEventListener('pagehide',()=>navigator.sendBeacon('/api/bye'));
const KF_ORIGIN=new URL(KF_URL).origin;
let pop='',lastUpd='',fails=0,popSig='';
const fmt=t=>t?new Date(t*1000).toLocaleString('de-DE',{day:'2-digit',month:'2-digit',hour:'2-digit',minute:'2-digit'}):'';
const KF_SNIPPET=[
"// D-Hub-Brücke für KumpelFunk. Die mit <- markierten Stellen an den eigenen Code anpassen.",
"(function(){",
" var DHUB_ORIGIN='http://127.0.0.1:8766', host=null;",
" addEventListener('message',function(e){",
"  var d=e.data;",
"  if(e.origin!==DHUB_ORIGIN||e.source!==window.parent||!d||d.source!=='dhub')return;",
"  host=e.source;",
"  if(d.type==='hello'){ /* <- hier merken: 'ich nutze D-Hub' und das den Freunden im Status zeigen */ sendProfile(); }",
"  if(d.type==='request-profile')sendProfile();",
"  if(d.type==='send') meineSendeFunktion(d.to,d.text);     // <- anpassen",
"  if(d.type==='open-chat') meinChatOeffnen(d.with);        // <- anpassen",
" });",
" window.dhubSendProfile=sendProfile; // bei jeder Änderung von Freunden oder Status aufrufen",
" function sendProfile(){",
"  if(!host)return;",
"  host.postMessage({source:'kumpelfunk',type:'profile',",
"   name: MEIN_NAME,                                        // <- anpassen",
"   friends: MEINE_FREUNDE.map(function(f){ return {name:f.name,status:f.status,dhub:!!f.nutztDHub,playing:f.spielt||''}; })",
"  },DHUB_ORIGIN);",
" }",
" // Neue Nachricht an D-Hub weitergeben:",
" window.dhubNachricht=function(von,text,id){ if(host)host.postMessage({source:'kumpelfunk',type:'message',from:von,text:text,id:id},DHUB_ORIGIN); };",
"})();"
].join('\n');
function copySnippet(){const done=()=>toast('Code kopiert. Schick ihn an Lerun3.');
 if(navigator.clipboard)navigator.clipboard.writeText(KF_SNIPPET).then(done,()=>{$('#snip').style.display='block';$('#snip').select()});
 else{$('#snip').style.display='block';$('#snip').select()}}
function kfSend(m){try{$('#kf').contentWindow.postMessage(Object.assign({source:'dhub',version:S.version},m),KF_ORIGIN)}catch(e){}}
function initKF(){if(S.settings.kfauto===false||S.demo)return;const f=$('#kf');if(!f.getAttribute('src'))f.src=KF_URL}
$('#kf').addEventListener('load',()=>kfSend({type:'hello'}));
setInterval(()=>{if($('#kf').getAttribute('src'))kfSend({type:'hello'})},45000);
addEventListener('message',e=>{
 if(e.origin!==KF_ORIGIN||e.source!==$('#kf').contentWindow)return;
 const d=e.data;if(!d||d.source!=='kumpelfunk')return;
 if(d.type==='profile')api('action',{name:'kfprofile',profile:{name:d.name,friends:d.friends}}).then(()=>updChrome());
 else if(d.type==='message'&&d.text)api('action',{name:'kfmsg',from:String(d.from||''),text:String(d.text),id:d.id}).then(()=>updChrome());
});
function askUpdate(){
 if(S.downloads.some(d=>['running','queued'].includes(d.state))&&!confirm('Es laufen noch Downloads. Trotzdem aktualisieren?'))return;
 api('action',{name:'applyupdate'}).then(()=>{updChrome();if(tab=='Einstellungen')render()});toast('Update wird geladen …')}
function togglePop(n){pop=pop==n?'':n;popSig='';updChrome(true);
 if(pop=='ml')setTimeout(()=>{if(pop=='ml')api('action',{name:'markread'}).then(()=>updChrome())},1500);
 if(pop=='fr')kfSend({type:'request-profile'})}
function updChrome(force){
 const u=S.upd||{},kf=S.kf||{},av=['available','downloading','restarting'].includes(u.state);
 const ub=$('#updbtn');ub.style.display=av?'':'none';ub.textContent=u.state=='available'?'Aktualisieren auf '+u.latest:'Update läuft …';ub.disabled=u.state!='available';
 const un=S.unread||0,ml=$('#mldot');ml.hidden=!un;ml.textContent=un>9?'9+':un;
 const on=(kf.friends||[]).filter(f=>f.dhub&&f.status!='offline').length,fd=$('#frdot');fd.hidden=!on;fd.textContent=on;
 $('#mlbtn').classList.toggle('on',pop=='ml');$('#frbtn').classList.toggle('on',pop=='fr');
 const ban=$('#ubanner');
 if(u.state=='downloading'||u.state=='restarting'){ban.style.display='flex';ban.innerHTML='<span style="flex:1"><b>Update wird geladen …</b> D-Hub startet gleich neu.</span>'}
 else if(u.apply_at>0){const sec=Math.max(0,Math.ceil(u.apply_at-u.now));ban.style.display='flex';
  ban.innerHTML=`<span style="flex:1"><b>${u.reason=='force'?'Wichtiges Update':'Update'} ${esc(u.latest)}</b> wird in ${sec} s installiert.${u.msg?' '+esc(u.msg):''}</span><button class="btn pri" onclick="askUpdate()">Jetzt</button>${u.reason=='force'?'':`<button class="btn" onclick="api('action',{name:'cancelapply'}).then(()=>updChrome())">Abbrechen</button>`}`}
 else ban.style.display='none';
 renderPops(force)}
function renderPops(force){
 const hb=Math.round(document.querySelector('header').getBoundingClientRect().bottom)+8;
 for(const id of ['#mlpop','#frpop']){$(id).style.top=hb+'px';$(id).style.maxHeight=Math.max(200,innerHeight-hb-48)+'px'}
 $('#mlpop').classList.toggle('on',pop=='ml');$('#frpop').classList.toggle('on',pop=='fr');
 const sig=pop+'|'+JSON.stringify([S.inbox.length,(S.inbox[0]||{}).id,S.unread,(S.kf||{}).ts]);
 if(!pop||(!force&&sig==popSig))return;popSig=sig;
 if(pop=='ml'){$('#mlbody').innerHTML=mlBody();if(force||!$('#mlcomp').contains(document.activeElement))$('#mlcomp').innerHTML=mlComp()}
 else $('#frbody').innerHTML=frBody()}
function mlBody(){return S.inbox.length?S.inbox.map(m=>`<div class="msg ${m.read?'':'new'}"><small>${esc(m.from)} · ${fmt(m.ts)}</small><p>${esc(m.text)}</p></div>`).join(''):'<div class="msg dim">Noch keine Nachrichten.</div>'}
function mlComp(){const k=S.kf||{},fr=k.friends||[];
 if(!k.name)return `<div class="dim" style="width:100%">Nachrichten schreiben geht über KumpelFunk. Sobald sich KumpelFunk mit D-Hub verbindet, kannst du hier direkt schreiben.<br><button class="btn" style="margin-top:8px" onclick="pop='';tab='KumpelFunk';render();updChrome()">KumpelFunk öffnen</button></div>`;
 return `<select id="mto">${fr.map(f=>`<option>${esc(f.name)}</option>`).join('')}</select><input id="mtx" placeholder="Nachricht" onkeydown="if(event.key=='Enter')sendMsg()"><button class="btn pri" onclick="sendMsg()">Senden</button>`}
function sendMsg(){const to=$('#mto').value,t=$('#mtx').value.trim();if(!to||!t)return;
 kfSend({type:'send',to,text:t});api('action',{name:'outmsg',to,text:t}).then(()=>{$('#mtx').value='';updChrome(true)});toast('An KumpelFunk übergeben')}
function openChat(n){kfSend({type:'open-chat',with:n});pop='';tab='KumpelFunk';render();updChrome()}
function frBody(){const k=S.kf||{},fr=k.friends||[];
 if(!k.name)return `<div class="msg"><b>Noch nicht mit KumpelFunk verbunden</b><p class="dim">Dein Name und deine Freunde erscheinen hier, sobald KumpelFunk sie an D-Hub weitergibt.</p><button class="btn" style="margin-top:8px" onclick="pop='';tab='Einstellungen';stab='Konten';render();updChrome()">Mehr dazu</button></div>`;
 const rank={online:0,dnd:1,offline:2},srt=l=>[...l].sort((a,b)=>rank[a.status]-rank[b.status]||a.name.localeCompare(b.name));
 const lbl={online:'Online',dnd:'Nicht stören',offline:'Offline'};
 const row=f=>`<div class="fr" onclick="openChat('${esc(f.name)}')"><div class="av" style="--h:${hue(f.name)}">${esc(f.name[0].toUpperCase())}<i class="${f.status}"></i></div><div style="flex:1;min-width:0"><b>${esc(f.name)}</b><div class="meta">${f.playing?'Spielt '+esc(f.playing):lbl[f.status]}</div></div></div>`;
 const grp=(t,l)=>l.length?`<div class="sect">${t} (${l.length})</div>`+srt(l).map(row).join(''):'';
 const d=fr.filter(f=>f.dhub),o=fr.filter(f=>!f.dhub);
 return `<div class="fr" style="cursor:default"><div class="av" style="--h:${hue(k.name)}">${esc(k.name[0].toUpperCase())}<i class="online"></i></div><div><b>${esc(k.name)}</b><div class="meta">Du · abgeglichen ${fmt(k.ts)}</div></div></div>`+
  grp('Nutzen D-Hub',d)+grp('Weitere KumpelFunk-Freunde',o)+(fr.length?'':'<div class="msg dim">Noch keine Freunde in KumpelFunk.</div>')}
function kfPanel(){const k=S.kf||{};
 return `<div class="panel"><b>KumpelFunk</b><p class="dim">${k.name?'Verbunden als <b>'+esc(k.name)+'</b> · '+(k.friends||[]).length+' Freunde · abgeglichen '+fmt(k.ts):'Noch keine Verbindung. D-Hub kann Namen und Freunde nur übernehmen, wenn KumpelFunk sie herausgibt.'}</p>
 <div class="row"><span style="flex:1">Code für den Entwickler von KumpelFunk</span><button class="btn" onclick="copySnippet()">Kopieren</button></div>
 <textarea class="code" id="snip" readonly style="display:none" onclick="this.select()">${esc(KF_SNIPPET)}</textarea></div>`}
function updSig(){const u=S.upd||{};return u.state+'|'+u.err+'|'+u.latest}
function updView(){const u=S.upd||{};lastUpd=updSig();const busy=['checking','downloading','restarting'].includes(u.state);
 return `<div class="panel"><b>Aktualisieren</b>
 <p class="dim">Installiert: ${S.version}${u.latest?' · Neueste: '+esc(u.latest):''}${u.checked?' · geprüft '+fmt(u.checked):''}</p>
 ${u.err?`<p style="color:var(--warn)">${esc(u.err)}</p>`:''}
 ${u.msg?`<p class="dim">Nachricht vom Herausgeber: ${esc(u.msg)}</p>`:''}
 ${u.state=='available'?`<p>Version <b>${esc(u.latest)}</b> ist bereit.</p>${u.notes?`<pre class="notes">${esc(u.notes)}</pre>`:''}`:u.state=='idle'&&u.latest&&!u.err?'<p>D-Hub ist auf dem neuesten Stand.</p>':''}
 ${u.state=='downloading'?'<p>Update wird geladen …</p>':u.state=='restarting'?'<p>D-Hub startet neu …</p>':''}
 <div class="row"><span style="flex:1">GitHub-Repository (Besitzer/Name)</span><input id="urepo" style="width:210px" placeholder="${esc(u.repo||'nutzer/D-Hub')}" value="${esc(S.settings.updrepo||'')}" onchange="api('settings',{updrepo:this.value.trim()});toast('Prüfe …')"></div>
 <div class="row" style="border:0"><button class="btn" ${busy?'disabled':''} onclick="api('action',{name:'checkupdate'});toast('Prüfe auf Updates …');setTimeout(()=>{if(tab=='Einstellungen')render()},2500)">Nach Updates suchen</button>
 <button class="btn pri" ${u.state=='available'?'':'disabled'} onclick="askUpdate()">Jetzt aktualisieren</button></div>
 <p class="dim">${u.frozen?'D-Hub ersetzt sich selbst und startet neu.':'D-Hub ersetzt dhub.py (Sicherung: dhub.py.bak) und startet neu.'} Updates kommen nur aus dem eingetragenen Repository. Wichtige Updates kann der Herausgeber verpflichtend machen.</p></div>`}
(function(){const sp=$('#splash');let skip=false;
 try{skip=sessionStorage.getItem('dhs')||localStorage.getItem('dhub_nosplash')}catch(e){}
 try{sessionStorage.setItem('dhs','1')}catch(e){}
 if(skip){sp.remove();return}
 const end=()=>sp.remove(),quick=()=>{sp.style.animation='spOut .25s forwards';setTimeout(end,300)};
 setTimeout(end,2900);sp.onclick=quick;addEventListener('keydown',quick,{once:true})})();
$('#updbtn').onclick=askUpdate;$('#frbtn').onclick=()=>togglePop('fr');$('#mlbtn').onclick=()=>togglePop('ml');
document.addEventListener('click',e=>{if(pop&&!e.target.closest('.pop,#frbtn,#mlbtn')){pop='';updChrome()}});
document.addEventListener('keydown',e=>{if(e.key=='Escape'&&pop){pop='';updChrome()}});
$('#flt').onchange=e=>{flt=e.target.value;render()};
$('#q').oninput=e=>{q=e.target.value.toLowerCase();render()};
$('#vw').onclick=()=>api('settings',{view:S.settings.view=='cover'?'list':'cover'}).then(render);
api('state').then(()=>{const st=S.settings.starttab;if(st&&['Alle',...S.launchers,'Downloads','Wert','KumpelFunk'].includes(st))tab=st;render();initKF();updChrome(true)});
setInterval(async()=>{const a=document.activeElement,typing=a&&a.tagName=='INPUT',old=S.pending,ob=S.busy;try{await api('state');fails=0}catch(e){if(++fails>=3)$('#lost').style.display='flex';return}status();updChrome();
 if(!typing&&((ob&&!S.busy)||(tab=='Einstellungen'&&stab=='Updates'&&updSig()!=lastUpd)||tab=='Downloads'||(tab=='Wert'&&(S.pending||old))))render()},1000);
</script></body></html>
'''


# ---------- Server ----------
class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send(self, body, ctype="application/json"):
        b = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(200)
        self.send_header("Content-Type", ctype if ctype.startswith("image") else ctype + "; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        LAST[0] = time.time()
        if self.path.split("?")[0] == "/logo.png":
            for d in (HERE, getattr(sys, "_MEIPASS", HERE)):
                f = os.path.join(d, "logo.png")
                if os.path.exists(f):
                    self.send(open(f, "rb").read(), "image/png")
                    return
            self.send_error(404)
            return
        if self.path == "/api/state":
            self.send(state())
        else:
            self.send(PAGE.encode("utf-8"), "text/html")

    def do_POST(self):
        try:
            b = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or "{}")
        except Exception:
            b = {}
        LAST[0] = time.time()
        p = self.path
        if p == "/api/bye":
            if time.time() - START > 15:  # direkt nach einem Neustart ignorieren (altes Fenster schliesst sich)
                BYE[0] = time.time()
            self.send({})
            return
        if p == "/api/quit":
            self.send({})
            threading.Thread(target=lambda: (time.sleep(0.3), os._exit(0))).start()
            return
        if p == "/api/launch":
            launch(b["id"], b["store"], b.get("install", False))
        elif p == "/api/price":
            store["prices"][b["id"]] = b["price"]
            save()
        elif p == "/api/settings":
            store["settings"].update(b)
            save()
            if "demo" in b:
                scan()
                kick()
            if "steamkey" in b or "steamid" in b:
                kick()
            if "updrepo" in b:
                threading.Thread(target=check_update, args=(True,), daemon=True).start()
        elif p == "/api/rescan":
            scan()
            kick()
        elif p == "/api/action":
            do_action(b)
        elif p == "/api/connect":
            connect(b["store"])
        elif p == "/api/dl":
            a = b["action"]
            if a == "add":
                downloads.append(dict(id=str(time.time()), title=b["title"], store=b["store"], sid=b["sid"],
                                      pct=0.0, speed=0.0, state="queued"))
            elif a == "clear":
                downloads[:] = [d for d in downloads if d["state"] in ("queued", "running", "paused")]
            else:
                for d in downloads:
                    if d["id"] == b["id"]:
                        d["state"] = dict(pause="paused", resume="queued", cancel="cancelled").get(a, d["state"])
        self.send(state())


if __name__ == "__main__":
    url = "http://127.0.0.1:%d" % PORT
    try:  # laeuft D-Hub schon? Dann nur das Fenster oeffnen, bei alter Version die alte beenden
        ex = fetch_json(url + "/api/state")
    except Exception:
        ex = None
    if ex:
        if ex.get("version") == VERSION:
            open_window(url)
            sys.exit(0)
        try:
            urllib.request.urlopen(urllib.request.Request(url + "/api/quit", data=b"{}"), timeout=3)
        except Exception:
            pass
        time.sleep(1.5)
    srv = None
    for _ in range(10):
        try:
            srv = ThreadingHTTPServer(("127.0.0.1", PORT), H)
            break
        except OSError:
            time.sleep(0.5)
    if srv is None:
        if IS_WIN:
            import ctypes
            ctypes.windll.user32.MessageBoxW(0, "D-Hub konnte nicht starten, weil der Port belegt ist. Bitte D-Hub im Task-Manager beenden.", "D-Hub", 0x10)
        sys.exit(1)
    scan()
    for fn in (worker, price_worker, watcher, update_loop, apply_loop):
        threading.Thread(target=fn, daemon=True).start()
    kick()
    open_window(url)
    srv.serve_forever()
