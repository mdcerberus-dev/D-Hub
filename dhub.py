"""D-Hub Beta 2.4 - ein Hub fuer Steam, Epic, GOG und mehr. Nur Python 3 noetig, keine Pakete."""
import random, os, sys, json, re, glob, time, threading, subprocess, shutil, webbrowser, base64, mimetypes
import urllib.request, urllib.parse, sqlite3, tempfile, sysconfig, html
import xml.etree.ElementTree as ET
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler

HERE = os.path.dirname(sys.executable) if getattr(sys, "frozen", False) else os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "hub_data.json")
PORT = 8766
VERSION = "2.8"
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


LOGF = os.path.join(HERE, "dhub.log")


def log(msg):
    try:
        if os.path.exists(LOGF) and os.path.getsize(LOGF) > 200000:
            old = open(LOGF, encoding="utf-8", errors="replace").read()[-60000:]
            open(LOGF, "w", encoding="utf-8").write(old)
        open(LOGF, "a", encoding="utf-8").write("%s  %s\n" % (time.strftime("%d.%m. %H:%M:%S"), msg))
    except Exception:
        pass


def debug_info():
    try:
        tail = "".join(open(LOGF, encoding="utf-8", errors="replace").readlines()[-40:])
    except Exception:
        tail = "(noch kein Protokoll)"
    return "D-Hub %s\nFenster: %s\nProgramm: %s\nOrdner: %s\nPort: %s\nSpiele: %d\nDiagnose: %s\nOverlay-Fehler: %s\nOverlay-Ziel: %s\n\nProtokoll (dhub.log):\n%s" % (
        VERSION, WINMODE[0], sys.executable, HERE, PORT, len(LIB["games"]), " | ".join(LIB["diag"]), OV["err"] or "-", OV["target"], tail)


def norm(t):
    return re.sub(r"[^a-z0-9]", "", str(t or "").lower())


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
                                installed=True, launch="steam://rungameid/" + appid,
                                dir=os.path.join(lib, "steamapps", "common", g("installdir"))))
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
                            launch="uplay://launch/%s/0" % sub, dir=d))
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
                            installed=True, launch=uri, dir=m.get("InstallLocation", "")))
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
                            launch="exe:" + v("exe") + "|" + v("path"), dir=v("path")))
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
    return dict(store=st, sid=str(sid), title=str(title or "").strip(), size=0, installed=False, launch="")


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
        # Nur Spiele, die wirklich dir gehoeren. Galaxy kennt in GamePieces auch Store-Katalog, Wunschliste usw.
        owned, src = set(), []
        for q, nm in (("SELECT releaseKey FROM OwnedGames", "OwnedGames"),
                      ("SELECT gameReleaseKey FROM ProductPurchaseDates", "Kaeufe"),
                      ("SELECT releaseKey FROM UserReleaseProperties WHERE isOwned = 1", "Eigene")):
            try:
                got = {r[0] for r in con.execute(q) if r[0]}
            except sqlite3.Error:
                continue
            if got:
                owned |= got
                src.append("%s %d" % (nm, len(got)))
        if not owned:
            raise RuntimeError("GOG: In Galaxy ist nichts als Besitz gespeichert. Melde dich in Galaxy an, warte bis die Bibliothek geladen ist, und lies dann neu ein.")
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
        if pre in names and key in owned and str(t or "").strip():
            out.append(owned_entry(names[pre], sid, t))
    LIB["gal_info"] = "Galaxy: %d Eintraege in der Datenbank, %d davon im Besitz (%s)" % (len(titles), len(out), ", ".join(src))
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
                owned[name] = [g for g in fn() if str(g.get("title") or "").strip()]
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


WINMODE = ["noch nicht gestartet"]


def run_window(url):
    """Eigenes Programmfenster (WebView2, ohne Edge-Optik). Blockiert bis das Fenster zu ist.
    Gibt False zurueck, wenn das nicht moeglich ist, dann greift der Edge-App-Modus."""
    if not IS_WIN:
        WINMODE[0] = "Edge/Browser (kein Windows)"
        return False
    try:
        import webview
        WINMODE[0] = "eigenes Programmfenster (WebView2)"
        win = webview.create_window("D-Hub", url, width=1320, height=840, min_size=(960, 620), background_color="#050805")
        WIN[0] = win

        data = os.path.join(HERE, "webview-data")
        def watch():
            """Bleibt das Fenster schwarz/leer, wird neu geladen. Hilft das nicht, kommt eine Fehlerseite mit Knoepfen."""
            for attempt in range(2):
                time.sleep(9)
                try:
                    ok = win.evaluate_js("!!window.__ok")
                except Exception:
                    ok = False
                if ok:
                    return
                log("Fenster blieb leer, lade neu (Versuch %d)" % (attempt + 1))
                try:
                    win.load_url(url + "?r=%d" % int(time.time()))
                except Exception as e:
                    log("load_url: %s" % e)
            try:
                win.load_html('<body style="background:#050805;color:#ddd;font:15px system-ui;padding:40px"><h2>D-Hub startet nicht richtig</h2>'
                              '<p><a style="color:#5cff0c" href="%s">Neu laden</a></p><pre style="white-space:pre-wrap;color:#9a9">%s</pre></body>'
                              % (url, debug_info().replace("&", "&amp;").replace("<", "&lt;")))
            except Exception:
                pass
        try:
            webview.start(watch, gui="edgechromium", private_mode=False, storage_path=data)
        except TypeError:
            webview.start(watch, gui="edgechromium")
        return True
    except Exception as e:
        WINMODE[0] = "Edge-Fallback, weil: %s: %s" % (type(e).__name__, e)
        log(WINMODE[0])
        return False


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
        todo = [g for g in LIB["all"] if g["id"] not in store["listprices"] or
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


LIB = dict(games=[], all=[], demo=False, launchers=[], perr="", diag=[], errs={}, busy=False)


GAME_SKIP = re.compile(r"unins|setup|crash|redist|dxsetup|helper|updater|dotnet|install|cef|prereq|anticheat|easyanti|battleye|notif|report|vc_", re.I)
SKIP_DIRS = re.compile(r"redist|directx|support|__installer|crashreport|_commonredist|prerequisites", re.I)
COVERS = os.path.join(HERE, "covers")


def pretty_title(n):
    n = re.sub(r"[\s_-]*[\[(]?v?\d+(\.\d+){1,3}[\])]?$", "", n.strip())
    n = re.sub(r"[_.]+", " ", n).strip()
    return n or "Spiel"


def pick_exe(folder):
    """Groesste sinnvolle .exe im Ordner (bis 3 Ebenen tief) gilt als das Spiel."""
    best, size, base = "", 500000, folder.rstrip("\\/").count(os.sep)
    try:
        for root, dirs, files in os.walk(folder):
            if root.count(os.sep) - base >= 3:
                dirs[:] = []
            dirs[:] = [d for d in dirs if not SKIP_DIRS.search(d)]
            for f in files:
                if f.lower().endswith(".exe") and not GAME_SKIP.search(f):
                    try:
                        sz = os.path.getsize(os.path.join(root, f))
                    except OSError:
                        continue
                    if sz > size:
                        best, size = os.path.join(root, f), sz
    except OSError:
        pass
    return best


def game_dirs():
    out = [x.strip() for x in re.split(r"[\r\n;]+", str(store["settings"].get("gamedirs") or "")) if x.strip()]
    if IS_WIN:
        for dl in "CDEFGH":
            for n in ("Games", "Spiele", "Game"):
                out.append("%s:\\%s" % (dl, n))
        out.append(os.path.join(os.path.expanduser("~"), "Games"))
    seen, res = set(), []
    for d in out:
        if d.lower() not in seen and os.path.isdir(d):
            seen.add(d.lower())
            res.append(d)
    return res


def folder_games():
    out = []
    for root in game_dirs():
        try:
            subs = sorted(os.listdir(root))
        except OSError:
            continue
        for sub in subs:
            full = os.path.join(root, sub)
            if sub.startswith(".") or not os.path.isdir(full):
                continue
            exe = pick_exe(full)
            if exe:
                out.append(dict(store="PC", sid=full, title=pretty_title(sub), size=0, installed=True,
                                launch="exe:%s|%s" % (exe, os.path.dirname(exe)), dir=full))
    return out


def registry_games():
    """Programme aus der Windows-Programmliste, die in einem Ordner namens Games/Spiele liegen."""
    out = []
    if not IS_WIN:
        return out
    for hive, path in ((winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
                       (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"),
                       (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall")):
        try:
            k = winreg.OpenKey(hive, path)
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
                sk = winreg.OpenKey(k, sub)
                name = str(winreg.QueryValueEx(sk, "DisplayName")[0] or "")
                loc = str(winreg.QueryValueEx(sk, "InstallLocation")[0] or "")
            except OSError:
                continue
            low = loc.lower().replace("/", "\\") + "\\"
            if name and loc and re.search(r"\\(games|spiele)\\", low) and "\\steamapps\\" not in low and os.path.isdir(loc):
                exe = pick_exe(loc)
                if exe:
                    out.append(dict(store="PC", sid=sub, title=name, size=0, installed=True,
                                    launch="exe:%s|%s" % (exe, os.path.dirname(exe)), dir=loc))
    return out


def pc_games():
    res, seen = [], set()
    for g in registry_games() + folder_games():
        key = os.path.normcase(g["dir"]).rstrip("\\/")
        if key not in seen:
            seen.add(key)
            res.append(g)
    return res


def custom_games():
    out = []
    for c in store.get("custom") or []:
        out.append(dict(store="Eigene", sid=c["id"], title=c["title"], size=0, installed=True,
                        launch="exe:%s|%s" % (c["exe"], os.path.dirname(c["exe"])), dir=os.path.dirname(c["exe"])))
    return out


def pick_file(kind):
    """Datei-Auswahlfenster. Im eigenen D-Hub-Fenster, sonst ueber Windows-Standarddialog."""
    ft = [("Programme", "*.exe"), ("Alle Dateien", "*.*")]
    try:
        if WIN[0]:
            import webview
            dlg = getattr(getattr(webview, "FileDialog", None), "OPEN", None) or webview.OPEN_DIALOG
            r = WIN[0].create_file_dialog(dlg, file_types=("Programme (*.exe)", "Alle Dateien (*.*)"))
            return r[0] if r else ""
    except Exception as e:
        log("Dateiauswahl (Fenster): %s" % e)
    try:
        import tkinter
        from tkinter import filedialog
        root = tkinter.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        path = filedialog.askopenfilename(filetypes=ft)
        root.destroy()
        return path or ""
    except Exception as e:
        log("Dateiauswahl (tk): %s" % e)
        return ""


def diagnose(real):
    return ["System: " + ("Windows" if IS_WIN else sys.platform + " (Launcher-Erkennung funktioniert nur unter Windows)"),
            "Steam-Ordner: " + (steam_root() or "nicht gefunden"),
            "Gefundene Spiele: Steam %d, Epic %d, GOG %d, Ubisoft %d, PC-Ordner %d, Eigene %d" % tuple(sum(g["store"] == n and g.get("installed") for g in real) for n in ("Steam", "Epic", "GOG", "Ubisoft", "PC", "Eigene")),
            "Durchsuchte Spielordner: " + (", ".join(game_dirs()) or "keine"),
            "Legendary: " + (find_legendary() or "nicht gefunden")]


def scan():
    ok_title = lambda g: bool(str((g or {}).get("title") or "").strip()) and norm(g["title"]) != ""
    inst = [g for g in steam_games() + epic_games() + gog_games() + ubisoft_games() + pc_games() + custom_games() if ok_title(g)]
    real = list(inst)
    have = {(g["store"], g["sid"]) for g in real}
    for lst in store.get("owned", {}).values():
        for g in lst:
            if ok_title(g) and (g["store"], g["sid"]) not in have:
                have.add((g["store"], g["sid"]))
                real.append(g)
    demo = bool(store["settings"].get("demo"))
    raw = DEMO if demo else real
    merged = {}
    for g in raw:
        e = merged.setdefault(norm(g["title"]), dict(id=norm(g["title"]), title=g["title"], versions=[]))
        e["versions"].append(g)
    for e in merged.values():  # gleiches Spiel schon ueber einen Launcher gefunden: den Ordner-Fund nicht doppelt zeigen
        if any(v["store"] not in ("PC", "Eigene") and v.get("installed") for v in e["versions"]):
            e["versions"] = [v for v in e["versions"] if v["store"] != "PC"]
    LIB["all"] = list(merged.values())
    LIB["games"], LIB["demo"] = [e for e in LIB["all"] if any(v.get("installed") for v in e["versions"])], demo
    LIB["launchers"] = ["Steam", "Epic", "GOG"] if demo else detect_launchers({g["store"] for g in inst})
    LIB["diag"] = diagnose(real)
    try:
        coin_check()
    except Exception as e:
        log("coin_check: %s" % e)


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


# ---------- Ingame-Overlay (Fadenkreuz, FPS, KumpelFunk) ----------
OV = dict(on=False, visible=True, cross=None, showfps=False, showkf=False, fps=0.0, fpsmsg="", members=[], bubbles=[],
          hwnd=0, err="", hidden=False, fpsproc=None, flags={}, stats={}, gpid=0, ghwnd=0, rect=None, target_vis=False,
          target="wartet auf Spiel …", armed=0.0, hint_dirs=[], hint_title="", dllgid=None, hidewin=False)
WIN = [None]
GCFG_DEF = dict(cross=False, cstyle="cross", ccolor="#5cff0c", csize=12, cgap=4, cthick=2, fps=False, kf=False,
                ping=False, cpu=False, gpu=False, ram=False, clock=False, spos="tr", mp=True)


def gamecfg(gid):
    c = dict(GCFG_DEF)
    c.update((store.get("gamecfg") or {}).get(gid, {}))
    return c


def clean_cfg(c):
    c = c if isinstance(c, dict) else {}
    out = dict(GCFG_DEF)
    for k in ("cross", "fps", "kf", "ping", "cpu", "gpu", "ram", "clock"):
        out[k] = bool(c.get(k))
    out["mp"] = bool(c.get("mp", True))  # unbekannte Spiele gelten vorsichtshalber als Mehrspieler
    out["spos"] = c.get("spos") if c.get("spos") in ("tl", "tr", "bl", "br") else "tr"
    out["cstyle"] = c.get("cstyle") if c.get("cstyle") in ("cross", "plus", "dot", "circle") else "cross"
    col = str(c.get("ccolor") or "")
    out["ccolor"] = col if re.fullmatch(r"#[0-9a-fA-F]{6}", col) else GCFG_DEF["ccolor"]
    for k, lo, hi in (("csize", 2, 60), ("cgap", 0, 30), ("cthick", 1, 8)):
        try:
            out[k] = max(lo, min(hi, int(c.get(k, GCFG_DEF[k]))))
        except (TypeError, ValueError):
            out[k] = GCFG_DEF[k]
    return out


# ---------- Coins, Shop und Gamble (nur Spiel-Coins, niemals Echtgeld) ----------
RNG = random.SystemRandom()
PLAY_CAP, LOSS_LIMIT, MAX_BET = 60, 1000, 500
CATALOG = [
    dict(id="t_neon", cat="theme", name="Neon-Grün", price=0, desc="Das Original.", data=["#5cff0c", "92,255,12"]),
    dict(id="t_ocean", cat="theme", name="Ozean", price=150, desc="Kühles Cyan-Blau.", data=["#2ec5ff", "46,197,255"]),
    dict(id="t_sunset", cat="theme", name="Sonnenuntergang", price=150, desc="Warmes Orange.", data=["#ff8a3d", "255,138,61"]),
    dict(id="t_violet", cat="theme", name="Violett", price=200, desc="Tiefes Lila.", data=["#b46bff", "180,107,255"]),
    dict(id="t_crimson", cat="theme", name="Karmesin", price=200, desc="Kräftiges Rot.", data=["#ff4d6d", "255,77,109"]),
    dict(id="t_ice", cat="theme", name="Eis", price=250, desc="Helles Eisblau.", data=["#9fe9ff", "159,233,255"]),
    dict(id="t_gold", cat="theme", name="Gold", price=300, desc="Für Leute mit Stil.", data=["#ffcb3d", "255,203,61"]),
    dict(id="h_lift", cat="hover", name="Anheben", price=0, desc="Standard: Karte hebt sich an."),
    dict(id="h_tilt", cat="hover", name="Kippen", price=120, desc="Karte neigt sich in 3D."),
    dict(id="h_pop", cat="hover", name="Hüpfen", price=150, desc="Karte federt nach oben."),
    dict(id="h_glow", cat="hover", name="Pulsieren", price=180, desc="Leuchtrahmen pulsiert."),
    dict(id="h_shine", cat="hover", name="Glanz", price=220, desc="Ein Lichtstreifen wandert über das Cover."),
    dict(id="ti_none", cat="title", name="Kein Titel", price=0, desc=""),
    dict(id="ti_collector", cat="title", name="Sammler", price=100, desc="Zeigt neben deinem Kontostand."),
    dict(id="ti_night", cat="title", name="Nachteule", price=100, desc="Zeigt neben deinem Kontostand."),
    dict(id="ti_pro", cat="title", name="Profi", price=250, desc="Zeigt neben deinem Kontostand."),
    dict(id="ti_legend", cat="title", name="Legende", price=800, desc="Zeigt neben deinem Kontostand."),
]
CAT_BY_ID = {i["id"]: i for i in CATALOG}
FREE = {"theme": "t_neon", "hover": "h_lift", "title": "ti_none"}
MILESTONES = [
    ("m_start", "Willkommen bei D-Hub", 20, lambda c: True),
    ("m_g10", "10 Spiele in der Bibliothek", 50, lambda c: len(LIB["games"]) >= 10),
    ("m_g25", "25 Spiele in der Bibliothek", 100, lambda c: len(LIB["games"]) >= 25),
    ("m_g50", "50 Spiele in der Bibliothek", 200, lambda c: len(LIB["games"]) >= 50),
    ("m_custom", "Erstes eigenes Spiel hinzugefügt", 30, lambda c: bool(store.get("custom"))),
    ("m_cover", "Erstes eigenes Cover gesetzt", 20, lambda c: bool(store.get("covers"))),
    ("m_overlay", "Overlay benutzt", 30, lambda c: bool(store.get("lastov"))),
    ("m_s7", "7 Tage Serie", 100, lambda c: c.get("streak", 0) >= 7),
    ("m_s30", "30 Tage Serie", 500, lambda c: c.get("streak", 0) >= 30),
    ("m_p10", "10 Stunden gespielt", 100, lambda c: sum((store.get("playtime") or {}).values()) >= 36000),
    ("m_p50", "50 Stunden gespielt", 300, lambda c: sum((store.get("playtime") or {}).values()) >= 180000),
]
WHEEL = [0, 0, 0, 0, 0.5, 0.5, 1, 1, 1, 1.5, 2, 4]  # durchschnittlich 95,8 % Rueckzahlung
SLOT_SYMS = ["🍒", "🍋", "🔔", "⭐", "💎", "7️⃣"]
SLOT_W = [40, 30, 15, 10, 4, 1]
SLOT_PAY = [5, 8, 15, 30, 100, 250]  # drei gleiche; zwei Kirschen zahlen 1,1x (zusammen ca. 94 %)


ARC_CAP = 150  # Arcade: höchstens so viele Coins pro Tag


def cs():
    c = store.setdefault("coins", {})
    for k, v in (("balance", 0), ("streak", 0), ("last_daily", ""), ("owned", list(FREE.values())), ("active", dict(FREE)), ("done", []),
                 ("day", ""), ("play_today", 0), ("play_acc", 0), ("net", 0), ("rounds", 0), ("arc_today", 0), ("arc_best", {}), ("inv_best", {}), ("arc_start", 0), ("feed", []), ("fid", 0), ("total", 0)):
        c.setdefault(k, v)
    return c


def coin_day(c):
    d = time.strftime("%Y-%m-%d")
    if c["day"] != d:
        c.update(day=d, play_today=0, net=0, rounds=0, arc_today=0)
    return d


def coin_feed(c, msg):
    c["fid"] += 1
    c["feed"] = (c["feed"] + [dict(id=c["fid"], msg=msg)])[-12:]


def coin_add(c, n, why):
    c["balance"] += n
    if n > 0:
        c["total"] += n
    if why:
        coin_feed(c, "%+d Coins: %s" % (n, why))


def coin_check():
    """Einmalige Meilenstein-Belohnungen."""
    c = cs()
    changed = False
    for mid, label, reward, test in MILESTONES:
        if mid not in c["done"]:
            try:
                ok = test(c)
            except Exception:
                ok = False
            if ok:
                c["done"].append(mid)
                coin_add(c, reward, "Meilenstein: " + label)
                changed = True
    if changed:
        save()


def daily_amount(c):
    d = time.strftime("%Y-%m-%d")
    y = time.strftime("%Y-%m-%d", time.localtime(time.time() - 86400))
    streak = c["streak"] + 1 if c["last_daily"] == y else (c["streak"] if c["last_daily"] == d else 1)
    return 10 + 5 * min(streak - 1, 6), streak


def coin_public():
    c = cs()
    coin_day(c)
    amt, streak = daily_amount(c)
    ready = c["last_daily"] != c["day"]
    return dict(balance=c["balance"], streak=c["streak"], daily_ready=ready, daily_amount=amt, next_streak=streak, owned=c["owned"], active=c["active"],
                play_today=c["play_today"], play_cap=PLAY_CAP, net=c["net"], rounds=c["rounds"], feed=c["feed"], catalog=CATALOG, max_bet=MAX_BET,
                loss_limit=LOSS_LIMIT, arc_cap=ARC_CAP, arc_today=c["arc_today"], arc_best=c["arc_best"], inv_best=c["inv_best"], limit_hit=c["net"] <= -LOSS_LIMIT, total=c["total"],
                milestones=[dict(id=m[0], label=m[1], reward=m[2], done=m[0] in c["done"]) for m in MILESTONES])


def coin_op(op, b):
    c = cs()
    coin_day(c)
    msg, ok, res = "", True, None
    if op == "daily":
        if c["last_daily"] == c["day"]:
            ok, msg = False, "Heute schon abgeholt."
        else:
            amt, streak = daily_amount(c)
            c["streak"], c["last_daily"] = streak, c["day"]
            coin_add(c, amt, "Täglicher Bonus (Tag %d in Serie)" % streak)
            msg = "+%d Coins" % amt
    elif op == "buy":
        it = CAT_BY_ID.get(str(b.get("id")))
        if not it:
            ok, msg = False, "Unbekannt."
        elif it["id"] in c["owned"]:
            ok, msg = False, "Schon freigeschaltet."
        elif c["balance"] < it["price"]:
            ok, msg = False, "Nicht genug Coins."
        else:
            c["balance"] -= it["price"]
            c["owned"].append(it["id"])
            c["active"][it["cat"]] = it["id"]
            coin_feed(c, "Freigeschaltet: " + it["name"])
            msg = "Freigeschaltet: " + it["name"]
    elif op == "equip":
        it = CAT_BY_ID.get(str(b.get("id")))
        if it and it["id"] in c["owned"]:
            c["active"][it["cat"]] = it["id"]
        else:
            ok, msg = False, "Noch nicht freigeschaltet."
    elif op == "arcstart":
        c["arc_start"] = time.time()
    elif op == "arcade":
        ok, msg = arcade_reward(c, b)
    elif op == "gamble":
        ok, msg, res = gamble(c, str(b.get("game")), b.get("bet"), b)
    save()
    coin_check()
    return dict(ok=ok, msg=msg, result=res, coins=coin_public())


def arcade_reward(c, b):
    """Belohnung fürs Arcade-Spiel. Nur Spiel-Coins; Plausibilitätsprüfung und Tageslimit."""
    try:
        done, kills, win = int(b.get("completed", 0)), int(b.get("kills", 0)), bool(b.get("win"))
    except (TypeError, ValueError):
        return False, "Ungültige Daten."
    inv = b.get("mode") == "inv"
    st = c.get("arc_start") or 0
    el = time.time() - st
    c["arc_start"] = 0
    if not st or not (0 <= done <= (30 if inv else 20)) or kills < 0 or kills > (40 if inv else 400) * max(1, done + 1) or el < done * (10 if inv else 14):
        return False, "Runde nicht gewertet."
    key = "inv_best" if inv else "arc_best"
    best = c[key]
    if done > best.get("wave", 0) or (done == best.get("wave", 0) and kills > best.get("kills", 0)):
        c[key] = dict(wave=done, kills=kills)
    amt = (done * 3 + min(40, kills // 10)) if inv else (done * 2 + min(60, kills // 15) + (30 if win else 0))
    amt = max(0, min(amt, ARC_CAP - c["arc_today"]))
    c["arc_today"] += amt
    if amt:
        coin_add(c, amt, "Invaders" if inv else "Arcade")
        return True, "+%d Coins (Arcade, heute %d/%d)" % (amt, c["arc_today"], ARC_CAP)
    return True, "Heute ist das Arcade-Limit von %d Coins erreicht." % ARC_CAP


def gamble(c, game, bet, p):
    try:
        bet = int(bet)
    except (TypeError, ValueError):
        return False, "Einsatz ungültig.", None
    if c["net"] <= -LOSS_LIMIT:
        return False, "Für heute ist Schluss: Das Verlustlimit von %d Coins ist erreicht. Morgen geht es weiter." % LOSS_LIMIT, None
    if bet < 1 or bet > MAX_BET:
        return False, "Einsatz zwischen 1 und %d Coins." % MAX_BET, None
    if bet > c["balance"]:
        return False, "Nicht genug Coins.", None
    pay, det = 0, {}
    if game == "flip":
        pick = "kopf" if p.get("pick") == "kopf" else "zahl"
        out = RNG.choice(["kopf", "zahl"])
        pay = int(bet * 1.9) if out == pick else 0
        det = dict(out=out, pick=pick)
    elif game == "wheel":
        i = RNG.randrange(len(WHEEL))
        pay = int(bet * WHEEL[i])
        det = dict(index=i, mult=WHEEL[i])
    elif game == "slots":
        r = RNG.choices(range(len(SLOT_SYMS)), weights=SLOT_W, k=3)
        if r[0] == r[1] == r[2]:
            pay = bet * SLOT_PAY[r[0]]
        elif r.count(0) == 2:
            pay = int(bet * 1.1)
        det = dict(reels=r, syms=[SLOT_SYMS[i] for i in r])
    elif game == "dice":
        mode = "over" if p.get("mode") == "over" else "under"
        try:
            t = max(5, min(95, int(p.get("target"))))
        except (TypeError, ValueError):
            t = 50
        chance = t if mode == "under" else 100 - t
        roll = RNG.randint(1, 100)
        win = roll <= t if mode == "under" else roll > t
        pay = int(bet * 95 / chance) if win else 0
        det = dict(roll=roll, mode=mode, target=t, chance=chance)
    else:
        return False, "Unbekanntes Spiel.", None
    c["balance"] += pay - bet
    c["net"] += pay - bet
    c["rounds"] += 1
    if pay > bet * 9:
        coin_feed(c, "Großer Gewinn im Gamble: +%d Coins" % (pay - bet))
    return True, "", dict(game=game, bet=bet, payout=pay, net=pay - bet, detail=det)


def running_game_ids():
    if not IS_WIN:
        return []
    import ctypes
    from ctypes import wintypes as W
    arr, need = (W.DWORD * 2048)(), W.DWORD(0)
    if not ctypes.windll.psapi.EnumProcesses(arr, ctypes.sizeof(arr), ctypes.byref(need)):
        return []
    dirs = [(g["id"], v["dir"].lower().rstrip("\\/") + "\\") for g in LIB["games"] for v in g["versions"] if v.get("installed") and v.get("dir")]
    found = set()
    for pid in arr[:need.value // 4]:
        if pid > 4:
            path = proc_path(pid).lower()
            if path:
                for gid, d in dirs:
                    if path.startswith(d):
                        found.add(gid)
                        break
    return list(found)


def play_loop():
    """Zaehlt Spielzeit der in D-Hub bekannten Spiele und zahlt dafuer langsam Coins aus (Tageslimit)."""
    n = 0
    while True:
        time.sleep(30)
        try:
            ids = running_game_ids()
            if ids:
                pt = store.setdefault("playtime", {})
                for gid in ids:
                    pt[gid] = pt.get(gid, 0) + 30
                c = cs()
                coin_day(c)
                c["play_acc"] += 30
                while c["play_acc"] >= 600:
                    c["play_acc"] -= 600
                    if c["play_today"] < PLAY_CAP:
                        c["play_today"] += 2
                        c["balance"] += 2
                        c["total"] += 2
                n += 1
                if n % 4 == 0:
                    coin_check()
                    save()
        except Exception as e:
            log("play_loop: %s" % e)


DLL_NAME = "dhub_overlay.dll"
DLL_MARK = b"DHUBOVERLAYv1"


def dll_src():
    for d in (getattr(sys, "_MEIPASS", ""), HERE):
        f = os.path.join(d, DLL_NAME) if d else ""
        if f and os.path.isfile(f):
            return f
    return ""


def ov_target(gid):
    """Ordner der Spiel-EXE, in den die Overlay-Datei kaeme."""
    g = next((x for x in LIB["games"] if x["id"] == gid), None)
    for v in (g["versions"] if g else []):
        if not v.get("installed"):
            continue
        l = v.get("launch", "")
        if l.startswith("exe:"):
            return os.path.dirname(l[4:].split("|")[0])
        d = v.get("dir")
        if d and os.path.isdir(d):
            exe = pick_exe(d)
            if exe:
                return os.path.dirname(exe)
    return ""


def is_ours(path):
    try:
        with open(path, "rb") as f:
            return DLL_MARK in f.read()
    except OSError:
        return False


def dll_info(gid):
    tgt = ov_target(gid)
    dst = os.path.join(tgt, "dxgi.dll") if tgt else ""
    exists = bool(dst) and os.path.isfile(dst)
    return dict(target=tgt, avail=bool(dll_src()), installed=exists and is_ours(dst), foreign=exists and not is_ours(dst), mp=gamecfg(gid)["mp"])


def _clean_line(t):
    return re.sub(r"[|\r\n=]", " ", str(t))[:60]


def dll_write(gid, off=False):
    """Einstellungen und Messwerte fuer die Overlay-Bibliothek im Spielordner schreiben."""
    tgt = ((store.get("dllinst") or {}).get(gid)) or ov_target(gid)
    if not tgt:
        return
    c = gamecfg(gid)
    live = OV["on"] and OV.get("dllgid") == gid and not off
    lines = [_clean_line(x) for x in ov_lines(fps=False)] if live else []
    kf = []
    if live and c.get("kf"):
        kf = [("*" if m["speaking"] else "") + _clean_line(m["name"]) for m in OV["members"]]
        kf += [_clean_line("%s: %s" % (w, t)) for w, t, e in OV["bubbles"] if e > time.time()]
    rows = ["visible=%d" % (1 if live and OV["visible"] else 0), "cross=%d" % (1 if c["cross"] else 0), "fps=%d" % (1 if c["fps"] else 0),
            "cstyle=" + c["cstyle"], "csize=%d" % c["csize"], "cgap=%d" % c["cgap"], "cthick=%d" % c["cthick"], "ccolor=" + c["ccolor"].lstrip("#"),
            "spos=" + c["spos"], "lines=" + "|".join(lines), "kf=" + "|".join(kf)]
    f = os.path.join(tgt, "dhub_overlay.cfg")
    try:
        with open(f + ".tmp", "w", encoding="utf-8", newline="\n") as fh:
            fh.write("\n".join(rows) + "\n")
        os.replace(f + ".tmp", f)
    except OSError as e:
        OV["err"] = "Overlay-Datei: %s" % e


def dll_install(gid):
    i = dll_info(gid)
    if i["mp"]:
        return False, "Bei Mehrspieler-Spielen ist das gesperrt. Nutze die Safe-Variante."
    if not i["avail"]:
        return False, "Die Overlay-Bibliothek ist in diesem D-Hub nicht enthalten."
    if not i["target"]:
        return False, "Der Spielordner wurde nicht erkannt."
    if i["foreign"]:
        return False, "Im Spielordner liegt schon eine fremde dxgi.dll (zum Beispiel ReShade). D-Hub fasst sie nicht an."
    try:
        shutil.copy2(dll_src(), os.path.join(i["target"], "dxgi.dll"))
    except OSError as e:
        return False, "Kopieren nicht moeglich: %s" % e
    store.setdefault("dllinst", {})[gid] = i["target"]
    save()
    dll_write(gid, off=True)
    log("Overlay-Bibliothek installiert: %s" % i["target"])
    return True, "Installiert."


def dll_remove(gid):
    tgt = ((store.get("dllinst") or {}).get(gid)) or ov_target(gid)
    dst = os.path.join(tgt, "dxgi.dll") if tgt else ""
    try:
        if dst and os.path.isfile(dst) and is_ours(dst):
            os.remove(dst)
        for n in ("dhub_overlay.cfg", "dhub_overlay.cfg.tmp"):
            if tgt and os.path.isfile(os.path.join(tgt, n)):
                os.remove(os.path.join(tgt, n))
    except OSError as e:
        return False, "Entfernen nicht moeglich (laeuft das Spiel noch?): %s" % e
    (store.get("dllinst") or {}).pop(gid, None)
    save()
    log("Overlay-Bibliothek entfernt: %s" % tgt)
    return True, "Entfernt."


def cfg_on(cfg):
    return any(cfg.get(k) for k in ("cross", "fps", "kf", "ping", "cpu", "gpu", "ram", "clock"))


def ov_apply(cfg, gid=None):
    """Overlay mit den Einstellungen eines Spiels starten. Es verknuepft sich mit dem Fenster dieses Spiels."""
    store["lastov"] = cfg
    if gid:
        store["lastovgid"] = gid
    gid = gid or store.get("lastovgid")
    save()
    OV["cross"] = cfg if cfg.get("cross") else None
    OV["showfps"] = bool(cfg.get("fps"))
    OV["showkf"] = bool(cfg.get("kf"))
    OV["flags"] = dict(cfg)
    g = next((x for x in LIB["games"] if x["id"] == gid), None)
    OV["hint_title"] = norm(g["title"]) if g else ""
    OV["hint_dirs"] = [v.get("dir") for v in (g["versions"] if g else []) if v.get("dir")]
    OV["armed"] = time.time() + 900  # 15 Minuten: das naechste grosse Fenster im Vordergrund ist das Spiel
    inst = bool(gid) and not cfg.get("mp", True) and dll_info(gid)["installed"]
    OV["dllgid"], OV["hidewin"] = (gid if inst else None), inst  # installiert: das Spiel zeichnet selbst, das D-Hub-Fenster bleibt unsichtbar
    if cfg_on(cfg):
        ov_start()
        if inst:
            dll_write(gid)
        if OV["showfps"] and not inst:
            threading.Thread(target=fps_loop, daemon=True).start()
    else:
        ov_stop()


def ov_start():
    if not IS_WIN or OV["on"]:
        return
    OV["on"], OV["err"], OV["visible"] = True, "", True
    threading.Thread(target=_ov_thread, daemon=True).start()


def ov_stop():
    OV["showfps"] = OV["showkf"] = False
    OV["cross"], OV["flags"] = None, {}
    OV["gpid"], OV["ghwnd"] = 0, 0
    g_off, OV["dllgid"], OV["hidewin"] = OV["dllgid"], None, False
    if g_off:
        dll_write(g_off, off=True)
    stop_fps()
    try:
        if OV["hwnd"]:
            import ctypes
            ctypes.windll.user32.PostMessageW(ctypes.c_void_p(OV["hwnd"]), 0x0010, 0, 0)
    except Exception:
        pass


def ov_bubble(sender, text):
    if OV["on"] and OV["showkf"]:
        OV["bubbles"] = (OV["bubbles"] + [(clean_name(sender) or "KumpelFunk", str(text)[:140], time.time() + 9)])[-4:]
        if OV["dllgid"]:
            dll_write(OV["dllgid"])


def ov_voice(members):
    out = []
    for m in (members or [])[:8]:
        if isinstance(m, dict) and clean_name(m.get("name")):
            out.append(dict(name=clean_name(m["name"]), speaking=bool(m.get("speaking"))))
    OV["members"] = out
    if OV["dllgid"]:
        dll_write(OV["dllgid"])


def _hex(c):
    r, g, b = int(c[1:3], 16), int(c[3:5], 16), int(c[5:7], 16)
    if (r, g, b) == (255, 0, 255):
        g = 1
    return r | g << 8 | b << 16


HK_KEYS = {"F7": (0, 0x76), "F8": (0, 0x77), "F9": (0, 0x78), "F10": (0, 0x79), "F11": (0, 0x7A), "F12": (0, 0x7B),
           "Strg+F9": (2, 0x78), "Strg+F10": (2, 0x79), "Alt+F9": (1, 0x78), "Alt+F10": (1, 0x79),
           "Strg+Umschalt+F9": (6, 0x78), "Strg+Umschalt+F10": (6, 0x79)}
HK_DEF = dict(hk_toggle="Strg+F9", hk_close="Strg+F10")
HK = dict(tid=0)


def hk_name(k):
    v = store["settings"].get(k)
    return v if v in HK_KEYS else HK_DEF[k]


def hk_reload():
    if IS_WIN and HK["tid"]:
        import ctypes
        ctypes.windll.user32.PostThreadMessageW(HK["tid"], 0x8001, 0, 0)


def ov_toggle_visible():
    """Ein- und ausblenden. Laeuft noch kein Overlay, wird es mit den zuletzt benutzten Einstellungen gestartet."""
    if not OV["on"]:
        ov_apply(clean_cfg(store.get("lastov")))
        return
    OV["visible"] = not OV["visible"]  # das Overlay-Fenster blendet sich im naechsten Takt selbst ein oder aus
    if OV["dllgid"]:
        dll_write(OV["dllgid"])


def ov_toggle_run():
    """Overlay ganz beenden oder (neu) starten."""
    if OV["on"]:
        ov_stop()
    else:
        ov_apply(clean_cfg(store.get("lastov")))


def hotkey_loop():
    if not IS_WIN:
        return
    try:
        import ctypes
        from ctypes import wintypes as W
        u, k = ctypes.windll.user32, ctypes.windll.kernel32
        u.RegisterHotKey.argtypes = [ctypes.c_void_p, ctypes.c_int, W.UINT, W.UINT]
        u.UnregisterHotKey.argtypes = [ctypes.c_void_p, ctypes.c_int]
        u.PostThreadMessageW.argtypes = [W.DWORD, W.UINT, W.WPARAM, W.LPARAM]
        HK["tid"] = k.GetCurrentThreadId()
        msg = W.MSG()
        u.PeekMessageW(ctypes.byref(msg), None, 0, 0, 0)  # Nachrichtenschlange dieses Threads anlegen
        while True:
            for i, nm in ((1, hk_name("hk_toggle")), (2, hk_name("hk_close"))):
                u.UnregisterHotKey(None, i)
                mod, vk = HK_KEYS[nm]
                u.RegisterHotKey(None, i, mod | 0x4000, vk)  # 0x4000 = nicht wiederholen
            while True:
                r = u.GetMessageW(ctypes.byref(msg), None, 0, 0)
                if r <= 0:
                    return
                if msg.message == 0x8001:
                    break
                if msg.message == 0x0312:
                    try:
                        (ov_toggle_visible if msg.wParam == 1 else ov_toggle_run)()
                    except Exception as e:
                        OV["err"] = "%s: %s" % (type(e).__name__, e)
    except Exception as e:
        OV["err"] = "Tastenkuerzel: %s: %s" % (type(e).__name__, e)


IGNORE_EXE = {"explorer.exe", "dwm.exe", "steam.exe", "steamwebhelper.exe", "epicgameslauncher.exe", "galaxyclient.exe", "galaxyclient helper.exe",
              "upc.exe", "ubisoftconnect.exe", "eadesktop.exe", "origin.exe", "battle.net.exe", "msedge.exe", "chrome.exe", "firefox.exe",
              "discord.exe", "taskmgr.exe", "applicationframehost.exe", "searchhost.exe", "shellexperiencehost.exe", "startmenuexperiencehost.exe",
              "textinputhost.exe", "lockapp.exe", "cmd.exe", "powershell.exe", "windowsterminal.exe", "code.exe", "d-hub.exe", "python.exe",
              "pythonw.exe", "msedgewebview2.exe", "systemsettings.exe", "presentmon.exe", "legendary.exe", "notepad.exe", "spotify.exe"}


def _w32():
    import ctypes
    from ctypes import wintypes as W
    u, k = ctypes.windll.user32, ctypes.windll.kernel32
    H = ctypes.c_void_p
    u.GetForegroundWindow.restype = H
    u.GetWindowThreadProcessId.argtypes = [H, ctypes.POINTER(W.DWORD)]
    u.IsWindow.argtypes = [H]
    u.IsIconic.argtypes = [H]
    u.GetClientRect.argtypes = [H, ctypes.POINTER(W.RECT)]
    u.ClientToScreen.argtypes = [H, ctypes.POINTER(W.POINT)]
    u.GetWindowTextW.argtypes = [H, W.LPWSTR, ctypes.c_int]
    k.OpenProcess.restype = H
    k.OpenProcess.argtypes = [W.DWORD, W.BOOL, W.DWORD]
    k.QueryFullProcessImageNameW.argtypes = [H, W.DWORD, W.LPWSTR, ctypes.POINTER(W.DWORD)]
    k.CloseHandle.argtypes = [H]
    k.GetExitCodeProcess.argtypes = [H, ctypes.POINTER(W.DWORD)]
    return ctypes, W, u, k


def proc_path(pid):
    ctypes, W, u, k = _w32()
    h = k.OpenProcess(0x1000, False, pid)
    if not h:
        return ""
    try:
        buf, n = ctypes.create_unicode_buffer(1024), W.DWORD(1024)
        return buf.value if k.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(n)) else ""
    finally:
        k.CloseHandle(h)


def pid_alive(pid):
    ctypes, W, u, k = _w32()
    h = k.OpenProcess(0x1000, False, pid)
    if not h:
        return False
    try:
        code = W.DWORD(0)
        return bool(k.GetExitCodeProcess(h, ctypes.byref(code))) and code.value == 259
    finally:
        k.CloseHandle(h)


def client_rect(hwnd):
    ctypes, W, u, k = _w32()
    r, p = W.RECT(), W.POINT(0, 0)
    if not u.GetClientRect(hwnd, ctypes.byref(r)) or not u.ClientToScreen(hwnd, ctypes.byref(p)):
        return None
    return (p.x, p.y, max(1, r.right - r.left), max(1, r.bottom - r.top))


def game_tick():
    """Findet das Spielfenster und merkt es sich. Das Overlay legt sich genau darauf und zeigt sich nur, solange das Spiel vorne ist."""
    ctypes, W, u, k = _w32()
    sw, sh = u.GetSystemMetrics(0), u.GetSystemMetrics(1)
    fg = u.GetForegroundWindow()
    pid = W.DWORD(0)
    if fg:
        u.GetWindowThreadProcessId(fg, ctypes.byref(pid))
    fp = pid.value
    if OV["gpid"] and not pid_alive(OV["gpid"]):
        OV.update(gpid=0, ghwnd=0, fps=0.0, target="wartet auf Spiel …")
    if not OV["gpid"] and fg and fp and fp != os.getpid():
        path = proc_path(fp)
        base = os.path.basename(path).lower()
        if path and base not in IGNORE_EXE and not path.lower().startswith(os.environ.get("WINDIR", r"c:\windows").lower()):
            rc = client_rect(fg)
            big = bool(rc) and rc[2] * rc[3] >= 0.4 * sw * sh
            buf = ctypes.create_unicode_buffer(256)
            u.GetWindowTextW(fg, buf, 256)
            hint = any(d and path.lower().startswith(d.lower().rstrip("\\\\/")) for d in OV["hint_dirs"]) or (
                OV["hint_title"] and OV["hint_title"] in norm(buf.value))
            if hint or (time.time() < OV["armed"] and big):
                OV.update(gpid=fp, ghwnd=fg, target="%s (PID %d)" % (os.path.basename(path), fp))
                log("Overlay verknuepft mit %s" % OV["target"])
    vis = False
    if OV["gpid"]:
        if fp == OV["gpid"] and fg:
            rc = client_rect(fg)
            if rc and rc[2] * rc[3] >= 0.3 * sw * sh:
                OV["ghwnd"] = fg
        h = OV["ghwnd"]
        if h and u.IsWindow(h) and not u.IsIconic(h):
            rc = client_rect(h)
            if rc:
                OV["rect"] = rc
                vis = fp == OV["gpid"]
    OV["target_vis"] = vis


def game_loop():
    try:
        ctypes, W, u, k = _w32()
        try:
            u.SetProcessDPIAware()
        except Exception:
            pass
        while OV["on"]:
            try:
                game_tick()
            except Exception as e:
                OV["err"] = "Spielfenster: %s: %s" % (type(e).__name__, e)
            time.sleep(0.4)
    except Exception as e:
        OV["err"] = "Spielfenster: %s: %s" % (type(e).__name__, e)


def ping_ms(host):
    try:
        r = subprocess.run(["ping", "-n", "1", "-w", "1000", host], capture_output=True, text=True, errors="replace", creationflags=0x08000000, timeout=4)
        m = re.search(r"[=<]\s*(\d+)\s*ms", r.stdout)
        return int(m.group(1)) if m else None
    except Exception:
        return None


def game_server():
    """Haeufigste oeffentliche TCP-Gegenstelle des Spiels, sonst None."""
    import ipaddress
    try:
        out = subprocess.run(["netstat", "-ano", "-p", "TCP"], capture_output=True, text=True, errors="replace", creationflags=0x08000000, timeout=6).stdout
    except Exception:
        return None
    for ln in out.splitlines():
        p = ln.split()
        if len(p) >= 5 and p[3] == "ESTABLISHED" and p[4] == str(OV["gpid"]):
            try:
                ip = ipaddress.ip_address(p[2].rsplit(":", 1)[0].strip("[]"))
            except ValueError:
                continue
            if ip.is_global:
                return str(ip)
    return None


def gpu_percent(pid):
    try:
        out = subprocess.run(["typeperf", r"\GPU Engine(pid_%d*)\Utilization Percentage" % pid, "-sc", "1"], capture_output=True, text=True,
                             errors="replace", creationflags=0x08000000, timeout=8).stdout
        rows = [ln for ln in out.splitlines() if ln.startswith('"') and "PDH-CSV" not in ln]
        vals = re.findall(r'"([0-9.,]+)"', rows[-1])[1:] if rows else []
        return min(100.0, sum(float(v.replace(",", ".")) for v in vals)) if vals else None
    except Exception:
        return None


def stats_loop():
    import ctypes
    from ctypes import wintypes as W
    k = ctypes.windll.kernel32

    class MEM(ctypes.Structure):
        _fields_ = [("l", W.DWORD), ("load", W.DWORD), ("a", ctypes.c_ulonglong), ("b", ctypes.c_ulonglong), ("c", ctypes.c_ulonglong),
                    ("d", ctypes.c_ulonglong), ("e", ctypes.c_ulonglong), ("f", ctypes.c_ulonglong), ("g", ctypes.c_ulonglong)]

    def times():
        a, b, c = W.FILETIME(), W.FILETIME(), W.FILETIME()
        k.GetSystemTimes(ctypes.byref(a), ctypes.byref(b), ctypes.byref(c))
        f = lambda x: (x.dwHighDateTime << 32) | x.dwLowDateTime
        return f(a), f(b) + f(c)
    prev, n = times(), 0
    while OV["on"]:
        fl = OV["flags"]
        try:
            idle, tot = times()
            if tot > prev[1]:
                OV["stats"]["cpu"] = max(0, min(100, 100 * (1 - (idle - prev[0]) / (tot - prev[1]))))
            prev = (idle, tot)
            m = MEM()
            m.l = ctypes.sizeof(MEM)
            k.GlobalMemoryStatusEx(ctypes.byref(m))
            OV["stats"]["ram"] = m.load
            if n % 3 == 0 and OV["gpid"]:
                if fl.get("gpu"):
                    OV["stats"]["gpu"] = gpu_percent(OV["gpid"])
                if fl.get("ping"):
                    host = game_server()
                    ms = ping_ms(host or "1.1.1.1")
                    OV["stats"]["ping"] = (ms, bool(host))
        except Exception as e:
            OV["err"] = "Messwerte: %s: %s" % (type(e).__name__, e)
        if OV["dllgid"]:
            dll_write(OV["dllgid"])
        n += 1
        time.sleep(2)


def ov_lines(fps=True):
    fl, st, out = OV["flags"], OV["stats"], []
    if fl.get("fps") and fps:
        out.append(("FPS %d" % round(OV["fps"])) if OV["fps"] > 0 else (OV["fpsmsg"] or "FPS …"))
    if fl.get("ping") and st.get("ping") and st["ping"][0] is not None:
        out.append("%s %d ms" % ("Ping" if st["ping"][1] else "Netz", st["ping"][0]))
    if fl.get("cpu") and st.get("cpu") is not None:
        out.append("CPU %d %%" % round(st["cpu"]))
    if fl.get("gpu") and st.get("gpu") is not None:
        out.append("GPU %d %%" % round(st["gpu"]))
    if fl.get("ram") and st.get("ram") is not None:
        out.append("RAM %d %%" % st["ram"])
    if fl.get("clock"):
        out.append(time.strftime("%H:%M"))
    return out


def _ov_thread():
    try:
        import ctypes
        from ctypes import wintypes as W
        u, g, k = ctypes.windll.user32, ctypes.windll.gdi32, ctypes.windll.kernel32
        H, I, LR = ctypes.c_void_p, ctypes.c_int, ctypes.c_ssize_t

        def sig(f, res, *a):
            f.restype, f.argtypes = res, list(a)
        WNDPROC = ctypes.WINFUNCTYPE(LR, H, W.UINT, W.WPARAM, W.LPARAM)

        class WNDCLASS(ctypes.Structure):
            _fields_ = [("style", W.UINT), ("lpfnWndProc", WNDPROC), ("cbClsExtra", I), ("cbWndExtra", I), ("hInstance", H),
                        ("hIcon", H), ("hCursor", H), ("hbrBackground", H), ("lpszMenuName", W.LPCWSTR), ("lpszClassName", W.LPCWSTR)]

        class PS(ctypes.Structure):
            _fields_ = [("hdc", H), ("fErase", W.BOOL), ("rc", W.RECT), ("fRestore", W.BOOL), ("fInc", W.BOOL), ("res", ctypes.c_byte * 32)]
        sig(k.GetModuleHandleW, H, W.LPCWSTR)
        sig(u.DefWindowProcW, LR, H, W.UINT, W.WPARAM, W.LPARAM)
        sig(u.RegisterClassW, W.ATOM, ctypes.POINTER(WNDCLASS))
        sig(u.CreateWindowExW, H, W.DWORD, W.LPCWSTR, W.LPCWSTR, W.DWORD, I, I, I, I, H, H, H, H)
        sig(u.SetLayeredWindowAttributes, W.BOOL, H, W.COLORREF, W.BYTE, W.DWORD)
        sig(u.BeginPaint, H, H, ctypes.POINTER(PS))
        sig(u.EndPaint, W.BOOL, H, ctypes.POINTER(PS))
        sig(u.FillRect, I, H, ctypes.POINTER(W.RECT), H)
        sig(u.DrawTextW, I, H, W.LPCWSTR, I, ctypes.POINTER(W.RECT), W.UINT)
        sig(u.ShowWindow, W.BOOL, H, I)
        sig(u.SetWindowPos, W.BOOL, H, H, I, I, I, I, W.UINT)
        sig(u.InvalidateRect, W.BOOL, H, H, W.BOOL)
        sig(u.SetTimer, ctypes.c_size_t, H, ctypes.c_size_t, W.UINT, H)
        sig(u.GetMessageW, W.BOOL, ctypes.POINTER(W.MSG), H, W.UINT, W.UINT)
        sig(u.TranslateMessage, W.BOOL, ctypes.POINTER(W.MSG))
        sig(u.DispatchMessageW, LR, ctypes.POINTER(W.MSG))
        sig(u.PostMessageW, W.BOOL, H, W.UINT, W.WPARAM, W.LPARAM)
        sig(u.PostQuitMessage, None, I)
        sig(u.DestroyWindow, W.BOOL, H)
        sig(g.CreateSolidBrush, H, W.COLORREF)
        sig(g.DeleteObject, W.BOOL, H)
        sig(g.DeleteDC, W.BOOL, H)
        sig(g.CreateCompatibleDC, H, H)
        sig(g.CreateCompatibleBitmap, H, H, I, I)
        sig(g.SelectObject, H, H, H)
        sig(g.BitBlt, W.BOOL, H, I, I, I, I, H, I, I, W.DWORD)
        sig(g.SetTextColor, W.COLORREF, H, W.COLORREF)
        sig(g.SetBkMode, I, H, I)
        sig(g.Ellipse, W.BOOL, H, I, I, I, I)
        sig(g.RoundRect, W.BOOL, H, I, I, I, I, I, I)
        sig(g.CreatePen, H, I, I, W.COLORREF)
        sig(g.GetStockObject, H, I)
        sig(g.CreateFontW, H, I, I, I, I, I, I, I, I, I, I, I, I, I, W.LPCWSTR)
        KEY = 0x00FF00FF
        fonts = {}

        def font(h, bold):
            if (h, bold) not in fonts:
                fonts[(h, bold)] = g.CreateFontW(-h, 0, 0, 0, 700 if bold else 500, 0, 0, 0, 1, 0, 0, 3, 0, "Segoe UI")
            return fonts[(h, bold)]

        def rect(dc, x1, y1, x2, y2, col):
            br = g.CreateSolidBrush(col)
            r = W.RECT(int(x1), int(y1), int(x2), int(y2))
            u.FillRect(dc, ctypes.byref(r), br)
            g.DeleteObject(br)

        def text(dc, s, x, y, w, h, col, size=14, bold=False, flags=0x8000 | 0x20 | 0x800):
            g.SelectObject(dc, font(size, bold))
            g.SetBkMode(dc, 1)
            for dx, dy, c in ((1, 1, 0x000000), (0, 0, col)):
                g.SetTextColor(dc, c)
                r = W.RECT(int(x + dx), int(y + dy), int(x + w + dx), int(y + h + dy))
                u.DrawTextW(dc, s, -1, ctypes.byref(r), flags)

        def box(dc, x1, y1, x2, y2, edge, fill):
            pen, br = g.CreatePen(0, 1, edge), g.CreateSolidBrush(fill)
            op, ob = g.SelectObject(dc, pen), g.SelectObject(dc, br)
            g.RoundRect(dc, x1, y1, x2, y2, 12, 12)
            g.SelectObject(dc, op)
            g.SelectObject(dc, ob)
            g.DeleteObject(pen)
            g.DeleteObject(br)

        def draw(dc, sw, sh):
            rect(dc, 0, 0, sw, sh, KEY)
            c = OV["cross"]
            if c:
                col, cx, cy, s, gap, t = _hex(c["ccolor"]), sw // 2, sh // 2, c["csize"], c["cgap"], c["cthick"]
                h = t // 2
                st = c["cstyle"]
                if st in ("cross", "plus"):
                    gp = gap if st == "cross" else 0
                    rect(dc, cx - gp - s, cy - h, cx - gp, cy - h + t, col)
                    rect(dc, cx + gp, cy - h, cx + gp + s, cy - h + t, col)
                    rect(dc, cx - h, cy - gp - s, cx - h + t, cy - gp, col)
                    rect(dc, cx - h, cy + gp, cx - h + t, cy + gp + s, col)
                    if st == "plus":
                        rect(dc, cx - h, cy - h, cx - h + t, cy - h + t, col)
                elif st == "dot":
                    rect(dc, cx - t - 1, cy - t - 1, cx + t + 2, cy + t + 2, col)
                else:
                    pen = g.CreatePen(0, t, col)
                    old = g.SelectObject(dc, pen)
                    ob = g.SelectObject(dc, g.GetStockObject(5))
                    g.Ellipse(dc, cx - s, cy - s, cx + s, cy + s)
                    g.SelectObject(dc, ob)
                    g.SelectObject(dc, old)
                    g.DeleteObject(pen)
                    rect(dc, cx - 1, cy - 1, cx + 2, cy + 2, col)
            ky = 18
            if OV["flags"].get("kf"):
                for m in OV["members"]:
                    sp = m["speaking"]
                    pen = g.CreatePen(0, 3, 0x0CFF5C if sp else 0x555555)
                    br = g.CreateSolidBrush(0x1A1F1A)
                    op, ob = g.SelectObject(dc, pen), g.SelectObject(dc, br)
                    g.Ellipse(dc, 18, ky, 54, ky + 36)
                    g.SelectObject(dc, op)
                    g.SelectObject(dc, ob)
                    g.DeleteObject(pen)
                    g.DeleteObject(br)
                    text(dc, m["name"][:1].upper(), 18, ky + 6, 36, 26, 0xFFFFFF, 17, True, 0x1 | 0x20 | 0x800)
                    text(dc, m["name"], 62, ky + 8, 230, 22, 0x0CFF5C if sp else 0xDDDDDD, 15, sp)
                    ky += 44
                ky += 6
                now = time.time()
                OV["bubbles"] = [b for b in OV["bubbles"] if b[2] > now]
                for who, tx, _ in OV["bubbles"]:
                    box(dc, 18, ky, 430, ky + 34, 0x0CFF5C, 0x141814)
                    text(dc, "%s: %s" % (who, tx), 30, ky + 6, 390, 24, 0xFFFFFF, 15)
                    ky += 40
            lines = ov_lines()
            if lines:
                pos, lh, bw = OV["flags"].get("spos", "tr"), 26, 150
                bh = len(lines) * lh + 10
                x = 14 if pos in ("tl", "bl") else sw - bw - 14
                y = (ky + 8 if OV["flags"].get("kf") else 14) if pos == "tl" else (14 if pos == "tr" else sh - bh - 14)
                box(dc, x, y, x + bw, y + bh, 0x2A4A2A, 0x0A0F0A)
                for i, ln in enumerate(lines):
                    first = i == 0 and ln.startswith("FPS ") and OV["fps"] > 0
                    text(dc, ln, x + 12, y + 6 + i * lh, bw - 20, lh, 0x0CFF5C if first else 0xEEEEEE, 18 if first else 15, first)
        st = dict(pos=None, shown=False)

        def proc(hwnd, msg, wp, lp):
            try:
                if msg == 0x000F:
                    r = OV["rect"] or (0, 0, 100, 100)
                    sw, sh = r[2], r[3]
                    ps = PS()
                    hdc = u.BeginPaint(hwnd, ctypes.byref(ps))
                    mdc = g.CreateCompatibleDC(hdc)
                    bmp = g.CreateCompatibleBitmap(hdc, sw, sh)
                    old = g.SelectObject(mdc, bmp)
                    draw(mdc, sw, sh)
                    g.BitBlt(hdc, 0, 0, sw, sh, mdc, 0, 0, 0x00CC0020)
                    g.SelectObject(mdc, old)
                    g.DeleteObject(bmp)
                    g.DeleteDC(mdc)
                    u.EndPaint(hwnd, ctypes.byref(ps))
                    return 0
                if msg == 0x0113:  # Timer: auf das Spielfenster legen, ausblenden wenn das Spiel nicht vorne ist
                    r = OV["rect"]
                    if r and OV["target_vis"] and OV["visible"] and not OV["hidewin"]:
                        if r != st["pos"] or not st["shown"]:
                            u.SetWindowPos(hwnd, H(-1), r[0], r[1], r[2], r[3], 0x10 | 0x40)
                            st["pos"], st["shown"] = r, True
                        else:
                            u.SetWindowPos(hwnd, H(-1), 0, 0, 0, 0, 0x13)
                        u.InvalidateRect(hwnd, None, False)
                    elif st["shown"]:
                        u.ShowWindow(hwnd, 0)
                        st["shown"] = False
                    return 0
                if msg == 0x0014:
                    return 1
                if msg == 0x0010:
                    u.DestroyWindow(hwnd)
                    return 0
                if msg == 0x0002:
                    u.PostQuitMessage(0)
                    return 0
            except Exception as e:
                OV["err"] = "Zeichnen: %s: %s" % (type(e).__name__, e)
                return 0
            return u.DefWindowProcW(hwnd, msg, wp, lp)
        cb = WNDPROC(proc)
        wc = WNDCLASS()
        wc.lpfnWndProc, wc.hInstance, wc.lpszClassName = cb, k.GetModuleHandleW(None), "DHubOverlay"
        u.RegisterClassW(ctypes.byref(wc))
        # TOPMOST | LAYERED | TRANSPARENT (Klicks gehen durch) | TOOLWINDOW | NOACTIVATE; zunaechst unsichtbar
        hwnd = u.CreateWindowExW(0x8 | 0x80000 | 0x20 | 0x80 | 0x08000000, "DHubOverlay", "D-Hub Overlay", 0x80000000,
                                 0, 0, 100, 100, None, None, wc.hInstance, None)
        if not hwnd:
            raise OSError("Overlay-Fenster konnte nicht erstellt werden")
        OV["hwnd"] = hwnd
        u.SetLayeredWindowAttributes(hwnd, KEY, 0, 1)
        u.SetTimer(hwnd, 1, 200, None)
        threading.Thread(target=game_loop, daemon=True).start()
        threading.Thread(target=stats_loop, daemon=True).start()
        msg = W.MSG()
        while u.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            u.TranslateMessage(ctypes.byref(msg))
            u.DispatchMessageW(ctypes.byref(msg))
    except Exception as e:
        OV["err"] = "%s: %s" % (type(e).__name__, e)
    OV.update(on=False, hwnd=0, gpid=0, ghwnd=0, rect=None, target_vis=False)
    stop_fps()


# FPS: Windows verraet die FPS eines fremden Spiels nicht von selbst. Dafuer nutzt D-Hub das kostenlose Intel-Tool PresentMon.
def presentmon_path():
    for f in sorted(glob.glob(os.path.join(HERE, "PresentMon*.exe"))):
        return f
    OV["fpsmsg"] = "Lade PresentMon …"
    req = urllib.request.Request("https://api.github.com/repos/GameTechDev/PresentMon/releases/latest", headers={"User-Agent": "D-Hub/" + VERSION})
    rel = json.loads(urllib.request.urlopen(req, timeout=20).read().decode("utf-8"))
    for a in rel.get("assets", []):
        if re.fullmatch(r"PresentMon.*x64\.exe", a.get("name", "")) and a["browser_download_url"].startswith("https://github.com/GameTechDev/PresentMon/"):
            data = urllib.request.urlopen(urllib.request.Request(a["browser_download_url"], headers={"User-Agent": "D-Hub"}), timeout=120).read()
            if data[:2] == b"MZ" and len(data) > 100000:
                p = os.path.join(HERE, "PresentMon.exe")
                open(p, "wb").write(data)
                return p
    raise RuntimeError("PresentMon nicht gefunden")


def stop_fps():
    p, OV["fpsproc"] = OV["fpsproc"], None
    if p:
        try:
            p.kill()
        except Exception:
            pass


def fg_pid():
    import ctypes
    u = ctypes.windll.user32
    pid = ctypes.c_ulong(0)
    u.GetWindowThreadProcessId(u.GetForegroundWindow(), ctypes.byref(pid))
    return str(pid.value)


def fps_loop():
    if not IS_WIN or OV["fpsproc"]:
        return
    try:
        exe = presentmon_path()
    except Exception as e:
        OV["fpsmsg"] = "FPS: PresentMon fehlt (%s)" % e
        return
    for args in (["--output_stdout", "--no_console_stats", "--stop_existing_session"], ["-output_stdout", "-stop_existing_session"]):
        try:
            p = subprocess.Popen([exe] + args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, creationflags=0x08000000, text=True, errors="replace")
        except OSError as e:
            OV["fpsmsg"] = "FPS: PresentMon startet nicht (%s)" % e
            return
        OV["fpsproc"] = p
        OV["fpsmsg"] = "FPS: warte auf Spiel …"
        cols, frames, last, got, lines = None, {}, 0, False, []
        for line in p.stdout:
            if not OV["fpsproc"]:
                return
            parts = line.strip().split(",")
            if cols is None:
                if "," in line and any(x in parts for x in ("ProcessID", "ProcessName", "Application")):
                    cols = {n: i for i, n in enumerate(parts)}
                else:
                    lines.append(line.strip())
                continue
            try:
                ms = float(parts[next(cols[n] for n in ("MsBetweenPresents", "msBetweenPresents", "FrameTime") if n in cols)])
                pid = parts[cols["ProcessID"]] if "ProcessID" in cols else parts[cols.get("ProcessName", cols.get("Application", 0))]
            except (StopIteration, ValueError, IndexError, KeyError):
                continue
            got = True
            fl = frames.setdefault(pid, [])
            fl.append((time.time(), ms))
            del fl[:-40]
            if time.time() - last > 0.5:
                last = time.time()
                try:
                    fl = frames.get(str(OV["gpid"]) if OV["gpid"] else fg_pid()) or max(frames.values(), key=lambda x: x[-1][0])
                    OV["fps"] = 1000.0 / max(0.01, sum(m for _, m in fl) / len(fl)) if time.time() - fl[-1][0] < 3 else 0.0
                except Exception:
                    pass
        if got or not OV["fpsproc"]:
            return
        OV["fpsproc"] = None
        if args[0] == "-output_stdout":
            tail = " ".join(lines)[-120:]
            OV["fpsmsg"] = "FPS braucht Admin-Rechte (D-Hub als Administrator starten)" if re.search(r"admin|privileg|Performance Log", tail, re.I) else "FPS: PresentMon-Fehler"


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
                    cfg = gamecfg(gid)
                    if cfg_on(cfg):
                        ov_apply(cfg, gid)
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


def uninstaller():
    """Pfad zum Deinstallationsprogramm, nur vorhanden wenn D-Hub per Setup installiert wurde."""
    if not (IS_WIN and getattr(sys, "frozen", False)):
        return ""
    for f in sorted(os.listdir(HERE)):
        if re.fullmatch(r"unins\d+\.exe", f, re.I):
            return os.path.join(HERE, f)
    return ""


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
    elif n == "uninstall" and IS_WIN:
        un = uninstaller()
        if un:
            try:
                autostart_set(False)
            except Exception:
                pass
            subprocess.Popen([un], cwd=HERE, close_fds=True)
            threading.Thread(target=lambda: (time.sleep(1.5), os._exit(0)), daemon=True).start()
    elif n == "gamecfg" and isinstance(b.get("id"), str):
        store.setdefault("gamecfg", {})[b["id"][:80]] = clean_cfg(b.get("cfg"))
        save()
        if OV["on"] and store.get("lastovgid") == b["id"]:
            ov_apply(gamecfg(b["id"]), b["id"])
    elif n == "ovstart" and IS_WIN and isinstance(b.get("id"), str):
        ov_apply(gamecfg(b["id"]), b["id"])
    elif n == "ovstop":
        ov_stop()
    elif n == "kfvoice":
        ov_voice(b.get("members"))
    elif n == "restartapp":
        ov_stop()
        restart()
    elif n == "addgame":
        title, exe = str(b.get("title") or "").strip()[:80], str(b.get("exe") or "").strip().strip('"')
        if norm(title) and exe.lower().endswith(".exe") and os.path.isfile(exe):
            store.setdefault("custom", []).append(dict(id="c%d" % int(time.time() * 1000), title=title, exe=exe))
            save()
            scan()
    elif n == "delgame":
        store["custom"] = [c for c in store.get("custom") or [] if c["id"] != b.get("sid")]
        save()
        scan()
    elif n == "delcover":
        (store.get("covers") or {}).pop(norm(b.get("id")), None)
        save()
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
            ov_bubble(b.get("from"), b["text"])
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
        gone = BYE[0] and LAST[0] <= BYE[0] and time.time() - BYE[0] > 4
        if gone and OV["on"]:  # D-Hub-Fenster ist zu: Overlay und FPS-Messung sofort mit beenden
            ov_stop()
        if gone and not busy:
            ov_stop()
            os._exit(0)


def state():
    pending = 0
    for g in LIB["all"]:
        lp = store["listprices"].get(g["id"])
        manual = store["prices"].get(g["id"])
        pending += lp is None
        g["manual"] = manual
        g["played"] = (store.get("playtime") or {}).get(g["id"], 0)
        g["price"] = manual if manual is not None else (lp or {}).get("v")
        g["psrc"] = "Manuell" if manual is not None else (lp or {}).get("src", "")
    return dict(games=LIB["games"], allgames=LIB["all"], covers=store.get("covers") or {}, demo=LIB["demo"], launchers=LIB["launchers"], pending=pending, version=VERSION, diag=list(LIB["diag"]) + ["Fenster: " + WINMODE[0]] + ([LIB["gal_info"]] if LIB.get("gal_info") else []), perr=LIB["perr"],
                downloads=downloads, settings={k: v for k, v in store["settings"].items() if k != "steamkey"},
                errs=LIB["errs"], busy=LIB["busy"], upd=upd_public(), inbox=inbox_public(), unread=unread_count(), kf=store.get("kf") or {}, perms=dict(mic=consent("microphone"), cam=consent("webcam")), autostart=autostart_get(), coins=coin_public(), winopen=time.time() - LAST[0] < 6, native=WINMODE[0].startswith("eigenes"), gamecfg=store.get("gamecfg") or {}, hk=dict(toggle=hk_name("hk_toggle"), close=hk_name("hk_close")), ov=dict(target=OV["target"], on=OV["on"], err=OV["err"], fps=OV["fps"], msg=OV["fpsmsg"]), uninst=bool(uninstaller()), galaxy=os.path.exists(GALAXY_DB), haskey=bool(store["settings"].get("steamkey")), legendary=bool(find_legendary()))


PAGE = r'''<!DOCTYPE html>
<html lang="de"><head><meta charset="utf-8"><title>D-Hub</title>
<link rel="icon" href="/logo.png">
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
:root{--acrgb:92,255,12;--bg:#050605;--panel:#0e120c;--line:#1f2c19;--tx:#ececec;--dim:#8d9889;--ac:#5cff0c;--warn:#ffb454;
--Steam:#66c0f4;--GOG:#b88cff;--Epic:#dcdcdc;--EA:#ff7a7a;--Ubisoft:#5aa9ff;--Battlenet:#3fb6ff}
*{box-sizing:border-box}
.coinchip{width:auto!important;padding:0 12px!important;gap:6px;display:flex!important;flex-direction:row!important;align-items:center;white-space:nowrap;font-size:14px;color:var(--tx)}.coinchip small{color:var(--dim);font-size:12px}
.shop{display:grid;grid-template-columns:repeat(auto-fill,minmax(270px,1fr));gap:10px}.item{display:flex;align-items:center;gap:12px;background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:12px}
.sw{width:30px;height:30px;border-radius:50%;box-shadow:0 0 14px currentColor;flex:none}
.coinwrap{perspective:600px;height:130px;display:flex;align-items:center;justify-content:center}.cn{width:100px;height:100px;position:relative;transform-style:preserve-3d}
.cf{position:absolute;inset:0;border-radius:50%;display:flex;align-items:center;justify-content:center;font-weight:800;letter-spacing:1px;background:radial-gradient(circle at 35% 30%,#ffe27a,#d9a400);color:#4a3300;border:4px solid #b88700;backface-visibility:hidden}.cf.cb{transform:rotateY(180deg)}
.wheelwrap{position:relative;width:230px;height:230px;margin:10px auto}.wh{position:relative;width:230px;height:230px;border-radius:50%;border:4px solid var(--line)}.wh span{position:absolute;left:50%;top:50%;margin:-9px 0 0 -18px;width:36px;text-align:center;font-weight:700;font-size:13px;color:#fff;text-shadow:0 1px 2px #000;transform-origin:center}
.ptr{position:absolute;left:50%;top:-8px;margin-left:-10px;border:10px solid transparent;border-top:18px solid var(--ac);z-index:2}
.reels{display:flex;gap:10px;justify-content:center;margin:10px 0}.reel{width:84px;height:96px;display:flex;align-items:center;justify-content:center;font-size:46px;background:#0a0d09;border:2px solid var(--line);border-radius:12px}.reel.spin{filter:blur(1.5px)}
.dnum{font-size:64px;font-weight:800;margin:6px 0;min-height:78px}
.gres{min-height:34px;text-align:center;font-size:20px;font-weight:700;margin-top:10px}.gres.win{color:var(--ac)}.gres.lose{color:var(--warn)}.gres.even{color:var(--dim)}
body.hv-tilt .card:hover{transform:perspective(700px) rotateX(5deg) rotateY(-8deg) translateY(-6px) scale(1.05)}
body.hv-pop .card:hover{animation:hvpop .45s forwards}@keyframes hvpop{0%{transform:none}40%{transform:translateY(-16px) scale(1.07)}70%{transform:translateY(-3px) scale(1.04)}100%{transform:translateY(-7px) scale(1.05)}}
body.hv-glow .card:hover .art{animation:hvglow 1.1s infinite}@keyframes hvglow{0%,100%{box-shadow:0 0 0 2px var(--ac),0 0 10px rgba(var(--acrgb),.3)}50%{box-shadow:0 0 0 2px var(--ac),0 0 36px rgba(var(--acrgb),.85)}}
body.hv-shine .art{position:relative;overflow:hidden}body.hv-shine .art::after{content:"";position:absolute;inset:0;background:linear-gradient(110deg,transparent 35%,rgba(255,255,255,.3) 50%,transparent 65%);transform:translateX(-120%);pointer-events:none}body.hv-shine .card:hover .art::after{transform:translateX(120%);transition:transform .7s}

body{margin:0;background:radial-gradient(900px 420px at 50% -15%,rgba(var(--acrgb),.10) 0,transparent 65%),var(--bg);color:var(--tx);
 font:14px/1.45 "Segoe UI Variable Text","Segoe UI",system-ui,sans-serif;display:flex;flex-direction:column;height:100vh}
header{display:flex;align-items:center;gap:16px;padding:12px 24px;border-bottom:1px solid var(--line);flex-wrap:wrap;background:rgba(5,6,5,.88);backdrop-filter:blur(8px)}
.brand small{font:500 12px "Segoe UI",sans-serif;color:var(--dim);margin-left:6px;letter-spacing:0}
.brand{display:flex;align-items:center;gap:10px;font:italic 800 25px/1 "Segoe UI Variable Display","Segoe UI",sans-serif;letter-spacing:1px}
.brand b{color:var(--ac);font-weight:800}
.brand img{height:42px;width:42px;object-fit:contain;mix-blend-mode:lighten}
nav{order:5;flex-basis:100%;display:flex;gap:10px;align-items:stretch;overflow-x:auto;scrollbar-width:none;padding-bottom:2px}
nav::-webkit-scrollbar{display:none}
.ng{display:flex;align-items:center;gap:2px;padding:3px 4px 3px 12px;border:1px solid var(--line);border-radius:14px;background:linear-gradient(180deg,rgba(255,255,255,.035),rgba(255,255,255,.01));flex:none}
.ng.r{margin-left:auto}
.ngl{font-size:9.5px;font-weight:700;letter-spacing:.14em;text-transform:uppercase;color:var(--dim);opacity:.65;margin-right:8px;white-space:nowrap}
nav button{display:flex;align-items:center;gap:7px;background:none;border:0;color:var(--dim);padding:7px 12px;font:inherit;font-size:14px;cursor:pointer;border-radius:10px;white-space:nowrap;transition:background .15s,color .15s,box-shadow .15s}
nav button svg{width:16px;height:16px;fill:none;stroke:currentColor;stroke-width:1.8;stroke-linecap:round;stroke-linejoin:round;flex:none}
nav button:hover{color:var(--tx);background:var(--panel)}
nav button.on{color:var(--ac);background:rgba(var(--acrgb),.14);font-weight:600;box-shadow:inset 0 0 0 1px rgba(var(--acrgb),.45),0 0 14px rgba(var(--acrgb),.18)}
body.light nav button.on{color:#fff;background:var(--ac);box-shadow:none}
@media(max-width:1480px){.ngl{display:none}.ng{padding-left:4px}}
#tools{display:flex;gap:8px;align-items:center;margin-left:auto}
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
.card:hover .art,.card:focus-visible .art{box-shadow:0 16px 32px rgba(0,0,0,.55),0 0 0 2px var(--ac),0 0 26px rgba(var(--acrgb),.30)}
.art img{position:absolute;inset:0;width:100%;height:100%;object-fit:cover}
.play{position:absolute;left:0;right:0;bottom:0;padding:34px 12px 12px;background:linear-gradient(transparent,rgba(8,10,18,.92));font-weight:600;z-index:2;
 opacity:0;transform:translateY(10px);transition:opacity .2s,transform .2s}
.cog{position:absolute;top:8px;right:8px;z-index:5;width:30px;height:30px;border-radius:50%;border:1px solid var(--ac);background:rgba(5,8,5,.85);color:var(--ac);font-size:16px;cursor:pointer;opacity:0;transition:opacity .2s,transform .2s}.card:hover .cog,.cog:focus-visible{opacity:1}.cog:hover{transform:rotate(60deg)}
#gm input[type=range],#gm input[type=checkbox]{accent-color:var(--ac)}#gm{display:none;position:fixed;inset:0;background:rgba(0,0,0,.7);z-index:40;align-items:center;justify-content:center;padding:20px}#gm.on{display:flex}#gm .box{background:var(--bg2,#0c110c);border:1px solid var(--ac);border-radius:14px;max-width:480px;width:100%;max-height:90vh;overflow:auto;padding:20px}
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
body.light .btn.pri{color:#fff}
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
.msg.new{border-left:3px solid var(--ac);background:rgba(var(--acrgb),.05)}
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
@keyframes spGlow{0%,100%{filter:drop-shadow(0 0 0 rgba(var(--acrgb),0))}50%{filter:drop-shadow(0 0 28px rgba(var(--acrgb),.8))}}
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
#ubanner{display:none;align-items:center;gap:12px;padding:10px 24px;background:rgba(var(--acrgb),.09);border-bottom:1px solid var(--ac)}
.empty{max-width:520px;margin:40px 0;color:var(--dim)}
footer{padding:9px 24px;border-top:1px solid var(--line);color:var(--dim);font-size:13px}
#toast{position:fixed;bottom:48px;left:50%;transform:translateX(-50%);background:var(--panel);border:1px solid var(--ac);padding:9px 18px;border-radius:10px;opacity:0;transition:opacity .2s;pointer-events:none;z-index:9}
#toast.on{opacity:1}
@media(prefers-reduced-motion:reduce){*{transition:none!important}.card:hover{transform:none}}
.arwrap{position:relative;max-width:1100px}.arwrap canvas{width:100%;height:auto;display:block;background:#0f121a;border-radius:12px;border:1px solid #2a2f3a}
.arov{position:absolute;inset:0;display:none;align-items:center;justify-content:center;background:rgba(8,10,16,.84);border-radius:12px;overflow:auto}
.arbox{padding:16px;max-width:100%;text-align:center}.arcards{display:flex;gap:10px;flex-wrap:wrap;justify-content:center}
.arcard{width:150px;padding:12px;border:1px solid #333b4a;border-radius:10px;background:#171b25;cursor:pointer;color:#e8ecf2}.arcard:hover{border-color:var(--ac)}
.arcard.dis{opacity:.45;pointer-events:none}.gshell>h2,.gshell>.sub{display:none}.gbar{margin:0 0 10px}
.gshell .arwrap{width:min(1100px,100%,calc((100vh - 250px)*1.6));margin:0 auto}
.gshell .dim{text-align:center}
.gopen{animation:gopen .8s cubic-bezier(.2,.8,.2,1) both}
@keyframes gopen{0%{opacity:0;transform:scale(.9) translateY(16px);clip-path:circle(6% at 50% 45%);filter:blur(6px) brightness(2.2)}60%{opacity:1;filter:blur(0) brightness(1.2)}100%{opacity:1;transform:none;clip-path:circle(150% at 50% 45%);filter:none}}
.arhub{display:flex;gap:22px;flex-wrap:wrap;margin-top:8px}
.gcard{width:380px;max-width:100%;border:1px solid var(--line);border-radius:18px;background:var(--panel);padding:14px;cursor:pointer;transition:transform .2s,box-shadow .2s,border-color .2s;outline:none}
.gcard:hover,.gcard:focus-visible{transform:translateY(-5px);border-color:rgba(var(--acrgb),.65);box-shadow:0 14px 36px rgba(var(--acrgb),.2)}
.gcard h3{margin:14px 0 4px;font-size:20px}.gfoot{display:flex;align-items:center;justify-content:space-between;margin-top:14px;gap:10px}
.gart{height:200px;border-radius:12px;position:relative;overflow:hidden}
.gsurv{background:radial-gradient(circle at 50% 55%,#6aae4a,#2f6a22 78%)}
.gmas{position:absolute;left:50%;top:50%;height:112px;transform:translate(-50%,-46%);filter:drop-shadow(0 6px 6px rgba(0,0,0,.4))}
.gring{position:absolute;left:50%;top:50%;width:0;height:0;animation:gspin 14s linear infinite}
.gring>span{position:absolute;left:0;top:0;width:0;height:0;display:block}
.gring>span>i{position:absolute;left:-14px;top:-14px;font-size:26px;font-style:normal}
.gcs{animation:gspin 14s linear infinite reverse;font-style:normal;display:block}
@keyframes gspin{to{transform:rotate(360deg)}}
.ginv{background:linear-gradient(#04050d,#1b1238 62%,#47244a)}
.gtile{position:absolute;width:34px;height:46px;border-radius:6px;border:2px solid #cfd6dd;animation:gbob 2.2s ease-in-out infinite}
.gtile::before{content:"";position:absolute;left:6px;top:14px;width:6px;height:6px;border-radius:50%;background:#fff;box-shadow:12px 0 #fff}
@keyframes gbob{50%{transform:translateY(14px)}}
.gcity{position:absolute;left:0;right:0;bottom:0;height:78px;background:repeating-linear-gradient(90deg,#080a16 0 26px,#0d1126 26px 46px,#080a16 46px 64px);box-shadow:inset 0 -26px 0 #15161f}
.gcity::before{content:"";position:absolute;inset:8px 0 26px;background:repeating-linear-gradient(90deg,transparent 0 9px,rgba(255,210,122,.8) 9px 13px,transparent 13px 31px);-webkit-mask:repeating-linear-gradient(0deg,#000 0 8px,transparent 8px 16px);mask:repeating-linear-gradient(0deg,#000 0 8px,transparent 8px 16px);opacity:.7}
.gmas2{position:absolute;left:50%;bottom:4px;height:62px;transform:translateX(-50%)}
.aric{font-size:30px}.arpr{margin-top:6px;font-weight:800;color:#ffd23f}
</style></head><body>
<div id="splash" aria-hidden="true"><div class="sp-logo"><img src="/logo.png" alt=""></div>
 <div class="sp-tag">Alle Spiele. <b>Ein Ort.</b></div>
 <div class="sp-bar"><i></i></div></div>
<header>
 <div class="brand"><img src="/logo.png" alt="" onerror="this.remove()"><span>D-<b>HUB</b></span><small id="ver"></small></div>
 <nav></nav>
 <div id="tools"><input id="q" type="search" placeholder="Spiele suchen"><button class="btn" id="rf" title="Neu einlesen: neu installierte Spiele erscheinen hier">↻</button><button class="btn pri" id="plus" title="Eigenes Spiel hinzufügen">＋</button><button class="btn" id="vw"></button></div>
<div id="hact">
 <button class="btn pri" id="updbtn" style="display:none"></button>
 <button class="hbtn coinchip" id="coinbtn" title="Coins und Shop" onclick="tab='Shop';render()"><span>🪙</span><b id="coinn">0</b><small id="cointitle"></small></button>
 <button class="hbtn" id="frbtn" title="Freunde" aria-label="Freunde"><svg viewBox="0 0 24 24"><circle cx="9" cy="8" r="3.5"/><path d="M2.5 20c.6-3.6 3.2-5.5 6.5-5.5s5.9 1.9 6.5 5.5"/><circle cx="17.5" cy="9" r="2.6"/><path d="M17 14.3c2.5.1 4.2 1.6 4.7 4.2"/></svg><span class="dot" id="frdot" hidden></span></button>
 <button class="hbtn" id="mlbtn" title="Nachrichten" aria-label="Nachrichten"><svg viewBox="0 0 24 24"><rect x="3" y="5" width="18" height="14" rx="2.5"/><path d="m3.5 7 8.5 6.5L20.5 7"/></svg><span class="dot" id="mldot" hidden></span></button>
</div>
</header>
<div class="pop" id="mlpop"><h3>Nachrichten<span style="flex:1"></span><button class="btn" onclick="api('action',{name:'clearinbox'}).then(()=>updChrome(true))">Leeren</button></h3><div class="body" id="mlbody"></div><div class="compose" id="mlcomp"></div></div>
<div class="pop" id="frpop"><h3>Freunde</h3><div class="body" id="frbody"></div></div>
<div id="lost"><div><b style="font-size:18px">D-Hub wurde beendet oder startet neu.</b><p class="dim">Nach einem Update öffnet sich automatisch ein neues Fenster.<br>Du kannst dieses Fenster schließen.</p></div></div>
<div id="ubanner"></div>
<div id="wd" style="display:none;position:fixed;inset:0;z-index:99;background:#050805;color:#ddd;align-items:center;justify-content:center;flex-direction:column;gap:14px;text-align:center;padding:24px;overflow:auto"><b style="font-size:18px">D-Hub startet nicht richtig?</b><div style="color:#9a9">Probier zuerst "Neu laden". Wenn das nicht hilft, "D-Hub neu starten".</div>
<div><button class="btn pri" onclick="location.reload()">Neu laden</button> <button class="btn" onclick="dbg()">Debug anzeigen</button> <button class="btn" onclick="fetch('/api/action',{method:'POST',body:JSON.stringify({name:'restartapp'})})">D-Hub neu starten</button> <button class="btn" onclick="document.getElementById('wd').style.display='none'">Schließen</button></div>
<pre id="wdp" style="text-align:left;max-width:900px;white-space:pre-wrap;font-size:12px;color:#9a9"></pre></div>
<div id="gm" onclick="if(event.target==this)closeCfg()"><div class="box" id="gmb"></div></div>
<main></main>
<div id="kfwrap" class="hide"><div class="bar"><span style="flex:1">KumpelFunk von Lerun3. Das Gespräch läuft weiter, wenn du den Reiter wechselst.</span>
 <button class="btn" onclick="api('connect',{store:'KumpelFunk'})">In eigenem Fenster öffnen</button></div>
 <iframe id="kf" title="KumpelFunk" allow="microphone; camera; display-capture; autoplay; clipboard-write; fullscreen"
  sandbox="allow-scripts allow-same-origin allow-forms allow-popups allow-modals allow-downloads"></iframe></div><div id="toast"></div>
<footer id="status">Bereit</footer>
<script>
window.__errs=[];
function __e(m){__errs.push(m);try{fetch('/api/jslog',{method:'POST',body:JSON.stringify({m:String(m).slice(0,400)})})}catch(x){}}
addEventListener('error',e=>__e((e.message||'Fehler')+' @'+(e.lineno||'?')));
addEventListener('unhandledrejection',e=>__e('Promise: '+((e.reason&&e.reason.message)||e.reason)));
async function dbg(){const w=document.getElementById('wd');w.style.display='flex';let t='';try{t=await (await fetch('/api/debug')).text()}catch(e){t='Server nicht erreichbar: '+e}document.getElementById('wdp').textContent=t+'\n\nJS-Fehler:\n'+(__errs.join('\n')||'-')}
setTimeout(()=>{if(!window.__ok)document.getElementById('wd').style.display='flex'},7000);
addEventListener('keydown',e=>{if(e.key=='F5'){e.preventDefault();location.reload()}if(e.ctrlKey&&e.shiftKey&&e.key.toLowerCase()=='d'){e.preventDefault();dbg()}});
const KF_URL='https://kumpelfunk.lerun3-funk.workers.dev';
const NAVG=[
 {l:'Bibliothek',t:[['Alle','Spiele','<rect x="3" y="8" width="18" height="10" rx="5"/><path d="M8 11v4M6 13h4"/><circle cx="16" cy="12" r=".9"/><circle cx="18" cy="14" r=".9"/>'],['Downloads','Downloads','<path d="M12 4v10m0 0-4-4m4 4 4-4M5 19h14"/>'],['Wert','Wert','<path d="M4 19V5m0 14h16M8 15l3-4 3 3 5-7"/>']]},
 {l:'Spielhalle',t:[['Arcade','Arcade','<circle cx="12" cy="12" r="7"/><circle cx="12" cy="12" r="1.8"/><path d="M12 2v4M12 18v4M2 12h4M18 12h4"/>'],['Gamble','Gamble','<rect x="4" y="4" width="16" height="16" rx="3"/><circle cx="9" cy="9" r=".9"/><circle cx="12" cy="12" r=".9"/><circle cx="15" cy="15" r=".9"/>'],['Shop','Shop','<path d="M5 8h14l-1 12H6zM9 8a3 3 0 0 1 6 0"/>']]},
 {l:'Community',t:[['KumpelFunk','KumpelFunk','<rect x="9" y="3" width="6" height="11" rx="3"/><path d="M5 11a7 7 0 0 0 14 0M12 18v3"/>']]},
 {r:1,t:[['Patchnotes','Patch Notes','<path d="M7 3h8l4 4v14H7zM15 3v4h4M10 12h6M10 16h6"/>'],['Einstellungen','Einstellungen','<circle cx="12" cy="12" r="3"/><path d="M12 3v3M12 18v3M3 12h3M18 12h3M5.6 5.6l2.1 2.1M16.3 16.3l2.1 2.1M18.4 5.6l-2.1 2.1M7.7 16.3l-2.1 2.1"/>']]}
];
const HKS=['F7','F8','F9','F10','F11','F12','Strg+F9','Strg+F10','Alt+F9','Alt+F10','Strg+Umschalt+F9','Strg+Umschalt+F10'];
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

function visible(){return S.games.filter(g=>g.title.toLowerCase().includes(q)).sort((a,b)=>a.title.localeCompare(b.title))}

function readB64(f){return new Promise((res,rej)=>{const r=new FileReader();r.onload=()=>res(String(r.result).split(',')[1]||'');r.onerror=rej;r.readAsDataURL(f)})}
async function uploadCover(id,f){if(!f)return;if(f.size>8e6){toast('Bild ist zu groß (max. 8 MB)');return}
 const ext=(f.name.split('.').pop()||'').toLowerCase();await api('cover',{id,ext,data:await readB64(f)})}
const nid=t=>t.toLowerCase().replace(/[^a-z0-9]/g,'');
function openAdd(){
 $('#gmb').innerHTML=`<h2 style="margin:0 0 4px">Spiel hinzufügen</h2><div class="sub">Für Spiele, die D-Hub nicht von selbst findet.</div>
 <div class="panel"><div class="row"><span style="width:110px">Name</span><input id="an" style="flex:1" placeholder="z. B. Mein Spiel"></div>
 <div class="row"><span style="width:110px">Programm</span><input id="ae" style="flex:1" placeholder="Pfad zur .exe-Datei"><button class="btn" onclick="pickExe()">Suchen …</button></div>
 <div class="row"><span style="width:110px">Cover (Bild)</span><input type="file" id="ac" accept="image/png,image/jpeg,image/webp,image/gif" style="flex:1"></div></div>
 <div style="display:flex;gap:8px"><button class="btn pri" onclick="doAdd()">Hinzufügen</button><button class="btn" onclick="closeCfg()">Abbrechen</button></div>`;
 $('#gm').classList.add('on')}
async function pickExe(){try{const r=await (await fetch('/api/pick',{method:'POST',body:JSON.stringify({kind:'exe'})})).json();
 if(r.path){$('#ae').value=r.path;if(!$('#an').value){const n=r.path.split(String.fromCharCode(92)).pop().split('/').pop();$('#an').value=n.replace(/[.]exe$/i,'')}}
 else toast('Kein Auswahlfenster möglich: Pfad bitte eintippen')}catch(e){toast('Auswahl nicht möglich')}}
async function doAdd(){const t=$('#an').value.trim(),e=$('#ae').value.trim().replace(/^"|"$/g,''),f=$('#ac').files[0];
 if(!t||!e){toast('Name und Programm angeben');return}
 await api('action',{name:'addgame',title:t,exe:e});
 if(!S.games.some(g=>g.id==nid(t))){toast('Programmdatei nicht gefunden oder Name ohne Buchstaben');return}
 if(f)await uploadCover(nid(t),f);
 closeCfg();render();toast('Hinzugefügt')}
async function cvSet(id,f){await uploadCover(id,f);render();toast('Cover gespeichert')}
function closeCfg(){$('#gm').classList.remove('on')}
function openCfg(id){
 const g=S.games.find(x=>x.id==id);if(!g)return;
 const c=Object.assign({cross:false,cstyle:'cross',ccolor:'#5cff0c',csize:12,cgap:4,cthick:2,fps:false,kf:false,ping:false,cpu:false,gpu:false,ram:false,clock:false,spos:'tr',mp:true},(S.gamecfg||{})[id]||{});
 window._cfgId=id;window._cfg=c;
 const ck=(k,l,d)=>`<label class="row" style="cursor:pointer"><span style="flex:1">${l}${d?`<div class="dim">${d}</div>`:''}</span><input type="checkbox" ${c[k]?'checked':''} onchange="cfgSet('${k}',this.checked)"></label>`;
 const num=(k,l,lo,hi)=>`<div class="row"><span style="width:110px">${l}</span><input type="range" min="${lo}" max="${hi}" value="${c[k]}" style="flex:1" oninput="this.nextElementSibling.textContent=this.value" onchange="cfgSet('${k}',+this.value)"><span style="width:28px;text-align:right">${c[k]}</span></div>`;
 $('#gmb').innerHTML=`<h2 style="margin:0 0 4px">${esc(g.title)}</h2><div class="sub">Overlay und Einstellungen</div>
 <div class="panel"><div class="row"><span style="flex:1"><b>Spieltyp</b><div class="dim">Bei Mehrspieler-Spielen bleibt das Overlay in der Safe-Variante.</div></div><select onchange="cfgSet('mp',this.value=='mp');loadOvp()"><option value="sp" ${c.mp?'':'selected'}>Einzelspieler</option><option value="mp" ${c.mp?'selected':''}>Mehrspieler</option></select></div>
  <div id="ovp" class="dim" style="margin-top:8px">Prüfe …</div></div>
 <div class="panel"><b>Fadenkreuz</b>${ck('cross','Fadenkreuz anzeigen')}
  <div class="row"><span style="width:110px">Form</span><select onchange="cfgSet('cstyle',this.value)">${[['cross','Kreuz mit Lücke'],['plus','Plus'],['dot','Punkt'],['circle','Kreis']].map(([v,l])=>`<option value="${v}" ${v==c.cstyle?'selected':''}>${l}</option>`).join('')}</select>
  <input type="color" value="${c.ccolor}" onchange="cfgSet('ccolor',this.value)" title="Farbe" style="margin-left:8px;width:44px;height:30px"></div>
  ${num('csize','Größe',2,60)}${num('cgap','Lücke',0,30)}${num('cthick','Dicke',1,8)}</div>
 <div class="panel"><b>Anzeigen</b>${ck('fps','FPS','Lädt beim ersten Mal das kostenlose Intel-Tool PresentMon herunter. Braucht meist Administrator-Rechte.')}${ck('ping','Ping','Zur Gegenstelle des Spiels, sonst zum Internet allgemein.')}${ck('cpu','CPU-Auslastung')}${ck('gpu','GPU-Auslastung','Nur dieses Spiel, über die Windows-Zähler.')}${ck('ram','Arbeitsspeicher')}${ck('clock','Uhrzeit')}
  <div class="row"><span style="flex:1">Position der Werte</span><select onchange="cfgSet('spos',this.value)">${[['tr','oben rechts'],['tl','oben links'],['br','unten rechts'],['bl','unten links']].map(([v,l])=>`<option value="${v}" ${v==c.spos?'selected':''}>${l}</option>`).join('')}</select></div>
  ${ck('kf','KumpelFunk-Overlay (oben links)','Zeigt, wer spricht, und neue Chat-Nachrichten. Läuft nur, wenn KumpelFunk die Daten an D-Hub schickt.')}</div>
 <div class="panel"><b>Cover</b><div class="row"><input type="file" accept="image/png,image/jpeg,image/webp,image/gif" style="flex:1" onchange="cvSet('${id}',this.files[0])"><button class="btn" onclick="api('action',{name:'delcover',id:'${id}'}).then(()=>{render();toast('Zurückgesetzt')})">Zurücksetzen</button></div></div>
 ${g.versions.some(v=>v.store=='Eigene')?`<button class="btn" style="margin-bottom:10px" onclick="if(confirm('Aus D-Hub entfernen? Das Spiel selbst bleibt auf dem PC.')){api('action',{name:'delgame',sid:'${(g.versions.find(v=>v.store=='Eigene')||{}).sid}'}).then(()=>{closeCfg();render()})}">Aus D-Hub entfernen</button>`:''}
 <p class="dim">Das Overlay hängt sich an das Fenster des Spiels, das du über D-Hub startest, und liegt nur über diesem Spiel, nicht über dem ganzen Monitor. Es erscheint nur, solange das Spiel vorne ist. Es klappt im Fenster-, Randlos- und normalen Vollbildmodus von Windows, nicht im exklusiven Vollbild (dort stellst du im Spiel „Randlos“ ein).${S.ov&&S.ov.on?`<br>Verknüpft mit: <b>${esc(S.ov.target)}</b>`:''} <b>${S.hk.toggle}</b> blendet es ein oder aus, <b>${S.hk.close}</b> beendet oder startet es. Beides funktioniert auch im Spiel. Die Tasten änderst du unter Einstellungen → Allgemein. Wenn du D-Hub schließt, schließt sich das Overlay mit.${S.ov&&S.ov.err?`<br><span style="color:var(--warn)">Fehler: ${esc(S.ov.err)}</span>`:''}</p>
 <div style="display:flex;gap:8px;flex-wrap:wrap"><button class="btn pri" onclick="api('action',{name:'ovstart',id:'${id}'}).then(()=>{toast('Overlay läuft')});closeCfg()">Overlay jetzt starten</button>
 <button class="btn" onclick="api('action',{name:'ovstop'});toast('Overlay beendet')">Overlay beenden</button><button class="btn" onclick="closeCfg()">Schließen</button></div>`;
 $('#gm').classList.add('on');loadOvp()}
async function loadOvp(){
 const id=window._cfgId,el=$('#ovp');if(!el)return;let i;
 try{i=await (await fetch('/api/ovinfo',{method:'POST',body:JSON.stringify({id})})).json()}catch(e){el.textContent='Nicht lesbar';return}
 const row=(t,x)=>`<div class="row"><span style="flex:1">${t}</span>${x||''}</div>`;
 let h=row('<b>Safe-Variante</b><div class="dim">Overlay als Fenster über dem Spiel. Am Spiel wird nichts verändert.</div>',i.installed?'':'<i class="b ok">aktiv</i>');
 if(window._cfg.mp)h+=row('<b>Im Spiel installieren</b><div class="dim">Für Mehrspieler-Spiele gesperrt, wegen Anti-Cheat.</div>','<i class="b mute">gesperrt</i>');
 else if(!i.avail)h+=row('<b>Im Spiel installieren</b><div class="dim">Die Overlay-Bibliothek ist in diesem D-Hub nicht enthalten.</div>','<i class="b mute">nicht verfügbar</i>');
 else if(!i.target)h+=row('<b>Im Spiel installieren</b><div class="dim">Der Spielordner wurde nicht erkannt.</div>','<i class="b mute">nicht möglich</i>');
 else if(i.foreign)h+=row('<b>Im Spiel installieren</b><div class="dim">Im Spielordner liegt schon eine fremde dxgi.dll (z. B. ReShade). D-Hub fasst sie nicht an.</div>','<i class="b mute">blockiert</i>');
 else if(i.installed)h+=row('<b>Im Spiel installiert</b><div class="dim">'+esc(i.target)+'<br>Gilt für DirectX-11-Spiele in 64 Bit, auch im Vollbild.</div>','<button class="btn" onclick="ovDll(\'remove\')">Entfernen</button>');
 else h+=row('<b>Im Spiel installieren</b><div class="dim">Funktioniert auch im Vollbild, aber mit Risiko. Zielordner: '+esc(i.target)+'</div>','<button class="btn" onclick="warnInstall()">Installieren …</button>');
 el.innerHTML=h}
function warnInstall(){
 const id=window._cfgId,g=S.games.find(x=>x.id==id);
 $('#gmb').innerHTML=`<h2 style="margin:0 0 8px;color:var(--warn)">Achtung: Overlay im Spiel installieren</h2>
 <div class="panel" style="border-color:var(--warn)"><p>D-Hub legt eine Datei (<b>dxgi.dll</b>) in den Ordner von <b>${esc(g.title)}</b>.</p>
 <p>Anti-Cheat-Programme können das als Manipulation werten. Mögliche Folgen: Das Spiel startet nicht, wird blockiert, oder dein Konto wird gesperrt.</p>
 <p>Nutze das nur bei Einzelspieler-Spielen ohne Anti-Cheat. Es funktioniert nur bei 64-Bit-Spielen mit DirectX 11. Bei DirectX 12, Vulkan oder OpenGL erscheint nichts.</p>
 <p>Du kannst es jederzeit mit „Entfernen“ zurücknehmen. Spiel-Updates oder „Dateien überprüfen“ können die Datei auch wieder löschen.</p>
 <label class="row" style="cursor:pointer"><input type="checkbox" id="wk" onchange="$('#wgo').disabled=!this.checked"><span style="flex:1;margin-left:8px">Ich habe das verstanden und trage das Risiko.</span></label></div>
 <div style="display:flex;gap:8px;flex-wrap:wrap"><button class="btn pri" id="wgo" disabled onclick="ovDll('install')">Installieren</button><button class="btn" onclick="openCfg('${id}')">Abbrechen, Safe-Variante nutzen</button></div>`}
async function ovDll(op){
 const id=window._cfgId;let r;
 try{r=await (await fetch('/api/ovdll',{method:'POST',body:JSON.stringify({id,op})})).json()}catch(e){toast('Nicht möglich');return}
 toast(r.msg);openCfg(id)}
function cfgSet(k,v){window._cfg[k]=v;api('action',{name:'gamecfg',id:window._cfgId,cfg:window._cfg})}
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
 return Object.entries(S.errs).map(([s,m])=>{
  const b=s=='Steam'?`<button class="btn" onclick="api('connect',{store:'Steam'})">Steam-Datenschutz öffnen</button>`
   :s=='Epic'?`<button class="btn" onclick="api('connect',{store:'Epic'});toast('Legendary wird geladen oder die Anmeldung öffnet sich')">${S.legendary?'Epic anmelden':'Epic verbinden'}</button>`
   :`<button class="btn" onclick="api('connect',{store:'Galaxy'})">GOG Galaxy holen</button>`;
  return `<div class="panel" style="border-color:var(--warn)"><div class="row"><span style="flex:1">${esc(m)}</span>${b}<button class="btn" onclick="api('rescan',{});toast('Lädt neu …')">Neu einlesen</button></div></div>`}).join('')}
const ibadges=g=>[...new Set(g.versions.filter(v=>v.installed).map(v=>v.store))].map(s=>`<i class="b ${sc(s)}">${s}</i>`).join('');
function coverOf(g){const f=(S.covers||{})[g.id];if(f)return '/cover/'+encodeURIComponent(f);const st=g.versions.find(v=>v.store=='Steam');return st&&!S.demo?`https://cdn.cloudflare.steamstatic.com/steam/apps/${st.sid}/library_600x900.jpg`:''}
function libView(){
 const gs=visible();
 let h=`<h2>Meine Spiele</h2><div class="sub">${gs.length} Spiele</div>`;
 if(!gs.length) return h+(S.games.length?'<div class="empty">Keine Treffer.</div>'
  :`<div class="empty"><b>D-Hub hat auf diesem PC noch keine installierten Spiele gefunden.</b><br>Mit ↻ liest du neu ein, mit ＋ fügst du ein Spiel selbst hinzu.<br>Weitere Spielordner trägst du unter Einstellungen → Erweitert ein.<br><br><button class="btn pri" onclick="openAdd()">＋ Spiel hinzufügen</button> <button class="btn" onclick="api('settings',{demo:true}).then(render)">Demo-Daten ansehen</button></div>`);
 if(S.settings.view=='list') return h+`<table><tr><th>Titel</th><th>Quelle</th><th>Größe</th><th></th></tr>`+
  gs.map(g=>`<tr><td>${esc(g.title)}</td><td>${ibadges(g)}</td><td>${gb(Math.max(...g.versions.map(v=>v.size)))}</td>
  <td><button class="btn pri" onclick="play('${g.id}')">Spielen</button> <button class="btn" title="Overlay und Spieleinstellungen" onclick="openCfg('${g.id}')">⚙</button></td></tr>`).join('')+'</table>';
 return h+'<div class="grid">'+gs.map(g=>{
  const c=coverOf(g), img=c?`<img src="${c}" loading="lazy" alt="" onerror="this.remove()">`:'';
  return `<div class="card" tabindex="0" onclick="play('${g.id}')" onkeydown="if(event.key=='Enter')play('${g.id}')">
  <div class="art" style="--h:${hue(g.title)}"><div class="badges">${ibadges(g)}</div><span>${esc(g.title)}</span>${img}
  <div class="play">Spielen</div><button class="cog" title="Overlay und Spieleinstellungen" onclick="event.stopPropagation();openCfg('${g.id}')">⚙</button></div>
  <div class="name">${esc(g.title)}</div><div class="meta">${gb(Math.max(...g.versions.map(v=>v.size)))}${g.played>=60?' · '+(g.played/3600).toFixed(1)+' h gespielt':''}</div></div>`}).join('')+'</div>'}

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
 const gs=[...S.allgames].sort((a,b)=>(b.price||0)-(a.price||0)), sum=gs.reduce((a,g)=>a+(g.price||0),0), no=gs.filter(g=>g.price==null).length;
 const per=S.launchers.map(s=>[s,S.allgames.filter(g=>g.versions.some(v=>v.store==s)).reduce((a,g)=>a+(g.price||0),0)]).filter(x=>x[1]>0);
 return banner()+`<h2>Wert deiner Bibliothek</h2><div class="sub">Basis sind Online-Listenpreise ohne Rabatt: zuerst Steam, dann GOG, dann der Preisvergleich CheapShark (auch für Epic und Ubisoft, Umrechnung von USD). Eigene Preise überschreiben alles.</div>
 <div class="panel"><div class="stat">${eur(sum)}</div><div class="dim">${S.allgames.length} Spiele, jedes nur einmal gezählt${no?' · '+no+' ohne Preis':''}${S.pending?' · Preise werden geladen ('+S.pending+' offen)':''}</div>${S.perr?'<div class="meta" style="color:var(--warn);margin-top:8px">Preisabfrage fehlgeschlagen: '+esc(S.perr)+'</div>':''}
 ${per.length?'<div style="margin-top:12px">'+per.map(([s,v])=>`<i class="b ${sc(s)}">${s}</i> ${eur(v)} &nbsp; `).join('')+'</div>':''}</div>
 <div class="panel"><table><tr><th>Spiel</th><th>Stores</th><th>Preis</th><th>Eigener Preis (€)</th></tr>`+gs.map(g=>`<tr><td>${esc(g.title)}</td><td>${badges(g)}</td>
 <td>${g.price==null?'<span class="dim">–</span>':eur(g.price)}<div class="meta">${esc(g.psrc||'')}</div></td>
 <td><input type="number" min="0" step="0.01" style="width:100px" value="${g.manual??''}" placeholder="${g.price??''}" onchange="api('price',{id:'${g.id}',price:this.value===''?null:+this.value}).then(render)"></td></tr>`).join('')+'</table></div>'}

function setView(){return setView0()+uninstPanel()}
function uninstPanel(){
 const T2=S.uninst;
 return `<div class="panel" style="border-color:rgba(255,80,80,.35)"><b>Deinstallieren</b><p class="dim">${T2?'Entfernt D-Hub von diesem PC, inklusive Desktop-Verknüpfung und Autostart. Deine Spiele und Launcher bleiben unberührt.':'Diese Version wurde nicht über das Setup installiert. Zum Entfernen einfach die Datei D-Hub.exe samt Ordner löschen.'}</p><button class="btn" ${T2?'':'disabled'} onclick="if(confirm('D-Hub wirklich deinstallieren?')){api('action',{name:'uninstall'});toast('Deinstallation startet …')}">D-Hub deinstallieren</button></div>`}
function setView0(){
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
  row('Start-Reiter',sel('starttab',['Alle','Downloads','Wert','KumpelFunk'].map(x=>[x,x=='Alle'?'Spiele':x]),T.starttab||'Alle'))+
  row('Mit Windows starten',chk(S.autostart,"api('action',{name:'autostart',on:this.checked}).then(render)"))+
  row('Preise automatisch laden',chk(T.autoprices!==false,"api('settings',{autoprices:this.checked})"))+row('KumpelFunk beim Start verbinden',chk(T.kfauto!==false,"api('settings',{kfauto:this.checked})"))+row('Updates automatisch suchen',chk(T.autoupdate!==false,"api('settings',{autoupdate:this.checked})"))+row('Updates automatisch installieren (beim Start)',chk(T.autoinstall!==false,"api('settings',{autoinstall:this.checked})"))+row('Overlay ein-/ausblenden (Taste)',sel('hk_toggle',HKS.map(x=>[x,x]),S.hk.toggle))+row('Overlay beenden/starten (Taste)',sel('hk_close',HKS.map(x=>[x,x]),S.hk.close))+row('Startanimation zeigen',chk(T.splash!==false,"api('settings',{splash:this.checked});try{localStorage.setItem('dhub_nosplash',this.checked?'':'1')}catch(e){}"))+'</div>';
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
  row('Datenordner öffnen',act('opendata','Öffnen'))+`<div class="row" style="display:block"><b>Zusätzliche Spielordner</b><p class="dim">Ein Ordner pro Zeile. Jeder Unterordner mit einer .exe zählt als Spiel. D-Hub schaut außerdem selbst in C:\\Games, D:\\Games und Spiele-Ordner.</p><textarea id="gd" style="width:100%;height:70px">${esc(T.gamedirs||'')}</textarea><button class="btn pri" onclick="api('settings',{gamedirs:$('#gd').value});toast('Gespeichert, liest neu ein …')">Speichern</button></div>`+row('Fenster neu laden (bei schwarzem Bild)',`<button class="btn" onclick="location.reload()">Neu laden</button>`)+row('Debug-Info anzeigen (Strg+Umschalt+D)',`<button class="btn" onclick="dbg()">Anzeigen</button>`)+row('D-Hub neu starten',act('restartapp','Neu starten'))+row('Preis-Cache leeren',act('clearprices','Leeren'))+'</div>'+
  `<div class="panel"><b>Diagnose</b>${S.diag.map(d=>`<div class="meta">${esc(d)}</div>`).join('')}</div>`;
}
function render(){
 applyPrefs();applyCoins();
 const key=S.launchers.join(',');
 if(key!==navKey){navKey=key;$('nav').innerHTML=NAVG.map(g=>`<div class="ng${g.r?' r':''}">${g.l?`<span class="ngl">${g.l}</span>`:''}${g.t.map(x=>`<button data-t="${x[0]}"><svg viewBox="0 0 24 24">${x[2]}</svg>${x[1]}</button>`).join('')}</div>`).join('');
  document.querySelectorAll('nav button').forEach(b=>b.onclick=()=>{tab=b.dataset.t;render()})}
 if(!['Alle','Shop','Gamble','Arcade','Downloads','Patchnotes','Wert','KumpelFunk','Einstellungen',...S.launchers].includes(tab))tab='Alle';
 document.querySelectorAll('nav button').forEach(b=>b.classList.toggle('on',b.dataset.t==tab));
 $('#ver').textContent='Beta '+S.version;
 $('#tools').style.display=isLib()?'flex':'none';
 $('#vw').textContent=S.settings.view=='cover'?'Als Liste':'Als Cover';
 const kf=tab=='KumpelFunk';
 $('#kfwrap').classList.toggle('hide',!kf);$('main').style.display=kf?'none':'';
 if(kf){if(!$('#kf').getAttribute('src'))$('#kf').src=KF_URL}
 else if(tab=='Arcade'){arcRender()}else{$('main').innerHTML=isLib()?libView():tab=='Shop'?shopView():tab=='Gamble'?gambleView():tab=='Patchnotes'?patchView():tab=='Downloads'?dlView():tab=='Wert'?valView():setView();if(tab=='Gamble')gRefresh()}
 if(tab!='Arcade'){arStop();ivStop();arcMode=null}
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
function applyCoins(){
 const c=S&&S.coins;if(!c||!$('#coinn'))return;const b=document.body;
 const th=c.catalog.find(i=>i.id==c.active.theme)||c.catalog[0];
 if(!b.classList.contains('light')){b.style.setProperty('--ac',th.data[0]);b.style.setProperty('--acrgb',th.data[1])}else{b.style.removeProperty('--ac');b.style.removeProperty('--acrgb')}
 [...b.classList].filter(x=>x.startsWith('hv-')).forEach(x=>b.classList.remove(x));b.classList.add('hv-'+c.active.hover.slice(2));
 $('#coinn').textContent=c.balance;const t=c.catalog.find(i=>i.id==c.active.title);$('#cointitle').textContent=t&&t.id!='ti_none'?'· '+t.name:'';
 const last=c.feed.length?c.feed[c.feed.length-1].id:0;
 if(window._fid===undefined)window._fid=last;
 else if(last>window._fid){c.feed.filter(f=>f.id>window._fid).forEach(f=>toast(f.msg));window._fid=last}
}
async function coinOp(op,id,extra){
 const r=await (await fetch('/api/coin',{method:'POST',body:JSON.stringify(Object.assign({op,id},extra||{}))})).json();
 S.coins=r.coins;if(r.msg&&!(extra&&extra.game))toast(r.msg);applyCoins();if(tab=='Shop')render();return r}
function shopView(){
 const c=S.coins,sec=(cat,title)=>`<h3 style="margin:24px 0 10px">${title}</h3><div class="shop">`+c.catalog.filter(i=>i.cat==cat).map(i=>{
  const act=c.active[cat]==i.id,own=c.owned.includes(i.id);
  const btn=act?'<i class="b ok">Aktiv</i>':own?`<button class="btn" onclick="coinOp('equip','${i.id}')">Ausrüsten</button>`:`<button class="btn pri" ${c.balance<i.price?'disabled':''} onclick="coinOp('buy','${i.id}')">🪙 ${i.price}</button>`;
  return `<div class="item">${cat=='theme'?`<span class="sw" style="background:${i.data[0]};color:${i.data[0]}"></span>`:''}<div style="flex:1"><b>${esc(i.name)}</b><div class="dim">${esc(i.desc)}</div></div>${btn}</div>`}).join('')+'</div>';
 const done=c.milestones.filter(m=>m.done).length;
 return `<h2>Shop</h2><div class="sub">Coins gibt es nur in D-Hub zu verdienen. Sie haben keinen Geldwert und lassen sich nicht kaufen.</div>
 <div class="panel"><div class="row"><div style="flex:1"><span style="font-size:26px;font-weight:800">🪙 ${c.balance}</span><div class="dim">Serie: ${c.streak} Tage · Insgesamt verdient: ${c.total}</div></div>
  ${c.daily_ready?`<button class="btn pri" onclick="coinOp('daily')">Täglicher Bonus: +${c.daily_amount}</button>`:'<i class="b mute">Heute abgeholt, morgen wieder</i>'}</div>
  <div class="row"><div style="flex:1"><b>Spielzeit heute</b><div class="dim">Alle 10 Minuten Spielen: +2 Coins, höchstens ${c.play_cap} pro Tag.</div></div><div style="width:160px"><div class="bar"><i style="width:${100*c.play_today/c.play_cap}%"></i></div><div class="dim" style="text-align:right">${c.play_today} / ${c.play_cap}</div></div></div></div>
 ${sec('theme','Farbthemen')}${sec('hover','Hover-Effekte für Spiele')}${sec('title','Titel')}
 <h3 style="margin:24px 0 10px">Meilensteine (${done}/${c.milestones.length})</h3><div class="shop">${c.milestones.map(m=>`<div class="item" style="opacity:${m.done?.55:1}"><span style="font-size:18px">${m.done?'✅':'⬜'}</span><div style="flex:1">${esc(m.label)}</div><b>🪙 ${m.reward}</b></div>`).join('')}</div>`}
/* ===== ARCADE (Brotato-Stil) ===== */
const ARS={body:'data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAALoAAADDCAYAAAA1Iwv4AACxLklEQVR42ux9d4BdVbX+t/be597pLclMpk8KEBJ66G0CKE26TEB9goqoD9GfSrEzieX5FH3qU3yCqIiVDEgRRaQkQ+9ISSAKKTPpfWYy7Z699/r9cdo+Z2ZCeGJ7ct+LpMzcOfecddZZ61vf+j7Cm6/xX50Q7fMg5s0DvqigrRn3qyqaPo7avY9H0fBGHNd0uMDoKj5c5VFfUstcWg1RUgHu78Veo/1oIAErCCJXTevKmrF8aAA0soPtUA9ICTxri7Bi49OQehTPbxZYs+wybAbQl/2hixhyKUDLusBd82EB8JsXbNcvevMUOLHdCYF5EAvmwRLBpiMaNYdegX1LqsUxVXNoqiYcVFpO9SpnGoqqmfIl5ElJkCAQLBgcn1x2TnbwdyL+XfB1wVcYA5BPKIwAhVFVYN9bVxiidVzQz65/tvCnzcvsww/9J14AUIgvoACuMlBvBv2bgb7LV8ciyI4O4DwJw05oN5yH5kPehUOLq+XxnpJHVTTrxtJqmlxWAkgAFgQNwMDCgAELw4bBRGAGETknN/gzhb9NopuDmBRMIAKTIAgJEIQUkPAgIUAANEbgo2+jRaGfVusRemxwu3nolUfxxGOfwlNAcFOSAK66D2rhElgsTN+obwb6v+KLQR2AWARYoiQD7v81HNC0tzi9cSYfnS/FUdXNKM2BYCChYaEBy2ALzQjimIgRBzVxGLgcBK5zhsNczgi/xIIASxwcjBAAEXFwLwiAwCwYDMsA2FqwtQwmSCnZK4IAoLBjK9mRAf/ZHTvpt8t/g8ef/oy5K8roixiyqwvomg/zZpj/iwV6J0PMAWg+JRd//0+ro2Yeyu+auq86orjaHlBaY0BgGADahwGBCRAEQRTmZeYwYtmtR4IwZQ7qEQtmApgFMYFISAEAksCQsFDgsMwRkJAQ8MCwYFgY6OBJYYKwJQJI2vCCKUjkABAMDAQ0JAijsOhfbV9e9xzd9tyD6vqlXx999c3w/hcL9I5FkLe8AyZuKI9H3Ts+j9Mr6+n9FXXysKoqAR8EHwbWsmbLBIYAMxERhABABGIKoo4By0kdzgwmCCskGAJCgoWM74EgtP2CRGE7s7Z6x+B6pqpJ4o92SAxs38zYuYVQPdV7vnoa92zf6NNgn89D2wF/ALA+4BUB+UqLkskC5dUeKmskXrrPHM85U1IxxdopbbDDO8S+Xjma8xUyv7OPMbi1cNeG5/GTPz+F3y67BoOgf+3aXf1fD/BFHbAUZvCjv4vDZhwu31/VzKdX11KdB8YoDAaN0ZZJEJEgkCICgnScNJMUZW8mWAu2IEuSGGCZF0R5QAoIDI9YjGyjgZ192ALGs9tW0hYaFs8PDHDP5ud0r7ejZFX3T/oIwPb00Y7sxieyAEajP1yX+bc8amryB54/VN10jD6gdQ/RyOCTy6fwRhCWdHZCLPwXrtvp/3iAMwAc/3Oc1bCXd3ndTHHUpEqLAgwKlg1bkCAIik5FGMgc5uIoYwsQ2DKDYFmAPMFCQAAARvsFBjfyxsFt+vnCDtE9PIolD/+PXr3291gPTFAfE8AWoss5/0vda7Ek9R8AwLzMb+Zk0JVsM/3m6/9woHd2QixYEAAfAPDWW+TZbfuKy2rb6KhSjzAMZqutBVgEdUjmLMRZW8ToCAFWCoucIJkDMDgC9K+lzYOb5JMD6/SSjUvx2P2deimAbakTS4C1EAsAgSXAss3g2UvBCxfEuCO/4deSgc4FoGVzQLOngJYA6D4O5k3I8f9OoNMihoiazPZr5Vl7HCOuqN/DHplXhGHAss9RR5mJDQ6igDioVYhgLSwLtkpIVYKg2t6xyt+2/VXcZTT/8EeXmGewIj3IYYZYsARi2WZw11IwFoL/ggCjXWJGb77+9QK9YxHkzefBMAP7fxcH7LO/+G7rweqooiJg1GrLFiAK4Og4iYclSpx6GQHiYdgKAVaKpYRC31qleaf9zZal6pauzw/eN7QMG+BOJ5eAlm1+XYMacs65aAeA9nZ0d3dnv3dXkKBMfX4Am9rbqbu7O7oJ3F9vvv7pA51BixBn8YqO+2lh837y0upJSvmwRvuGiFgQITOFJDBFnWYQd5atgSBRIkAwQN9qLN+5Wfzw8R/gzmd/WHjJzdrzu0BdHbC7gWJEQS06Ojq4q6uLo8HO7oAElZWVZdEf+voQMQEsgP7dfA/RAVB4E0Q/m98M9H+yLB4NQo7+Lt4x5wzx+cZm7G1BPOITSwkhmDNXlWIUhQSBmGEZhpSkYggx4ltseUn/ce2TdO0d79c3RDAIM8R8gLqwW8Etwl88QVYub2lpmQyg2Pj+0Z7nAczF2trjpZIkSRAE2Gq7B8BTGcTMTNbG94dWUj4JEpoEsTWGrLU7pFIPWmtJa/2qUmrFjh07+vr7+7eNd73b29vlv2Lg0z/h0EctJOiSs1F/5mfzX5+2D7+zOG8wbFiDoYQIrx6HZQk4KL8dVMUyrFCwZSA1MqTQvwZ/2LS88I2fn2H+EJ2Vzvt3e5ROTnC7X5trqa/fl4U4Rkq5JwMHEVEDETUCQa9AlJROweFR/OhgBpgcfDPsH8KPFJdd0bcH2D4DzBbAJgJ6ALyore1h5kdHR0ef27Rp08ZUwujokF1dXfhXCHr6Z2IT8oKgVzzmv3Hefmd636hrUY2jMIa1JSGsiFGTMHtHVQtzULJYJmYBkxdSSRD6Vtr7ep4o/Oei83FvxBU59j6o3UQqogCPM3dzc/MMYj5ZCHE0mA8nKduEEDEdgDmc5wNgZk2IJv4RM4bD/+fwFkj6iGASy4hvJqLUcyosyiSH49v4RgLAlmGs3WaNWWqYHxNC3DE0NPTc1q1bBzK1/+spr94M9L9mqXLWH+i/Zh1OHy8tFxgy0IJYiWj0TmEVzk6TGaRzwLJVnhQ55DCwXrzc8/Dwt3/Rob+fqr13nxciowBvqqio8aqqToEQF5GgIwSJImZGUG5wXMJQyIuJu+CgWQgDkhNkk6MbMwreKGtHN0H0YArKrxiXj4ZaEZ2GYSPWGAECRIJIQAgRIE2M9cx8L4AfrFy58jEAfqbZNW8G+t/wNfcpeE8fDH/qZWg97b3y561z6KhRsDU+B6PI8LFNILBI0BQOURbLzCRgy4WQA1toaNUf8T8/v2D0KqzHEDNofhfE6whwEWW8abW1dSgu/ncS9EEh1FQihrEWsGxsQGIUBAgIikuROK3Dyddx3g7HU5wEPcCAoLikYesENThuqJkino1DDQ5LnbiuITARWYBYEEkSgogI1lpYa5eztb8a9f1frlu3bvlET6w3A/2v9FrEkPMJ5ojv4LQDz8jd0NJCkwa11YatEiIKjpATG0d3xB5ksIXxlJISAhueNt0v3s8f677S/yMRcOz9UN3HQb+Ow4myOLW0tFwphfiElLLWBgRDE0aUAEAcHge5sR3QG1PcdKaItEWIS2/3a1yymBvAYyD14AYhpwKKyp3kjxSfJwr+jZnZAiwIAfnBGOszsMj3/R+uXbt2sfO5/+lrePpHhw5PvUt9cM7h4jsVVdYb9q2WJFSU1RicPO/jmjUIBgPocilV/1o5svnP/O8/Om70JwC4czHUwtc3LYwzW0NDwwGekt+WUh0bBJ3VIJJJTIdlBkcBGf01R2T0aDwVBh2lPjQzIIVAyHQcBzGKeb5hAHPSi4ASqiOSGyQgE1P8s13mJQd0YTDYMhMLQVIICcsWRttHfO1/ee3atb/LlmtvBvobFORCga0BznzA+9o+R3tXKNLs+5qlJBE1lu7sJcrqHF0yQZwXSmz5k3m4+0f+h//4VTzHDFqwAPQ6iU1xUm5tbf04AV8iQom10CTCAKdszs3sFXEKJEnKEg6wShHdGLAgEhgtjMJoAykltNYwxoABSBFQekFBoJqApB5zdASJOIiZLZgBIYKaXIb/zeVy8HK58GstrLVwCyUiYmZYIiYhpOCApfnzoaGhz27YsGH1P3N2V/9oyIpQsNag/IJl3pen7S0/YmF8raGkkCIIaLgVbXjKw4xl2eY9KcyIpFce52/+ZJ7/CQDoXAxFwUIQTzDUsRPU44y5c1Xbli2/kFKea6yBtWyEIOWWDOzGeXTTBU2Cw32hOBvHtyoBNsrwIAgiXHHlldhnzj4YGBiAIILvazAYUkpIGazgWWvAxsJG9XxyUgL6sLXBzWA0BgZ2YtPmzVi7Zg3+/MqfsWrlKmzdvgVKKpSVl0FJFdfzzEyCIBkEY4wVRPCUeldpaclJra2tV6xevfqGf9bsTv9IhKwvfBGWLSrOuNe7+4AT1OHG+BqWlYwe5Qm0kEIhGARrYco8kn2bafDFP+hLf/dv9oZOhsACYIIsvquLJQDw3Llz1fatW38tlTxNa62ZA+KLO211g9iFEVPD15CVnpTXSVkRfSSpJIwxmD59Bq7/4fVobW19w8/x8PAwVry6Ao8++gh++7u78OSTT8IvjKC6qhoggtE6PHCbMM+YNREpQQRtzG3brH1fX0/P9vb2dtXd3a3fDPTXG+RfgOVqVLzzN97vZx4pj9BW+8TsCadhQwi3RU9bhg0wYkCXKKE2vkirX36QT7vnEv/FDzwF77qDJ8ziEoBuamoq1lrP27Bhw++deiMa/si21tZbpZSnWWt8IvISKJuTDblUwIfZmmOyQVLEuHBg8jCI63oAUEpiYGAA5WWV+MH11+PY9mMwNDSMXM7L9J7Oqp4zYKIASMxQMZ07O9jZi//87DPP4ue/+DnuuO02FAoFlJSWoFDwg5kTp1peBthKqaQx5tXRQuHd69ate7QdUN2AfjPQd7NcoS/C8h4ov/jXlXfPmG2P6DeDmgAVFMAhOzzc7iGIYNJJApYZitgvFvBeXabvvPbt9j14GVs7GLKLxs3WMTzY0tJypLV8tWJ5yaq1q55PujkwAK+ttfVWIeht1lgfRF6AaITB6+LZ0cQyDGtK8M4I40yhIO5TKb4VQtTF2qA2Hxocgqc8XP+jH+KtJ54IrTWUUmDLqWY2Oj9BsxoPm5BmaCLeY0W4FQUb0CCipveJJ55AZ2cnnnnmGVSWl8HXGoLEmOaJAS2lVMzo90dHr+hZu/a6sPz9hw92+Xevyb8UlCv/9ofSP8zar+iIQVPQkqCiyV4U1KmLF3Z1QgpdQsJ7/i7z2x8fZc6hrdjZsQiya59xg1yFpUqupaXl01KIn1lrr+xZ23Ofk8UFgPz0trZbpFKnaWO0EOTFgUUJxMduznQxcUpGshEqRM62kptRieJnVPxelhn5fA6WGXfeeSdmztwDs2bNgjEmoQhE3xfefDasyWPA1SboimULq00AwlOA6AgpABF+n7Vobm7G2WefjVUrVuGJJ55EaWlpWIoRSEQdN0EIEsxsBFGxyuVPrygrG9rR1/fQ3LlzvfXr19s3A30iDvkSiEWdKD7n/vwf9j1SHDFohrQgqyjGVShGFVJIi2XOCTY5kOpdaj9//dH+pcwwCxhi2aVj6nGKsk7zlCkzKidV3+wp770Ff/Tra9eu+2Z0A7S3t6vVq1eblpaW73o5751a64IUwnPr8HjnSFAq0DKVd6pBpExwj/8wDX8vKMykDM/LgYhwxx13YOrUqdh3v31hjAmQlCD64vIiQldIUPx795dUEkJKCEHxDSBC7rIQAtrXyHl5nHHWGdiwfj0ef+wxlJWXJ/d+/PQggCCYmRlslPJOKisrG3r55ZcfDM+jfbN0yfzcToZcSNDnPZ67Zvah6pKCLfgC7IUhBGZKJoCUVB6WwZ6UpgSk1jzvX/nt/QtXMwco3Tjswhjra2yc+kFP5b8ipagu+IUne3vXHt7e3i66u7tNVLM3Nja+u6gof6M11gfgjVfnchxczhSTyMHCyUFeeMzRJAwV61RL5EB8yVNDKYmtW7fimGOOxW233Q4TljbRS2sNKRVuueVm/PrXv0Z5eRm01jEsSSAUFRehdkot9tl3XxxxxBFoa2sDABgTvBfb4BitsWBiCEG44N0XYPH996OyshIF33emue55YAbISCnVaKFwZU9Pz9Vz58J7+umYSvAmvNi5OAjyk39BX9vzUFwyaoZ9AnlRn+nKR8RbQAwQG3ikbBGEWr2scOV39y9c/YGn4E0AHUaYr2ppafqiJ71PcqxUQZcDsN3d3VGEmZaWlnpB+A4by8wsxw50nH21kEKYwIqcyulj7jaX1JXUuw7izjHcyJzcOMZY+FrjnHPOCfdMOfUMjsqpX/3yl/jD3b9HdXUVtAnKGBv+N6rZhZCorq7GvHnz8PHLPoE9Zu4Bv1AIbxyCVALGWpCQ+MY3v4nTTj0V27dvh5QSxmj3wkQ/m8AsrTXa83Jfa2hoGHz66XXf+0et2f/mGb19cTB6P/FXubcfdIa6WeWHjdEQQhJFOZyIIEgiGqXbEO3wpPWLGN6KpebK6/a1V0eU3V1Ah7K1ufk25anTrDUjQsgiX5tre3p6PhQiBiasy9HW2rJYSnWM0TqY3Li0WTedE42TnRmU/sswYwePGeF0udmChTPYRvT+QghYa5DPF+Hee+9DY1MjjA5Kl+jmEEJgw4YNOPmkkzC4c2eQoTkZIgVIT9B4MgDta+zYsQOVlVX49re/jdNOPw3+aAHK8+IJqu/78DwPN3fdjI9+5KOoqq6E7/vx5JacKW7MISOyzDw6PDLSvmHDhqf+EXF2gb8xC/GB46H3+4J34Kx5+V+UFENbI4RURGP2wNgGF80GB6mk9IuR81553H7tun3t1dcyvAmCXAEwkyZNmjWtra3by3mnGWNHAZEzxq4bHBy8AoBwgty0NTf/m+fljrHWaBJCkEOSikfuFNTmiFGNZCwfU2kT8jiIRIKfx0iJ04RGKI57wzg/k4gwNDSMY489NghyY4ImMnwsWR3E0aOPPoYtW7bCy+VgrIExFsaY5Je10NpA+xrMQHV1NYaHh3DRRe/Ho48+BuV50EYjYlxKKeEXCjjl1FOw1957oq+vLxmAhcdnQ+47h5gms4UgKinK529orWyt6hyv5vuXCfROiEUdYD4WZUe8x/t5bR1ywwUtpAxIfohKBeIUd4PJAtbqcgjvz48Vfnv9EeaqTob6ICYMcl1fXz+rvKzsPiHoqGDQw0oIIdjiGyEHO1L5tNOnT68kJf/LWmMBkk6sphY8I/guLk44PfJ363HG2JIGmfdKfo1tVtkyrDUw1uBtp50Wc1xi7TsknJnF998HtiYmgKXeizMjYGJYq1FWXgLLBXz16q+ABMHzvFTz6uVyKC0txfs/8D4MjQyCYQPUxjluOCKqRCQtWCtPzeFK/tXCoGSU+Fes0TvnQRBBn/eo/H5zs917SPtaKVbMCYLBUdPuDIm0taZCkepdWnjs+iP8s5hhKEmAY4K8tb5+lsjl7iNBDcYYDZCQgsgYf/3OoaEfONRTAcBqrb+W87wao7WhFHg8TmfrZO0gy8EhaDl4tcOqTLI+xf82Vl/DYTAywGQxPDyCutpaHHXUUUCIjjAzSABsCUopbN2yBQ8/9DCU58H3dXIsxAEnhpJGlwwBRoCNgh4BSsUkPLnkeZx98jtQXJyHDetDKRWKiosxZfIkaDYopkoM9/mQnoD0AuSGyPkMHMOryhrr5zzvpObm5g/29vZe+49Ur/9NAr0jRFiOu0Fc0DyX3zWoR7UgoTjM4DEfj0SMZAgiWMu2TAnatIrX3H5Z4T1E0PO7IDGWPy4B6Kk1U/emIMjrLbMRQipm1kJK5RfM18NsHsFgtq6ueY6S4gPWmqAu55jpm6rDU8OhuGl0ud805nvi+b4T8Mw2biCTqSjF2ZjDrKtIYceOPrztbadh0qRJ0L6G9GT8pNFGQwiBp59+GqtXr0ZFVTkM+wHEyALWlzDDFroQDIiUBxRVEYqmWKhKAznJhyi2EJ5BT+E2gCyECISapCLAEp4bsNAjAg3HlWJki4DuV9AjDNISsMlTjB36MQPKWGulEFdPnTr19yERTPwjwI7qbzHeXwZgzw9g1l5vpf9SHrQZhRSKHZyCwk2gZMJorOWcBPdthFz6kD5z1d1Y7m4aZcovM7WmZu+i8ty9Qoh6ZjZEQgKwUkqptV43MjJyXYSwRCc/n6PPEUlYazjAh5FMGzkaYFIc2FlyFlxYMRzxk1O2MFEKh0lJSTv1hbVu/R7g3kopnHrqqTEUGF8wT8HzAnj/8aceg7EaXBAYGQy+Jl8KlNZrFE8fQfmMEVS1aZS0WFQ0G5TVEVQJIEssZCh2Kp0aR4KgICCRgwGw02dsWrMDW18V6H9JYmC1wLbHSjHcWwZSNqUBlSwzwSopy/P5/LUATu3o6KCuri76ezMe6W8lLHTBY96S1sPQPuJrIwMONwQlywWIR/zxuEgXCaOeuct8+OZT7fc+cC286z44BqMVAGx7e7tatXLlS0rJmcba8GnBAEMLIVTB9z/R29v7TWcvkpubm2d7Uj4bAnYxysIpii2lcPwsNyXc1UsmoOyUPOGN6z4BxhDAOH31AxhQwFqL6upq3H33H1BVXQVrDTwvBwAYHtmJJx5/En+4sxu333wnBka3oqTNonLOEOoOGsGkAwqo2FMjNwlwvQwigo+CgIIHhsDoCGF4g0J/r8aWZRbbVmj0ryIUtuWg+yX0zhykLkJhmKALFoIIyhMgKZwnWKYnCP6opZRqeGTkHWvWrPnVP0IJo/7KJYuYTzCn/LL0/zUdivZRPaolkXIq3WjLK8GZCWCrTLXy1PKlw7+6+VT7vXDTSE+wEJFbtWLVzUrJmcZYTURhkDMzQ/la9xPRDVG26ejoEF1dXVYSfU4q5WmtNQCRlBluGZFu6oiSdaEx2T9eYkZcw7olT/y+0Wid0lRjBsPaADLs7+/HKaecgkmTJwWZVkq89NIyLPrlrfjdrXdjVc+rKJ8+ijnvy6HhLRpV+xh45UOwGIFvCNoHtGZ4SiAXyAKjsJ0wsCKHra8o9L+Sx9DKEgysy2PHSgu/n1CSq8CkmimY09qIGQdNQ8u0ZszYYzp+/OMf4J57/oDyivIAZ2fA3aBKBmaJSoG1TJYtKyU/D+CWfwSoUf1VURbAtl2Oqa1H4GuCwAQrhDsZwdjJirXWFiumTb3Da36/sPBhZghaMK7ylALgNzU1Xa888TajjQYFPPHwtBsphTK+7urt7d0R8Vm6urpMXV3LNCHl222wwaxSEhLRbRdPPdPKXpzZ+0RWcRcOySqlukFwlWasZccjIFqZsjAmmGq+vaMDhn38/nd34Zc/uhVLHnwIhaqN2PMtwClnWDQePYKiMgULAW0YfgHgnAeSPoQEeEBgZFURNj5Xik3P5rDxGcK2FRIj/QGyX1SSw0hhCOee93Zc8pEPYdLkakyZNGUMEPeTG38I5XlQQoGtBlOauUmpmQBHJgjSWmuUVLOnT299+4oVq3/196b1/tUCfdECEBHshc8ULWxsFblBM2qUEDKmlGbLuxCyygmwHvDlU7frD6/pwrb5XZBYOG7z6Tc3N38i76kLjbU+SeHBmSqGNTdr4CdRfIYjf1tURBdKKT1jjCZykZYYG3b+KsxSYQYTFGJD7DIXEa/MpedKlOL+UdiLsLPyRs7qm1ACo6MjaG5twvN/fAZfvPLLWPbSS5h01AiO/b7AtLcIFFXsxAhbjA4IjO7w4ZVZlCqJEqmwfZ2H9c+UYPX9OfQ9kcNQrwc9nAMJAjwD4RmUTArmE1L4GB0ZwFtPPB6z9poVEsMIxmiwsSAhsHXbVrz00ksoCklm8fwg0soh58aOF7ttUHyGf8EsPjd37txbQqrF/60avWMRZNd5MMd+F4cc+k7vieIqYWBZpiQbKDhZlmyY4QDLrCs8Ust+Rz/9+dvMBRNMPiUA09rYerjKiYejti/Q7I/LCiMESWPMM6tWrz7YHULOnDkzZ7R+SQgxzVprQykIZxczaSIdRm2IxsREeAdmTFu5xGgMIbWwHfPG46ULTnHLCQShCGwYhT6JgcJ2NB9XwH4fIbTNG0GREBhiieHCKIQwyHsBHDjwqoeNDxZjzf3F2Pp8EUa35mF9BpQPyjGkR8GgNyw5rAlW7QqFUZSWluC++xajvqEhnEYHZLGIB7Nk8WK84x3no7y8AtbaMeZjaanW5DxEODsAI4SU/qh+x+o1q3/195yY/jUyOs3uAGM+ZNsRue+XV1ke1j4Fa/tJbUcxaUuEwaltkUdy08u04XffNB8JS5bxmIjc0tJSTYSbQESWraVwyJ3Ux2CCgDXmVgAcyrABgPZHRk718rlpJvC/kO6tnqLcRrV4rK6Sqd3ZQRCdRxM5SxVJHZ6hDXBUl4dvIAEpAe73gDyj4Zw+zLloBPVzR1BgjYF+geEcUFIiMCmfw+jOEvT+jrDytmKsfcTD0CYBUkC+VECVBnmBDQXJwweIQlZDQLWFUh42bdqE97//YjQ1N2N0dBQ5LxcfazQhve/+++D7PqK5dczDSTkjADyGuxlvU5G1zJD4HIBfh9oxfxcERv0VuCxyIUG/7ZbS8xsPkAcN2iEjiWXE/aBwCspxXRpwnj1SLI0QKx7xO7ffi775mLBk0bD2BpnLtVhjDIhkhHqIZL4uDFiP+v6tAFJqtUzUwezQqyK2YQx9U+Ib5NZW7nAoaiidoZIzDYgYO3HjGT3JnLIqBHosIBlmiDA6Akx9y07MuWwH2g4NNkmHhgFtBbwqiyLfw9DzeTx7Sw4v31YEf3M5pBCg/CiKpthgcGktjE/JUkZ0q3JCO8jnc9i8ZTOOPPIofOITn4D2dbI3Gu6cSiGxbetW/P73d6O8vBxsOR6WRcvpkZAGu9PiOIQpXPpmwWyNlGJOY2PjUWvXrl3y99KKecMDfd482G7Aaz3IfF6RZWOtc9dz6Mgm4i6dA1UUU6GE7H1JP/Wbi+x1kZ7LeEHe3Nh4gZfLnWFthLAk9XB48xghSPpa/3Hjxo1LndizlZWVVUT0Vg5gAwGnUYzQk6BZdsI2goddNS03LTGnchSTIzPBSZZP7V0wgWRQ5A5vlci1DOOQy/ow851DyElgZDgHr0iiqlhh1Jfo/Y2H569j9D4MNE6ejg++/93YsXMjfvbTn6KsqAy+r2GjSahzL0kSqQZYax87dmzDIYcehuuuuw4VlZWwxiBqU9gytK+RL87jxz++AStefRVTJk+GDnF8BzF1GJeU9Fk2PIEcLn2HdYwgYiXEJQAW/59oRtsXBzX122/3zqttVbOH7YgRYIkMidXCglhEcBxLQRjYyfa535nPhepZ4y4r1zQ1NXqe900iWAqx+LGmFUHkWWt/HTagkXoslZeXHyelnMwBOURSjIQkjSLGTDsTwNudaKZnPg5HJyNSNIamaAHyGGLEg9ECMz+wHQd+YisqajUGtgkMF1lUlBQgdpTh1TtK8OQPCNuXFmHf/ffDR/7rHJx2+tswZVId5nfMx+aNW6GrTVxSMIeELg4aQikDjZbCaAHWMmprp+Df//0SXHHllSgrK4O1FkLK+Ng0B0H+0rKX8L1rrkFVZSViJV/m+GkXIC/k1OYcy+TBodmHMwhpLYOEOKmhoaF53bp1a/4e01L1V8jmsqqFPy3IZ/JBUomkeXOf3BSFvDQVwlOvrBz81YOX4+6uy8ZOP9sDtqEuF+IbUsoaY4wGQSHWWWQHuYE0xhQA3BaWLTaczrEg6hBCwJh4m9hVpHD3g6Ja30FRKI2lc3aTyGnV0qui8ZCJiAHJ8LdJ5BsLOPCr27D/yRraehixjOIai/4VOTz+82I8/1MLr68Cp5x+Kt7xlQ4cffRREOEcc8OGDZBSYN7x8yClCreOgqejNQFEyZZBUqCstBQtLa044IADcPTRR6G5pSVs/m1M+QUBRgclzMjICC6/4jIUCgWUlZdCa+Pcp+l5QIK6jG/IkeyRW62UKrdavxPAV51dgX++QO9YBLmQYM9aVHxw1czCnCFdYCmEIBaOVqCjbkvBRSHPim1brb/iN/xFZtD8+WPeWnYDuq2p6SQvlzvXBvNwGcsku80eYIlIGGvX9/b2vhrFbldXl6msrKwSRG+xgYacJKcsYXA4/3eJXDRmcYIcdTAGpZaj4dThMWLuQIgkApGhwlaJmrf047CvbkF5m8aQlagWeWBTJZ79kcIffwAUtpbg3HedgQ9cchH2nbNfjOZoo0EgTJ40Cb/45S/TdAQeswEU89rdl1/wIaWMKb9BkBsIGchtfPTSj+Cpp57CpOoa+FrH5Vh0pqyrgsAZGT3Xc5US5eDoziApjwsD/Z+3GV3UEQw1i6foz3plLEwBWgoWcUiMacoIJKCLAfXyUr3o/s9iWddnILu6UtmcAHBNTU2F8LxrAchgh53HhUWZ2YqAT34nAiF/FZ3UsrKyg6RSU8IhkUit6bFbb1OKBpuM7V0EJVN3c5bt6NAEOCAZEEuYnRJ7Xt6HOZdvB3kWFsDwRsKKX5Vhxc1VWPtyAS17NuCGu3+IvWbOinkuRAAJGWwDMQcfK2t0EFN+E7JYsCJnQu44QsQlueSWLayxUEqhv78fH730Utx5552omVQTrNBlhJk4+lxueZZKEJwSmySKVk4grLVEwCG1tbV1oU773xR9EW+YZAXB7tuJpprZOMFYy8SQzBTwySnhbyfNi4EnIQqD8Ncv5y+BQUsnWIcrLy19v5SyNaDdRh7iGMPxRmhTbn0/bnra29spzGzHhtnNRggEU1pHPU0LJ0eYMxnqWIeukNBiQ31FclfcQsaOItgCY2RQY/8vrMfhn+5DuZeD6i/F6u9Oxt0nNeDhL1dgYIsEFw/ilFNOwl4zZ2FocAi+b0JYUMSNH0XaLBltFxIByJoEfXi+ZUAS8zwVMkJtsFMabisppfDkE09g/vz5+MM992DylCmBDB4HagKIxGwYGR5+QlxLcYFS64IR3YGIASOkrMkrddjfY+nnDflhnQuC99nvVHVhXW2uVBnPSBWofluLeHcxypMc9CwmD0Vb1/Mtiz+ApYsAsZBSdRsBMFOmTCkjIS6zxlhmFtkbxgl2JkBqrUd9a5+MklZ3d7cNxHvkMTGU7Tx+3XF2qgogCretM0vOHIALQWCnfbGSrwsCgjwC+cEp3v/bm7DHxf0o6FGsvl1h8bmT8dTCaoz0KeRrNAwXYDXhsEMPhe/7kCooY40x0FrD9/04QOPNIRNuD2kDX2uYcCk62DQKflkd/rLBtFMqCS/nQSqJ5194AZdddhne+c53YPnLL6OqqgpGJyjM2K0v58nHCfaUFj5Nw62cZH8WRCAljw0tZuifrXShBYBZCOQqqr1/kxBaQsJadis3p6sLFwekQqFg6Znb+UdgUFcGaQnREl1SUvIBpVSD0VqDoFwFLKcaDjO1kID90/r169c6Wi2oqKioEYSDrLXh6tfEI2F2ixDnqRFLP1Ma33GJWsKRbSZlYYYIIq9x9I83ovmYEfQ+UITl19Rgy4MlEDlCbrKF1QQ2EkOjw2htacO8446Labh4gwVLjDFYtWo1Hnv0Efzmt3fikYcfwcjQEKqrq5HPq3A3FGOevrEwUkS6I2c6zBjf2skVU4qetoEY6jEAOKQE/M3KF/UGNKGCCObkb8uTmvagWQSDUs+gAIIfhKFhHc78mcCWBcMYJaXqW2Mff/xy3N95GWhhGmmh7u5uU1NTUyGILmdmjroqih/LFD/O47AMJF9eAGBCfx4CYMrKyg4iIao46I6EK2sXRTGFF1A42WtiGZawUrfpkiXKdsoj8LCCKNE4+qb1mFKv8cCHp2DNr0sBVshVBWWBKTiLGZZRWlaKX4ZNpggXnY0xYGsT9CbUdYlG7eyUKtEwh6QIxlHWYnBoEJs2bcL6devQ09ODlStX8ZbNmxkElJeVobKqCtoYkLExUICMLk3kGsIidNmOFy7SUZqaL0T/npxIwdYyEe09ZcqUqZs3b97gJqN/+ECfvTQ40NIy2vSnxYU7avewXOijfVUFalQJFXvFlC8vkSFwytBg+CAh4WP9SnsDAIMlafGb9hBpKS0qer+Ust4YYwCSCRZI6eALpdoCbXvzxxBpofbAwxOelPuSILYalgjCvSI8bkanRBvReS4l/YXzrOKUnxAgGGZYIj/F4PDvb8LWpRIPdEzGyBaFfJWFVBrGF3BbXrYWxcUlWLVyJT75yU/GtbS1jGSinNxgRATLDGsNGxMV0gRBFMjhShl9BsHhewghKJfPcz6fF5MnTybLDBvdRNFAWVBqM8odmgVzIObU7C84OexAjxQ2rCIWBknOFbFlI4Qoz+fzRwC49W+5QP0XB3qkVHvLRfpxAGeGf51vbUdx4yGYVNqQm733gbJy20b/2IaDBW9dz4dOmqkqhvr8ykduwN0AsHBJGlPtDoLeE0pewIHCzjg65M5UMogCwQzymaL6nEN+C0jKQ2PCbZyKoiEPOaMsDtf54OgkUqp8yUj3Oy4TwWFaA8gSH9PftR0vf7cS624vg6w0KJlswJrAOpwaRfZ5ccPLUEqhuqoqIUUlTDI4iYDDkJPMTNFki4ggRQAbRjdCLE4UStaxNcSM9dqYkdSTIGp0LSFte4c4bTOzB6CJhCASIl2uZBDOSJ89zBUmmA4TMbMN7kc6AMCt7YkR8D8Pe7GzE2LOguD9zpMwvOtxgKg+Bo3bH8SaCYSHTFNT07E5pbqZ2cZLyw4TMKgZ4xqdw4u7o7+/f99t27a5NXpRS0vL455S+wbQIgkidup7yiwQJJos2bDG2NFQsp0f/Z0BZLkGG0BvLUJ+koHRABtONbWu1UpK/SqyXAkpjYICxZusy5w2BtYaC8Y6EK1lZkHAnzzP+7NvjNBab8/lco/48AEf0FpzoVAgInpp69atg6+jPiYA3NTUlB8eHt6nuLiYlFJEROz7o/sQyRZPSmutncrA3KAMs/sSiTxFlvREkCK4+aSUXBgt/H5VT8+pf8sJKf3V3peBzgWgZXNAs6eAMC/4hy8qaLsLSk9YW5vW5uafeZ73LmOMJoRbQwlpKzn4YCRtBQnha72qp6dnmrtmV1NTU1FeVrZGSlkeRBIRUZpzx05zGegTYswKHJAlMozF2eOA1QwIQHgEGMcLNLFejL7RUgTdEARxVvSZYNlqErRTCPqj1nYAFk9Y+Jt8377keXbNzp3+1m3btvXjH+hVXV3dXF5errQemaFU0XRmPtjz1B6CaF8p5aRCwf/zylWr9vy/EOiv/XM7QeOY1QoAtr6+fnLO816VUpaHWt0EdwSPMbqGRgghjba/XtWzqiMc+SP0HTqyuCi/GIAKGuG0p89YiWVOBj6cFY9z1t+QLDMnWDwn9onEIczg7nWwtTYY3xCEComWIBnIkRlrhy3zOmvto4B9CRAv+L7/yvDw8Pq+vr4drwETcwcgNoWwXVgSjJex7f/iuvMEcDS1t7cnJWcI5U4UvJWVlVUlJSX75nJCrl69dsk/FeoynsnWUoCWLAFqN4NnLwUvBJAJasbCcT8gARBKqYOkEBXWBpxxciQhUsrRHDtfMIFgrNkKwG7atCmeiBYVFdVJIXLaBKSNRL+c4vkHORrjSSOaUHHdIYhARgUgtS2VSKyzjbbnrAlGJiRJCKEcNMVaXsFsnmVDLxBR98jIyOqNGzf2TEBjpSiQnSB2f1EXwAj0JBEGILkUZecg7f9i3kIZ+Ne9oWwGdhfxjQBQd/j3fX19O/r6+h4clxjzfyWjkwCscdHAcRCc2bNzy5YtKzQ3N37XU96HrbFaEKmkcXPxa8c5wrIRUspCofCRNWvWfBeADBsd3dLU9M2ioqKP6cC3RKUYZjSeDiKnJqOxzmB0Qzh25tHaXeLGAdigwLfMlijQTgp3YRkA9zLzk5b1fb5vH1kn1i3HGgyPKd8A2ZU2JnDhJnKCbJcZdBevqsrKytf+qr4+9AW/2/F6h5Dh2iLG+Qzi72HYq94oCgAWwh73XbQ2zyn5zqixq2yxeXHjE/6WfCOefeKLoL4XsGJXQQ5ALFu2rNDQ0HCEgLzAWrZhFoTLZ8/Qv4NYFcGwRkq5Ysw8RcriBANzfDczZFp2a21C2gQ3xVtPcz3CB4pFyBeUQggSJKwlaG02WaL7rbYPW9jH16xZswzAYPYQw5syDoiuZMeVopmCGyzjIBVeXV1dUz6fR2F4+GgvV1QiPWmNMfsTUUswIiABAlvL5daYA4KOPHkWCYro5EHTCGZweTkqAVZKPQVgxAYUC2uMISXEQ4DY7htfAHhMStm/Y8eOvv7+/m2ZmzDVf23atCn6rPRPl9Ejhdwz7xPvPur4uht9jEBjEKMjPoa2wo5shvFqsOzVp/ne352DKzoZNM64n5vq608SSv1KSlmVcgGntFJWANiGa20CkWEmDw4OvXXjxo2LAciOjg50dXWZtra2XyohzjfW6ujGTpdB8eZ62jkuM9bOmNFGIIkNaDREFNofArzcMu4yhcLDmnnxunXrtmazdVh+WFcc3Qn2iTKdbGxs3MfzvCqt9TxPeqSNniulqGXmSgB7AGApZSJ5TenJF0eQ5TjTA0Z60uNuKbnzgiwOFcCWFsxsLfNmQbRaG+MrpZYY39+scrlHBwcHt2zatGnVOE8eCmcm/Ne2dXxDAj1cYjbvfdH7atsc7xO+HvFBVsESGbBkCVMlIJ97BDf98iicn1l6lgBMfX39McVFRQ8E8Jk1YeEsCUzMmZOd4c1SYJC5XUjZ9sorr/Q73bxsaW5ekfNUi7EcQovjg4ZZXIVhYwFQ4sTAPERKQIKkIBH5fS6zxtylrb1jzZo1jwEoZOBSylxIEQb2eMa+JVOmTGkoKiraWwixp6fUnkbb/YWiWjCmxW4XMT5unVhmAKQz/cLYRVe3qaQM1SGbfaJdmbFnzKXVqay7R7JCSGC2I8y8loBHLLBca/2wlPK5np6e7eOUPdG1s/9wpcsCwC4EmEboEI2ChLEAkeQgKNkayT7I6m2FPwDAnHEmx8xcztY+QEK0CCnaiALFqnCJmQRIpBASciQngouuthqTZX4YKaXPGU551MnyOGhL7BvqUG2D0gQMIknBL4DtJt/Y+7XWP+rt7V0CJCpioYYJOxeMHHcNBmCi8qOurq4tn8/PVEIcAaKDrTEHgKhOCpGP8HPlyZgYZ9hYGLYJC4JjfkWoha6CpjqZbJJIKGyUFTlNiU2GzXi0TRTP1lhm7SOT5ZIYpeKQ8WgjFaPw/SSIiqQUM4hohuBgxc9Yu725uXkZgEcB3MHMS9esWbMtNSEPzuMbEvT0RmHm1TNQ8c77vaUVrbbRDlsrJAkhACsAIYqNHCRx//d2nvrQlfj9BBqKAID6+voSpXCUJO9sBuYLISaFtYQmIZQjaRHxO1gIImNs7+qenjkAdkYZdNKkSeUV5eUvCyEawGwZLHb1sSPiktOomqCRDvzbfGOGwfxry3zr9u3blwwMDGzNJA12LopARwfQ1ZX6nA0NDZNyQuxjYc8kku0kxD5KqRwJCuWibXQTmsS5MSa3CHexglJW1KlNCKfconQjnUKs0iUckaPozs7NnkoKY5v5ZN7gCKi6vU7SX4S+jiQimZEYnWDeYK19HmRvZxa/7+npWTEO6vO/Lm/UGzARpYUEO6sT9cVFXiPYgGQhzjXWMkP4cnS7GNr8EJ4HgK6OiZ2a169fPwTgHgD31NXVLSjO5c4mKT+lPK8t3A5iZghHo9sKEhJKPANgwOXzSSnnMLghWrZws1CK5pvhpTOzIYYkIUINTn6uYPzbhplu3ORcgHC4BSdzuxfEIPi30mlNTXMg5Wkk6Qi22I+IaiUlOunGGMuaLVHCbgdBitgSPRnXk8O7SSn6pvicaSs8d6ofiYHE7AJ2WvO42XZg0ox2i7P1nVoxjKLexsfozjoCYc2AA5PQ5phhQ0UGSURTlZRTQepEy3Z4Wlvby5b5t6Ojo3ds2LDhyYQH1a668fqz/F8c6EvCmqqoRByZnwS2lgwpUgjHIlYTK+XTcAGrlt+BbTy+DaYLkREA0Q5Q98aNmwBcW1tbe1tpcfHlUsrLQYKs0RpEKpZxEAJk7RhufS6Xs+TQBcasxlFqO5+ZYQQJRVJIywZs7G+179/Us3btz53ji24k29XVlQ1uCwAza2oqbFXVMdbas8A4UQjREq1yWw4aNw6agEg/UhCFaxORtQshxfUeI31HiLe2yP0HR30gotOmDM/CII8LlfRya8pxI/ZNcucELv8nnvhG/5R+rzHTZHafHIJIRCuRYX3IQSYjomJBdKCU8kBB+FxLc/Niy/zDfuCu7jXd25zrwLsb8OINWIgGALQe5wHKJ+ZCmDpFzN8gAHoYrwIY6XptamZQwwbqqwRAbdq0aePK1auvKAwPH13w/edICMXWGhGqS0UM3jDLJpgbPI5MkNyLEJjLcmJCy2wAkJRSMaCt0bf6vj56xapVp/WsXftTALY9SApRo8Tt7e3RiTYAbENDQ3NLS8tF09va7kBV1YtSijs9z3u/krKFYWG00UYbw8xMRIKIFIhktIqUbQQ5JpaFSsNRGUGOWBJzsrwV6cxkSnCC46zh5n7ne5ObJZHLY0RBTo6OS6J3Q84qHbuOII5SF40j7kSpBY7gzQJzASIQJAESzGystdr3NRhQUh7nKfmzKqKlbS0t32xoaDgwOu+I1JD/2hl9zrzgmIc30UEWBpYtifBmY6d23NZLm3Zxs03UaTOSgJc969c/DOCw1paWhZ6nPgmGic6Zr3Uh+8ZGmJyCABOlSFSx6CezAVhIKaU1ps+39npmvn716tUvZ2vD0PNIhsfD3d3dtq6urrSoqKgdjA9IKY4XQpTHPj/a2KS6IEHECmNqYmf1Da5KLzuBxbHtejyFpaz1I8fL55FNO4iTDROm1C4rx9RCV+lgrN5WnOXH6epcJw+iZEfArVY4luxL9G7YHfw50tyxIGAgVRguJ0I4/QpJJadKIT9GUl7a1tZ2EzN/ffXq1X/MGCbzX9XDKD+ZWhkiiAx2jjQs/4rq+REAWLpkTCdow+CxGThuvICXAAqre3o+pY29UkgpBZFhZnhS3g8AmzZtihNOoVCYJ6QEhZBgKCeNAP/mCHOmgu//aGhkZP9Vq1ZdHga5cCQZ3OytAaCtre2wttbWH5SWFi/zlPqt56kzhaBya60x1hob4H6CGRKpBphSi8zsboxE9utwVQXSK4jJlJYzAzRHDI7TchxR8BIh5T8U1deU0cbmtPLruPo0KaanwwFNM4I4Zvm6K4sksqxNm5ae5nGBXxmUdsTaaA22Sgh6F5ifbm1u/mmY4SMLTvlXCfRwoblI1NBUBmtjYawPA5+sBHFeKZDNY2V38PRYkskNdXV1+zQ2Nn6grq5ujjMa5gmOzQDA3LlzvVWrVl09NDx8JTPnw2Zz1IGlorOTc6WgiWFBZKQMSNsF3791eGTk6J6enos2bNiwur09XZ5Ewd7d3a0rKyurW5uaLpnW1vaQkuqxfC7/filkC7O11hhjg3GiFAQp4m3mcQaAzKl5LIfWGhTxJJxMG1xaSvTXM/+b4lEyTQypEY+r0u267qXVBNiRsk5PgS0HRTVbm/gjMaWdsl3OD7mMU0oBRHCGc9H7JdJ342pbEoEUM9hoEzyNlfy3XM57Ytq0aQsqKyurwxhRb3SgBxPOioqSUiMOroFQFXmRKyuCLMpDSAXylAINeX79HDwUiRy5TZ2S8oNFudy1xUVFz86YMePxGTNmvDsKto4JsvvTTz/tA/DWrFlz9fDQ0CdEYKc2zm0sQoq3BQe6EUJKKY213dqYo3p6es5ZH5RDAoDo7k7KpAg5aZg0aa9pra1fmzxp0rO54qJrPKWOYjasjW+CJwMJEiRjEWUXtoshUDh1sis4SonTnbvswWmXakcdMrFtHDMF4Akph4nwkGs1n10NpJTEnCtAmeLRu9wVZktgA7AJ+xwTLFrAEMiAyDAj+LegTEw+oRvQHG+IxbV8bOGeenrEzQtF2jzWWiMgpFKqc1JNzUNNTU2HANAdHR3yDR8YNc3uH+57SX7r1S0ozXmSQbpJlfHeJVNFbVWNLN3BwHOPhuSlBTE0x11dXaQ8WUVEJlhcEYcKEjfOmD79Ml/ry7t6eu51NzszP9afO3eu9/TTT39zj/LyVk94U5DQRaPRIawxzMZYIWXOGPMnbcwXe3p6fuE0MpxpagwAtNbXzyLP+38k6EIpVTHAMNroAP+FIJBMcWFSgqMJVz6umaP/dTO6k/GZ3BKE0/yaMY9yTi+NuMhIVnweiUNFdFAUSzwnrE12VoJEDDzG9QtHciJEofhTxnF4jNULp0sYOCrD4W9tgD5xpEhKoRg9ubo07Ay5MjdcdN4lg2F8XwspZntKPdzS0vLOrq6um12Z6jdsYDTOKrjc71K0zG5Xp1rFx979DfOhvoew3RHWYgCY1tb2Z6XUzGAvNFR0EkJaa2G0/VbPmp4rwvprPJI+AaC6/eqKywbL2l599dWl4RRSdnd365aGps/ni/Nf8H0fxpjv9e/c+bm+vr7tji2McZ5qFgCam5tPlFJeKohOEEQlAXvcaiKSMUuGMma6bo2JdPaO4c3MIIWZYxm5rMVjym3DiRAGZ3yWKJ36MyN4jKdBk5qIchhfNsq0NvINTb6c0ruyHOKjQXu5GsBmDmEsHsdkm5LvJ2beh4jy4UaYFJTcZI52vg0V10CRK3H8tHQa9LF+aUCgyUkAkV8oXN27du2VkX8S/W/Zip3zIBbMgxUUqpSFP9TaoAzoAngcRdzUgkVlZWX1lMmTXyJCrTEW4NAPIuBWsFJKWmN+Wz1p1dlPPx2XFa+Jm0Y2Iq3NzV+TUl4wPDp64fr16+92/UidEkWHE9m35jzvSqnUW6LlZGbW0UVP2aCDg9kep1cxElpvGEAurTcD2I9XLydlCdLceLcpTMlIuD8/5ViQHihF5vOJ3YxLohIh3Jm0h8aC2e6wzOuJ6BW29nkI0aO17ldKPQ4Ao6Oj2Lhx4wZgLM14oldNTU2TUipXVFTEo6Ojs/P5fKPV+mipRJUxPFsIMUkIqooGwdEACpZN2MiEx+r2DDae1MaGHERGSaVGR0b+s2fNmk8HZQ5e/2JFJoDL696PyVMbwdtfBfX8DKkP38nIAbAZ54qAyDVlytEl5WUPWssm7pZdyItQ8JSXs5bvfLW66hw8/bSe0HQhfRMQAK6vr5/V32+3DQ5u3OSiKO4jrbW1dRaA/5ZCvDXcd7QcqbZT5MfFGbITTXgQiNENSmUeZKSmGYQJBmdpKWaHO0WcfvzDUQSgzDIKpW6+0LosHO1EfB2K1AaYN1tr1xDhSWbz1Oio/nM+n3+up6dnYDfc5AR2zxKdX2N+km9qaio1xsyVUu4pifZnoiPBvKeUwkvv9kJz4LJDlpk4VA92VBIYREYIoSzzMStWrHiIXpddS8hPmf4BtOx7hnrH1Fk4Kq/EAYLRIPPE7EuCJ1dv26R7Bnr8O5b/Qv/ixV9hY2Sq25XcIBKAmTp16ux8Lrc0rMSYiMa7+r5U0rOW7ywqWvH2ZcviQcHr4TxIp0xhADx16tQpxfniL0ol3g1CiTVBCiciyc72e6wW4JQrkV9PFpobKyWdQS0yY/lUOe0yDDkS9gy5NxndSnbEVZPlD5GlF3O4ImuJSJHDK2FrtxljXrDMS5h5MdD/wpo1/dsmOnftYRB3j13y4Nd5HUSGohttH020iEENDQ17CiEOJuIjieSRQtABSqpYkImtNQAHwcNh4RI8bQ0RCWvMn3xrj9ytQA/LSVv1PrSe/rH8pRWT7Qcm13NFHhYGIlwwU+AwjhgaFhaD6+y27a/gp/ffYr6y8r+xMRT4d3nYPHXq1FNznvdTKagqumLZWtKy9XO5nOdr/1crV656x+vwrRTOxYi/p625+YNSys8IJVtMQKIyRCST4EzX1Yn+t6MOFvsWjbU8YQcXI0eLxh2vZxW/XC68O0hKISjjuPlRdqgTbjglwQ1obSyApxl4whjzkO/7928M6BXjlXzIjNb5b7wbIcKbAN3jXOOGhoYDlFCnC0mnE9FcKaSIkDXLVocj+XDQxIZA0jfmQ68V6NS5GHLhcdAn/06eu8cc7/qmllzlEHwU4GsygRCIDNsOBgciHmFzk1NQJfCwY53c+OT9o1++7932O5y28pTBAnPd+cVFpb9khgkHLORqqVi2EIJ8pTxvZHj40p41a64J181e1zpWQ0PDgZ6UX5BKnhYGnRYkJCOSBgjLDWR3FjJ0Vs6oAMQmukgPPSIJ6fGdJlPa61kpDIzZiiKnlh+DWFswLBEUiUDXxRhjQfyktfY3Wts7165d+1w2CYTU4f/N0gNF52D+/PnCpV1Ev+/KaAx2dXUh5Abx6wx+Fw9NXe/Gxsb9i3K5Q6y17ydBhxAJYQOkzYIQGLGBSBu9jHYZ5Bz4EZ19C762z6nqinyRQsGwZjaSExDYGV0kmuUidEC0kLZY5qVhwgv3DP6i6yR9oZDQ9nPB+l20J9ra2voeT6kfW2s0GGrMqIXAggQbawdGRkb22LBhw5bdyDgEANOnT68wWn+agI8JKfPM1oQPOJoI9UgV3RkOt0thTai9brXCsWTyREGe3nslp/7Oqg7zrmpeGxDAglwT1ttPWWtvY+bb16xZ82J6lQ2yqyu2iefXZqZ2RqWGmDdvHubNm8cArAjnE3+nl4iMIVIQd1PTPkR0JgFnEnCIiCS2mTUTFO0iyGkhwZ75B/m1/Y7nKxRbXbAkpRKUtQqPaj/rTNoCsFmAQdAWTMqaMli1fIn97Q3H8dnMIeRD4AglaW5qui6X8y422pioVhaC3Ee/ISGk1oVFPT1rztuNrC4AcF1d3eyinHePUl49g00YtBKUZQXyGMdod5c0yvZwKb3jkkE45S06HroSNYJxW8JpRCZRIXCPgSMCGhOREmH2tlqv9q290xjzw3Xr1j2blqOA7O7erY0d6ujoEJdccgnNC5h6hoh4wuXq1kpc9PaL1AsvvHDIzJnT1PTpe6C5uQEVFdWQAAaGhjDU34/tAwNYt24dli9fzrfddtt9oW79G1USRUGfunEbamvfonK5t5PEeZJU9YQ4etQ4vu1B+tqBR3tXeLA+w3jWEqwWwTKyiPK4DYyi2DE+dzIih3Vr8CSFX5aT3h/vsnd2ncrnLGLY+ZSelLa1tv5RCDEn2sckjIk8TUKo4ZGR09etW3fnbnhXRrFS2tLScpmS8tNSiiJr2YS1t0xlTkqQEcIEOLdLhHIVAcYpS8YrN5L17KTWTzjx480440BVQoQ0AcPbrDXdFubWvr6dv3F0X6I5wmsFtxvYVghhs1m6rW1q6/ved0lNPp8/pG7KlJZ8UW6/2tqp1UNDQ/vUTKrhkuJiCVBFaWkpioqK4HleoEEXSlZH3baxBkVFRbilq2vBRz/2sYWLFy9Wxx13nH7DM316iwt1dXW1UsqLAYxtRts7oboXQh/wzpKLDvmIuJ7g+ZKNVzbJoLR5FMXFgA+FYZ8gBIMoCPQom4eStw6UlgD8ARAq/HKR8/70+OiXfni4+XznYqiFx8WELdPa0HCEKipeArbSBjJrlJkMGCGk8H399Oqe1YfuJrYeH05bY+P+5KlrlFJHARR17jpmUZIrXpRY0vAYAhSNURZwl6cT/dOMBWO2dqGI2EPZusSGDh6SgldwrOAH2dhbhVK/WLly5cbMlPs1g7uzs1MuWLCAhRAmE9jll1566X61k2vnHXbEIZOJ5DH5XG7fyZMn58rLy1GUL4LyApvGQqEAv1CANgZCkEmWLmyIwSflV+ixxFXVVXjm6af7TnvH6bN3bti5OSWX9sa/ZLamH8OQ7wTo5zNnehaFZVLLNj1KMJaFLDaobLJoOcHHrHeNYvIew+hnC2MCMSpimyIPiRTcFWU+ATChSBb5XPC97jv8k+7pMH+IoMuohJnWOu065YmL/YKvEaIHbs3AzIZIyJGh0bPWb1p/+246EqcGRI2NjW/L53LnMfO7BJGwSVmAcMxPFG/qjM+nZEf4KuUzm7FmzOgdZQIe7jQpCtQ4uMNyZZmx9i7f928NuTnZC7qrJo8WLVokOjo6QETuOSp63/ved8S+s2cf2djSeERdbf2BNZNqGmpqJiGXy8GYwF3aLxQAQIfHEZg7aBvuaBOEkKF2bqJ+FstXU0JUg2VdVFyk7rzjtos+eMmlP/orZfVxBbEQONwjy33RTQ1NX8nl1KdAMCQD+zyrLawvwb5CUbXFnPk7ceAVgzCVBgUf8IQGkY2XcAUEwBY2ehSHluiBboiwJTJH63tGV3/no/rAjtsw0IXQRD6oqduKi/JLwZQnZ4bsWm8TCeH7+p41a9ec9Dqtt1NUgsbGxv2VwMUgeYqQYrogEcg3RCBSMDlMxAPIdTjhlH9odgYeWxJmB0SRlmjgAmHD3UkVBX9oefgyiO8whu/o6el5wlm+jupusxvBzUQUf9aysrIpl33sYyfMmDnzrLbp0w+eVFMzY/KkSRBSolAoYHR0NCAaJxuhIrrhkl4m0WIXFPoUOfbwrj1jpFgQuuXZqqoqenHp8y8cd9wJhzCz/1fO6hMuRwfTysn1B+VL808KQYEufsiYZ+bg5pQAa8LIdoGpBwEnXLcDZdMHMegXoKRLIY2QCIZlG/CSo4tOBMvQJVKol+7n//rFCeayqISJREabmpquK8rnLjYmNM51ZB3CE2QBFEZGRw9cv379yxMJVhIRbrrpJjl//nzzGo+34qb6+nNULnc2Ec2TQkyCw8UI8WmX6S1cy/cQfEk+duTVnjToNm7hKQwihGuARLDWwFj7Jzb4HRP/ure391F3VtDeDtXdjV3pvoybuUtKMPXyyz97wuy9Z5/V1NRyXENj46Ty8jJoX2NkZCQIbGuJwUIEZPGEtkAZzUke20GM6VkiMDS8CRIePCCEsNr44kc/+sHxX/nK1YsXLVo03nX5mwS6aG1pucfLee3BxIkkx5acblZiiByg+3Iob/Zxys+2wdtzCAXfIifDxYuQkBZ7UbriLEQgCxYK1vRR4d7/NMc89hU8syCg/QIAt7S0TFNKPkeM0rCsIDgi/QA0ESm/UPhS79q1n9/FEClE/FiGgxQe27m3i250x99bV1dXW5LP72eZzoTAoYJoDyKqFkQp4Z60WVjo5RUkZSfNOXk/cr0L1CC2W2tfAfMTmuzzmvXDG3s3/mkXshk8EQS4YMECytTcdQuuuurE2fvMPrt+akN7Q0NDTWlZGbTvY3S0YKyJgJugvoz10d3Nfriy2onjXTyVdXyOUuhVVLaECxYJRTlYJ6yqrpL33HvvLf/2b//WwcwyU0791QNdAdD19fXvKCkp/kUU5Fn2W3ZrXuWAwk6BiiaDk2/ZCtk4BDKAFBEV1BlMpvs4EABtWZcpqV54QP/mV+04w7FGlwBMW1vLrUp6Z2mtTUzMSTZbLIFIG7Omt7d3ZigaFJ/yKFt88oorrpjaUNfz8Y9ffhMAhLWhmYCqMq4uYEtLS7Xv+wfnlarRzMcppXLGmKOkgBdwvgBmbqOkxBogoi3RcrExxpdCPKytLiiVW2xGR7eR5z01joDPbgf3vHnzxLx581wIsPiLX/ziUdOnT//gtGnTTmhtba0uKS7G8PAQRoZHQ3mcoBSJOSGue0dK3ZHCE5GW1E4rDE+MQFGs6BXcLKF4A6xlW1paInp6ewc+/vGPtz366KPbJyxfCOiwkF30xih4pcauzY2Nz3q53JwQNRA8xn48Wb+KH0ceY2gzo+14H6f8cisGhYYSIvRPcSYp4NiCPBEdAgvJPLRDDC+7uWiPuz84tD7UcSQAtrm5+a1KibutDfTPx6iwcGCgO1oovHXdunX3urV6FOhf+crXOt5x/vxFS5ct/el11/33VbfffvcqIsL999+vlixZYhcuXGgnamIiodLdmNpxdXX13kRUCg+Aj3Xbtm1bh/GFQjHB3q7d1ddFwf2Wt7xFB4zm4HXRhRceddiRh79r5h57ntTY0Di9ZlINfF+jMFowxvgIZG8EpS1rONUsx/Coc8GJKEV3SAyLGOOy0dxBl+OmzSnXOoKQwhQVFcuf3PiTD37yk5+8rrOzUy1cuFBP1EsRAXxVMFz8SwNdhlvsh+dz3kOh5rcYszDrUj9dHjEzIA2GNhHe+q0B7HVRAYMFC6UsiHWaiRcu0QbnLfDsZBa6VCn1wiOF/1p0FF8WydshoBbmW6xdHmzSx7op7s2ilVKq4Ptf6enp+UyE2oT/TkTEtbW1dXfe+ds/77fvPuUvv/zy5ieefPyaD3zgQ98HArIZM8uuri7Mnz9/l8hFzMEI1/R2cQOMTx0OA7o7PdXk3cS5U8Ob97733/Y//viTTmtqqD+9sqrysMaGJhhrMDQ0zNYaSyAhpIznUDE33HWZyyxIU0ZdOHWemVPDwGw2R+ZnpCi/bME28FoL1RpMZWWlvPfeex8//x3vOCK8TjZLHjz6U7mPVu3Hs+58p/8xAIVOHmPP+foCPeZut7be4Cl1YWhaqxL0K60BnhXiDE4Yw/oC5Q3A2XeNwFb3g60PSTqFN7toXQRrMAtbpITYsNau+Z+Pmz15EUaIEvvFlpbma3Ked0lwXIHcmjM5NFJKqY15avXq1Uc6EgjR1wgiovvvu/+hvWfNOnhoeFAVFRXhhRde3Pjqile+f+1PfvCT5594fmVMd7VWdnV1YenSpbxw4ULeHXrBOBl+IlYfvyavqLOTAIgFCxaMGeCcfMbJBx5/9PGn7bnnHqe3tbbNbWxuEtYY7BzYCWO0Dnz7hHDFUJM6m5N9TM4sinAaSWGHvyOEiD2RBAlIKSCkhKcUhJSIJrOJDTulqMqRR2owqwhACaM1lOdxT8/qwsUXf2C/559//k+dnZ0ierLGUPMXS+fN+5xavP7V4WdeuqNw2oOfwPpdKbztVulSW1tbV1Za+jIBlTYxz0w9guIuPLy7Y6ZeWJaonEBhu8JhXxjEPv8+gMERH55XyJryxZCbiLQvg4eKhU/iud+OHH/HuVgcfiAAsI2Njcflct69odqgyKyKBenAWva1bl23bl2vi75Ezc5dd971wxPeesL7Vq54VQOM0rJylcvlsGLlyuFNmzb9+rHHHrv1P/7jP5YA2OqofEFrLRcsWBAPpHYj+Hd7itfZ2ZnikIwzwCn+t/PPP/roY49tb5s+/cTa2imHtLa0AkQY3DkArY22gWiTSHkeZurncXySnF/h0CCQ3IZSHjzPQy6fg+cpeF4uNgAT4o0RjAid9ozWWl53/XXv/eilH70hg75E92fpJa9ULW+dQY0bNgy89MJi/Zb73oV1V10FsfB/UcYQADQ3N19YlM/foLVvAokG1y4oGgRQTDmNgtvlWgtJ4IJC5XSLU3+7DbZkEIJNOCklJ9yT7YFY68+SrlAkX3nB/5/r9sOHnfKFAeRam5tXSSnrLXPISEspS1lBRL4xJ/X29t7j1unRUOI3v/nNv5922mnf8/2C3rF9h+rr72MhhC0rLZMlxcXY0deHV1es2Ny7evV9q3pW3varX3U9tXTp0hXZoBZCwBgjAGDJkiXxlV+yZAnmzJmT+tqlS5dGnBEAwLx582wWesy8ys8776ymgw8+4uCa6klntbS2HlI7ZUrz5MmTQSTgFwow1mhjrAhdxyllE+now6T1YtK6iEIISKkglUQul0dRPodcLgcVZumJZNSSRW8ObNrTm7AYs9vGjvSGI3MRYncagOp+YPGP57Uf/z5mVkSks7Tw9z5aflf9ofpEK0bElpf5pZsuwDEnP4Ed4cyFX7cknRSBm29sCZm0LuN4cSY4acwJoWAXnHI+trxE2NjNaHobo+ATAlAi8a8MC5aU7gdbK30QFVWKt6HeXrEAGF6YNCQFy7xSEtVHpJEMs88SkbLWvhWBZmN8pTZv3swAsGzZn1afdJJvPS8nptTWoqKykvr7B+TQ4E4eHBy0SinsvddeU2bvvff5AwMD58+bd/zI9q1b/9Tf3//Exs0bn9q6dcfjd955c9/zz/9pDRH5u+sysXDhwnH/vqioqPWzn/2s6u/vP3Jaa+te9fX1+5eUFs+dMqV2yqRJk1VxcTF87WN4aNju3LnTsmURvmLfI3cwQyJkHdmgk7EcaKUIEVgyKuUhn8+juKgIuXweylPjZmh21MvgyDC6EkPxxNvh8ATQhTM7YQ59Um24BEGgsE4HA9pooTyFmupJ8wCUSCmH3MfRgiXBU7nv5dGHGg41Jw8PYrhpFvY+9fv4r5sIFzJD0Ot8sqrZmJ0bpqEjrDUUD0GQVC8p3x8nsKPDEkThfla4cuEzeu7Oo+1twxiNYoEjUTNK5IghEkhKgoYNbFGFaDn0UjuXCA92LILcdE0omE90P4Aj0+LccVMcCKlLOStbCy9dupQBYNu2TS9u3LhJNDU1wlrL+XyepkzJo1BZQQP9/bK/vw87B/qZiIxSipobG4v2mDlzPyHEfsNDw+/f0deHk0480SpP/nnHjr6+rVu3baqurn569cqV3Lu6l6QSPSed+tYXB3cMilxpqV23bl3lo48+elR1dTXX19dRW0sbNm/eOrdmUnXt1Pp6uXNgYL+qqmpZXl4mSkqKoWTIIfF9+L5vRkZGotwY7EjKVDaMkY8ooBDW01JKyLxCLpdDPp9HPp+Ly48slz4d0BTJgCbL2445QGq7KePml+Lhh27V1lgoT0FmtIS0H4ATxBSZsk370Ifet9f3v/+jZzs6OkRXpDwc+s7qLbjVbBefzVeZvIXnz5jtXbD/14duJcJtr7dep7q6ujnF+fyzQgiPw1k1kUhJLoyLozvCnex4vuoRoHom4/S7t4CLNYR1BHpAY5bXA+qrhTYwUpH889340KJT7LUhgxKh5+hZOc+71VprRKRwG8o8MNgKQUJr81JPb+8hAIYyuusoLS2te+bpZ5buNWuvGmusjQYl0WcrFAoYGhzEzsGdGB0Zha8LDBBLKazycqgoL1fFxcUAUUBm0hpGm5ilZ6wOa88ga3leDrmcFzRrQkTeRsHxMsP3fYyOjsBoHQkfEcAiKB0EUYaPbqMSxJrgnAkBKQSUUsjl8sgX5VFcVAwv50EpmR7muMKhyDhuIzEUjhEYHscwmDPXHeRoWYa0hRDylDK4PL29vVi0aBEeeeQRlJWW4B3vfBdOPvlk+AUdymmwUTkpb7pp0YXnn3/+jRnuCwHgmhpUnLck31u9r66AFn6pIm/5s4WnbjwIh0TlzW5ndM8TRwgpFXOwdoQMfu/e+Vm7ELejj06bV0QYWkcYXF2Kir2HoeEjKrfT5Z/zBxYAWZbEKC3FwQCune2wErXWaz2lRomEx86uZGy+HYyXm6ZMmUJhueKiH2JoaGhDcUnJcgBHgMAkHHjUMnKeh3xNDaom1UTTQxoeGiIhSPjax9PPPIOdAwPc2NTEzc1NLEhgcOdOHi2MQgVNHAWOysGP9f0CjNE2UgSwlmGMpmSbCEIEaIWM1/EirYeME3HwdRJSCkhVjFwuh6J8HrlcHrm8FwYWjcnWNM4SNY0hATvU6sxNkA1wuGVMeNmD7M2QSsYB/sKLL+KH11+PX/7iF9i0eXP8k2786c9w9dVX4/LLL0dhpAAJwQChqqriLQBuDJc63LATRBgpEL+kUHTYqB0SO3y2U2bSAWdcLw8m0k+9nqyulMg3Egliqx0lPBESMTgRv3Eoexwv5SZ0vAitIgUU+oEdL+dQvbcP32pIGZY3nEzLAnU4hg0l16wlQWBwKc8FQAsAszD8yb7vv1yUzxekFHlrAySe01eRRcBynA7g+fEEj1avXuW1tDSP600UqJIESxCe50EphbKyUixfvhwXX3wxHnnkURijqaysjA44YH+cfNLJOOH44zFt2jRIJTE8PIrR0ZGA0xOq0WljRKLYmwxoYrpvJG1BSEREBUEKESAfXlB+eDkvPiaR9iwN1gwtpwNVwBEowpgtpZivktnrZsf/nVw+Po0j4MMGRlt4ngepAK01Ft+7BNf/6Ef47R23Y2BwKJXdI0j6qquuwulnnIE9Z+wBrTVJAI2NTfuMd71CV76CPyKXSqjDAILWbEvKoSrmmAsAPDV7yu7rEikLe7yKWeQOusMuWS+qh8fCVykNnbC2MxrY8opBWzwVFRBuK+4O2iJ0RwAMiZJJPAUwigh+9GDcunUrl5WWbQZQHgpRk2MzTiAYQaLY87yZTqCnXtu2bRsdoy8YFVTOTRuMrC0GBgZwzjnnYNmyZSjyFAQEdu7ciYceehgPPfQwvvGNb+CYY9tx5plnYN68eZi5xx4AgMHBQQwPD0NrPUbbnIggZFByKM+DDIcoAR4d3CBCEEgIuF5FKSCeE930iApLKRVbZFb+nImoo+gVz0OcyWhSjgaqtiBnEYQBY03gUCAlRE5i48aNuPW22/CTG27AY489FupdAnlPwQ9LOyRa9RgeHsaS+5dgrz33hNFGePBQWlq21+zZs+uIaIOLp8ditP14ymL0fUzMikhKALkqehdm84IvHI9tu+tup5RSBU4puMJxQ0Csl21d2WJXZ5vGIWoLYMerAj5McMIcMkWiYEwOd5tAxIJBVgnU13dgv/VdeBodIAQ7jjuFoD8CmB7p28cKUpQS/i9MNEqePXt2N4CjCMSx7rhjgRLJMfu+D8/zcM89fwiCPJ9HoTCaUp8lImzfsQN33HE77rjjdkydOhVvectbcNaZZ+Koo4/G1KlT4x9ufBMEpQwGLtjFBqhDQ06CUsBp6F2eiUgvgBBSAG52pS/Fqad0Seo2mK49BoU8FWttcHOGMhMvv/wyfvbTn+HGn96I3t7e5CYWgXmZ8fWEKi/FRUUuemOnTJlScsIJJ+y1bNmyDcuWJTvMyzYHB7FxqempP5KZFYRQTLA5M3U6ag75aOHQJz+E33csgtid8kVo3z/UWgZbFrGuZHYHMs4eBMcuIdQYT2rJKLtISShsE7AwsGTBZEL94MQyKni/RMNdEMEaQmm1kAddELTrznI5tDGl2UFI2MG7GO+EkTS0c2ch9egJSsTwuGmMutuKlSuDoZExsOxYDbKFNSYoMZRCTils2LABP/vZz3BuRwf2229fnHnGGfje976Hl156GdKTUDkVB7nRwbTQGhOogYWBFECCIbtRBLbpcZ3M6TqeMgO4RIqZs04CY2a0lHURzqjbIdyLtcbChpCgUgo7BwZw++23o6OjA4ceeii+/B9fRm9vb5DdhYgnoTHnJajEIIiQz+VQKBQwdepUnHjyibDWwlOKANjS0lJMm9a6FwDMnj07PrjZS4Ojbj607LlCv7SQEGBibS2X5iz2nI1jAWB2x+6VL4KIKjjcDoqtRAiZ7JtlHiZKqxiDtROEJOghBWMBCE6szCM5qVhOL5EVJiIIligpUZg+KxE/jVySmfkponG4lJS4ahQKhePc73Ffm7dupWhpOz70AMBz/DeTJ1ZbSyuMMXGdGQcVJ1rj2hgUtA5XxoILvnnzFtzxm9/gwx/+MA499BC0t7fjC1/4Ah566CH09/VDKhkPZ5gBv1CA1Sa9DM3pTByhNcnxOYkndTM4ZFGnB0jZujjBHzMNwybaWgPjB/beUgWfZ/ny5fjSl76Iww47DGeddRZuvvlm7BwYgFIqktWIFkXG4TsElcBooYC62lrc8OMbMHXq1BBfF7Am6Iva2qbXAsCCBcmALZpALP7ytqLRQUhJubCXM2ShUd2A2cAYh8NdDIyIMoti2eLZdVIYA2g4u2MhOzHAv2BGgkVq6TkagTbT8IxxP7RWCBIv/E4cB9hHNjnNRi6XezWbijij9yaJyrJvu2TJkqDU6++Pvdp5HAXcaA1QCgmjDU4+5RQcNPcgPPP0M0EdHW3+ONtFLkQQ1aMu92Pnzp144IEH8MADD6CzsxNtbW044vDDceyxx+DQww7HrFl7oaSkNPU+wR6mSBGw4kwc0x8ceM89IzxWRprdLE5Z+5jghjEc1d4KkMDAwADuu+8+/PznP8ddd92FwcHA7FrGSyIBZ2WXuHU4UKyvr8e73vUufPQjH0FzS0uSPDjxRBkcHDwqnB+z4+nJWAB6tRw7cp7aqEB1PvnMbMmHBDMOBUzxeYTh3anTFYFj8R64q1ARpziSFyZ2nIMTmw5yrSej/TNrYAoIoCdv7KOSEo+SUM/EhknGwALIT7E5AJjnGAdYa3Mi6uJT8sjJk4iknLBWGxkaitcyo8WplJWgizQwo7KyEnfcfjs+8fHLcMdvbsfIyGgI3O46gWSDPmg4g+BYtWoVVq1ahV/+6lcQQmDPvfbEkUcehfZ57TjskEOxxx57QKlEbi0uz1yfw8QjYxxHXBpH4Tez/EGOzB6CDf3oqfXEE09i0aJFuO22X+PVVxP3QyUlLDNMmLlpDH2dUoQwhyuEefPm4eqrr059pijhsWVAAvvss8/oeBJJnQyxcCe2FJWKVwWoTkAwyEoNWFWGqdM/jDkrrsFTHR0QXV27rtMFZ43PYlH4sRrblMkwycPAGRyF94slC5JjKQkUli3sPmeZw/cKTlZRZfiYmDce+48i76p4mJKoqRJNTCaKCSsZC92whDKJlYqUEtZYNDY24aZFN+Hxx5/AGWecAXYgs90xbI2CvuBrGGNj8pSUEtZavPzSy/jRD3+IC999AQ45+GAcfthh+MQnPo6lL74IKYMnS7yVJCgG+Dhj6OXyy2m8sb4DJ8ZWK0SwHAT5XXfdhWOOOQZHH30UvvGNr+PVV1fEk1YiCvoUpzzhzC3Gkb47uec7+Ppnn30Wgzt3Qhf8tPBpmBgBoKamOrcLkVLa0Wu9hDYiYHyLokmWDj8P+ch3fncWhSluZFwxehcfjyZtnDz+bOTkkLncQS9NkKUMmeOMF47r9oDY1iSok5PPKWzi7Zhe8OSxjNfYAZywK4JdriiXikrK+tBSElBMDJJBFh4ZGcF+++2HioqKRNp5AtmsXf19cM4czZOwxIka2p07d+LJp57CN7/5LRxx5BG499574SkVBAyN/dzs4okpMTEaIwHKzDEzJ1b0DAxAcPfdf8Bpp52Ghx56CFprKCnjMs1tLjG+EELK7miMQDqA9evXY+OmzcGTytiE906ARWCZuXbt2kMAVMjgiUyOIzkA8PB2toBBpHPMYKs8YMOzYh4AbNoNPF1Ya/tjd4aYjpuoyab0SijtwUOUdPdO+QhiQlEFw4MC28zACexMVJ0L4byvLYwbMJTi4cQ+OnAsScQ49pDzog34sVAepyasydJAWKKJcHizc+dOPPnEE6lMtbvW2zTB7UChb4/WGr7WAXVZCORyeQwM7MSXv/wfMGxTThhIe8fEkKhry5JdcyMnwbj4gTEWIODb3/4WrLUoLspH6FYoKzgWkHk9LCpmhhQCg4ODWLt2TZDMRFpML2qCKyoqvaBDG3+iP7QNudjNmgEbEEgxaWZY4s7bjYwulXqChADiLQ+nHMnYA7q2gOkKkdPNvyUUNzIIMmNNwrEzXKppYtesBNDjBLpQ5MeMUMtOmZUgQKGz9LiviooKGvMzORPg4DHTQikl1q9fi81bNo8b6K918TkTYOla20UPOWzyfEgpsWXLFvi+Hze27NplkJNkkt239MFYZ25BGc5S6O2pfY1169ZCCIHRgj/O1f3fa8cxABHW6Zs2bUo7d2T0JcvKSriysnIMwBBOR1HdTM+GtBaOSlwCUDF99w9NWGNzEaqQ1fvOXiDmiRZqKK7/ZIi1l04zifoUj4OAxZxgp+ZnAkNgcFuQmpcAqK3tZgAYHS4ckYhduT5AEZ4OGGPyE33Qtra20VSgOhPCeA+W0gHFYbBs2bQlgAbjkfZrp3Nyzt+YEi+zyZOoWslQPMjgoAMPRFFRUVynu0MuciwbkXpGpqHhNDJDaddoZnieh9bW1gDX9jxHl/31PKV2hV0jnhbHitZRv+G8iZIe2traMI7jIQFA3b7q1SArAyQJQgkoCBSV7L6PhRBS3M/OGkU8HSO3yXRLm4iBaNOZmZ1tccWomq7B0BnNQUe9yuG+RCNAghTGCkyZww8CQO1mcKw+LESzOzmMoTwih76AFydKSCtXrpwTBjqNYfGlILyEGRndFDuHhmLYL3XD864zGl5jCTWCCYWzejY8MoJp06bhqquuCphNUqRoc0jtbFHi8ugMasZOQ8OngHBYGCTAlvHpT38G1dVVGB0dhSflbtkU7s5+ITmeQ1prx9DXLQ+TG7WqauL3LAxQkQj5V/GQMLW+vxuBrrVea60NND0tI5ldpjeIEskKjqGpJBuG+1wEaJ8hy31M2msYBgUIWFBmaZ7Cs84UjbYDkWkShJGdjE07MEYGQkk16EJqIjxZ7NSERUq9AAChXETquvT19c1Oj9ozTsiWx3BTUky+7Kb7bmS3VGUxrs8Jh+gH0NTUiAsuuACdV12FBx54ADNmzgBbDpGP5MnlJiByMnnWwssROEub5YafX1IwsDn88MPxyCOP4Nxzz4UFj7uQQUjvDO8quMezwoggUxLODiunEZodO8aevwVhC93zuDnGQx7EMjwVDAsLvXP3M7ryff9RKYUWJDxQQK5L7ELS2Y5dzcBY/D42+gncFXYyphxgUNHiw1qGFNGzm1LPfGdYF3v+SCng9xt/2c8DIaIwmzOAvGXbIoJuhoSgFDEsWkAY9f3yiT5oS2vrkAttjTtk4TTyFF1Uz/NSJTBnnSjGFY0eq9NI42TFBG8+Dj/5yU+cJYWAZhBhzlnx0pS5X8bdjjMcFmT8RiNIVSoJ3/cxa9beuOj9F+Hmm2+Og3KiNO4eQwrCHzfTc6gxXZXqTzg05bXWQkiB0dFRrFq1asIglUVcBsjIsxFWW/gK2LFh93sGsXHjxj8D9GcRLAiwi20nxSWnRTIdL7SE/BU0OOwrNBzvo8gToW6WU0Eyp7M6RMzAs6GchQU2bO7CCwQAs4P+vKamJg/mvTjBQhMDreAuEdaYEd/3/zxB/OWrq6or4fBCUpQnToJJRE8XZ8JZXl6GfL4ohfL8bx7r494Y4c3U2NAEYwxGRkbCAKDUJlCCakVLLwxEjmwuEpbte6I/2GigF2LxYbKQUqQnnTyxy5b7ZOZIhWsXYjRaa+TzeTQ0Njic+JTUT4CqDA1RX1/fhE8Mrwo2hjIMgzWgDbB+efDBlyzZPRy9AODREOqxEVErNugld9mWUn6ZCZ4eBABrQnG1QMtZPgwIMhNQFK9oWbhFEkGAGCygMbzDXx+uP1JEeCiVcooIJQhcnZh4YBTUT6OTJ09e4cZTZ2enICK711571Q8NDc6Jh2TgSFLSkYtL6luidJafMqUWlZWVQQBmsiftwlVuV3V79ktmz94bUkp4nhdTAGKyFrlDjMRPiVMZ3UGzxg1Wdtw5XAQsuKEbGxrheV5Ixd3VTToWOZuQAsCM6poatLW1xXQDDhlywnmyDg+P+AA4WzYtiLJUBWCgo0VMCAUhNKNudjBpqd382uhLSKkzN7NlWDBZtinuFaVQk0TmwlXWjOSTCwNAbfsI6vaS0AUViNpxulZMFMgSmIicS1gY5KcB8ALXHr2oaB+pZFms0ZzB98MTNLR+/fpxx5bvetd7S2tr62x0MiladEjzEpIyynJ8QbSvUV9fj+aWlngw9VoZfHdtT2S4glZUVISDDjpojP1LSgwoFhFya/+kHBQkACHSjAaX8Zgl6lnXZQNobWtDbW1tCKmK3TIW2tUXRAjVzOkzMGnSJGhfI6aBRn2LCCDttra2JwH0G2PkeKe0pNJajSEAAQks5wmYgsCGFbvvcSoAoGDMA8bodRTJgAlK7Pwi5cwYRw8fgZyp/RiwkjHz/cMBd4Vs6OUuwv8mj61osBGISgc0XsEgw4wtG/ipiJUWsRCJ6JAwmFNCgRSp1AZ35nPbt2/vc2zP489nrX9oeXl54BTNSX5Mrwm6ZvfskKIYSikcesghwT9PQAHI0hxoN6ALpQIOyb777ItZs/aC9nW8opImZ4VTW+bUAlyWSu2Whln4NMVNiY6QIxNkg+rqauyzzz6xCu7rNQyl7EpCeGcdfthhIb/Fxvx6Eumzs3rVKj3OWwbKXMVoHNxg5lgYWAvBbOFJBd0vC8/8PNgPzviCTWiHodavXz/EjHsDc4VkWExj1q2yrMFQC10B/oBC0wmjmHaEjxE9AiFNTBeNt1YiDDnCsjkMd2NZkBWD2+3oqmV4CADmLwDX1tZymPn2iXSVQtKCa07LFDhXPA+kKboLFgQPv/3227dZKRnDhWxT/JjUFBEuYuFkphOOPz6DvEwMrbGrmz5BMLBjyXjuuW+H8ryg/qSxmHlcpxClKF3k2KQlIZ7UlanNI2dWz44OpmtL097e7jTkeF3inZzJ9toEjMhTTn1bslYXqn65ljcA8OLSpT7GupMHJd3FRVKUUbkNH+aWyUqw0MO8cvAevEgEoOu1l6RFDMURfT8azLLTiwYFBSeMIAeRIQCkCKKgIKsYB31+AKBRgPwAZ6dkiBE0IiI14An33WEZnFOWBreaPz7/GfyZGQILwYH8wewciA4Nt8xFquEKRU5CYtP9E0GLVVWVB0cYeup4iFL6g1G3nTAGg8VkYyyOO/54tLYGHHU1waOdX+PvOFPD+ibIpP/27ncHmLkn0mzD1Kh/bA9g2WY4L5ShICeBTeFWkuU074g5UXM455xzUFJSEhDQdkEcol18Zg6fesyM/fbbF0cfcxSstZBKhg10ws1RSjEA1np0SUipjt+6Y07wY6bug9aKJrIEsqGDqBXQGB4xjwLAVRZqd4a3InJUXr169RPGmKUyWEy3UTefCA1FID2lzGMlAYVhhf3+YwcmzyqgoG3gdccAh04lFs5NEzZTgdlXsBuphLQE4nUv8cNhfS6isqOhYcexRDTVBo8BkVJ7DbyChTFmp+/7z2J8Z2Ovpbm5Octu5fGaKocvzw5TyWiN6upqXHTR+4KJolLjN6G77s7S9XnIYHzve9+LhoaGgKctZFL0ZBaEiBKNfgvrLFckDf1EZrtj5Csc17tg4iigtcFee+2FM888E9Za5KR8jaZ0170HM+PSSz+CXC4XbFIh6X9CaJEByL6+PtqyZfuDALD5e9+L3zoialU0ib3LcnkhrbIkFEmSNMqE7Ru5+/UsXgg34Bn4ipBEQgh2CUNpLkAIC8oAHBzaIDDjyk04oGMIfoEQcOopNTAhx8fIVTCLKKZKCjGwQ1LPU7gZAJbNT+pzJcTxIkj9dpxm34Yubc+HTsgii7icc847m0tKS/YGwCQyghtpeAVjpDTCesDLeTDG4MOXfgTTpk/DaMEHjad0tasM6ODPEXZeX9+AT37yUzGe7OKD5ADwFI3Pw/U0SqHWjqox0YSAPrstf5xVOdXTMjM+/elPIZ/PQ09AsHqtRlRKCV9rHHTgXLz7gnfHixZZYhoHgU7r163bcc0116xhZpofGO4CAD48L3wa12KOggoFpAEplOjbJEefvRePRCXu6wl0HWb1m7U2L4MgmULjKiGSR2fkqCI0zAhhaLvCtE9txlGX98P6DBHa4ApOmp1kcdclJYW8FiIYSyYHIdav0Mue/SKeZobo6oIN7QMFgLeGmz2U4PFJ+AQbL3x3WGMKpz6nYO/09ENqa2vzAKwUksZytRN8Og4UkZY+jpq5mupqfOe/vxs6zQkQ7W4lm1RaUdlDAK6/7jrU1k4BWxt6skZZ2llwpvREk10T3szY1Ua4ustDT8kHOAsvGdRGSgljDPbddz987GMfh28MvFzu9Y3/w/fK5/K47gfXIp/Pp/nnzs+zJpBl2r5j2zPr16/fMsHMSeTK7X4+RkEwMGxtTjAN9/PTK/8zLnHt6wn06Nz7vh79pD9stBlOPIukIkgVjN15WGB4s4CYVMBh161H+2cGAC1hhQ2zDTuYO2WYepxM1iLXBRLwLfHgRv4igEKou0cAuLmueW8SYq4xJnB2zFASiCCstZqZbxmnPgcRobKi8szwIjp6mDwhjMCpR1Fy/EpI+KM+3va2U/HlL38ZWutwOUGkHAHEOMEf/TRPqcCEyBhce+11OPW0t6EwMhoEeWriSGNFqHch/hnTWTLDvTSPJwk4pnDg41IDQohS+xqdnVfhmGOPRaFQCKfCFJ+KbDS6T6lQhBXf+e53MHfuXGitIaUMzM8sxzu6JAiel2MC4eVlf3o43HlMxeJ8gqmei3KVs/sVMARrjRDWWobG1j+ZWwFwGCt4vYFuOjoge3vX3VG5x2h3WROEPyB0YQdhZJtAYYcCsULVPhb7dA7gtHs246BzR8C+hKUQemSbHjuzQ9whd0wRRpYRtlQKsWbl6PqbT8XNzKCFxwU9AwCGsh+UUhIRmbhjsrHClhEkyDI/39vbuzTjN0pSSsPMsm5q3TGRVjo75YlbW7ncMkK6UaWIB2MZUkgURgr4zGc+gy9/+cvhEoVFzvPgKRm7XFOmcZRSQiqFUV+jqKgYv/rlr3DxBy6G1hpezktnbKePSWnnMJxlZ8KE7r1E6SWIRN/VgVPZ4XplnaoJeS+PW2+9BW898a3wfT/QalEKUogxTbFSEjnPi5ekv/2tb+Piiy+G9nVAJ3C19G1iKgAJsWXzFv71nXf8AQDmO/V5x6IgLg97rzyqppGqbLj/5ZGVG3p0/8M/tTeBQQuX7L4kXfqOCFaSxP4fG7r6zDu201tv3oJDvroRBy7YiCP/Zw1Ouq0Xp/1uA47+xCiqaiUGCwSSDBHtlLqyGM7aXDSNif7Ncoihk2GCpRWP2/8GoLuS47E1NTUVUslzEBCNZGDzl6KmMgNktb4lLFti/Lyjo0NYa/Ge97znyJaW1noAVsigqCa4aYlSNFa3EYxWgmIokgKKqPI8aK3xmc98Br/73e+wzz77YNT3UdAGxiaaKy6PPBLEP/zww7FkyRKcd/55wTaPUsH0UyT8jzGDnsz2Q4KEObu7LiTKnOHwcIqZSeGeLrsriM5QUAgByxaTaibjt3f+Fp/73OeQKy7BiK+hjY31baLP5muDgu9jv333xe/vugsf/X8fxehIAVLINN9fkCuWZAHQspeWrbjzttseZWbqcurz2R3BQVVMxak5DwIGLISwpUpR3xr8ct1N6O0E5OuxexnzPIwMsz7yVMXX95g7fNkW+MZDwM0yIPiGoG2w8CsoPdgOkoWAq9Hobr0kF9HCaDbFOciNr+LR783kozsZWEiwaIdCN3Rzc/Mn8rncN4wxGgSFFGIc6vMybx4eUfts2PDKlpSYFbMSQujf/ua3V53ytlMWWmO1kEK5zVu2pcsqajFnRyOUGnRETdbQ0CAWLVqEn/38Z3j2mT9i27Zt8dfk8nk0NTbg4IMPQUfHfJx+2mnIF+WDR7pSAV/H8V+JZeSicbOjl+g6jSTTaHZmCsmZEWk2XojoxcspKdVcQtowMKqlo68jIrz44ou47gfX4d5770XP6h4MDg1CSYkpU2px4EEH4R3nnx9Dk76vA7UABx2Lt6SSG1YDUDfeeOOnL7zwwv/kxazouFgfPRZOe+8TuT/WH+LvZ0bZFOcVBrdKvu2q0aP/fA2e6OjaPeGirFFU/JoP2CDY+6+84snSo6YcTIf3jZKWEgpkALKQ0TCIo4aM4mFSyh45nH5SxFwM5eksE8sc8cgW4Pk/FD5CBLusKxz5d8PW1dWVKiEvC5tN4S5DBBfcGhJCFXz/dxs2rNo8jvWiZeZcU1Pz2UglLR4zs4y4Oy7cFtEgomMmyiT/cPPIGIOSklK85z3vxXve816sWrUKa3rXYPuOHcjnc2hsbERjYyOqQrK1NRba15BKjnHhJUHxmCLmFI2HU3N6ns+ckmFwkk96hdGVfiaH28EJ1TE18BIieG9jDPbZZx/897f/G0NDQ+hd04u+HX3I5/Ooq6tLq5IZA89T8VPQFVwKQoBhLbMQQqxatWr4+uuvv4mIsGDJgsTDqAOCCOawj5bMLW719x32YeETl+atWvGi/tafv4fHF10DOf91Wryo8WQGujqCBPPWT+XOO7Q690LlDF3RXxi1UrFITUqj4GNnWsmZ0TWQegwTEXLwdCmE9+wTw//90CV4un0xVNdx0O2A6ga0J+UlQlKDtUYTCeWag1EwiRLG2h3M/B9ZI92OjkWSiOxnP/nZQ5tbmg6wgUaDZMsZHnn4pLEZxVjOGFeRUxqkgizgblsTLBETCG1tbeNuyhitg2VoErGmSaz9FwYoc5r37aJVNA4XfgzjhtLEg7Sq1wRKXWGZxZze2yJnMh7d0NZalBQVY68990p/tnCBWshA8TeWDIkIZAJZ9w0LQD7zzDM/e/DBB1dmvUZnLwoOYOY7zVk1tR4ND5tCcZlVG5bTmt9/FF/pZIj5eP3WLuN2rV1dMPO7IO796vaeZffi5MJm7i/JsfCNNeCQuUIJ5dOG7tBZ/jNHwqThkoWBgWHtl0t4vatG7vzl28yVzBDdYQPaDdi6uro2KeVCEyiHy+zyMjNbIYTQBf2NtWvX/jkb6IsWdQAAH3H0Ef9eVV3F1lobY+Oc5rCM3SwIlARIZGgBydaBY1SG2J4mUt4yOlC6ijb9rbEhSUoFAZ4ZKVrYhB9OmRWGzDK/HWM9ksAgLh03+xTg6OmUGZS5Z4Iyirkc7ppFCEIkS2eZg8+oLYy1sR68CHHyeKJOkfxFNBvgaCGbpZR45ZVXhr/3ve/9JzPTggULUiDOAsBgP5RWN+A9khUqi/MkRqV88R774cHnsWlZV1p36i8KdADomg+zaBHkbR/a/uiyJfrkofVqoMQT0rdGB/esDBhz7Jq1GDCCaWhUpoACfy02FsayXyrhrVo+/Mh3vqLPEQKjzhNaALBFXv6rSqni8D6hSF88fGwbIYTytX55zbo1X+noSPyKnCGR6ezsnHnooYeeYoyxIhg3xlRWRtrrNK67bfLI5xQuTMnFt+wYxCLG80PULLjoKsjaQkqQFOFjm8f4eLrkNndBPN0aU0o+bgy/hDKyehk0KRr2MV7b7TS14kYUo1vueJZEpEsjIEXaYidCVCJUKF6OsRRDzWHmls8991znfffdt2LJkiXS9XltXwxJAnzBF3InVNXJJkN6tBhe7pUl4uoHPoI7OhdD/W9d6XaJQ86fD9O5GOqX80cefeqGopOG/1zU2+AVKZKeNiwtsUyp6wbnxMZqViL8P2uFFV5OT/Zy3o4e8dtfXMCn8LXQn/98aH4Q1tjNDQ3vkZ6cb5kNidD9KNEjZABsrfW11h8BYJwNpHhIxMxUV1d3RHFpSbWUUgohjGXL5JjEwkEOYvQlVdIma3WxGrBjg5K1OAQCKQf3SZDIgDi7LG75QOOVFJRWHs5Mk93pdERfoIxUSNYtY1fQg2vPktqacibZPAF3kUMjAJeKkJ1DsFPCaq2tlEIsX778hXPPPfd7zCxDB2+kpqEMSVPMZ0Ru2ORRyPe+Mnz1L072r1zEkCH0/L96vSbntPsnsB0MecexIz1Dz/s/rdk3v8/k+vxeUhCNQlu2gfhImPuInc6PyRoicIkqknmrxKpn/F98e7Z//sg6DDPHNnoKgG5tbZ0llPqNZaZQFZdidmFQemghhDdaKCxct27dDa77HBJzLAYgrrzyyudaW1oeUkodNHXq1KlCCGLLmi2HMuTO5r9rDznuTii5iksJl0y400VHx5EZ40paZUxDHCuidOA5ilpjxJYm0K2MtrQ4y7aiFJkgWFxIIS70mtwcyg6vnJtGhKZh6YrLTQThw1IzlCf19u3b1Xe+8533PvLII8tmz54tXEixgyEXEswx3ys5cd9T5adywor1j+Pqa/bXV3Yy1KV/oVX6bs+wXRuNs64p/tDMdu+j1XuO7p3zNPwQemQIq8EEWPYAIcEY8RV29sjnVj8x+tVb3ml/SQSwjeNCAdA1NTV7V5SX3ieEmhqSfUR6xZC0lMIr+P7Vvb29V86dO9d7+umn/V2I51BocVj2sxtv/NCx8+Zd2tzc3Bqddmsh45U8IkeCMZ29U/uyjpJZyuLQ3dGPGmYno4qMH1RU6iG2MUTKEWPMgCjVmLpPhIzQf0j2ikoeHicmUpqJlCjrxjd8xqvIDfoY8cyaw0QuJpR2oLZs487BGNZCCXVzV9dn5s+f/5VsAwqAOhm0kJD72OryF6qqueWPtxe+cNu7C18O4W77l/q7El6fKg2F58UC8N6zqHReed3o+8ob7aGiWDTnqsizAvB3Mkb7eN3gdvx+3Wpzy+3n4veIYcvgoKNgnTRp0qzS0uL7lFQN1gZGWk5StKG/pzDWXr169eorI0v3cT64dKzHsWjRInn++ecbay3233//qs9/9rNfOfTww9/T3NxcFPF7rLWSsIuVfp54RWyXSj8Z3c/xlqWz21sJrYFSs55k5VDEEtzkuHOkfiBnhEZTpsiZL0OmDMqo8vI4TuHuTU+pzeEE+k35XgUAsy+E9G655ZbfnnvuuaeFQZ66fu2LobqPg+64Ti6sPxwnPnqv+ciTn8BToWGbwd/r1bEIMrOEImadhz1P/AEOPfsWHHL4pTgUQLn7FOxYlCqTCABaW1tnzZg2be0eM2dwW2urbmtt5bbWVjutrc1Mb2vzZ86YwdOmtenmxsYvOnDoazFkpfswXbx4sUpW6t6110MPPPD9zZs3D3L4Mtpov+BbaywbY9gay9YYNjb4Pdvgl2XLbJmttcGvzJ/Tv3e+z3lftpatNcnXGRP+nfN9xjCb4P2YmW38vZy8p/vLhL9s+HNs8I0283Uc/8zgGNiMfa/U9/E4n9lyeByc+b7gV/x/1rLRlo02BWbmF1988TcAVFKSjv868Qve/m7wv5ExS3/J93YsgujoAM4TMOOtSS5iyK4uoGt+KgMLAKK5ufmdnlJfBtDIbAsBfQxERDJaDtZaP6uN+Whvb+9D49XkrnVLU1PTIQC2r1mz5hUn4E3CkGURPS4//YlPzDqt4+3/r7W59cLGxsbi8NlhDFtHoobG1J5xDmMao0OeVnoa/7RSvDjkGGExj8tXpzF2FPHUYkxZEuXsqHRxYVHXNTo6PutQCLLHS0i7fKcMClzDL3IUMd3Ns9CYQUAYCKiHH374zqOPPvocZtbhE2KXJQgJ4CoTrtH9A74InRAdiyA7FkGGsN9EmVc0NTU1trW2vrrHzJk8fdo0nj6tjadPm8YzZ0znGdOnD06bNu3Z1ubWfwdQ9BpNswKA1qlT954xfbqd1tY20Nzc/CEAOecmkC78yMzSzfA33XTT91etWrWRk5evfW3YsOUxmS3Ksjb9y82uNngiRK84O0Z/n83g2Qyd+ndOPRk4k52jLBwd3LiZOf5lUt9rdvFz3c8y/nuFfx9lcmvZGMs2yOI6ehJ1LerqAuAxM3V2dr4207AT4i9Mvn+VjP6X/EyurKysKi8vP0UJsae11tqgFH8RwDM9PT0rM6WImSDIdXNzc0M+l7+PCHuF0mdktHmMCZ29vb1/AIBw4mqj+r2zs1MsWLCAogx/yCGHTLrqqs+9p6Wl7f/NmTOn2dFA19YYSREtcTzKdLZxzaznUcqs1nWBG9v0ZQX23SY0dAdOKBeuXPREfUQGmWRkDL/CJpLG9MHpxQ7X9iY0Vkt2AsLHlNWGpZAWErK3t3forrvu+twHP/jBb4auO6+Zyf+VXxM9FTB37lwPAPbYY4/GmTNnLNtz5kye1tZm2lpbua2lRU9vC54QbW1t9zQ0NJyQkDM7pIvoZDM8gIqvf/3r71uyZMnD69evs06W10ZrrX1tjXHq7jjFc1y/upk4+T2n62n3SZCpv5NfnHlKZJ8CvOuMHNXjZuxTYszfOVk7ytTRcQXvE34G998MR+fBMLPPzKy15iVLlnRfeOGFB4SljPw7JdN/iIzu/mzZ3t4OdHcDwX/YRU4mqMcFAF1XV/e2kpKiawTJVht4qcgowwSLtAJSSWENg8Hf13ro26tXr395PISGmWnJkiXy+OOP11Em/Mzllx9w3FtPuKh12oz5e+yxRy2SHK9NYIcj4wERJ4JIbq3O46TcVI3N4+hEjIEHM3zziNeSydoYRwiDUlaMGPdYUpylLMSZ+vckgVsOnL1EQE6TAPDMM89s+uMf//ipiy666IZwvjcGQvxLIcJ/1kB/3TdFxFBsbm7+mBB0tSShmNlACOkSrmIFX4YhgiApyGg7xJZ/yv7oN1etW7c8U/ubGM/t7JRf+tKXdGwGW4raX/zoFyc21Td9oKWl+YjW1jblRI0xxgiSkijWAkFGQZjTe65EgWv2RHg3j39V4vJjvObQuUWYeIzdYlbCgsf5WXHJ4lCr06psDGZYKUU05EOhUMDy5cuX33PPPb+67LLLvgdgk5QSn/vc54Qz2qfMUsybgf5ar6lTp+5dVJT7vBTqHWEAWQo0NGL6Ko1DvwWzBgkVbN6bYcv8E2b+1qpVq5anNr4dfD6s4wVRzJPGOeecNuucc84/Y/bs2Wc0NNQfVVc31T08bY0NtukEZYI+kUhMG25TajkhbR3jUGjJHdrwxEZcWQe6cfBvjHtDUOI6yOSM8ANOhwiCO1ZmWL9uPV5d8eriRx555Ief/OQnFwHwiQg33XSTnD9/vpmgv5qo13oz0KPjm9HUNMNK+X5jzAekUtUAtCCSyYTCho1bRBngODtxOhoMCIpIwBozbK39nQWu7e3tvSfT5Fq3rIluArehuvDCC4865ZRTzpwxY8apLS0te9bW1nrpoAcxjBDOtkawhMCh2GeyyhYboY0DV0ZPh0S7PiMqhYmDOfuvjupTXHIlNGGKbi5mBstMcA8MDGDjxo0vr1+/vuvnP/nJndf+8IdPhMM8GGOyQ6AIPTEAcm0tLd9iZrW6t/cDf6+A/0cPdAGAm+rrz1Ke930iqg0IWmSISKZYKZS1+I4yZNoXNJjSsw1WOWWkPfikMeaHWusb16xZM5y5WKksP2/ePDFv3jzjBD197nOf2/OQQw45ZWr91HNqp0w5oq1tmnJmu8ZaE5nmCZCIUYtkCppR5hpX8m3iMiealFq2abIauWhL2EdE9prskrnYCiGtUyICAHbs2IGenp7eFStW3HP//fff9p3vfOc+IJCBY2bR1dVF8+fPt04RJNzep6mp6ex8zvt8zssdWND+s6++uuKgLK36zUAf28R4zc2N7xMQX83lcpXhLqbhyEs35I2kHv2OPnG8sWNjNVqmoPQhIZUIjapetlp3GeYbncHTeLU8Ojo65CWXXEJvectbknoewGmnnTjr7DM7zpi5555n7LHHzP3q6xtczXYbCIZZETCEyRWGC6E7xAsh7iJ0VNakYMuo/qdx+FhM45Q0HNfaoeAqZ9GttevWjaxauXL5xvXr77z7nnseve666x4E0B9l7/vuu08tWbLEZmpw4Z6blpaWI6UQVwiis4LrwpotP/fqypUHvxnor53ZLQBUVVW1VlZWXqqEeKcg0WA5oAVT4CMjYsMPR1YDjtpVVOYwuc6FwdYICZKhjuMIBO5ipp+a7du71/T3b3Ob4o4OMBYFfzF7ARjoxDiZHmeffXb9WW8/6y1N9U1nNzc3HdzY2NRcUlLihr0xbCBBZAkikqRmd1Xb8XpKsjqNcbhgZDelYp47Cwi2bFlIwdmtso2bNmLTxk2rNm/e/NiLL7740F2LF//+97fdttLhGcEYI7u6upDN3u0AdScrjKqlpeV8JeT7iHAcCQJbZgb7gkTO1/qO1T09Z75ZurxO5KWmpqaivKTkXCHleUR0ogzdjQMiR2DSk0zuMxQmdncmXf8ItjbI8iqwVBFgthu1Nvcaa6/t7e19cDyQrJOTkfUE5Q0AlCxcuPCo/faZffaes2YdUVFeuW9Tc3N24mvCqaWUQlBWoCga2KRdJjJbXUFwsZRjSxEA2LJlC3bs2LFi7do1z766asXDjz/5zIPXXXPN86FOfqzRcu+996rNmzezE9wpeDf62rq6utqSfP5ECHxCSe9AJsBqE5SHgiACPrMwo4VDV65Z8+Sbgf56sntHB6GrKz5ZDQ0NB3qe9z4hxOmCqBWJm7JJNHgTSUm3lqcMvdCGUA0DNvQalUQC2miAzCP9PeKmknPX/fGU/+Iqfwh8y0Lzp4GfY/ncD8B7+rq4ieVMIwshhHFRkI997GPT5849cN7kKXWnNTQ0HFxfP7V5yuQpyJY5sJYMsyAiIiak1JIsgxjWBIHNbvMYZfVNmzaN9vb2burr63tyYGDgvieffPKR//iP/1gOJNriUgkQC/japw8+fbC67uCntWuH1N7eLrq7u40T8PnWxsYjSalz2Nr5QsraEPo0IfIlrGUjBKlQl/q9K1avvuHvVbb8swZ6NsPHF6CysrKqrKz4dCm8s6UU7VKqmki30Fg2IUYsOHSIIdf1Im7ObGaDHcFQUBhhBjxqPW8AB3cOo7wOUBAY3WRGtvcW/v3ag+0NTrZXTlMWVxWLFi0SHR0dJKXS1qaSWunF/37x7HlHzzts5swZR5WWlB3f2tpSW1Ze7oa9hTXWACSV5PGmx9r3sXLlKn/zls1/3rFjx0ObN2++7/7773/6xhtvXB81kVF5Y60VC7oWqIXzv1AAGGjBdPRgPYDh4HO0K6DbVVbAlClTDijO508XUl4opZxBIjAysMYaCl+hvj4RCaG1v9bX5oK1a9fe//cM8n/WQB9Xo689WK6OL0xr65SpgstOIEVnAThOEE1K9AvZhF3dmAyYVApOuSMBvROoPGTEvO32ASgB4WvfgphLpVRDOzVue3fZt7bcW3PL5rkrHkN3SnpDtLdDdHfHQZ9FcDib7QHUfPnLC/edOXPWcZNrJ8+rra3dt76uvmbSpEmpD71x40YMDAys6enpWb1z584lmzat6/7617/Vs3z58uVpj82gzl6yZAmFjSSHiw72hJ+qQ6YdLb7Eg/q4ojx6177Et952Bn8q0uNsamqa7Uk604LOAvPBkWCoDUxDLcAynFVYIYRUoeyeNuZbAwMDX9uyZcv6ceRI3gz03X21t7erUIjUZtmRWRrB1Jkzp5T4/gkgOhdEx0sS1YxQucCyDTW5AosBEgHV3jGrlYrg75A4+Fs7MOudPoaGRuHlCoAFRkctl5WDHrqiGi9fVwmvxn/Z+vgNgMXGmKc2bNiwGeNzeNxSgDo7O2kXgT/lvRdfvH/H29++R3lp6UkjIyP96zZufHDJffc9/+Mf/3gZgIGsoq3WWi1ZsABLABuuGMZ19uwOqDnoMCv26Nr/iA+XLJ7SgIoRjMCDQBEUXug2dz749tY/FVXo0y1oppSCrA1kPdiyDk5SjMQzgWTQ/ZgRtvj9qO9/ff369Q//IwyK/lmbUd5r0qTy5Vu3DowD/dlx9C9FFhasq6urLcnlDrPASQScKqSYJoSManOAogAkAbBgDhyLC32Eo36yHW2njMIUNCQZWAtoDRSVWjz6+Ur94rfKZWktEWzk7MD9YH7aMN9uC/+/vTMPsuyq7/vnd8659y29Tc/Ws49mhIQRIIFAIA2g0YINmEUgpLC4jFMVBxJXqGASlytLVQrHTkLKscF2ERsbl0sklaSGzcJCsgCJQSsiBgRICKF9do2mp/f37nLOL3/c5d33usd2UhXPYKmrpjSq6Zm+773fOed3vr/vkt5/5MSJB0ae0ZT975ptzqZNm+Sqq64Kxphwplykqg0BzIEDB/Shhx7SkcKW8ufIwYNlK1I2Ee+/3f23C37W/MJSliZWTEuV4CIJoWfcLVfvJD1hCTbH+5ADBsWoFkZ9RjDGWlPeXxZE+XQewh8eOnTo8cbnEs4mv+WntdANoHv37r3NGNkQgn623+/fdnTAW6EkickZdvqKyNXcXdo7duy42Ir8LNa808ArrbV2wO0ozEwxqv3Tavb98aKcf8OChD7EhUwEHwxx7LnrYxP86Len6GzWIN6E8hJga4FwYYb/wxD0gTzP7+n3u5+bnX1sYXS3L5//ryt8rdIhPvWpT2kpMB5NVjE3gh5Yo9B2zMy8bGyi/ZrenH3nNTc998btb1lqpT2VIv9B8UHJVMLX37Y1LDwUGzemQhDVwiBGRIw1IoWvC/ptkM+kafqVo0ePHlpjKnpujdj56aDs+u3bt1/Sabe/Z8qAlyzNMhX+IoTwxX6/f/uzzz57ovna9u/fb9cqmlU7XPm1c9Om84nj12HMG4zIZdaaS6yxYAJLJ3Ou+KMFzv8HC/gexEYwVgjB0o49d//nKR75T9O0N3o0rznoWjpTKYirx/lBCapH0fBdjLkvz/N7V1ZWvlOGjfG32PFHTy1p0haaX7t3795iTLiMYK4VuCKoXha3jGSLMW/4w5Psvm6eLAFriySIPA9kuXD7m7fowqMmuHFTWKaWJq+q4amgekuaZl86evTo19bQ7OoZkJqz+uV+Gqr8xhtv5MCBA1hr/2nBew4ZYIw1kTHmXQLvil00NzE+/nUfwu1Jknzz2LFjj4wUsm0WRFk8Q0V/6OTJx4HHgZsA2bFly6tdu71H8/Ta3PP6KNbzLNJRU4wwg4LXHCEQOUMUOYxR8jJ6pZhZSZ0+EAqpUiinotvE2m3GmLeW1svPTU1MPBDgG5pl9x86dux+ICtPp9E2TRqn0xAVdtu2bRdExrwekauMMXsRudiZeEJcYRyq3pNmqU/6gZDWE1owAQmFm6w6MBax1hY5P7l/MifcGUL4xsrKys2NBSk33nijOXDgQLUQ6/vRyHO/sKP/bVuWzZs3b+6020+LSEtE1BTbuhaOBIII1oghKOQ+yzXog8H7u1Tk84cOHXqgORBZo83RNS6zfhTn+fChsS9P79C3JXnqnagNCt4rE63A/Z/YyMP/cSPRdEqehsKOrspBbVrZDbnHE4oRpxiRSsckVaDWj0MIzygcTNP0G8fS9PsM303YsWNHR1UvJoSrxJjXO+fOM0YussYabUS+aFBfSFTFFA1VkOS048o/nOW8G+YIfYOx5ZRBRAOW267bcHr2gfaddp3/dN7XuxocoEqxpeXv5eDI+zUzM7On2+2+NayEv3j6xNNPnW0u+k/Ljm6AvNPqvD+KbCvPco+ILbkfgoot7JBVvWZBg6oYccbaV0XOvcqH8JE95533lML9IYS7Q0gfOHz4+MPA8sGDB8/UJjRJSvLhT2B//5+TlDb2RTS5UCZjxzg8uU9JshQTSg9xM/Ah942sIFPbJ6ioYmvIs9DJhfIcsEbMi20cvxj0Z60x7I7jo3S7Xxf4modpK7IfkVeLyE5TWslViyn3Pm/EG5iAWgkj0elm0AsFX6SbYBTjjI9C7NwrnvvYoa/kv8dzA5RraWlJxsf/SkuBjAeo3sGpqanpqampawU+ZKy5Io6isZ5bHgM+XraQ+QuF/td/hSKzi18qCqh0/9ZGJGQo/QPEWDElPbGUNIuIFZHzrMh5WPveECzn7d59KKh+W0L4Otbee+rUqSNLS0sn1zhudf/+/XJ89qACLjIDlaeGMusIi0VYnFvg2OklWonDSkQrjmm1WkRRVPiFDwXbDuLOGx62jcuroEoIeV5J+YwRthnnfhHhF40OWedpMQFWaRjYuaFw28r/ES2yTI2SpeB7RegxVoswgtJ9zBHQsbx30UUXxW77qSia3ZGWhTqEas3MzFwWx/EVRuQa4HJrzeaSNhGyPMvVmNcDHx+N3Hmh0M94CZ3Zb5y9RIMGKWONZdTnb1jcW7mDmnKnC75MWShSX+xOCzsRuT74wIYNG2bXT09/X4w5mGv+AzyPHTly5CEgP3jwYOCbVSa9SR0xhgCmIJMpGSlKnhTKoSRN0TxleXkZEKLI0Wm16HTatDudIhOIwr3Lqy8WjDS5NzU1wTST7FA0aPANGqIMTh1xjUvBQOTRtJTTYsf3wdNbXGJuMSHzQkyLxHrE+mKaQCAPnjSQP/zwwykPk8KJimqx0zl3iai+E2Nfa4SX2TJTVIvMU6/Bl3NnnBG5dmZmZnOZGHhW2xd3jg+F5ODBg1gT31Ay94oLT2XnVjpCqQwb5Q9xssspkCpGSlfbUAyJVAsyijMi600UXQVc5bCoVfbu2fNkCOHhoP6Ia4X7lk9yau7Y8s7uBQbUiooneCHLc7KWkq/omgnSWZbhs4zeyjJqLc5a4jim0+7QarXKvNWBc+9oIFlDyy9icJSijVrPodpwzhqcGDIcAYKIkCQJzz13ikwzNICLOhhMIcCqn0El9YH+o9HemXXr3+YmeakJ0fmIXGxELjLWTgx5xHifl/NkI4jFaIUqBedcp+39q4BbR6m8LxR642gsj0snIm8t481NRUAp/n9gKCe1++2A2lp+cM2w4orMaCqZWsW+8t6Hyqq6FHXssdbusVgM+sF2W1h4MmXTBSlB1YgH9UAwhXFQ5IisJWorIatSnYuUOhFT7P7ek6YZaZrRW+nR7rRZPz1dejDKGo62FZdYaxLacECdDlnbNUIpBxGOOsiPds6xadMmrBOSRSFuzZHSK2ywfbFcvMfmeOIjm/9tq9XGRVLS26oTJ4QQagmjEREno7F+xaMWp69x1wO3lpfWF1qXM7Qt+c5t2653zu0OIXgBO+xoVeUmVR+oDDvONpPcdLVbpxkofURr6E5r1Q2YIqWwOiOCNRm5eDwuNPKMNDA1M8WODVuIN2T4NNQLsThBQkUhrl1orTUjyuSmt3TpZiCgWiiSBil/w0aPlQ61qO3K6nlgnFpz2hWsscWdwQk2tYgukqNoXqxwL+Bz8KqIGoyxXghaokCVw7GRovcqJXg6lPlU2BULxUJQxMrlQHTwLHNdzDlc6ArgYve+cpyuTcis7sSr2jAlGbfyK5fCq32QqqxVOF5j+1srFLcywDam2NlxQdV6j6WPhJDXCvlG3iktZzF21DZ6IGS2pogqdKYw0hdpmO8PdK2lMZHWJkdrpBjVoorVkKUM9XHNIOSKyx60SKvw3hfYrEoRyix1pgBiwTnBiLEi4kRwjcEUw8HGDImsB2ajIqqoEXnRzMzMjkbYwwuFPlJtfsuWLZsUuUaLhDQziHBrioel3sWkEkoTGuhaw1hWi12v3gWr3zSl+TLsCluQmXKyLOPEvYaWCUV6hxYzEhdBrpbZb3XBBkIobhLaSI6Ths+61AjMIJG7Vos29J+D9TIcQaMMgs/Kcqp/2GAg0EjZrb3gzUBip4EsCSSzipUip0TLnr9YrIaw5FBTpHuoNkJhtLRUrkMPBotEBkcnIiqqGoxIO4qiK8723Macw20Lzrmfs8ZOBlUPOuQ6X02Lhix/dDStXlcnNTT+BRkRWpq6yKsPtUg8Djm4buAn/zPi+O1dNncdnVaHTrtDuw0/+OQ4xw86tJUQch2xDRpkDRlT3SmaqRhVT10Fgo2ai0rjFBtek2UU6hntq6VhjVFpUUMI+DwgkefxL7YxeYfumEOtIJHQjeHIn49z+icWjTKCr07RgSuBSAPVQRjlm4V6UFU8nTVmf0kLkLNaUOcqU3H9unX/2hrz0hCCFnW4OjpxYE8yYl40KpTW1SnLTQuBoc5fZcTvvOC1GLUcuW2c3rNdQuqY/aHlu58c54k/nYB2aMDi1dJr7qjDv6+Kp06AayyIptc4ZRapjDAWq31K1ki3GPy5rFry1fe7trByqM38j7tsuiDQ6RhYNDz6+S4P/Lt1paMAw3H2zX6o4Z9uRFaFIQzWuEhQWvPz8596+umn9Wzt6nKuerns2LGj7ax9yIjsKQc/ZsjboolQyBoOQUMWEcNYe2WtJkYaibPD7ldDH1j1gdoCms8XLRIKTBqjRJNa7Ky6WqBcEX6HirdaeDpsPNccKFWLIIzAhzJi61HHXTYXrw5v/dL4nmbbbxzkyxFRW+lszchXYOmIw3Q8YsMgnHc08rE6IXQkZn5Vch5qjCFAsrS09LKTBZforCiN3Dk68vdW9XJjzB7VUA+JGrayQ1jzsKRfBzujrhEi1FgQ1aVWFTSENYvbGoMYizFgjC1guqm06FVD1Q4MEt1W5RitijgvitbYUUvFoh+WRnBY7aVemlJIA2YU0QLerIrfDs8Q1GtjF6hhpgKZsVpPnaKplJDD3DNFaxVN53XW6iCDS1c7Qzas6gZIkQyYQtUaU/XGSDuO4xdTEObkBXhxMCQStfZGawx54c5vhgtGVmcJ6nDIFKu9MhlKrVUljls458jyjLzKBS0jFqsiL2zsAr1enyRJiJyjO9YtLpzVImsc3avGf0MuWCXRKwj5vOBMhFgpBBzkuG5eWXcMrq8iGAvZoiEkhdm/aiAArSnFxRZSS75cTGZDKFAVNw4u1gbsMnjC7LRBfVHY3gimpcTtIhu1/5yB3GCdwVhTeSPgg6+sy4qg3cjgOmBipURNB2+vkYEdYJHAgIi8GvhKNQR8vrcudZ2ct3v3o87ZC3wIARXTMGEZMdgcNOpDx6eMZhZqGZhb0FUnJ6e46abPsm56HWmaEYKvd7I6mEuL1iFJ+szPz/Pwww9zyy1f4d577mZiYgJrTMOHdvSUqURKgwdWKYZJ0Xjgoo/OY8YckTXYyJMfsXzvdyfIvC9aHS2e1VpDSAzb37HA1G7oryghC5Aoz9zWITvVZsMrcrZfnZClQtLLyZKMZ+/osHIswrbKKHtbxGESB7Zft0jUNkgoRHCLj7Q4eX8LJbDl2nna2xRNDbZsuUIOPg+UOiPUC8mzlsVHuiQnIuxEXqIzDV/IgWWet8baLM///Olnnnnn2ZLWuXOxbdmxY8fLjTG7y1G9KTbggcmmMiyON9o47rWRsCYjbYQogmFufoG3vu3tXHzJxQSvGPu3W+9XXnklH/rgh/jMZz7Dx37jY0VidBNl0LUCVwZtk7Hglwxbfq7PJR9cIsFgMYwBP/mLLuk8mClFQ52tiubAWMarPrbMho1CjxUcyuxP2jz+xS7piue89y7zyl9K6JHgCCz1DF+5awwzZH+nZIswffUyb/j4LKFcfTFwz8c2ceyrHSZenHLNpxaQlidbg5ZiGmC64kiPzfP9P5jm0c9OYKIcv6r1M4PZtcjPUCSYJM97eLGEnwR4s7EmVtUgtSehDmILGzvnaDKzNHgeMpSIXPxd7z3OGG684YYCJ1eP94EsyUl6GUkvJemlpElGnmUFVyX3+DwnS1LyPOOX//Ev8+u//uusrKxgI7cKYqt6ZSMl/bWRwm5wbH1zQh6UdBGyRc9C5nnos53isjvkIxroLXimX96jvb7PXL9Pugjew4++EJOcsLQ25mx6zTILvkdvKZB5z5Nfc/Se7NAak/q0CcGT9gPb35SQBmF5Uch7cOoZy+NfcGT9jI2XJ0hsWexZ0lTo94T+iqG/LKRLQn9JSFYMWWbwAVpb4fLfOsnWd82RLVrEjSI+RQ9WTlYvnN62bdPZGhydU4VeccGtMa8vYQwZJFKPBL0OmeQ3c+5lMPZnGIGx1tFPE1592WVcsW8fIYRiV7aGqOVodSJanZhWJyZuRbgoIoqiMuve4KIIax0hBD7wgQ+wc+cu0iQbumhW9hJFrzIY83sNZL1AvCFl8xV9siBYhLilzD9pmf9um2i8oP1KObE1TvA5bL26hzEBgxJHggTh1N1jSBA2vTxnwwWFI0FsCkncs385jvhSSFGebCGF9mbPtqv75CjOCu0OzD7QYvkpg49TNu1fIZMU6wItK0x3DNNdmB4T1o0L68eFdV3BuuKO0+9lJMGz5/olPHnpF1ksriZag2qwItpW3X22WmZ3jvXnYdOmTeOq+poiOe0MwTejXFRYlbFZ/1EDbrTWkqUZ19/wbqIowvviAmat5Ytf+hJ3ffObTEyMk+c5eZ4jCHv37uU973kPExOTKIqxBu89k5OTXHDhBTz91FNMTI6XY30Z8Xksp6veoxLoLyjbf67P+I6cNDFYAYmVE3d2SE4J8cYcyaXeGSVYxrbCtjel5AXNhCj2zH6/w+IjbaJY2PqWBOMCuuKwrcDiCcepe7qYsYJ7jgjOCulixJa39JneE0gTwZV0iWN3jSHeMbEnZePlCWkoOmgTBb7z+xt49q4ubjKgueBzz56rE87/wGmSXJDc4G2gMx6w7YD31VBsCH0SBW+tcdbaVwB3P98L3QA+kujl1totVUahlqM/04gz12bMYW3CWUJnIsOxJo34k97KMhs3buStb3lr6apbfFeSJHzid/4LD37v+4yNdesFoKqcnpvj4Ycf5hOf/CRZmiIuIoSAtRYU8jwnSVL6vX6BUtSDocELa7Va9ZRy21v7BAngDNYpaSo8dqBNfznFWxC1GGOwkZDOGna8Z5np8wNpanAlrPjMrR2Sk4bujsC2n0/J1BS53TZw+Btdeoccbn2OZg0PdGPY8fYMS4xIgjhl+VTE8Qe6OGtZf0VGZyaQLgvWKKdPw4N/FpM/2cWOFSdDuuh54paY9dfM096h+AwiC/mCxWQxdtw3YE0aJDutqHFXAH9wNnjp59qOjm1xiTFGQ+HZ5owIoczGHAxEtDbRb1ATh/wUtZGYqRoQscwvLPK2t7+drdu2kvZTEIhbMXfeeQc/efQnbNu+rWAdNmi+Udzi0UcfJRR8G1QVay29Xo9DzxwCgc1bNnPhhReWHJiAz/JCdBHH5FnGD37wIHnfMnFewswbErwHCQETK8tPRkQdYctVCaoWkRwTGeKWJV0yvOgXeoU9cDk/SFYsT9zu6Pd7bHuVZ92OQJpZnA0ENRy5tVu415XsSQyExNDdlrF13wo5GUYhsoFDd7VZfMIgLc/2N/WxhIKn3lUO3d6i/2NLPL2M4FADZjLjJR9YYWwb5AGcU1rWcPiWSSRYMB71g+h1Ria61piZs9Uuu3ONrSi4ywp6ucogZlAawfONuNdRz2SqQRDDQbIIYoROp8173/f+0ljTlsUAnzvwOVZ6PbrdbiMipmAbep9x4QUXVrZuhXOXsTz++GMcPnwI1cC/+Oi/5H3ve1898AmqeJ8TxxG33XYrH/onv0ycr2PnGxeZ2hzo9wwu8mgQJnfCdV9aqPFxACsWKw4xSkZOEhRjFeeUuYdj5h6NMFFg5po+RrIC2msFFo9GPPetNtLJCXmpqbNC1rNs2rdEd1NGmgasKh544qsttG8Yf3HKrv0JQSGOFXJh08s9b7zlJM5JHRM+tiln4wWBRJVYDdIKPPjfu/z4f3Rw4wk+G4mGGRCcTVAlqF46NTU1NT8/f/rvWnF0rhV6JCIvLb38RBq0WpEzuJpo00BZymGLNu5BijGGleVlXvnKS3ndvtcRNGBsMRA5euQId999D5MTk/hy1xZVSlkBgnDNNdcOLhFl23LHHXeyuLjI7vP2cOWVV5L2U4IfUMdC8FhrOHDg8wWE6Tx735xjiBCXFosvK2wmcjKoT60CCcopvD9DADEB4y2xMzz7jS5hqcXk7j67r87xChIUK3D0a116xx3x+pSQF8UZSmRpxxt7BTQYhCgKLJ6wnLyvRQg52y5PmZq2LKUQxTk+g7Hzctbt9Rik9gkRAkkGkbX4JcPB35jkyT9bhxnPCYFVmUgyMvm1xrBly5Z8fn7+ebujCxD27t07Ebx/SRnJUgHngzyIegI56NWbqVgyxCNpMhaVpZUV3v3ud2OdJc9yVJUojrj55i9z8tlnmV4/TZqmVDI95xz9fp+XXPQSrrn2avIsBzVYJ/T7fW79ylcIIee6665j+/bta76oZ54+xDcPfoPIt4n29lh/RYZ6h7UJJGBcIIqKD8FhCRh80XigQNoX1AJBsKIkqfD0HW3IhA2XJazbFUhSizOe3MPTt8UFR6U0Gwoa8H1wGzLWX9EnJyAKzsKpe7usHDZIFNh47Qo5aUmSUzpxhZeXNgWNnSiY4lJs44g9VwnPfg3SOUcokxZVGQpZL/N2RZGgMD47O3sxcM/zdUc3gF9eXr602+l0C3crMQyRsBoMuWb/XffkjSUwwnJM+n127tjOO95xXdGuFPpd+r0eBw4coNVuFzt/CYmZ0jF2YWGRf/bhDzM+MUGWZmjwONPijq9/je9+568YH59ARLj5yzeTJknp31g8UxxF3HfvvSytLBD3Jtn4uiXcxAohtcTWIlHO/CNt5p4wuFgxDoyzddhYq6V0LunV/PkoDhz9dsTx7wq57TGzr4+Sl1QG4fRRYe7BFq5TcmBUEQf5HMxcldDdFshTwUqRWHbs9jFIHOMvythwRUoSFAmCjYSnvtTl+F0tovFS/+mVNFF2vSFjzzsScjzBBl78th6nHhEe/Ph6oumAZjqcCTbIfUUL2DiK43jmbECM59RktNVqdUXEhRB8s1h1lHBaS+QahK5RLlX57VEUs7CwwPXvup7NM5tJkxRBiFoRt/75Ldx/331Mr1/H4uIiWjIS8zxHRPjVj36Ud99wA3meE8UReZYRQuDTf/Qn5LknjmP+5NOfxocc77VEagLGGIwpRNCTE5P0vbLzzb0yX9RiiYic8MC/2sDTd7Qw3bxAgUSwLWAxZtf7l7nyv2b4VFHNMQKH/rJFb9bT2aCsf21CpjmCpWUiFg8b8tkW0VRG7ovWS1PwTnjJh3pYVfIgmCiwdNLy7P8ukKBN+3uMb/b4vmBRfAoPfnKMU98aI5pUstwX78mS8tj/Ut7zhlO01mckeR9vle75QtDi+et5x9BG04AaVWk7t/J8vowWnbj3l1PIzHQIA6/zeMoY2upSWtf5cPCgUkQcihFC8IyNjfGe976vvoRWg51HH3uMK/ZdweTkFKG8CKLK9u3beff17+aaN15bTFKdI8syoijiC5//PPfcezdT66bw3tPutLGluDmUBKgCYRDECn4ZJl+esP6yjCQDGwJxHDj+UMzsj1pMbPfkoXwtGsB48j5sfdMKQTy5GpwResvC8Tu7WCd0txs6uwrc2qFk3rNhd05nW8LyEx2Cy8hSD87z6o8tsvfKPstpgYY4qxy9s8PK4RZ2ImHLz/cIBLwKnY7yzJ2WY/cZ2uMpqCNyUlAsOrD7+h7tyUCWClkO0oGlZyxZmuNKu3mRgXaUISZQsQ8lab4PuO15uaPv37+fgwcPYpzbLEPE/iqprZGzrDqIE6zbcx3VC9UQ1+zsHFdddRUvfelFZFleYNTWoKp85CMf4Vc/8qsDeuuIjjNLM4w1dZE//vjj/OZv/haTExMlClNoL4MPA0lfZQMRwImgScyuN/cZG1eWeoJ6CCQ8fnOH5LTQ2iCYYOvJrk+EeJNn+hU9VnoeSSCeUE5+J2bpiS5jE9Bel9LuuILaKzkrKXRmAm/83AmOfHWcdFmQTs7W1/bY9sqMXiKoFkOifNnw+B+vx+LovKjH9tcGyB3WeIKHaApe/e/naY0ZRBzGFEquyT0551/dJ7UQspKvFuDEHR2MG+Why6pM6+oUNs7saH7mz6dCr/wPRUP4GawtpERSRSU2rR4GnPN6SEQz5VgGgVZl757nOe9//y8UsGMxuqupuAWqEYbUMmWsI9WgSkSIoogf/uAH/Mqv/Apzc7O0Wm2yLKtRBW3oQgfqoUCWAq2EHVf3sBoTSyAznoU5w+Hb2miU48OgLTORQRdjNr9ljsldKemCwfuCP378m12054jXebJjFjtnMRsTfA7OCFlqmNgbuPRDPcomhJTAUl/IvaABpiYC3/4P63j2e0JkUja9rsfkhJIkDnEeH4SZSwM7L00Kpmd5Ja0upr0QyPNCkDExJjxxa4cT97VpTTXNdWRIN1CxNkEL/box6fO5damcWPdqs7FbJScbhOQOSWV04HZVXUattSRJwitfeSlvf8fbERHidmttUHPkEDVmMNNYWlripptu4hO/87v0+iuMj42T59lAaaM6KqqpBQnpkjJ1YZ+pF/XIfNECtFpw4sGIxUciTMcXMGD5b1gjBPHs/PllbKlowirZouHYV8dRl+MlZ+WQ4dE/Geeyf5PTU8V7QcST9ZRFMiAv6k4E4xxRO2BRHvztKb77e+O4dX1WZi1bL18pzIvEYLQ4VUKqrIQKzsrr09OU7MsoMjgsh+9r81e/tgHXkjLhpdE8VixdbSIvUr23LwgvrLVJrayhKKKC89LAWaQ5iGhYuA1ilGvKQJIk7HvdPk7PzZEmCXEU18ZCQ1O7ATEGVaXX6/H0009z//33cftf3s5DDz3E2NgYrVabNM2GWhxtqEMLDLz0iXMGMtjz5h6dqYx+DqYlREY5fmcHsgg7Uah7Sik26RKM78rYfU0fjNKa9ETAoXtaPPcDh3Qy8j6Y8Zzv/UEHMwGv+IfLRJOFLZ5QeinWZ5ol9RHPfcfxw092efLmDtFkRt4T1u3O2Ht5HyMBG1M6SJYWIeiQ6xcoeS4ki3D6yZgnvjDOkzdNkAeQKECQUn8iDV+lptmS1Lwjc6bojueB8KLan815u3c/aa3dFYqRpRnSETV1nXVfPtj8qz57OFC24JlEzmGdG5gJhYApskka2Hvx4Ybg6fX79FZ6+ODptNu04lahQAphIFAqp4UDw1NtAEIGa4WQCZMXLeM2ppCbYhwfZyx9Z5x8LkbNsHxPc6G9MTD1mhWcGiyWYDPmn4hY+lEHiULp1agYMfhly+TejA2XrNDaltJZ7zGd4v1JTxuWj0acfiRm8ZEOvmexEznqA3kSaK8LTL28V+BAlR7WFIRLiwEVQlCCp/jVE5ZOCCtHHLriiCa1NpkTM+wlM6TNbYTmOedcnmV/+sRTT/2jMoMqfz7t6HrjjTfaMqLk+4Ls1FDoROtUUGlCVloSvJo4iwzJ5erUZAqsPCkJYRqGbSOayvy62MpJarfbLdVISl5GoEtNIhs44Wp9TaYhYi7lZZFy+sEuIW2XGHtxXEXjYKIyBr30egFFWpDNRRz/wlRDpA1RWwp8PJjC9KN8jW7Cs/iMZfbRSQhgzCAaPZSjShuDG1fseA6+GDW7liVdtBz7qhvpqQc770DIUr6TBqyDqKUwHVAPEswq5kpNuCsyjUcBAg3wzPPZBcACfteuXW+zxnzZe58V9uNiijbBDFuxidQtS1PIXFIHVg2PjJERQyAZ9uUSGdYtDYl+Wc1OGoI2Gwug/G9zBlCmmtaLr7BKHThqSWOoUre1ZvA6TNXwhgGqM+rzIm4QkF13BtX31aN5gwyBIVr+nNqqse6tm2YK2lwADR7LiAnksLtB9Q4XydGoqi+jRmyv17vo+PHjP/q7dgOw5xDPxc7Pz/94vNuNrXVXlR9bXjK5ynoUmpIdaX5yGhoUXRnxeal2urDK+4URjXvzk2oMY+vw3QbrdEQMXeD71YKrprWqUu/kooIG05gNDBsVVS9VQzEJFkzR/zaLrbGw6xNJCyPQ4Mtn80UJqcqITVzj/l7pWkL5S4ufW7xF5Ysvn12ojEYbzgIizdDhxg20ZrtoNVowxlhjjEnS9NeOHj1689mwvLDnGKnLLiwufm1ifPy4MfZi6+y0La7pxdVe8KVyTiit7evGoWYpji6EAeRY4fENg5KhXb6yomgcB7UnStOTZbV6afh7apRf16AlyBrHabOIRQa2eDr4u9qgDgurzZBQHWrB6oVZWWw3wxMYcj1qUJyrU2gQXCAyQr2Q0YzL2uWoyjEKIogxRoyxBjBZnj+SZdkHDx8+/Kdny9flXDUw0t27p9b5bOz9Lor2WWOuFSNbmn6IheNDYSdVmBuJDL+e6lgeMBqrBqH2S1nLxaoq+vq41mHFUM2klMG/09jd6oVV4fBmRJ89RDSuartAj4ZOh/o5QMoMA2W1amo1iFEiUbKGv0zj51Wt09Brr6S1zdezCn6tAXKtjkgxxpjiM6jbqgDHCeGOPITPHT58+MtVEvXZikmXcznpgkEo1XpgvxXZJyIXBdXLrLWbjDGDa35p8F92pQ0XtXL/LStVG1oNbcCWjDqzSJNyOtg165Uy6sM+gqdLHYDXRIUa1wwjDQZmGLbMG703jLYHosP+jY2CH14oQ+ba9TPUHpUyEG03e+7yZCvvlcX7WdS/AGLFUJtp129/CM+papGqp3r//Pz8QwsLC7Nn+kxfKPS/OQSX6enpqW63e6kxZp9z5sUE9oFsM1Y6A4u5wcWtdM7Nh0waKz8GqkZaajuMWr3ULJczFLWM7Io68t8apaFRZCLIao5awyZa1qzpevooOmCRhDDU3tRF32xrmq9Fw+CmMVhYYfXhprZu/RqtYOHfEk4GDQuicm/w/sfB+3tX0nStnNQzpXq/UOh/zXOaRqry6M5gZ2ZmdrXb7d3e+yudYatiLgW2ish2I2KK3b+BqDBoL3T4g1BpHuaCqI4ErDT64AHKoWugEIzY0VFTE4Z75MGEtXlF1ibapA2HUB25QA8jeDoEMUmjcdMhQ2k7GHyZoSVbUhhCCD4RsT/03qfGyje815Mich/w2OHDh+fWaEPs/v1ImVp3zsSj/7RFpK+ZmHzjjeiBA2c8Eid2zcxspNXqeO9fZ62N1ftrrXNOYb1oeCliVESmbbUQGDq+67ZjaI9WHbYaKGDzptvd2gOUNYDKwQWwRmh0qHVqJrzIkHNws+uoh26D3ryBiVenWmPF5d4nQE9Vc2vMAwpeQ/g+zj2T9HoLrVbrW4uLi/np06cP/Q0tZu1gfS4V9t+XQmfNmIrSg3vz5oN64ED95p/xa2pqat26detYWVl5TbvdbjnnSJLkWmvtmDMmeNXNquEV1ohSJCNGim6XUaPpGiXRYWP/BiO7obkZbLY1B2T1LaGC72Qoga8hNNFQu4M15gdLYngOFQ0aBOWkGPO9KjHaGPOAjaITeZ5Iv5891O12Ty0sLOgaLceq97bMYKVR1JzLhf33tdD/pgVQBebSyLz82+5ApnEpbvV6vZd1ogicq7xhoiRJ9tuS02owGGdGB4aCMQpInmVXKnRX9R2Fab6ItY85534CmFq9XeF2BX2hbMnloTiOD2VZJnmeB7JMUtVjs7OzR0d22f8bmFlGiHacLZTkhUL///P6a0ut/fv3M+Ic5n+aX9/+/ftt4/UwUsj607Yz/79+/R+k3TF9nUQG6QAAAABJRU5ErkJggg==',ll:'data:image/png;base64,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',rl:'data:image/png;base64,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'};
const ARI={};for(const k in ARS){const i=new Image();i.src=ARS[k];ARI[k]=i}
const AW=[
 {id:'pistol',n:'Pistole',ic:'🔫',cd:.55,dmg:10,rng:380,price:12,desc:'Zuverlässig, mittlere Reichweite'},
 {id:'smg',n:'Maschinenpistole',ic:'💥',cd:.15,dmg:3.5,rng:300,price:18,desc:'Sehr schnell, wenig Schaden pro Schuss'},
 {id:'shotgun',n:'Schrotflinte',ic:'🌰',cd:1.1,dmg:6,rng:240,price:22,desc:'5 Kugeln, stark auf kurze Distanz'},
 {id:'sword',n:'Schwert',ic:'🗡️',cd:.65,dmg:16,rng:125,price:15,desc:'Stößt zu, trifft mehrere Gegner'},
 {id:'wand',n:'Zauberstab',ic:'🪄',cd:.9,dmg:9,rng:420,price:20,desc:'Zielsuchende Magie-Kugeln'},
 {id:'rocket',n:'Raketenwerfer',ic:'🚀',cd:1.7,dmg:24,rng:460,price:30,desc:'Explosion trifft alle in der Nähe'}];
const AIT=[
 {id:'hp',s:'hp',v:8,n:'Energydrink',d:'+8 Max-HP',price:10},
 {id:'dmg',s:'dmg',v:10,n:'Sammler-Edition',d:'+10 % Schaden',price:14},
 {id:'spd',s:'spd',v:7,n:'Turnschuhe',d:'+7 % Tempo',price:10},
 {id:'as',s:'as',v:10,n:'Controller',d:'+10 % Angriffstempo',price:14},
 {id:'arm',s:'arm',v:2,n:'Poster-Schild',d:'+2 Rüstung',price:10},
 {id:'reg',s:'reg',v:1,n:'Pizza',d:'+0,5 Leben pro Sekunde',price:12},
 {id:'pick',s:'pick',v:30,n:'Magnet-Maus',d:'+30 Sammelreichweite',price:8}];
const ATM=[1,1.9,3.2,5],ATC=['#c9d1d9','#5ab0ff','#b06aff','#ff5a4a'],ATN=['I','II','III','IV'],AWAVES=20,ASC=.55;
const ATYPES={basic:{w:34,h:46,hp:1,sp:62,dmg:1,drop:1,col:'#cfd6dd',tint:'rgba(0,0,0,0)'},
 runner:{w:26,h:34,hp:.5,sp:125,dmg:1,drop:1,col:'#7CFC9a',tint:'rgba(0,255,120,.20)'},
 tank:{w:54,h:72,hp:3.5,sp:38,dmg:2,drop:3,col:'#b06aff',tint:'rgba(90,0,150,.30)'},
 elite:{w:46,h:62,hp:6,sp:58,dmg:2,drop:6,col:'#ff5a4a',tint:'rgba(255,0,0,.25)'},
 boss:{w:112,h:150,hp:14,sp:44,dmg:3,drop:30,col:'#ffd23f',tint:'rgba(255,190,0,.18)'}};
let AR=null,arRaf=0,arK={},arLast=0,arImgs=[],arCtx=null;
const arClamp=(v,a,b)=>Math.max(a,Math.min(b,v));
function arView(){const c=S.coins,b=c.arc_best||{};
 return `<h2>Arcade</h2><div class="sub">Überlebe ${AWAVES} Wellen. Belohnung nur in Spiel-Coins (kein Echtgeld, kein Geldwert). Heute verdient: <b>${c.arc_today||0} / ${c.arc_cap||150}</b> 🪙 · Beste Runde: Welle ${b.wave||0}, ${b.kills||0} Kills</div>
 <div class="arwrap"><canvas id="arc" width="960" height="600"></canvas><div id="arov" class="arov"></div></div>
 <div class="dim" style="margin-top:8px">WASD oder Pfeiltasten bewegen · P = Pause · Die Waffen im Kreis zielen automatisch. Gegner sind deine eigenen Spiele-Cover. Zwei gleiche Waffen derselben Stufe verschmelzen.</div>`}
function arHold(){const m=$('main');if($('#arc')&&m.contains($('#arc')))return m.innerHTML;setTimeout(arInit,0);return arView()}
function arStop(){if(arRaf){cancelAnimationFrame(arRaf);arRaf=0}}
function arLoadImgs(){arImgs=[];const gs=(S.games||[]).slice().sort(()=>Math.random()-.5).slice(0,14);
 gs.forEach(g=>{const u=coverOf(g);if(!u)return;const i=new Image();i.src=u;arImgs.push({img:i,name:g.title||'?'})})}
function arNew(){return {w:1,t:0,dur:20,phase:'start',sp:0,kills:0,mat:0,rr:0,paused:false,
 p:{x:480,y:420,hp:30,mhp:30,walk:0,mv:false,inv:0},s:{hp:0,dmg:0,spd:0,as:0,arm:0,reg:0,pick:0},
 weapons:[],en:[],pr:[],gems:[],pt:[],offers:[],time:0}}
function arInit(){arStop();arK={};arCtx=$('#arc').getContext('2d');arLoadImgs();AR=arNew();arShowStart();arLast=performance.now();arRaf=requestAnimationFrame(arLoop)}
function arShowStart(){AR=arNew();const o=$('#arov');
 o.innerHTML=`<div class="arbox"><h3>Wähle deine Startwaffe</h3><div class="arcards">${AW.map(w=>`<div class="arcard" onclick="arStart('${w.id}')"><div class="aric">${w.ic}</div><b>${w.n}</b><div class="dim">${w.desc}</div></div>`).join('')}</div></div>`;o.style.display='flex'}
function arStart(id){const o=$('#arov');o.style.display='none';AR=arNew();AR.weapons=[{id,tier:0,cd:.3,ang:-Math.PI/2,th:0}];AR.phase='play';AR.boss=false;
 fetch('/api/coin',{method:'POST',body:JSON.stringify({op:'arcstart'})}).catch(()=>{})}
function arDef(id){return AW.find(w=>w.id==id)}
function arPos(i,n){const p=AR.p,a=-Math.PI/2+i*2*Math.PI/n,R=64;return {x:p.x+Math.cos(a)*R,y:p.y-52+Math.sin(a)*R,a}}
function arHpBase(w){return 8+w*3.2+w*w*.12}
function arSpawn(type){const g=AR,T=ATYPES[type],side=Math.floor(Math.random()*4);let x,y;
 if(side==0){x=40+Math.random()*880;y=70}else if(side==1){x=40+Math.random()*880;y=588}else if(side==2){x=34;y=90+Math.random()*490}else{x=926;y=90+Math.random()*490}
 const hp=arHpBase(g.w)*T.hp;g.en.push({x,y,t:type,hp,mhp:hp,sp:T.sp*(1+g.w*.012),dmg:T.dmg,w:T.w,h:T.h,col:T.col,tint:T.tint,drop:T.drop,ph:Math.random()*6.28,fl:0,kx:0,ky:0,
  im:arImgs.length?arImgs[Math.floor(Math.random()*arImgs.length)]:null,nm:'?'})}
function arShoot(w,d,tg,ang){const g=AR,P=arPos(g.weapons.indexOf(w),g.weapons.length),dm=d.dmg*ATM[w.tier]*(1+g.s.dmg/100),bx=P.x+Math.cos(ang)*26,by=P.y+Math.sin(ang)*26;
 const mk=(a,sp,life,x)=>g.pr.push(Object.assign({x:bx,y:by,vx:Math.cos(a)*sp,vy:Math.sin(a)*sp,dmg:dm,life,r:5,col:ATC[w.tier],hit:null},x||{}));
 if(d.id=='pistol')mk(ang,700,d.rng/700+.1);
 else if(d.id=='smg')mk(ang+(Math.random()-.5)*.25,760,d.rng/760+.1,{r:4});
 else if(d.id=='shotgun'){for(let i=0;i<5;i++)mk(ang+(i-2)*.13+(Math.random()-.5)*.05,620,d.rng/620+.05,{r:4})}
 else if(d.id=='wand')mk(ang,330,2.4,{homing:1,r:7,col:'#c58bff'});
 else if(d.id=='rocket')mk(ang,380,d.rng/380+.3,{aoe:78,r:8,col:'#ff9a3a'})}
function arHit(e,dm,kx,ky){e.hp-=dm;e.fl=.09;e.kx+=kx||0;e.ky+=ky||0;
 for(let i=0;i<2;i++)AR.pt.push({x:e.x,y:e.y,vx:(Math.random()-.5)*160,vy:(Math.random()-.5)*160,life:.3,col:'#fff'});
 if(e.hp<=0&&!e.dead){e.dead=1;AR.kills++;AR.gems.push({x:e.x,y:e.y,v:e.drop});for(let i=0;i<6;i++)AR.pt.push({x:e.x,y:e.y,vx:(Math.random()-.5)*260,vy:(Math.random()-.5)*260,life:.45,col:e.col})}}
function arUpdate(dt){const g=AR,p=g.p,s=g.s;g.time+=dt;
 let dx=(arK.KeyD||arK.ArrowRight?1:0)-(arK.KeyA||arK.ArrowLeft?1:0),dy=(arK.KeyS||arK.ArrowDown?1:0)-(arK.KeyW||arK.ArrowUp?1:0);const l=Math.hypot(dx,dy);p.mv=l>0;
 if(l){dx/=l;dy/=l;const sp=190*(1+s.spd/100);p.x+=dx*sp*dt;p.y+=dy*sp*dt;p.walk+=dt*sp/17}
 p.x=arClamp(p.x,60,900);p.y=arClamp(p.y,165,580);p.inv=Math.max(0,p.inv-dt);
 p.mhp=30+s.hp;p.hp=Math.min(p.mhp,p.hp+s.reg*.5*dt);
 g.t+=dt;
 if(!g.boss&&g.w%5==0){g.boss=true;arSpawn('boss')}
 g.sp-=dt;if(g.sp<=0){g.sp=Math.max(.2,1-g.w*.04);const n=1+Math.floor(g.w/5);
  for(let i=0;i<n&&g.en.length<130;i++){const r=Math.random();arSpawn(g.w>=6&&r<.06?'elite':g.w>=4&&r<.2?'tank':g.w>=2&&r<.45?'runner':'basic')}}
 const cx=p.x,cy=p.y-45;
 for(const e of g.en){const ex=cx-e.x,ey=cy-e.y,d=Math.hypot(ex,ey)||1;e.x+=(ex/d*e.sp+e.kx)*dt;e.y+=(ey/d*e.sp+e.ky)*dt;e.kx*=.9;e.ky*=.9;e.fl=Math.max(0,e.fl-dt);
  if(d<e.w*.45+22&&p.inv<=0){p.hp-=Math.max(1,Math.round(e.dmg*(1+g.w*.1)/(1+s.arm*.1)));p.inv=.5}}
 if(p.hp<=0){arEnd(false);return}
 const n=g.weapons.length;
 g.weapons.forEach((w,i)=>{const d=arDef(w.id),P=arPos(i,n);let tg=null,bd=1e9;
  for(const e of g.en){const q=Math.hypot(e.x-P.x,e.y-P.y);if(q<bd){bd=q;tg=e}}
  let want=P.a;const inr=tg&&bd<=d.rng+(tg.w/2);if(inr)want=Math.atan2(tg.y-P.y,tg.x-P.x);
  let df=want-w.ang;df=Math.atan2(Math.sin(df),Math.cos(df));w.ang+=df*Math.min(1,dt*14);
  w.cd-=dt*(1+s.as/100);w.th=Math.max(0,w.th-dt);
  if(inr&&w.cd<=0){w.cd=d.cd;
   if(d.id=='sword'){w.th=.2;const dm=d.dmg*ATM[w.tier]*(1+s.dmg/100),ax=Math.cos(w.ang),ay=Math.sin(w.ang);
    for(const e of g.en){const vx=e.x-P.x,vy=e.y-P.y,q=Math.hypot(vx,vy);if(q<d.rng+e.w/2&&(vx*ax+vy*ay)/(q||1)>.35)arHit(e,dm,ax*220,ay*220)}}
   else arShoot(w,d,tg,w.ang)}});
 for(const b of g.pr){if(b.homing){let t=null,bd=1e9;for(const e of g.en){const q=Math.hypot(e.x-b.x,e.y-b.y);if(q<bd){bd=q;t=e}}
   if(t){const sp=Math.hypot(b.vx,b.vy),a=Math.atan2(b.vy,b.vx);let df=Math.atan2(t.y-b.y,t.x-b.x)-a;df=Math.atan2(Math.sin(df),Math.cos(df));const na=a+df*Math.min(1,dt*5);b.vx=Math.cos(na)*sp;b.vy=Math.sin(na)*sp}}
  b.x+=b.vx*dt;b.y+=b.vy*dt;b.life-=dt;
  for(const e of g.en){if(e.dead)continue;if(Math.abs(e.x-b.x)<e.w/2+b.r&&Math.abs(e.y-b.y)<e.h/2+b.r){
    if(b.aoe){for(const o of g.en)if(Math.hypot(o.x-b.x,o.y-b.y)<b.aoe+o.w/3)arHit(o,b.dmg,(o.x-b.x)*3,(o.y-b.y)*3);for(let i=0;i<14;i++)g.pt.push({x:b.x,y:b.y,vx:(Math.random()-.5)*380,vy:(Math.random()-.5)*380,life:.4,col:'#ffb04a'})}
    else arHit(e,b.dmg,b.vx*.2,b.vy*.2);b.life=0;break}}}
 g.pr=g.pr.filter(b=>b.life>0&&b.x>22&&b.x<938&&b.y>48&&b.y<590);g.en=g.en.filter(e=>!e.dead);
 const pr=70+s.pick;
 for(const m of g.gems){const d=Math.hypot(cx-m.x,cy-m.y);if(d<pr){m.x+=(cx-m.x)/d*320*dt;m.y+=(cy-m.y)/d*320*dt}if(d<18){g.mat+=m.v;m.got=1}}
 g.gems=g.gems.filter(m=>!m.got);
 for(const q of g.pt){q.x+=q.vx*dt;q.y+=q.vy*dt;q.life-=dt}g.pt=g.pt.filter(q=>q.life>0);
 if(g.t>=g.dur)arWaveEnd()}
function arWaveEnd(){const g=AR;g.gems.forEach(m=>g.mat+=m.v);g.gems=[];g.en=[];g.pr=[];g.pt=[];
 if(g.w>=AWAVES){arEnd(true);return}
 g.phase='shop';g.rr=0;arOffers();arShowShop()}
function arGameName(){const gs=S.games||[];if(!gs.length)return 'D-Hub';let t=(gs[Math.floor(Math.random()*gs.length)].title||'D-Hub');return t.length>20?t.slice(0,19)+'…':t}
function arOffers(){const g=AR,o=[];
 for(let i=0;i<4;i++){if(Math.random()<.5){const d=AW[Math.floor(Math.random()*AW.length)];let t=0;const r=Math.random();if(g.w>=13)t=r<.4?2:1;else if(g.w>=7)t=r<.4?1:0;
   o.push({k:'w',id:d.id,tier:t,price:Math.round(d.price*[1,2.1,3.8,6][t]*(1+g.w*.04))})}
  else{const it=AIT[Math.floor(Math.random()*AIT.length)];o.push({k:'i',id:it.id,gn:arGameName(),price:Math.round(it.price*(1+g.w*.06))})}}
 g.offers=o}
function arMerge(){const g=AR;let ch=true;while(ch){ch=false;
 for(let i=0;i<g.weapons.length&&!ch;i++)for(let j=i+1;j<g.weapons.length;j++){const a=g.weapons[i],b=g.weapons[j];
  if(a.id==b.id&&a.tier==b.tier&&a.tier<3){a.tier++;g.weapons.splice(j,1);ch=true;break}}}}
function arCanMerge(id,t){return AR.weapons.some(w=>w.id==id&&w.tier==t&&t<3)}
function arBuy(i){const g=AR,o=g.offers[i];if(!o||o.sold||g.mat<o.price)return;
 if(o.k=='w'){if(g.weapons.length>=6&&!arCanMerge(o.id,o.tier)){toast('Alle 6 Waffenplätze belegt');return}
  g.weapons.push({id:o.id,tier:o.tier,cd:.4,ang:-Math.PI/2,th:0});arMerge()}
 else{const it=AIT.find(x=>x.id==o.id);g.s[it.s]+=it.v;if(it.s=='hp')g.p.hp+=it.v}
 g.mat-=o.price;o.sold=true;arShowShop()}
function arReroll(){const g=AR,c=Math.round(2+g.rr*2+g.w*.5);if(g.mat<c)return;g.mat-=c;g.rr++;arOffers();arShowShop()}
function arNext(){const g=AR;g.w++;g.t=0;g.dur=20+Math.min(10,Math.floor(g.w/2));g.boss=false;g.sp=0;g.phase='play';g.p.inv=1.2;g.p.hp=Math.min(30+g.s.hp,g.p.hp+(30+g.s.hp)*.3);$('#arov').style.display='none'}
function arShowShop(){const g=AR,o=$('#arov'),c=Math.round(2+g.rr*2+g.w*.5),s=g.s;
 const offer=(x,i)=>{if(x.sold)return `<div class="arcard dis"><b>Verkauft</b></div>`;
  const aff=g.mat>=x.price;
  if(x.k=='w'){const d=arDef(x.id);return `<div class="arcard ${aff?'':'dis'}" onclick="arBuy(${i})"><div class="aric">${d.ic}</div><b>${d.n}</b> <i style="color:${ATC[x.tier]}">${ATN[x.tier]}</i><div class="dim">${d.desc}</div><div class="dim">Schaden ${Math.round(d.dmg*ATM[x.tier])} · Tempo ${(1/d.cd).toFixed(1)}/s</div><div class="arpr">✦ ${x.price}</div></div>`}
  const it=AIT.find(y=>y.id==x.id);return `<div class="arcard ${aff?'':'dis'}" onclick="arBuy(${i})"><div class="aric">🎒</div><b>${esc(x.gn)}-${it.n}</b><div class="dim">${it.d}</div><div class="arpr">✦ ${x.price}</div></div>`};
 o.innerHTML=`<div class="arbox"><h3>Welle ${g.w} geschafft</h3><div class="dim">Material: <b style="color:#ffd23f">✦ ${g.mat}</b> · Kills: ${g.kills} · Leben ${Math.round(g.p.hp)}/${30+s.hp}</div>
 <div class="arcards" style="margin:12px 0">${g.offers.map(offer).join('')}</div>
 <div class="dim" style="margin-bottom:8px">Waffen (${g.weapons.length}/6): ${g.weapons.map(w=>`<span style="color:${ATC[w.tier]}">${arDef(w.id).ic} ${arDef(w.id).n} ${ATN[w.tier]}</span>`).join(' · ')}<br>Schaden +${s.dmg}% · Tempo +${s.spd}% · Angriffstempo +${s.as}% · Rüstung ${s.arm} · Regen ${s.reg*.5}/s</div>
 <button class="btn" ${g.mat<c?'disabled':''} onclick="arReroll()">Neu würfeln ✦ ${c}</button> <button class="btn pri" onclick="arNext()">Welle ${g.w+1} starten</button></div>`;o.style.display='flex'}
async function arEnd(win){const g=AR;g.phase='over';const done=win?AWAVES:g.w-1,o=$('#arov');
 o.innerHTML=`<div class="arbox"><h3>${win?'🏆 Geschafft! Alle '+AWAVES+' Wellen überlebt':'💀 Ausgeschieden in Welle '+g.w}</h3><div class="dim">Kills: ${g.kills} · Berechne Belohnung …</div></div>`;o.style.display='flex';
 let msg='';try{const r=await coinOp('arcade',null,{completed:done,kills:g.kills,win:win?1:0});msg=r.msg||''}catch(e){msg='Belohnung konnte nicht abgeholt werden.'}
 if(tab!='Arcade')return;
 o.innerHTML=`<div class="arbox"><h3>${win?'🏆 Geschafft! Alle '+AWAVES+' Wellen überlebt':'💀 Ausgeschieden in Welle '+g.w}</h3><div class="dim">Geschaffte Wellen: ${done} · Kills: ${g.kills}</div><p><b>${esc(msg)}</b></p><button class="btn pri" onclick="arShowStart()">Nochmal spielen</button></div>`}
/* --- Zeichnen --- */
function arRR(c,x,y,w,h,r){c.beginPath();if(c.roundRect)c.roundRect(x,y,w,h,r);else c.rect(x,y,w,h)}
function arWeapon(c,w,P,t){const d=w.id,col=ATC[w.tier];c.save();c.translate(P.x,P.y);c.rotate(w.ang);if(Math.cos(w.ang)<0)c.scale(1,-1);
 if(w.tier>0){c.shadowColor=col;c.shadowBlur=9}
 if(d=='pistol'){c.fillStyle='#4a3a2a';c.fillRect(-6,-2,8,11);c.fillStyle='#8a939c';c.fillRect(-2,-4,22,8);c.fillStyle=col;c.fillRect(14,-4,4,8)}
 else if(d=='smg'){c.fillStyle='#4a3a2a';c.fillRect(-6,-2,8,11);c.fillStyle='#2f353d';c.fillRect(4,2,6,13);c.fillStyle='#8a939c';c.fillRect(-4,-4,32,8);c.fillStyle=col;c.fillRect(22,-4,4,8)}
 else if(d=='shotgun'){c.fillStyle='#7a4a22';c.fillRect(-12,-4,18,9);c.fillStyle='#9aa3ab';c.fillRect(6,-3,28,6);c.fillStyle=col;c.fillRect(26,-3,4,6)}
 else if(d=='sword'){const th=w.th>0?Math.sin((1-w.th/.2)*Math.PI)*30:0;c.translate(th,0);c.fillStyle='#8a5a2a';c.fillRect(-8,-2,12,4);c.fillStyle='#e0b43a';c.fillRect(3,-9,4,18);
  c.fillStyle='#e8eef4';c.beginPath();c.moveTo(7,-3.5);c.lineTo(40,-3.5);c.lineTo(50,0);c.lineTo(40,3.5);c.lineTo(7,3.5);c.closePath();c.fill();c.fillStyle=col;c.fillRect(8,-1,30,2)}
 else if(d=='wand'){c.fillStyle='#6b4a2a';c.fillRect(-8,-2,36,4);c.fillStyle='#c58bff';c.shadowColor='#c58bff';c.shadowBlur=12;c.beginPath();c.arc(31,0,7,0,6.3);c.fill();c.fillStyle=col;c.beginPath();c.arc(31,0,3,0,6.3);c.fill()}
 else if(d=='rocket'){c.fillStyle='#5a6b3a';c.fillRect(-10,-6,38,12);c.fillStyle='#2f353d';c.fillRect(-12,-7,4,14);c.fillStyle='#d94a3a';c.fillRect(28,-5,7,10);c.fillStyle=col;c.fillRect(8,-6,3,12)}
 c.restore()}
function arPlayer(c,g,t){const p=g.p,ph=p.walk,mv=p.mv,bs=mv?Math.abs(Math.sin(ph))*3:Math.sin(t*2.2)*1.2,bw=ARI.body.naturalWidth*ASC,bh=ARI.body.naturalHeight*ASC;const lh=ARI.ll.naturalHeight*ASC;
 if(!bw)return;
 c.save();c.fillStyle='rgba(0,0,0,.35)';c.beginPath();c.ellipse(p.x,p.y+2,38,9,0,0,6.3);c.fill();c.restore();
 c.save();if(p.inv>0&&Math.floor(t*20)%2)c.globalAlpha=.45;
 const top=p.y-lh+2-bh-bs;
 const leg=(img,ox,k)=>{const lw=img.naturalWidth*ASC,lh=img.naturalHeight*ASC,s=mv?Math.sin(ph+k*Math.PI):0,lift=mv?Math.max(0,s)*7:0,sw=mv?s*.5:0;c.save();c.translate(p.x+ox+lw/2,p.y-lh+2-lift);c.rotate(sw);c.drawImage(img,-lw/2,0,lw,lh);c.restore()};
 leg(ARI.ll,36.9*ASC-bw/2,0);leg(ARI.rl,117.9*ASC-bw/2,1);
 c.save();c.translate(p.x,top+bh);c.rotate(mv?Math.sin(ph)*.045:0);c.drawImage(ARI.body,-bw/2,-bh,bw,bh);c.restore();
 c.restore()}
function arEnemy(c,e,t){const w=e.w,h=e.h;c.save();c.translate(e.x,e.y);c.rotate(Math.sin(t*9+e.ph)*.12);
 c.save();arRR(c,-w/2,-h/2,w,h,6);c.clip();const im=e.im;
 if(im&&im.img.complete&&im.img.naturalWidth)c.drawImage(im.img,-w/2,-h/2,w,h);else{c.fillStyle='hsl('+((e.ph*57)|0)+',45%,32%)';c.fillRect(-w/2,-h/2,w,h);c.fillStyle='#fff';c.font='bold '+(w*.5|0)+'px sans-serif';c.textAlign='center';c.textBaseline='middle';c.fillText(im?(im.name[0]||'?').toUpperCase():'?',0,0)}
 c.fillStyle=e.tint;c.fillRect(-w/2,-h/2,w,h);if(e.fl>0){c.fillStyle='rgba(255,255,255,.75)';c.fillRect(-w/2,-h/2,w,h)}c.restore();
 c.lineWidth=e.t=='boss'?4:2;c.strokeStyle=e.col;if(e.t=='elite'||e.t=='boss'){c.shadowColor=e.col;c.shadowBlur=12}arRR(c,-w/2,-h/2,w,h,6);c.stroke();c.shadowBlur=0;
 const ey=-h*.12,ex=w*.2,er=Math.max(3,w*.09);c.fillStyle='#fff';c.beginPath();c.arc(-ex,ey,er,0,6.3);c.arc(ex,ey,er,0,6.3);c.fill();
 c.fillStyle='#111';c.beginPath();c.arc(-ex,ey+1,er*.5,0,6.3);c.arc(ex,ey+1,er*.5,0,6.3);c.fill();
 c.strokeStyle='#111';c.lineWidth=Math.max(2,w*.05);c.beginPath();c.moveTo(-ex-er,ey-er-1);c.lineTo(-ex+er,ey-er+3);c.moveTo(ex+er,ey-er-1);c.lineTo(ex-er,ey-er+3);c.stroke();
 if(e.hp<e.mhp){c.fillStyle='rgba(0,0,0,.6)';c.fillRect(-w/2,-h/2-8,w,4);c.fillStyle='#e0463a';c.fillRect(-w/2,-h/2-8,w*Math.max(0,e.hp/e.mhp),4)}
 c.restore()}
function arDraw(t){const c=arCtx,g=AR;if(!c||!g)return;
 c.drawImage(arBG(),0,0);
 for(const m of g.gems){c.save();c.translate(m.x,m.y);c.rotate(.785);c.fillStyle='#ffd23f';c.strokeStyle='#5a3d00';c.lineWidth=1.5;c.fillRect(-5,-5,10,10);c.strokeRect(-5,-5,10,10);c.restore()}
 for(const e of g.en)arEnemy(c,e,t);
 arPlayer(c,g,t);
 const n=g.weapons.length;g.weapons.forEach((w,i)=>arWeapon(c,w,arPos(i,n),t));
 for(const b of g.pr){c.fillStyle=b.col;c.shadowColor=b.col;c.shadowBlur=8;c.beginPath();c.arc(b.x,b.y,b.r,0,6.3);c.fill();c.shadowBlur=0}
 for(const q of g.pt){c.globalAlpha=Math.max(0,q.life*2.2);c.fillStyle=q.col;c.fillRect(q.x-2,q.y-2,4,4)}c.globalAlpha=1;
 if(g.phase=='play'||g.phase=='shop'){c.fillStyle='rgba(0,0,0,.55)';c.fillRect(0,0,960,38);
  const mh=30+g.s.hp,f=arClamp(g.p.hp/mh,0,1);c.fillStyle='#331';c.fillRect(12,10,220,18);c.fillStyle=f>.3?'#4cc864':'#e0463a';c.fillRect(12,10,220*f,18);
  c.fillStyle='#fff';c.font='bold 13px sans-serif';c.textBaseline='middle';c.textAlign='left';c.fillText('HP '+Math.ceil(g.p.hp)+' / '+mh,20,19);
  c.textAlign='center';c.font='bold 16px sans-serif';c.fillText('Welle '+g.w+' / '+AWAVES+(g.phase=='play'?'  ·  '+Math.max(0,Math.ceil(g.dur-g.t))+' s':''),480,19);
  c.textAlign='right';c.fillStyle='#ffd23f';c.fillText('✦ '+g.mat,830,19);c.fillStyle='#fff';c.fillText('☠ '+g.kills,940,19)}
 if(g.paused){c.fillStyle='rgba(0,0,0,.6)';c.fillRect(0,0,960,600);c.fillStyle='#fff';c.font='bold 38px sans-serif';c.textAlign='center';c.fillText('Pause (P)',480,300)}}
function arLoop(now){arRaf=0;if(tab!='Arcade'||!$('#arc'))return;const dt=Math.min(.05,(now-arLast)/1000||0);arLast=now;
 if(AR&&AR.phase=='play'&&!AR.paused)arUpdate(dt);
 if(AR){AR.ph=(AR.ph||0)+dt}arDraw(now/1000);arRaf=requestAnimationFrame(arLoop)}
addEventListener('keydown',e=>{if(tab!='Arcade'||arcMode!='surv')return;if(/^(Arrow|Key[WASD])/.test(e.code)){arK[e.code]=1;if(e.code.startsWith('Arrow'))e.preventDefault()}
 if((e.code=='KeyP'||e.code=='Escape')&&AR&&AR.phase=='play')AR.paused=!AR.paused});
addEventListener('keyup',e=>{delete arK[e.code]});addEventListener('blur',()=>{arK={};if(AR&&AR.phase=='play')AR.paused=true});

/* ===== INVADERS ===== */
let IV=null,ivRaf=0,ivLast=0,ivCtx=null;
function ivView(){const b=S.coins.inv_best||{};
 return `<h2>Invaders</h2><div class="sub">Deine eigenen Spiele-Cover greifen an. Belohnung nur in Spiel-Coins (kein Echtgeld). Heute Arcade-Coins: <b>${S.coins.arc_today||0} / ${S.coins.arc_cap||150}</b> · Beste Runde: Welle ${b.wave||0}, ${b.kills||0} Kills</div>
 <div class="arwrap"><canvas id="ivc" width="960" height="600"></canvas><div id="ivov" class="arov"></div></div>
 <div class="dim" style="margin-top:8px">A/D oder ←/→ bewegen · Leertaste schießen (gedrückt halten) · P = Pause. Nach jeder Welle gibt es ein Leben dazu (max. 5).</div>`}
function ivHold(){const m=$('main');if($('#ivc')&&m.contains($('#ivc')))return m.innerHTML;setTimeout(ivInit,0);return ivView()}
function ivStop(){if(ivRaf){cancelAnimationFrame(ivRaf);ivRaf=0}}
function ivNew(){return {phase:'start',w:1,px:480,lives:3,inv:0,cd:0,kills:0,bul:[],eb:[],pt:[],en:[],fx:70,fy:70,dir:1,ft:1,walk:0,total:1,paused:false,banner:0}}
function ivGrid(g){const rows=Math.min(5,3+Math.floor(g.w/3)),cols=8;g.en=[];
 for(let r=0;r<rows;r++)for(let c=0;c<cols;c++){const im=arImgs.length?arImgs[(r*cols+c+g.w)%arImgs.length]:null;g.en.push({r,c,hp:r==0&&g.w>1?2:1,mhp:r==0&&g.w>1?2:1,im,ph:Math.random()*6.28,fl:0})}
 g.total=g.en.length;g.fx=70;g.fy=-270;g.entering=true;g.dir=1;g.bul=[];g.eb=[]}
function ivInit(){ivStop();arK={};ivCtx=$('#ivc').getContext('2d');arLoadImgs();IV=ivNew();ivShowStart();ivLast=performance.now();ivRaf=requestAnimationFrame(ivLoop)}
function ivShowStart(){IV=ivNew();const o=$('#ivov');o.innerHTML=`<div class="arbox"><h3>Invaders</h3><div class="dim" style="margin-bottom:12px">Halte deine Spiele-Sammlung auf Abstand!</div><button class="btn pri" onclick="ivBegin()">Start</button></div>`;o.style.display='flex'}
function ivBegin(){$('#ivov').style.display='none';IV=ivNew();IV.phase='play';ivGrid(IV);fetch('/api/coin',{method:'POST',body:JSON.stringify({op:'arcstart'})}).catch(()=>{})}
const ivPos=(g,e)=>({x:g.fx+e.c*72,y:g.fy+e.r*72});
function ivUpdate(dt){const g=IV;
 const dx=(arK.KeyD||arK.ArrowRight?1:0)-(arK.KeyA||arK.ArrowLeft?1:0);g.px=arClamp(g.px+dx*360*dt,40,920);if(dx)g.walk+=dt*14;
 g.cd-=dt;if(arK.Space&&g.cd<=0){g.cd=.38;g.bul.push({x:g.px,y:488})}
 g.inv=Math.max(0,g.inv-dt);g.banner=Math.max(0,g.banner-dt);
 const al=g.en.filter(e=>e.hp>0);
 if(!al.length){g.w++;g.lives=Math.min(5,g.lives+1);ivGrid(g);g.banner=1.8;return}
 let mn=1e9,mx=-1e9,my=0;for(const e of al){const p=ivPos(g,e);mn=Math.min(mn,p.x);mx=Math.max(mx,p.x);my=Math.max(my,p.y)}
 const sp=(30+g.w*7+(1-al.length/g.total)*150)*g.dir;
 if(g.entering){g.fy=Math.min(70,g.fy+230*dt);if(g.fy>=70)g.entering=false}else if((mx+24>940&&g.dir>0)||(mn-24<20&&g.dir<0)){g.dir*=-1;g.fy+=22}else if(!g.entering)g.fx+=sp*dt;
 if(my+30>=470){ivEnd();return}
 g.ft-=dt;if(g.ft<=0&&!g.entering){g.ft=Math.max(.35,1.3-g.w*.08);const cands=al.filter(a=>!al.some(b=>b.c==a.c&&b.r>a.r));const e=cands[Math.floor(Math.random()*cands.length)];if(e){const p=ivPos(g,e);g.eb.push({x:p.x,y:p.y+30})}}
 for(const b of g.bul){b.y-=640*dt;for(const e of al){if(e.hp<=0)continue;const p=ivPos(g,e);if(Math.abs(b.x-p.x)<24&&Math.abs(b.y-p.y)<31){b.y=-99;e.hp--;e.fl=.1;
   for(let i=0;i<5;i++)g.pt.push({x:p.x,y:p.y,vx:(Math.random()-.5)*240,vy:(Math.random()-.5)*240,life:.4,col:'#7CFC9a'});if(e.hp<=0)g.kills++;break}}}
 for(const b of g.eb){b.y+=(250+g.w*8)*dt;if(g.inv<=0&&Math.abs(b.x-g.px)<26&&Math.abs(b.y-515)<30){b.y=999;g.lives--;g.inv=1.5;if(g.lives<=0){ivEnd();return}}}
 g.bul=g.bul.filter(b=>b.y>0);g.eb=g.eb.filter(b=>b.y<610);
 for(const e of g.en)e.fl=Math.max(0,e.fl-dt);
 for(const q of g.pt){q.x+=q.vx*dt;q.y+=q.vy*dt;q.life-=dt}g.pt=g.pt.filter(q=>q.life>0)}
async function ivEnd(){const g=IV;g.phase='over';const done=g.w-1,o=$('#ivov');
 o.innerHTML=`<div class="arbox"><h3>💀 Invasion in Welle ${g.w}</h3><div class="dim">Kills: ${g.kills} · Berechne Belohnung …</div></div>`;o.style.display='flex';
 let msg='';try{const r=await coinOp('arcade',null,{mode:'inv',completed:done,kills:g.kills});msg=r.msg||''}catch(e){msg='Belohnung konnte nicht abgeholt werden.'}
 if(tab!='Arcade')return;
 o.innerHTML=`<div class="arbox"><h3>💀 Invasion in Welle ${g.w}</h3><div class="dim">Geschaffte Wellen: ${done} · Kills: ${g.kills}</div><p><b>${esc(msg)}</b></p><button class="btn pri" onclick="ivShowStart()">Nochmal spielen</button></div>`}
function ivDraw(t){const c=ivCtx,g=IV;if(!c||!g)return;
 c.drawImage(ivBG(),0,0);c.fillStyle='#fff';ivStars.forEach(q=>{c.globalAlpha=.35+.55*Math.abs(Math.sin(t*1.4+q.p));c.fillRect(q.x,q.y,q.s,q.s)});c.globalAlpha=1;ivBlink.forEach((b,i)=>{if(Math.floor(t*1.2+i)%2){c.fillStyle='#ff3b3b';c.shadowColor='#ff3b3b';c.shadowBlur=8;c.fillRect(b.x-1.5,b.y-2,3,3);c.shadowBlur=0}});const sh=(t%11)/11;if(sh<.07){c.strokeStyle='rgba(255,255,255,'+(1-sh/.07)+')';c.lineWidth=2;c.beginPath();c.moveTo(150+sh*4000,40+sh*1500);c.lineTo(110+sh*4000,28+sh*1500);c.stroke()}
 for(const e of g.en){if(e.hp<=0)continue;const p=ivPos(g,e),w=44,h=58;c.save();c.translate(p.x,p.y+Math.sin(t*3+e.ph)*2);
  c.save();arRR(c,-w/2,-h/2,w,h,6);c.clip();const im=e.im;if(im&&im.img.complete&&im.img.naturalWidth)c.drawImage(im.img,-w/2,-h/2,w,h);else{c.fillStyle='hsl('+((e.ph*57)|0)+',45%,32%)';c.fillRect(-w/2,-h/2,w,h);c.fillStyle='#fff';c.font='bold 22px sans-serif';c.textAlign='center';c.textBaseline='middle';c.fillText(im?(im.name[0]||'?').toUpperCase():'?',0,0)}
  c.fillStyle=e.mhp>1?'rgba(255,0,0,.22)':'rgba(0,255,120,.12)';c.fillRect(-w/2,-h/2,w,h);if(e.fl>0){c.fillStyle='rgba(255,255,255,.7)';c.fillRect(-w/2,-h/2,w,h)}c.restore();
  c.lineWidth=2;c.strokeStyle=e.mhp>1?'#ff5a4a':'#cfd6dd';arRR(c,-w/2,-h/2,w,h,6);c.stroke();
  c.fillStyle='#fff';c.beginPath();c.arc(-9,-6,4,0,6.3);c.arc(9,-6,4,0,6.3);c.fill();c.fillStyle='#111';c.beginPath();c.arc(-9,-5,2,0,6.3);c.arc(9,-5,2,0,6.3);c.fill();
  c.strokeStyle='#111';c.lineWidth=2;c.beginPath();c.moveTo(-14,-12);c.lineTo(-5,-9);c.moveTo(14,-12);c.lineTo(5,-9);c.stroke();c.restore()}
 const S2=.3,bw=ARI.body.naturalWidth*S2,bh=ARI.body.naturalHeight*S2;
 if(bw){c.save();if(g.inv>0&&Math.floor(t*20)%2)c.globalAlpha=.4;const fy=548,mv=arK.KeyA||arK.KeyD||arK.ArrowLeft||arK.ArrowRight;
  const ll=ARI.ll,rl=ARI.rl,lh=ll.naturalHeight*S2,bob=mv?Math.abs(Math.sin(g.walk))*2:0;
  const leg=(im,ox,k)=>{const s=mv?Math.sin(g.walk+k*Math.PI):0;c.save();c.translate(g.px+ox+im.naturalWidth*S2/2,fy-lh+1-(mv?Math.max(0,s)*4:0));c.rotate(s*.5);c.drawImage(im,-im.naturalWidth*S2/2,0,im.naturalWidth*S2,im.naturalHeight*S2);c.restore()};
  leg(ll,36.9*S2-bw/2,0);leg(rl,117.9*S2-bw/2,1);
  c.drawImage(ARI.body,g.px-bw/2,fy-lh+1-bh-bob,bw,bh);
  c.fillStyle='#8a939c';c.fillRect(g.px-4,fy-lh-bh-bob-14,8,16);c.fillStyle='#7CFC9a';c.fillRect(g.px-4,fy-lh-bh-bob-14,8,4);c.restore()}
 c.fillStyle='#7CFC9a';for(const b of g.bul)c.fillRect(b.x-2,b.y-10,4,16);
 c.fillStyle='#ff5a4a';for(const b of g.eb){c.beginPath();c.arc(b.x,b.y,5,0,6.3);c.fill()}
 for(const q of g.pt){c.globalAlpha=Math.max(0,q.life*2.2);c.fillStyle=q.col;c.fillRect(q.x-2,q.y-2,4,4)}c.globalAlpha=1;
 c.fillStyle='rgba(0,0,0,.55)';c.fillRect(0,0,960,38);c.fillStyle='#fff';c.font='bold 16px sans-serif';c.textBaseline='middle';
 c.textAlign='left';c.fillText('❤'.repeat(Math.max(0,g.lives)),16,19);c.textAlign='center';c.fillText('Welle '+g.w,480,19);c.textAlign='right';c.fillText('☠ '+g.kills,944,19);
 if(g.banner>0){c.textAlign='center';c.font='bold 34px sans-serif';c.fillText('Welle '+g.w+'!',480,300)}
 if(g.paused){c.fillStyle='rgba(0,0,0,.6)';c.fillRect(0,0,960,600);c.fillStyle='#fff';c.font='bold 38px sans-serif';c.textAlign='center';c.fillText('Pause (P)',480,300)}}
function ivLoop(now){ivRaf=0;if(tab!='Arcade'||!$('#ivc'))return;const dt=Math.min(.05,(now-ivLast)/1000||0);ivLast=now;
 if(IV&&IV.phase=='play'&&!IV.paused)ivUpdate(dt);ivDraw(now/1000);ivRaf=requestAnimationFrame(ivLoop)}
addEventListener('keydown',e=>{if(tab!='Arcade'||arcMode!='inv')return;if(/^(Arrow|Key[AD]|Space)/.test(e.code)){arK[e.code]=1;if(e.code=='Space'||e.code.startsWith('Arrow'))e.preventDefault()}
 if((e.code=='KeyP'||e.code=='Escape')&&IV&&IV.phase=='play')IV.paused=!IV.paused});
addEventListener('blur',()=>{if(IV&&IV.phase=='play')IV.paused=true});

/* ===== PATCH NOTES (bei jeder Änderung oben ergänzen!) ===== */
const PATCHES=[
 {v:'2.8',d:'13.11.2026',t:'Spielhalle überarbeitet',i:[['Neu','Beide Spiele liegen jetzt im Arcade-Reiter. Dort wählst du über zwei Karten mit animiertem Symbol Survivor oder Invaders. Das Spiel öffnet sich direkt in D-Hub mit Aufgehen-Animation, ein Zurück-Knopf bringt dich zur Auswahl.'],['Neu','Survivor: Grasarena mit Gras, Blumen, Steinen und Rahmen. Die ganze Arena ist wie bei Brotato fest im Bild, Gegner kommen vom Rand.'],['Neu','Invaders: Nachtszenario mit dunklem All, Mond, Sternen, Skyline mit beleuchteten Fenstern, Neonschildern und einer Straße mit Laternen. Die Spiele fliegen zu Beginn jeder Welle aus dem All herein.'],['Fix','Material (✦) ist jetzt gold und auf dem Gras gut zu sehen. Der Reiter „Invaders“ entfällt, weil er in Arcade steckt.']]},
 {v:'2.7',d:'13.11.2026',t:'Neue Navigation, Einstellungen repariert',i:[['Fix','Der Einstellungen-Reiter blieb leer, weil die Auswahlliste für die Overlay-Tasten (Strg+F9 usw.) fehlte. Er funktioniert wieder.'],['Neu','Neue Navigation oben: eigene Zeile unter dem Logo, Reiter in Gruppen (Bibliothek, Spielhalle, Community) mit Symbolen, Patch Notes und Einstellungen rechts.'],['Neu','Der aktive Reiter leuchtet jetzt in der Akzentfarbe deines Themes.']]},
 {v:'2.6',d:'13.11.2026',t:'Invaders und Patch Notes',i:[['Neu','Neuer Reiter „Invaders“: Space-Invaders-Minispiel, bei dem deine eigenen Spiele-Cover angreifen. Du steuerst das D-Hub-Maskottchen mit Laufanimation (A/D oder Pfeiltasten, Leertaste schießt).'],['Neu','Wellen werden schneller und dichter, die oberste Reihe wird später zäher, nach jeder Welle gibt es ein Leben dazu (max. 5).'],['Neu','Belohnung nur in Spiel-Coins, zählt zum Arcade-Tageslimit (150 Coins).'],['Neu','Neuer Reiter „Patch Notes“ mit allen Änderungen. Er wird ab jetzt bei jeder Änderung mit aktualisiert.']]},
 {v:'2.5',d:'13.11.2026',t:'Arcade (Survivor)',i:[['Neu','Neuer Reiter „Arcade“: Brotato-Stil-Survivor mit dem D-Hub-Maskottchen als Spielfigur inklusive Beine-Laufanimation.'],['Neu','Bis zu 6 Waffen kreisen um die Figur und zielen automatisch: Pistole, Maschinenpistole, Schrotflinte, Schwert, Zauberstab, Raketenwerfer. Gleiche Waffen verschmelzen zu höheren Stufen (I bis IV).'],['Neu','20 Wellen mit Gegnern aus deinen Spiele-Covern (normal, schnell, Tank, Elite, Boss alle 5 Wellen). Nach jeder Welle gibt es einen Shop mit Waffen und Items, Neu-würfeln inklusive.'],['Neu','Coins als Belohnung (nur Spiel-Coins, Tageslimit 150, serverseitig geprüft). Beste Runde wird gespeichert.']]},
 {v:'2.4',t:'Coins, Shop und Gamble',i:[['Neu','Coin-System: Täglicher Bonus mit Serie, Coins für Spielzeit (alle 10 Minuten, begrenzt pro Tag) und Meilensteine.'],['Neu','Shop: Farbthemen, Hover-Effekte für Spiele und Titel freischalten.'],['Neu','Gamble-Reiter mit Münzwurf, Glücksrad, Slots und Würfeln. Nur Spiel-Coins, niemals Echtgeld. Auszahlung ca. 95 %, Einsatzlimit und Tages-Verlustlimit.']]},
 {v:'2.0 – 2.3',t:'Overlay, Bibliothek, Sicherheit',i:[['Neu','Spiel-Overlay: Fadenkreuz, FPS, Ping, CPU, GPU, RAM, Uhrzeit sowie KumpelFunk-Anzeige (wer spricht, Chat-Blasen). Es hängt am Spielfenster, funktioniert im Vollbild und schließt mit D-Hub.'],['Neu','Hotkeys zum Ein- und Ausblenden des Overlays. Einstellungen pro Spiel über das Zahnrad beim Hovern.'],['Neu','Einzelspieler und Mehrspieler getrennt: Overlay-Installation (DLL) nur für Einzelspieler mit Warnhinweis, daneben die sichere Variante ohne Eingriff in Spieldateien.'],['Neu','Bibliothek zeigt alle installierten Spiele gemeinsam, ohne Launcher-Reiter. Refresh-Knopf ↻ und Plus-Knopf für eigene Spiele mit eigenem Cover.'],['Fix','Sicherheit: Die lokale Schnittstelle akzeptiert nur noch Anfragen von D-Hub selbst (Host/Origin-Prüfung).']]},
 {v:'1.9',t:'Schwarzer Bildschirm',i:[['Fix','Beim zweiten Start blieb das Fenster schwarz. Neue Start-, Fokus- und Neustart-Logik mit Watchdog.'],['Neu','Bei langem schwarzem Bild gibt es Neu-laden- und Debug-Knöpfe (Strg+Umschalt+D).']]},
 {v:'1.5 – 1.8',t:'Natives Fenster und GOG',i:[['Neu','D-Hub läuft jetzt in einem eigenen Programmfenster (pywebview) statt im Edge-Browserlook. Edge bleibt als Ausweichlösung.'],['Fix','GOG zeigt nur noch Spiele, die du besitzt, nicht den ganzen Katalog.'],['Fix','Build-Workflow auf GitHub korrigiert, damit pywebview im Build landet und die Versionsnummer stimmt.']]},
 {v:'1.4',t:'Deinstallieren',i:[['Neu','Deinstallieren-Knopf ganz unten in den Einstellungen.'],['Neu','Diagnose-Bereich zeigt gefundene Spiele und Fenstermodus.']]},
 {v:'1.3',t:'Startabsturz',i:[['Fix','Absturz beim Start behoben, wenn ein Spiel keinen Titel hatte (NoneType-Fehler). Der Startscan ist jetzt abgesichert.']]}
];
const PTAG={Neu:'#7CFC9a',Fix:'#ffb04a',Info:'#5ab0ff'};
function patchView(){return `<h2>Patch Notes</h2><div class="sub">Alle Änderungen an D-Hub, neueste zuerst. Aktuelle Version: ${esc(S.version)}</div>`+PATCHES.map(p=>`<div class="panel" style="margin-bottom:12px"><div style="display:flex;align-items:baseline;gap:10px;flex-wrap:wrap"><b style="font-size:18px">Beta ${esc(p.v)}</b><span style="font-weight:600">${esc(p.t)}</span>${p.d?`<span class="dim">${esc(p.d)}</span>`:''}</div>
 <ul style="margin:10px 0 0;padding:0;list-style:none">${p.i.map(x=>`<li style="margin:6px 0;display:flex;gap:10px"><i class="b" style="flex:none;height:fit-content;background:${PTAG[x[0]]||'#888'}22;color:${PTAG[x[0]]||'#888'}">${x[0]}</i><span>${esc(x[1])}</span></li>`).join('')}</ul></div>`).join('')}

/* ===== Arcade-Hub, Hintergründe ===== */
let arcMode=null,arBGc=null,ivBGc=null,ivStars=[],ivBlink=[];
function rng(seed){let s=seed>>>0;return()=>((s=(s*1664525+1013904223)>>>0)/4294967296)}
function arcRender(){const m=$('main');if(!m.querySelector('#arhub,#arc,#ivc')){arcMode=null;m.innerHTML=arcHub()}}
function arcHub(){const c=S.coins,b=c.arc_best||{},v=c.inv_best||{};
 const ring=['🔫','🗡️','🪄','🚀','💥','🌰'].map((e,i)=>`<span style="transform:rotate(${i*60}deg) translateX(76px)"><i style="display:block;transform:rotate(${-i*60}deg)"><i class="gcs">${e}</i></i></span>`).join('');
 const tiles=[0,1,2,3,4].map(i=>`<div class="gtile" style="left:${10+i*18}%;top:${14+(i%2)*22}px;animation-delay:${i*.35}s;background:hsl(${i*70+260},45%,38%)"></div>`).join('');
 return `<h2>Arcade</h2><div class="sub">Wähle ein Spiel. Belohnung nur in Spiel-Coins (kein Echtgeld, kein Geldwert). Heute verdient: <b>${c.arc_today||0} / ${c.arc_cap||150}</b> 🪙</div>
 <div id="arhub" class="arhub">
  <div class="gcard" tabindex="0" onclick="arcOpen('surv')" onkeydown="if(event.key=='Enter')arcOpen('surv')"><div class="gart gsurv"><div class="gring">${ring}</div><img class="gmas" src="${ARS.body}" alt=""></div>
   <h3>Survivor</h3><div class="dim">Überlebe 20 Wellen auf der Wiese. Waffen kreisen um dich, nach jeder Welle kaufst du neue.</div>
   <div class="gfoot"><span class="dim">Beste Runde: Welle ${b.wave||0} · ${b.kills||0} Kills</span><button class="btn pri">Spielen</button></div></div>
  <div class="gcard" tabindex="0" onclick="arcOpen('inv')" onkeydown="if(event.key=='Enter')arcOpen('inv')"><div class="gart ginv">${tiles}<div class="gcity"></div><img class="gmas2" src="${ARS.body}" alt=""></div>
   <h3>Invaders</h3><div class="dim">Deine Spiele-Sammlung fällt aus dem All über die nächtliche Stadt her. Halte sie auf Abstand.</div>
   <div class="gfoot"><span class="dim">Beste Runde: Welle ${v.wave||0} · ${v.kills||0} Kills</span><button class="btn pri">Spielen</button></div></div>
 </div>`}
function arcOpen(m){arStop();ivStop();arcMode=m;
 $('main').innerHTML=`<div class="gshell gopen"><div class="gbar"><button class="btn" onclick="arcBack()">← Zurück zur Arcade</button></div>${m=='surv'?arView():ivView()}</div>`;
 if(m=='surv')arInit();else ivInit()}
function arcBack(){arStop();ivStop();arcMode=null;AR=null;IV=null;$('main').innerHTML=arcHub()}
function arBG(){if(arBGc)return arBGc;const cv=document.createElement('canvas');cv.width=960;cv.height=600;const c=cv.getContext('2d'),r=rng(7);
 c.fillStyle='#0d110c';c.fillRect(0,0,960,600);
 const g=c.createLinearGradient(0,46,0,592);g.addColorStop(0,'#4f9436');g.addColorStop(1,'#3b7a2a');c.fillStyle=g;c.fillRect(20,46,920,546);
 for(let x=20,i=0;x<940;x+=76,i++)if(i%2){c.fillStyle='rgba(255,255,255,.035)';c.fillRect(x,46,76,546)}
 for(let i=0;i<30;i++){const x=20+r()*920,y=46+r()*546,rad=40+r()*90,l=r()<.5,gr=c.createRadialGradient(x,y,0,x,y,rad);gr.addColorStop(0,l?'rgba(170,230,110,.16)':'rgba(10,60,10,.2)');gr.addColorStop(1,'rgba(0,0,0,0)');c.fillStyle=gr;c.fillRect(20,46,920,546)}
 c.save();c.beginPath();c.rect(20,46,920,546);c.clip();c.lineWidth=1.3;
 for(let i=0;i<3200;i++){const x=20+r()*920,y=46+r()*546,h=3+r()*6;c.strokeStyle=r()<.5?'rgba(15,65,15,.5)':'rgba(170,235,110,.38)';c.beginPath();c.moveTo(x,y);c.lineTo(x+(r()-.5)*4,y-h);c.stroke()}
 const fl=['#fff3a0','#ffffff','#ff9ec0','#c9a0ff'];for(let i=0;i<70;i++){const x=30+r()*900,y=56+r()*526;c.fillStyle=fl[i%4];c.beginPath();c.arc(x,y,1.8,0,6.3);c.fill();c.fillStyle='rgba(255,200,0,.8)';c.fillRect(x-.5,y-.5,1,1)}
 for(let i=0;i<9;i++){const x=40+r()*880,y=70+r()*500,w=10+r()*16;c.fillStyle='rgba(0,0,0,.25)';c.beginPath();c.ellipse(x+3,y+5,w,w*.5,0,0,6.3);c.fill();c.fillStyle='#8b9096';c.beginPath();c.ellipse(x,y,w,w*.62,0,0,6.3);c.fill();c.fillStyle='#b0b6bc';c.beginPath();c.ellipse(x-w*.2,y-w*.2,w*.55,w*.3,0,0,6.3);c.fill()}
 for(let i=0;i<12;i++){const x=40+r()*880,y=70+r()*500;c.fillStyle='#2f6a22';for(let k=-1;k<=1;k++){c.beginPath();c.moveTo(x+k*4,y);c.lineTo(x+k*7,y-11);c.lineTo(x+k*4+3,y);c.fill()}}
 c.restore();
 const v=c.createRadialGradient(480,320,260,480,320,620);v.addColorStop(0,'rgba(0,0,0,0)');v.addColorStop(1,'rgba(0,0,0,.45)');c.fillStyle=v;c.fillRect(20,46,920,546);
 c.lineWidth=12;c.strokeStyle='#2a2017';c.strokeRect(14,40,932,558);c.lineWidth=2;c.strokeStyle='#6b5338';c.strokeRect(20,46,920,546);
 return arBGc=cv}
function ivBG(){if(ivBGc)return ivBGc;const cv=document.createElement('canvas');cv.width=960;cv.height=600;const c=cv.getContext('2d'),r=rng(21);
 const sk=c.createLinearGradient(0,0,0,520);sk.addColorStop(0,'#02030a');sk.addColorStop(.45,'#0c0a24');sk.addColorStop(.8,'#26173f');sk.addColorStop(1,'#4a2444');c.fillStyle=sk;c.fillRect(0,0,960,600);
 [[200,130,280,'rgba(120,60,210,.26)'],[770,210,240,'rgba(40,170,210,.16)'],[470,60,220,'rgba(210,60,150,.13)'],[620,330,260,'rgba(90,50,170,.14)']].forEach(n=>{const g=c.createRadialGradient(n[0],n[1],0,n[0],n[1],n[2]);g.addColorStop(0,n[3]);g.addColorStop(1,'rgba(0,0,0,0)');c.fillStyle=g;c.fillRect(0,0,960,520)});
 const mg=c.createRadialGradient(840,100,10,840,100,110);mg.addColorStop(0,'rgba(255,250,225,.5)');mg.addColorStop(1,'rgba(255,250,225,0)');c.fillStyle=mg;c.fillRect(700,0,260,230);
 c.fillStyle='#f4efdc';c.beginPath();c.arc(840,100,32,0,6.3);c.fill();c.fillStyle='rgba(150,140,120,.35)';[[830,92,7],[852,110,5],[846,86,4],[828,114,4]].forEach(k=>{c.beginPath();c.arc(k[0],k[1],k[2],0,6.3);c.fill()});
 ivStars=[];for(let i=0;i<90;i++)ivStars.push({x:r()*960,y:r()*400,s:r()<.2?2:1,p:r()*6.28});
 const bld=(x0,x1,col,hmin,hmax,lit,wcols)=>{let x=x0;while(x<x1){const w=44+r()*70,h=hmin+r()*(hmax-hmin),top=520-h;c.fillStyle=col;c.fillRect(x,top,w,h+10);c.fillStyle='rgba(130,150,255,.10)';c.fillRect(x,top,2,h);
   if(r()<.4){c.fillStyle=col;c.fillRect(x+w/2-1,top-26,2,26);if(lit)ivBlink.push({x:x+w/2,y:top-26})}
   for(let wy=top+12;wy<500;wy+=17)for(let wx=x+8;wx<x+w-10;wx+=13){if(r()<lit){c.fillStyle=wcols[Math.floor(r()*wcols.length)];c.globalAlpha=.55+r()*.45;c.fillRect(wx,wy,6,9);c.globalAlpha=1}}
   x+=w+r()*5}};
 bld(-10,970,'#0d1126',90,200,.12,['#3b4278','#4a5090']);
 const hz=c.createLinearGradient(0,400,0,520);hz.addColorStop(0,'rgba(120,60,150,0)');hz.addColorStop(1,'rgba(160,70,140,.28)');c.fillStyle=hz;c.fillRect(0,400,960,120);
 ivBlink=[];bld(-10,970,'#080a16',120,270,.42,['#ffd27a','#ffb85a','#9ad8ff','#fff1b5']);
 [['#ff3fa4',120,350,60,16],['#29e0ff',430,310,48,14],['#ff3fa4',700,370,64,16],['#8dff5a',860,330,40,12]].forEach(n=>{c.shadowColor=n[0];c.shadowBlur=18;c.fillStyle=n[0];c.fillRect(n[1],n[2],n[3],n[4]);c.shadowBlur=0;c.fillStyle='rgba(255,255,255,.55)';c.fillRect(n[1]+4,n[2]+n[4]/2-1,n[3]-8,2)});
 c.fillStyle='#2a2c3a';c.fillRect(0,514,960,16);c.fillStyle='rgba(150,160,220,.22)';c.fillRect(0,514,960,2);
 const st=c.createLinearGradient(0,530,0,600);st.addColorStop(0,'#1a1b25');st.addColorStop(1,'#0a0b10');c.fillStyle=st;c.fillRect(0,530,960,70);
 c.fillStyle='rgba(216,194,90,.75)';for(let x=20;x<960;x+=92)c.fillRect(x,572,42,4);
 for(let k=0;k<5;k++){const x=90+k*200,hx=x+22,hy=412;
  const cone=c.createLinearGradient(0,hy,0,540);cone.addColorStop(0,'rgba(255,220,140,.22)');cone.addColorStop(1,'rgba(255,220,140,.02)');c.fillStyle=cone;c.beginPath();c.moveTo(hx-6,hy+4);c.lineTo(hx+6,hy+4);c.lineTo(hx+70,540);c.lineTo(hx-70,540);c.fill();
  const pool=c.createRadialGradient(hx,548,0,hx,548,95);pool.addColorStop(0,'rgba(255,210,120,.32)');pool.addColorStop(1,'rgba(255,210,120,0)');c.fillStyle=pool;c.fillRect(hx-100,520,200,80);
  c.fillStyle='#1b1d2a';c.fillRect(x-2,hy,4,110);c.fillRect(x-2,hy,26,4);c.fillStyle='#ffe2a0';c.shadowColor='#ffcf6a';c.shadowBlur=24;c.beginPath();c.ellipse(hx,hy+6,9,4,0,0,6.3);c.fill();c.shadowBlur=0}
 return ivBGc=cv}

let gm='flip',gbet=10,gbusy=false,dmode='under',dtarget=50,wrot=0;
const WHEEL=[0,0,0,0,0.5,0.5,1,1,1,1.5,2,4],WCOL={0:'#2b312b',0.5:'#7a5a1a',1:'#2f6b3a',1.5:'#1f8a8a',2:'#5a4ab0',4:'#d9a400'};
const SLOTS=['🍒','🍋','🔔','⭐','💎','7️⃣'],SLOTPAY=[5,8,15,30,100,250];
function gambleView(){
 const c=S.coins;
 return `<h2>Gamble</h2><div class="sub">Nur Spiel-Coins zum Spaß: kein Echtgeld, keine Auszahlung, Coins lassen sich nicht kaufen.</div>
 <div class="panel"><div class="row"><div style="flex:1">Kontostand <b id="gbal" style="font-size:20px">${c.balance}</b> 🪙 · Heute <b id="gnet"></b> · Runden <span id="grounds">${c.rounds}</span></div></div>
  <div class="row"><span style="width:70px">Einsatz</span><input type="number" id="gb" min="1" max="${c.max_bet}" value="${gbet}" style="width:90px" onchange="setBet(+this.value||1)">
   ${[10,50,100,250].map(x=>`<button class="btn" onclick="setBet(${x})">${x}</button>`).join('')}<button class="btn" onclick="setBet(Math.floor(gbet/2))">½</button><button class="btn" onclick="setBet(gbet*2)">×2</button><span class="dim">max. ${c.max_bet}</span></div>
  <div class="dim" id="gnote"></div></div>
 <div style="display:flex;gap:6px;margin:12px 0;flex-wrap:wrap">${[['flip','Münzwurf'],['wheel','Glücksrad'],['slots','Slots'],['dice','Würfel']].map(([k,l])=>`<button class="btn ${k==gm?'pri':''}" onclick="gm='${k}';render()">${l}</button>`).join('')}</div>
 <div class="panel" id="gstage" style="text-align:center">${gStage()}</div><div id="gres" class="gres"></div>`}
function gRefresh(){const c=S.coins,n=$('#gnet');if(!n)return;n.textContent=(c.net>=0?'+':'')+c.net;n.style.color=c.net>=0?'var(--ac)':'var(--warn)';$('#gbal').textContent=c.balance;$('#grounds').textContent=c.rounds;$('#coinn').textContent=c.balance;
 $('#gnote').textContent=c.limit_hit?'Für heute ist Schluss: Das Tageslimit von '+c.loss_limit+' Coins Verlust ist erreicht. Morgen geht es weiter.':c.rounds>=30?'Du spielst schon eine Weile. Vielleicht eine Pause?':'Tageslimit: Nach '+c.loss_limit+' Coins Verlust ist für heute Schluss.'}
function setBet(v){gbet=Math.max(1,Math.min(S.coins.max_bet,Math.floor(v)));const i=$('#gb');if(i)i.value=gbet}
function gStage(){
 if(gm=='flip')return `<div class="coinwrap"><div id="cn" class="cn"><div class="cf">KOPF</div><div class="cf cb">ZAHL</div></div></div><p class="dim">Richtig geraten: Einsatz ×1,9</p><button class="btn pri" onclick="play('kopf')">Kopf</button> <button class="btn pri" onclick="play('zahl')">Zahl</button>`;
 if(gm=='wheel')return `<div class="wheelwrap"><div class="ptr"></div><div id="wh" class="wh" style="background:conic-gradient(${WHEEL.map((m,i)=>`${WCOL[m]} ${i*30}deg ${i*30+30}deg`).join(',')});transform:rotate(${wrot}deg)">${WHEEL.map((m,i)=>`<span style="transform:rotate(${i*30+15}deg) translateY(-82px)">×${m}</span>`).join('')}</div></div><p class="dim">Von ×0 bis ×4</p><button class="btn pri" onclick="play()">Drehen</button>`;
 if(gm=='slots')return `<div class="reels"><div class="reel" id="r0">🍒</div><div class="reel" id="r1">🍋</div><div class="reel" id="r2">🔔</div></div><p class="dim">Drei gleiche: ${SLOTS.map((s,i)=>s+'×'+SLOTPAY[i]).join(' · ')}<br>Zwei Kirschen: ×1,1</p><button class="btn pri" onclick="play()">Drehen</button>`;
 const ch=dmode=='under'?dtarget:100-dtarget;
 return `<div class="dnum" id="dn">–</div><div class="row" style="justify-content:center"><button class="btn ${dmode=='under'?'pri':''}" onclick="dmode='under';render()">Unter</button><button class="btn ${dmode=='over'?'pri':''}" onclick="dmode='over';render()">Über</button></div>
 <input type="range" min="5" max="95" value="${dtarget}" style="width:80%;margin:12px 0" oninput="dtarget=+this.value;dInfo()"><div id="dinfo" class="dim">${dInfoText()}</div><button class="btn pri" onclick="play()">Würfeln</button>`}
function dInfoText(){const ch=dmode=='under'?dtarget:100-dtarget;return `${dmode=='under'?'Wurf ≤ ':'Wurf > '}${dtarget} · Chance ${ch} % · Auszahlung ×${(95/ch).toFixed(2)}`}
function dInfo(){const e=$('#dinfo');if(e)e.textContent=dInfoText()}
const sleep=ms=>new Promise(r=>setTimeout(r,ms));
async function play(pick){
 if(gbusy)return;gbusy=true;document.querySelectorAll('#gstage button,#gstage input').forEach(b=>b.disabled=true);$('#gres').textContent='';
 const body={game:gm,bet:gbet};if(gm=='flip')body.pick=pick;if(gm=='dice'){body.mode=dmode;body.target=dtarget}
 let r;try{r=await coinOp('gamble','',body)}catch(e){gbusy=false;return}
 if(!r.ok){toast(r.msg);gbusy=false;document.querySelectorAll('#gstage button,#gstage input').forEach(b=>b.disabled=false);gRefresh();return}
 const x=r.result,d=x.detail;
 if(gm=='flip'){const cn=$('#cn');cn.style.transition='none';cn.style.transform='rotateY(0)';void cn.offsetWidth;cn.style.transition='transform 1.3s ease-out';cn.style.transform='rotateY('+(1800+(d.out=='kopf'?0:180))+'deg)';await sleep(1400)}
 else if(gm=='wheel'){wrot=Math.ceil(wrot/360)*360+360*5+(360-(d.index*30+15));const w=$('#wh');w.style.transition='transform 4s cubic-bezier(.12,.7,.15,1)';w.style.transform='rotate('+wrot+'deg)';await sleep(4100)}
 else if(gm=='slots'){const stops=[900,1400,1900],t0=Date.now();const fin=[false,false,false];
  while(Date.now()-t0<2000){for(let i=0;i<3;i++){if(Date.now()-t0>=stops[i]){if(!fin[i]){fin[i]=true;$('#r'+i).textContent=d.syms[i];$('#r'+i).classList.remove('spin')}}else{$('#r'+i).textContent=SLOTS[Math.floor(Math.random()*6)];$('#r'+i).classList.add('spin')}}await sleep(70)}
  for(let i=0;i<3;i++){$('#r'+i).textContent=d.syms[i];$('#r'+i).classList.remove('spin')}}
 else{const dn=$('#dn'),t0=Date.now();while(Date.now()-t0<900){dn.textContent=1+Math.floor(Math.random()*100);await sleep(60)}dn.textContent=d.roll;dn.style.color=x.payout>0?'var(--ac)':'var(--warn)'}
 const res=$('#gres');res.className='gres '+(x.net>0?'win':x.net<0?'lose':'even');
 res.textContent=x.net>0?`Gewonnen: +${x.net} Coins`+(x.payout>=x.bet*9?'  🎉 JACKPOT':''):x.net<0?`Verloren: ${x.net} Coins`:'Einsatz zurück';
 gRefresh();gbusy=false;document.querySelectorAll('#gstage button,#gstage input').forEach(b=>b.disabled=false)}
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
" // Wer ist im Sprachkanal, wer spricht gerade? Bei jeder Änderung aufrufen: dhubVoice([{name:'Lerun3',speaking:true},{name:'Du',speaking:false}])",
" window.dhubVoice=function(members){ if(host)host.postMessage({source:'kumpelfunk',type:'voice',members:members},DHUB_ORIGIN); };",
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
 else if(d.type==='voice'&&Array.isArray(d.members))api('action',{name:'kfvoice',members:d.members.slice(0,8).map(m=>({name:String(m.name||''),speaking:!!m.speaking}))});
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
$('#rf').onclick=()=>{toast('Liest neu ein …');api('rescan',{}).then(()=>{render();toast(S.games.length+' Spiele gefunden')})};$('#plus').onclick=()=>openAdd();
$('#q').oninput=e=>{q=e.target.value.toLowerCase();render()};
$('#vw').onclick=()=>api('settings',{view:S.settings.view=='cover'?'list':'cover'}).then(render);
api('state').then(()=>{const st=S.settings.starttab;if(st&&['Alle',...S.launchers,'Downloads','Wert','KumpelFunk'].includes(st))tab=st;render();window.__ok=1;initKF();updChrome(true)});
setInterval(async()=>{const a=document.activeElement,typing=a&&a.tagName=='INPUT',old=S.pending,ob=S.busy;try{await api('state');fails=0}catch(e){if(++fails>=3)$('#lost').style.display='flex';return}status();updChrome();applyCoins();
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
        if not self.local_only():
            return
        if "probe=1" not in self.path:
            LAST[0] = time.time()
        if self.path.split("?")[0] == "/logo.png":
            for d in (HERE, getattr(sys, "_MEIPASS", HERE)):
                f = os.path.join(d, "logo.png")
                if os.path.exists(f):
                    self.send(open(f, "rb").read(), "image/png")
                    return
            self.send_error(404)
            return
        base = self.path.split("?")[0]
        if base.startswith("/cover/"):
            f = os.path.join(COVERS, os.path.basename(urllib.parse.unquote(base[7:])))
            if os.path.isfile(f):
                self.send(open(f, "rb").read(), mimetypes.guess_type(f)[0] or "image/png")
            else:
                self.send_error(404)
            return
        if base == "/api/state":
            self.send(state())
        elif base == "/api/debug":
            self.send(debug_info().encode("utf-8"), "text/plain")
        else:
            self.send(PAGE.encode("utf-8"), "text/html")

    def local_only(self):
        """Nur D-Hub selbst darf die Schnittstelle benutzen, keine fremde Webseite im Browser (Schutz vor Fremdzugriff)."""
        host = (self.headers.get("Host") or "").split(":")[0].lower()
        org = self.headers.get("Origin") or ""
        if host not in ("127.0.0.1", "localhost") or (org and org not in ("http://127.0.0.1:%d" % PORT, "http://localhost:%d" % PORT)):
            self.send_error(403)
            return False
        return True

    def do_POST(self):
        if not self.local_only():
            return
        try:
            b = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or "{}")
        except Exception:
            b = {}
        LAST[0] = time.time()
        p = self.path
        if p == "/api/bye":
            if time.time() - START > 15:  # direkt nach einem Neustart ignorieren (altes Fenster schliesst sich)
                BYE[0] = time.time()
                LAST[0] = 0
            self.send({})
            return
        if p == "/api/coin":
            self.send(coin_op(str(b.get("op")), b))
            return
        if p == "/api/ovinfo":
            self.send(dll_info(str(b.get("id") or "")))
            return
        if p == "/api/ovdll":
            gid = str(b.get("id") or "")
            ok, msg = (dll_install if b.get("op") == "install" else dll_remove)(gid)
            if ok and OV["on"] and store.get("lastovgid") == gid:
                ov_apply(gamecfg(gid), gid)
            self.send(dict(ok=ok, msg=msg, info=dll_info(gid)))
            return
        if p == "/api/pick":
            self.send({"path": pick_file(b.get("kind"))})
            return
        if p == "/api/cover":
            try:
                ext = str(b.get("ext") or "").lower().lstrip(".")
                data = base64.b64decode(b.get("data") or "")
                gid = norm(b.get("id"))
                if ext not in ("png", "jpg", "jpeg", "webp", "gif") or not data or len(data) > 8000000 or not gid:
                    raise ValueError("Bild ungueltig")
                os.makedirs(COVERS, exist_ok=True)
                fn = "%s_%d.%s" % (gid[:60], int(time.time()), ext)
                open(os.path.join(COVERS, fn), "wb").write(data)
                store.setdefault("covers", {})[gid] = fn
                save()
                coin_check()
            except Exception as e:
                log("Cover: %s" % e)
            self.send(state())
            return
        if p == "/api/focus":
            try:
                WIN[0].restore()
                WIN[0].show()
                WIN[0].on_top = True
                WIN[0].on_top = False
            except Exception as e:
                log("focus: %s" % e)
            self.send({})
            return
        if p == "/api/jslog":
            log("JS: %s" % str(b.get("m"))[:400])
            self.send({})
            return
        if p == "/api/quit":
            self.send({})
            threading.Thread(target=lambda: (time.sleep(0.3), ov_stop(), os._exit(0))).start()
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
            if "gamedirs" in b:
                scan()
            if "hk_toggle" in b or "hk_close" in b:
                hk_reload()
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
    try:  # laeuft D-Hub schon?
        ex = fetch_json(url + "/api/state?probe=1")
    except Exception:
        ex = None
    if ex:
        if ex.get("version") == VERSION and ex.get("winopen"):
            # Es gibt schon ein offenes Fenster: nur nach vorne holen, kein zweites Fenster (das gab den schwarzen Bildschirm)
            try:
                if ex.get("native"):
                    urllib.request.urlopen(urllib.request.Request(url + "/api/focus", data=b"{}"), timeout=3)
                else:
                    open_window(url)
            except Exception:
                pass
            sys.exit(0)
        # Alte oder sterbende Instanz (Fenster schon zu, andere Version): beenden und frisch starten
        log("Zweitstart: alte Instanz wird beendet")
        try:
            urllib.request.urlopen(urllib.request.Request(url + "/api/quit", data=b"{}"), timeout=3)
        except Exception:
            pass
        for _ in range(25):
            time.sleep(0.3)
            try:
                fetch_json(url + "/api/state?probe=1")
            except Exception:
                break
        time.sleep(0.5)
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
    try:
        scan()
    except Exception as e:
        LIB["diag"] = ["Fehler beim Einlesen der Bibliothek: %s: %s" % (type(e).__name__, e)]
    for fn in (worker, price_worker, watcher, update_loop, apply_loop, hotkey_loop, play_loop):
        threading.Thread(target=fn, daemon=True).start()
    kick()
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    log("Start, Version %s" % VERSION)
    if run_window(url):  # Fenster wurde geschlossen: Overlay sofort zu, D-Hub beendet sich, sobald keine Downloads mehr laufen
        ov_stop()
        LAST[0] = 0
        BYE[0] = time.time() + 1
        log("Fenster geschlossen")
    else:
        open_window(url)
    while True:
        time.sleep(3600)
