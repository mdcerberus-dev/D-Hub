"""D-Hub Beta 2.4 - ein Hub fuer Steam, Epic, GOG und mehr. Nur Python 3 noetig, keine Pakete."""
import random, os, sys, json, re, glob, time, threading, subprocess, shutil, webbrowser, base64, mimetypes
import urllib.request, urllib.parse, sqlite3, tempfile, sysconfig, html
import xml.etree.ElementTree as ET
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler

HERE = os.path.dirname(sys.executable) if getattr(sys, "frozen", False) else os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "hub_data.json")
PORT = 8766
VERSION = "3.5"
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
        guard = os.path.join(HERE, "startguard.txt")
        try:
            g = open(guard).read().strip()
        except Exception:
            g = ""
        if g == "started2":
            WINMODE[0] = "Edge-Fenster (WebView2 blieb zweimal leer, startguard.txt loeschen fuer neuen Versuch)"
            log(WINMODE[0])
            return False
        if g == "started":
            log("Letzter Fensterstart blieb leer: Edge-Fenster, WebView2-Profil wird geloescht")
            shutil.rmtree(os.path.join(HERE, "webview-data"), ignore_errors=True)
            try:
                open(guard, "w").write("retry")
            except Exception:
                pass
            WINMODE[0] = "Edge-Fenster (letzter Start blieb leer)"
            return False
        try:
            open(guard, "w").write("started2" if g == "retry" else "started")
        except Exception:
            pass
        WINMODE[0] = "eigenes Programmfenster (WebView2)"
        FS[0] = bool(store.get("settings", {}).get("fullscreen"))
        win = webview.create_window("D-Hub", url, width=1320, height=840, min_size=(960, 620), background_color="#050805", fullscreen=FS[0])
        WIN[0] = win

        data = os.path.join(HERE, "webview-data")
        def watch():
            """Bleibt das Fenster schwarz/leer, wird neu geladen. Hilft das nicht, kommt eine Fehlerseite mit Knoepfen."""
            for attempt in range(2):
                ok = False
                for _ in range(16):
                    time.sleep(.5)
                    try:
                        ok = bool(win.evaluate_js("!!window.__ok"))
                    except Exception:
                        ok = False
                    if ok:
                        break
                if ok:
                    try:
                        os.remove(guard)
                    except Exception:
                        pass
                    return
                log("Fenster blieb leer, lade neu (Versuch %d)" % (attempt + 1))
                try:
                    win.load_url(url + "?r=%d" % int(time.time()))
                except Exception as e:
                    log("load_url: %s" % e)
            log("Fenster bleibt leer: Edge-Fenster als Ersatz")
            try:
                open_window(url)
            except Exception as e:
                log("open_window: %s" % e)
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
FS = [False]
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
    dict(id="ch_dhub", cat="char", name="D-Hub-Maskottchen", price=0, desc="Die Standardfigur in Survivor und Invaders."),
    dict(id="ch_ozzy", cat="char", name="Ozzy", price=600, desc="Der Chef persönlich als Spielfigur in Survivor und Invaders."),
    dict(id="ch_blank", cat="char", name="Männchen (Blanko)", price=0, desc="Das nackte Männchen. Kleidung und Ausrüstung kaufst du im Shop und trägst sie in Survivor und Invaders."),
    dict(id="hat_none", cat="hat", name="Kein Hut", price=0, desc="Nackter Kopf."),
    dict(id="hat_cap", cat="hat", name="D-Hub-Kappe", price=400, desc="Schwarze Kappe mit grünem D-Logo."),
    dict(id="hat_wizard", cat="hat", name="Zauberhut", price=900, desc="Violetter Spitzhut mit Sternen."),
    dict(id="hat_crown", cat="hat", name="Goldene Krone", price=1600, desc="Für alle, die es sich leisten können."),
    dict(id="ear_none", cat="ear", name="Keine Kopfhörer", price=0, desc="Ohren frei."),
    dict(id="ear_phones", cat="ear", name="Neon-Kopfhörer", price=500, desc="Schwarze Kopfhörer mit grünem Leuchtring."),
    dict(id="face_none", cat="face", name="Kein Gesichtsschmuck", price=0, desc="Freies Gesicht."),
    dict(id="face_shades", cat="face", name="Sonnenbrille", price=350, desc="Dunkle Gläser, cooler Blick."),
    dict(id="shirt_none", cat="shirt", name="Kein Oberteil", price=0, desc="Nackter Oberkörper."),
    dict(id="shirt_hoodie", cat="shirt", name="D-Hub-Hoodie", price=700, desc="Schwarzer Kapuzenpulli mit D-HUB-Schriftzug."),
    dict(id="shirt_gold", cat="shirt", name="Goldrüstung", price=1800, desc="Glänzender Brustpanzer."),
    dict(id="feet_none", cat="feet", name="Barfuß", price=0, desc="Nackte Füße."),
    dict(id="feet_sneaker", cat="feet", name="Neon-Sneaker", price=300, desc="Schwarze Schuhe mit grünem Streifen."),
    dict(id="feet_gold", cat="feet", name="Goldstiefel", price=900, desc="Goldene Stiefel."),
    dict(id="sk_none", cat="skin", name="Standard", price=0, desc="Das normale D-Hub-Aussehen.", data=["#5cff0c", "92,255,12"]),
    dict(id="sk_matrix", cat="skin", name="Matrix-Regen", price=1200, desc="Digitaler Code-Regen hinter der ganzen Oberfläche, Schrift im Terminal-Look.", data=["#19ff6a", "25,255,106"]),
    dict(id="sk_crt", cat="skin", name="Retro-Röhre", price=1500, desc="Alter Röhrenmonitor: Scanlines, Flackern, Phosphor-Glühen, Computerschrift.", data=["#7dff6a", "125,255,106"]),
    dict(id="sk_synth", cat="skin", name="Neon Dusk", price=1800, desc="Synthwave-Sonnenuntergang mit bewegtem Gitterboden und flimmerndem Logo.", data=["#ff4fd8", "255,79,216"]),
    dict(id="sk_inferno", cat="skin", name="Inferno", price=2200, desc="Aufsteigende Glut, glühender Boden und flackernde Flammenschrift.", data=["#ff6a1a", "255,106,26"]),
    dict(id="sk_galaxy", cat="skin", name="Galaxie", price=2800, desc="Lebendiger Sternenhimmel mit Nebel, Spiralgalaxie, Sternschnuppen und Milchglas-Panels.", data=["#7aa8ff", "122,168,255"]),
    dict(id="sk_gold", cat="skin", name="Royal Gold", price=3500, desc="Schwarzer Marmor, wandernder Goldglanz, funkelnde Partikel und glänzende Überschriften.", data=["#ffd23f", "255,210,63"]),
]
CAT_BY_ID = {i["id"]: i for i in CATALOG}
FREE = {"theme": "t_neon", "hover": "h_lift", "title": "ti_none", "char": "ch_dhub", "skin": "sk_none", "hat": "hat_none", "ear": "ear_none", "face": "face_none", "shirt": "shirt_none", "feet": "feet_none"}
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


# ---- Ozzys Atlas: Grundstuecke, passives Einkommen -------------------------------------------
ATL_R = 1e-9          # Atlas-Coins pro Acre und Sekunde (Grundwert)
ATL_START = 2.0e-6    # Startkapital: reicht genau fuer ein Grundstueck
ATL_P0 = 1.5e-6       # Preis des ersten Grundstuecks
ATL_XCAP = 50         # So viele D-Hub-Coins pro Tag lassen sich aus Atlas-Coins tauschen
ATL_OFFLINE = 12 * 3600
ATL_TERRAIN = ["wiese", "wiese", "acker", "wald", "wald", "berg",
               "acker", "wiese", "wald", "see", "berg", "berg",
               "wiese", "acker", "see", "see", "wald", "stadt",
               "wiese", "wald", "acker", "stadt", "stadt", "stadt"]
ATL_TM = {"wiese": 1.0, "acker": 1.1, "wald": 1.2, "see": 1.3, "berg": 1.4, "stadt": 1.6}
ATL_EXP = [1, 2, 3.5, 6]
ATL_LVMAX = 10


def atl():
    a = store.setdefault("atlas", {})
    for k, v in (("bal", ATL_START), ("plots", {}), ("last", time.time()), ("x_today", 0), ("x_day", ""), ("earned", 0.0)):
        a.setdefault(k, v)
    return a


def atl_price(k):
    return ATL_P0 * 1.9 ** k


def atl_income_plot(i, pl):
    base = 10 * 1.55 ** pl["k"] * ATL_R * ATL_TM[ATL_TERRAIN[int(i)]]
    return base * ATL_EXP[pl["exp"] - 1] * (1 + 0.25 * (pl["lvl"] - 1))


def atl_income(a):
    return sum(atl_income_plot(i, pl) for i, pl in a["plots"].items())


def atl_tick(a):
    now = time.time()
    dt = max(0.0, min(now - a["last"], ATL_OFFLINE))
    gain = atl_income(a) * dt
    a["bal"] += gain
    a["earned"] += gain
    a["last"] = now


def atl_up_cost(pl):
    return atl_price(pl["k"]) * 0.45 * 1.5 ** (pl["lvl"] - 1)


def atl_exp_cost(pl):
    return atl_price(pl["k"]) * 2.2 * 1.8 ** (pl["exp"] - 1)


def atl_public():
    a = atl()
    atl_tick(a)
    d = time.strftime("%Y-%m-%d")
    if a["x_day"] != d:
        a["x_day"], a["x_today"] = d, 0
    owned = len(a["plots"])
    tiles = []
    for i, t in enumerate(ATL_TERRAIN):
        pl = a["plots"].get(str(i))
        if pl:
            tiles.append(dict(i=i, t=t, own=True, lvl=pl["lvl"], exp=pl["exp"], acres=round(10 * 1.55 ** pl["k"] * ATL_EXP[pl["exp"] - 1]),
                              inc=atl_income_plot(i, pl), up=atl_up_cost(pl) if pl["lvl"] < ATL_LVMAX else None,
                              ex=atl_exp_cost(pl) if pl["exp"] < len(ATL_EXP) else None))
        else:
            tiles.append(dict(i=i, t=t, own=False, price=atl_price(owned), acres=round(10 * 1.55 ** owned)))
    return dict(bal=a["bal"], inc=atl_income(a), tiles=tiles, owned=owned, x_left=max(0, ATL_XCAP - a["x_today"]), xcap=ATL_XCAP, earned=a["earned"], now=time.time())


def atl_op(c, op, b):
    a = atl()
    atl_tick(a)
    d = time.strftime("%Y-%m-%d")
    if a["x_day"] != d:
        a["x_day"], a["x_today"] = d, 0
    try:
        i = str(int(b.get("id", -1)))
    except (TypeError, ValueError):
        return False, "Ungültig."
    if op == "atl_x":
        try:
            n = int(b.get("amount"))
        except (TypeError, ValueError):
            return False, "Ungültige Menge."
        n = min(n, ATL_XCAP - a["x_today"], int(a["bal"]))
        if n < 1:
            return False, "Zu wenig Atlas-Coins oder Tageslimit (%d) erreicht." % ATL_XCAP
        a["bal"] -= n
        a["x_today"] += n
        coin_add(c, n, "Ozzys Atlas")
        return True, "%d Atlas-Coins in D-Hub-Coins getauscht." % n
    if i not in [str(x) for x in range(len(ATL_TERRAIN))]:
        return False, "Unbekanntes Grundstück."
    pl = a["plots"].get(i)
    if op == "atl_buy":
        if pl:
            return False, "Gehört dir schon."
        k = len(a["plots"])
        if a["bal"] < atl_price(k):
            return False, "Nicht genug Atlas-Coins."
        a["bal"] -= atl_price(k)
        a["plots"][i] = dict(k=k, lvl=1, exp=1)
        return True, "Grundstück gekauft."
    if not pl:
        return False, "Erst kaufen."
    if op == "atl_up":
        if pl["lvl"] >= ATL_LVMAX:
            return False, "Höchste Stufe."
        cost = atl_up_cost(pl)
        if a["bal"] < cost:
            return False, "Nicht genug Atlas-Coins."
        a["bal"] -= cost
        pl["lvl"] += 1
        return True, "Verbessert auf Stufe %d." % pl["lvl"]
    if op == "atl_exp":
        if pl["exp"] >= len(ATL_EXP):
            return False, "Voll ausgebaut."
        cost = atl_exp_cost(pl)
        if a["bal"] < cost:
            return False, "Nicht genug Atlas-Coins."
        a["bal"] -= cost
        pl["exp"] += 1
        return True, "Grundstück erweitert."
    return False, "Unbekannt."


ARC_CAP = 150  # Arcade: höchstens so viele Coins pro Tag


def cs():
    c = store.setdefault("coins", {})
    for k, v in (("balance", 0), ("streak", 0), ("last_daily", ""), ("owned", list(FREE.values())), ("active", dict(FREE)), ("done", []),
                 ("day", ""), ("play_today", 0), ("play_acc", 0), ("net", 0), ("rounds", 0), ("arc_today", 0), ("arc_best", {}), ("inv_best", {}), ("arc_start", 0), ("feed", []), ("fid", 0), ("total", 0)):
        c.setdefault(k, v)
    for it in CATALOG:
        if it["price"] == 0 and it["id"] not in c["owned"]:
            c["owned"].append(it["id"])
    for k, v in FREE.items():
        c["active"].setdefault(k, v)
        if v not in c["owned"]:
            c["owned"].append(v)
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
    elif op.startswith("atl_"):
        ok, msg = atl_op(c, op, b)
    elif op == "arcstart":
        c["arc_start"] = time.time()
    elif op == "arcade":
        ok, msg = arcade_reward(c, b)
    elif op == "gamble":
        ok, msg, res = gamble(c, str(b.get("game")), b.get("bet"), b)
    save()
    coin_check()
    return dict(ok=ok, msg=msg, result=res, coins=coin_public(), atlas=atl_public())


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
                errs=LIB["errs"], busy=LIB["busy"], upd=upd_public(), inbox=inbox_public(), unread=unread_count(), kf=store.get("kf") or {}, perms=dict(mic=consent("microphone"), cam=consent("webcam")), autostart=autostart_get(), coins=coin_public(), atlas=atl_public(), fs=FS[0], winopen=time.time() - LAST[0] < 6, native=WINMODE[0].startswith("eigenes"), gamecfg=store.get("gamecfg") or {}, hk=dict(toggle=hk_name("hk_toggle"), close=hk_name("hk_close")), ov=dict(target=OV["target"], on=OV["on"], err=OV["err"], fps=OV["fps"], msg=OV["fpsmsg"]), uninst=bool(uninstaller()), galaxy=os.path.exists(GALAXY_DB), haskey=bool(store["settings"].get("steamkey")), legendary=bool(find_legendary()))


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
.slot{width:min(470px,100%);margin:8px auto 14px;padding:14px 18px 12px;border-radius:28px;background:linear-gradient(160deg,#611325,#2b0610 55%,#4d0e1b);box-shadow:0 0 0 3px #e3bd55,0 0 0 6px #7a5a14,0 0 0 8px #2a1a04,0 20px 50px rgba(0,0,0,.65),inset 0 0 50px rgba(0,0,0,.55);position:relative}
.slotlights{display:flex;justify-content:space-between;padding:2px 6px}.slotlights i{width:10px;height:10px;border-radius:50%;background:#ffe9a0;box-shadow:0 0 8px 2px rgba(255,214,90,.8);animation:bulb 1s infinite}.slotlights i:nth-child(even){animation-delay:.5s}.slotlights.bot{margin-top:12px}
@keyframes bulb{0%,100%{opacity:1}50%{opacity:.25;box-shadow:none}}
.slot.win .slotlights i{animation-duration:.25s}
.slottitle{margin:8px 0 10px;font-weight:900;font-size:24px;letter-spacing:.14em;background:linear-gradient(#fff3b8,#e3bd55 50%,#a8791c);-webkit-background-clip:text;background-clip:text;color:transparent;text-shadow:0 0 18px rgba(255,200,60,.35)}
.rwin{position:relative;background:#07070b;border-radius:18px;padding:12px;margin:0 auto;width:fit-content;box-shadow:inset 0 0 0 3px #e3bd55,inset 0 0 0 5px #6b4c0e,inset 0 0 26px #000}
.rwin::before{content:"";position:absolute;inset:0;border-radius:18px;background:linear-gradient(115deg,rgba(255,255,255,.2),rgba(255,255,255,0) 38%);pointer-events:none;z-index:3}
.rwin::after{content:"";position:absolute;left:10px;right:10px;top:50%;height:2px;background:rgba(255,60,60,.75);box-shadow:0 0 8px #f33;z-index:2;pointer-events:none}
.reels{display:flex;justify-content:center;gap:8px;margin:0}
.reel{width:112px;height:132px;display:flex;align-items:center;justify-content:center;overflow:hidden;border-radius:10px;background:linear-gradient(#b4b4c0 0,#f6f6fb 16%,#fff 50%,#f6f6fb 84%,#b4b4c0 100%);box-shadow:inset 0 12px 16px rgba(0,0,0,.45),inset 0 -12px 16px rgba(0,0,0,.45)}
.rsym{font-size:64px;line-height:1;filter:drop-shadow(0 3px 2px rgba(0,0,0,.35))}
.reel.spin .rsym{filter:blur(2.2px);animation:rshake .09s linear infinite}@keyframes rshake{0%{transform:translateY(-22px)}100%{transform:translateY(22px)}}
.reel.land .rsym{animation:rland .4s cubic-bezier(.3,1.6,.5,1)}@keyframes rland{0%{transform:translateY(-34px)}100%{transform:none}}
.slotpay{display:flex;gap:6px;justify-content:center;flex-wrap:wrap;margin-top:12px;font-size:13px}.slotpay span{display:flex;gap:4px;align-items:center;background:rgba(0,0,0,.35);border:1px solid rgba(227,189,85,.45);border-radius:999px;padding:2px 9px}.slotpay b{color:#ffd878}
.slotbtn{padding:11px 44px;font-size:17px;font-weight:800;border-radius:999px;background:linear-gradient(#ffe27a,#d99a1c)!important;color:#2a1604!important;box-shadow:0 5px 0 #7a5410,0 10px 18px rgba(0,0,0,.4)}.slotbtn:active{transform:translateY(3px);box-shadow:0 2px 0 #7a5410}
.dnum{font-size:44px;font-weight:800;margin:2px 0;min-height:54px}
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
.gshell .arwrap{max-width:none;width:min(100%,calc((100vh - 135px)*1.6));margin:0 auto}
body.ingame nav,body.ingame footer{display:none}body.ingame main{padding:10px 16px}body.ingame header{padding:7px 18px}
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
.aric{font-size:30px}.gtitle{margin:0 12px;font-size:16px}.gbar{display:flex;align-items:center}
.arov{align-items:stretch;justify-content:flex-start}.arbox{margin:auto}
.shh{margin:0 0 8px;font-size:24px}.shtop{display:flex;gap:8px;justify-content:center;margin-bottom:12px}
.shchip{display:inline-block;padding:3px 10px;border-radius:999px;background:rgba(255,255,255,.07);border:1px solid rgba(255,255,255,.12);font-size:12.5px;font-weight:600}
.shcards{display:flex;gap:12px;flex-wrap:wrap;justify-content:center}
.shc{position:relative;width:176px;padding:12px 12px 14px;border-radius:14px;background:linear-gradient(170deg,rgba(255,255,255,.07),rgba(255,255,255,.02)),#10141c;border:1.5px solid var(--tc,#444);box-shadow:0 0 18px color-mix(in srgb,var(--tc,#444) 35%,transparent);cursor:pointer;color:#e8ecf2;text-align:left;transition:transform .15s}
.shc:hover{transform:translateY(-4px)}.shc.dis{opacity:.5;filter:saturate(.5)}.shc.sold{opacity:.35;display:flex;align-items:center;justify-content:center;min-height:180px;cursor:default}
.shart{height:84px;display:flex;align-items:center;justify-content:center;margin-bottom:6px;border-radius:10px;background:radial-gradient(circle at 50% 60%,color-mix(in srgb,var(--tc,#444) 20%,transparent),transparent 70%)}.shart img{height:82px}
.sht{position:absolute;right:10px;top:8px;font-style:normal;font-weight:800;font-size:12px;color:var(--tc);border:1px solid var(--tc);border-radius:6px;padding:0 6px}
.shb{display:flex;align-items:center;gap:6px;font-size:11px;margin-top:3px;color:#aab}.shb span{width:62px}.shb i{flex:1;height:5px;border-radius:3px;background:rgba(255,255,255,.1);overflow:hidden}.shb u{display:block;height:100%;background:var(--tc);border-radius:3px}
.shd{font-size:11.5px;color:#9aa3b2;margin-top:6px;line-height:1.3}.shp{margin-top:8px;text-align:center;font-weight:800;color:#ffd23f;background:rgba(255,210,63,.1);border:1px solid rgba(255,210,63,.35);border-radius:8px;padding:4px 0}
.shi{width:64px;height:64px;border-radius:50%;border:2px solid;display:flex;align-items:center;justify-content:center}.shi svg{width:34px;height:34px;fill:none;stroke-width:1.8;stroke-linejoin:round;stroke-linecap:round}
.shown{margin-top:14px;padding:10px;border-radius:12px;background:rgba(255,255,255,.04);border:1px solid rgba(255,255,255,.08)}.shl{font-size:11px;letter-spacing:.1em;text-transform:uppercase;color:#8a93a3;margin-bottom:6px}
.shrow{display:flex;gap:6px;flex-wrap:wrap;justify-content:center}.shmini{position:relative;width:76px;height:44px;border-radius:8px;border:1px solid var(--tc);background:rgba(255,255,255,.04)}.shmini img{width:100%;height:100%;object-fit:contain}.shmini i{position:absolute;right:3px;bottom:1px;font-size:10px;font-style:normal;font-weight:800;color:var(--tc)}
.gatl{background:linear-gradient(#17324a,#0e1f2e)}.gmini{position:absolute;inset:18px 30px;display:grid;grid-template-columns:repeat(4,1fr);gap:6px}.gmini i{border-radius:6px;animation:gpulse 3s ease-in-out infinite;opacity:.85}@keyframes gpulse{50%{opacity:.4;transform:scale(.94)}}
.gcoin{position:absolute;right:12px;bottom:10px;font-family:Consolas,monospace;font-weight:700;color:#ffd23f;text-shadow:0 0 10px rgba(255,210,63,.6);animation:gfloat 2.4s ease-in-out infinite}@keyframes gfloat{50%{transform:translateY(-6px)}}
#atl .atlh{display:flex;gap:26px;flex-wrap:wrap;align-items:flex-end;padding:14px 18px;border:1px solid var(--line);border-radius:14px;background:var(--panel);margin-bottom:12px}
.atbig{font-size:32px;font-weight:800;font-family:Consolas,monospace;font-variant-numeric:tabular-nums;color:#ffd23f}.atmid{font-size:17px;font-weight:700;font-family:Consolas,monospace;font-variant-numeric:tabular-nums}
#atx{margin-left:auto}.atmain{display:grid;grid-template-columns:1fr 290px;gap:14px}.atmap{display:grid;grid-template-columns:repeat(6,1fr);gap:8px}
.atile{position:relative;border-radius:10px;overflow:hidden;cursor:pointer;aspect-ratio:1.5;border:2px solid transparent;transition:transform .15s,border-color .15s}.atile svg{width:100%;height:100%;display:block}
.atile:not(.own) svg{filter:saturate(.35) brightness(.7)}.atile:hover{transform:scale(1.03)}.atile.sel{border-color:var(--ac);box-shadow:0 0 16px rgba(var(--acrgb),.5)}
.atinfo{position:absolute;left:0;right:0;bottom:0;padding:12px 6px 4px;background:linear-gradient(transparent,rgba(0,0,0,.85));font-size:11px;display:flex;flex-direction:column;color:#fff}.atinfo span{font-family:Consolas,monospace;color:#ffd23f}
.atlock::before{content:"🔒";position:absolute;right:6px;top:4px;font-size:12px}.atside{border:1px solid var(--line);border-radius:14px;background:var(--panel);padding:14px;align-self:start}.atside h3{margin:0 0 6px}
@media(max-width:980px){.atmain{grid-template-columns:1fr}}.arpr{margin-top:6px;font-weight:800;color:#ffd23f}

#skcv{position:fixed;inset:0;z-index:0;pointer-events:none}
body.skon>header,body.skon>main,body.skon>#kfwrap:not(.hide),body.skon>footer{position:relative;z-index:1}
.chsel{display:flex;gap:10px;align-items:center;margin:10px 0 2px;flex-wrap:wrap}.chb{display:flex;align-items:center;gap:8px;padding:5px 14px 5px 8px;border-radius:12px;border:1px solid var(--line);background:var(--panel);color:var(--tx);cursor:pointer;font:inherit}
.chb img{height:42px}.chb.on{border-color:var(--ac);box-shadow:0 0 12px rgba(var(--acrgb),.35)}.chb.lock img{filter:grayscale(1) brightness(.55)}.chb i{font-style:normal;color:#ffd23f;font-size:12px}.chimg{height:52px;flex:none}
.gozzy{position:absolute;left:50%;bottom:-8px;height:178px;transform:translateX(-46%);filter:drop-shadow(0 6px 10px rgba(0,0,0,.55))}.gatl .gmini{opacity:.5}
.gbub{position:absolute;left:56%;top:12px;background:#fff;color:#1b1f2a;font-weight:800;font-size:13px;padding:5px 11px;border-radius:12px;animation:gfloat 2.4s ease-in-out infinite}.gbub::after{content:"";position:absolute;left:12px;bottom:-7px;border:7px solid transparent;border-top-color:#fff;border-bottom:0}
.atozzy{display:flex;align-items:flex-end;gap:10px;max-width:380px}.atozzy img{height:98px;flex:none}.atbub{position:relative;background:#fff;color:#1b1f2a;font-weight:600;font-size:13px;padding:9px 13px;border-radius:14px;line-height:1.3;margin-bottom:18px}.atbub::before{content:"";position:absolute;left:-8px;bottom:12px;border:8px solid transparent;border-right-color:#fff;border-left:0}
.skp{display:block;width:70px;height:48px;border-radius:9px;flex:none;border:1px solid rgba(255,255,255,.25);position:relative;overflow:hidden}
.skp-sk_none{background:linear-gradient(135deg,#0a120a,#1c2a1c)}
.skp-sk_matrix{background:repeating-linear-gradient(90deg,#001a05 0 5px,#00330c 5px 8px);animation:skm 1.2s linear infinite;background-size:100% 200%}@keyframes skm{to{background-position:0 -48px}}
.skp-sk_crt{background:repeating-linear-gradient(0deg,#031a0a 0 2px,#0a3a18 2px 4px)}.skp-sk_crt::after{content:"";position:absolute;left:0;right:0;height:14px;background:linear-gradient(transparent,rgba(120,255,120,.35),transparent);animation:sks 1.8s linear infinite}@keyframes sks{from{top:-14px}to{top:48px}}
.skp-sk_synth{background:linear-gradient(#2a0a52 0 50%,#ff4f9a 50% 52%,#12032a 52%)}.skp-sk_synth::after{content:"";position:absolute;left:0;right:0;bottom:0;height:24px;background:repeating-linear-gradient(90deg,transparent 0 9px,rgba(255,79,216,.8) 9px 10px),repeating-linear-gradient(0deg,transparent 0 6px,rgba(60,220,255,.7) 6px 7px);animation:skg 1s linear infinite}@keyframes skg{to{background-position:0 7px,0 7px}}
.skp-sk_inferno{background:linear-gradient(0deg,#ff6a1a,#7a1a00 55%,#1c0200);animation:ski 1s ease-in-out infinite alternate}@keyframes ski{to{filter:brightness(1.35)}}
.skp-sk_galaxy{background:radial-gradient(circle at 70% 30%,#7aa8ff55,transparent 45%),radial-gradient(circle at 25% 70%,#b45cff55,transparent 50%),#070a26}.skp-sk_galaxy::after{content:"";position:absolute;inset:0;background:radial-gradient(1px 1px at 20% 30%,#fff,transparent),radial-gradient(1px 1px at 60% 70%,#fff,transparent),radial-gradient(1.4px 1.4px at 80% 25%,#fff,transparent);animation:skt 2s ease-in-out infinite alternate}@keyframes skt{to{opacity:.3}}
.skp-sk_gold{background:linear-gradient(115deg,#0b0905 20%,#ffd23f 48%,#0b0905 76%);background-size:240% 100%;animation:skgd 2.4s linear infinite}@keyframes skgd{to{background-position:-240% 0}}
body.sk-matrix{--panel:rgba(0,16,3,.78);--bg:#000;--line:rgba(25,255,106,.28);font-family:Consolas,"Courier New",monospace}
body.sk-matrix h2,body.sk-matrix h3,body.sk-matrix .brand b{text-shadow:0 0 10px rgba(25,255,106,.7)}
body.sk-crt{--panel:rgba(2,22,8,.8);--bg:#010a05;--line:rgba(125,255,106,.3);--tx:#b8ffb0;--dim:#6fbf68;font-family:Consolas,"Courier New",monospace;text-shadow:0 0 5px rgba(125,255,106,.55)}
body.sk-crt::after{content:"";position:fixed;inset:0;pointer-events:none;z-index:9998;background:repeating-linear-gradient(0deg,rgba(0,0,0,.3) 0 1px,transparent 1px 3px),radial-gradient(ellipse at center,transparent 58%,rgba(0,0,0,.6));animation:crtf .13s infinite}@keyframes crtf{0%{opacity:.93}50%{opacity:1}100%{opacity:.88}}
body.sk-synth{--panel:rgba(26,6,54,.72);--bg:#0d0020;--line:rgba(255,79,216,.4)}
body.sk-synth nav button{text-transform:uppercase;letter-spacing:.07em;font-size:12.5px}
body.sk-synth .brand b{animation:glt 3.2s infinite;text-shadow:0 0 12px rgba(255,79,216,.8)}@keyframes glt{0%,92%,100%{transform:none;text-shadow:0 0 12px rgba(255,79,216,.8)}93%{transform:translateX(-3px);text-shadow:3px 0 #3cdcff,-3px 0 #ff4fd8}95%{transform:translateX(3px);text-shadow:-3px 0 #3cdcff,3px 0 #ff4fd8}97%{transform:none}}
body.sk-synth .panel,body.sk-synth .card{box-shadow:0 0 14px rgba(255,79,216,.18)}
body.sk-inferno{--panel:rgba(30,6,0,.76);--bg:#0c0100;--line:rgba(255,106,26,.42)}
body.sk-inferno .brand b,body.sk-inferno h2{background:linear-gradient(#fff3b0,#ff9a1a 45%,#d92b00);-webkit-background-clip:text;background-clip:text;-webkit-text-fill-color:transparent;color:transparent;animation:fire 1.1s ease-in-out infinite alternate}@keyframes fire{to{filter:brightness(1.3) drop-shadow(0 0 6px #ff6a1a)}}
body.sk-inferno .panel,body.sk-inferno .card{animation:fglow 1.6s ease-in-out infinite alternate}@keyframes fglow{from{box-shadow:0 0 8px rgba(255,90,0,.15)}to{box-shadow:0 0 18px rgba(255,120,20,.32)}}
body.sk-galaxy{--panel:rgba(14,16,52,.55);--bg:#02030f;--line:rgba(122,168,255,.28)}
body.sk-galaxy .panel,body.sk-galaxy .card,body.sk-galaxy header,body.sk-galaxy .ng,body.sk-galaxy .atside,body.sk-galaxy .gcard{backdrop-filter:blur(9px)}
body.sk-gold{--panel:rgba(18,14,5,.78);--bg:#0b0905;--line:rgba(255,210,63,.35)}
body.sk-gold .brand b,body.sk-gold h2{background:linear-gradient(100deg,#8a6a14 20%,#fff0a8 40%,#ffd23f 50%,#8a6a14 80%);background-size:250% 100%;-webkit-background-clip:text;background-clip:text;-webkit-text-fill-color:transparent;color:transparent;animation:goldshine 4s linear infinite}@keyframes goldshine{to{background-position:-250% 0}}
body.sk-gold .panel,body.sk-gold .card{border-color:rgba(255,210,63,.35)}

.gcent{max-width:780px;margin:0 auto;text-align:center}.gcent .row{justify-content:center}.gcent h2,.gcent .sub{text-align:center}
.coinwrap{position:relative;height:290px;display:flex;align-items:center;justify-content:center;perspective:1000px;overflow:visible}
.cshadow{position:absolute;left:50%;top:calc(50% + 86px);width:120px;height:26px;margin-left:-60px;border-radius:50%;background:radial-gradient(ellipse,rgba(0,0,0,.65),transparent 70%);opacity:.6}
.ctilt{transform-style:preserve-3d;transform:rotateX(-24deg);width:130px;height:130px;position:relative}
.cjump{transform-style:preserve-3d;width:130px;height:130px;position:absolute;inset:0}
.cn{width:130px;height:130px;position:absolute;inset:0;transform-style:preserve-3d}
.cn .ce{position:absolute;inset:0;border-radius:50%;background:conic-gradient(from 0deg,#8a5d00,#d9a400,#7a5200,#c58a00,#8a5d00);border:3px solid #6e4a00;box-sizing:border-box}
.cn .cf{position:absolute;inset:0;border-radius:50%;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:2px;backface-visibility:hidden;transform:translateZ(7.5px);box-sizing:border-box;border:5px solid #ffe27a;
 background:radial-gradient(circle at 32% 28%,#fff3b0 0,#f5c518 38%,#c58a00 78%,#a06c00 100%);color:#5a3b00;box-shadow:inset 0 0 0 4px rgba(120,80,0,.35),inset 0 0 22px rgba(255,255,255,.35)}
.cn .cf::before{content:"";position:absolute;inset:11px;border-radius:50%;border:2px dashed rgba(100,65,0,.55)}.cn .cf::after{content:"";position:absolute;inset:0;border-radius:50%;background:linear-gradient(115deg,transparent 30%,rgba(255,255,255,.5) 46%,transparent 58%);animation:cshine 2.8s linear infinite}
@keyframes cshine{0%{opacity:0}30%{opacity:1}100%{opacity:0}}
.cn .cf.cb{transform:rotateX(180deg) translateZ(7.5px)}.cn .cf b{font-size:21px;letter-spacing:2px;text-shadow:0 1px 0 rgba(255,255,255,.55)}.cic{font-size:34px;line-height:1;text-shadow:0 1px 0 rgba(255,255,255,.6),0 -1px 0 rgba(90,55,0,.5)}
.d20w{position:relative;height:290px;display:flex;align-items:center;justify-content:center;perspective:900px;margin-bottom:4px}
.d20aura{position:absolute;width:320px;height:320px;border-radius:50%;background:radial-gradient(circle,rgba(150,100,255,.4),rgba(255,200,90,.1) 45%,transparent 68%);animation:aura 3.2s ease-in-out infinite}
@keyframes aura{50%{transform:scale(1.12);opacity:.75}}
.d20ring{position:absolute;width:250px;height:250px;border-radius:50%;border:2px dashed rgba(215,185,255,.4);animation:spin 26s linear infinite}.d20ring.r2{width:290px;height:290px;border:1px solid rgba(255,214,120,.3);animation-duration:40s;animation-direction:reverse}
@keyframes spin{to{transform:rotate(360deg)}}
.d20sp{position:absolute;left:50%;top:50%;width:5px;height:5px;border-radius:50%;background:#ffe9a8;box-shadow:0 0 8px 2px rgba(255,214,120,.9);opacity:0;animation:d20sp 3.4s ease-in-out infinite}
.d20sp:nth-of-type(1){--x:-120px;--y:-80px}.d20sp:nth-of-type(2){--x:110px;--y:-110px;animation-delay:.5s}.d20sp:nth-of-type(3){--x:-140px;--y:40px;animation-delay:1s}.d20sp:nth-of-type(4){--x:130px;--y:70px;animation-delay:1.5s}.d20sp:nth-of-type(5){--x:-40px;--y:-135px;animation-delay:2s}.d20sp:nth-of-type(6){--x:60px;--y:120px;animation-delay:2.5s}.d20sp:nth-of-type(7){--x:0px;--y:-100px;animation-delay:2.9s}
@keyframes d20sp{0%{opacity:0;transform:translate(0,0) scale(.4)}25%{opacity:1}100%{opacity:0;transform:translate(var(--x),calc(var(--y) - 40px)) scale(1)}}
.d20w.rolling .d20aura{animation-duration:.6s}.d20w.win .d20aura{background:radial-gradient(circle,rgba(110,255,150,.5),rgba(255,214,90,.14) 45%,transparent 68%)}.d20w.lose .d20aura{background:radial-gradient(circle,rgba(255,90,90,.4),rgba(120,60,200,.1) 45%,transparent 68%)}
.d20j,.d20t,.d20s,.d20fin{transform-style:preserve-3d;position:absolute;left:50%;top:50%;width:0;height:0}
.d20t{transform:rotateX(-18deg) rotateY(10deg) scale(1.3)}
.d20f3{position:absolute;left:-44px;top:-51px;width:88px;height:76px;transform-origin:50% 66.667%;backface-visibility:hidden;background:linear-gradient(160deg,#f1d27a,#a67a22 55%,#e6c15e);clip-path:polygon(50% 0,0 100%,100% 100%)}
.d20f3::before{content:"";position:absolute;inset:0;clip-path:polygon(50% 9%,7% 95%,93% 95%);background:linear-gradient(165deg,#3a2285,#150b3d 55%,#4a2a9c)}
.d20f3::after{content:"";position:absolute;inset:0;clip-path:polygon(50% 9%,7% 95%,93% 95%);background:rgba(255,255,255,var(--l,.05));pointer-events:none}
.d20f3 i{position:absolute;left:50%;top:66.667%;transform:translate(-50%,-48%);z-index:2;font-style:normal;font-weight:800;font-size:25px;color:#ffe9a8;text-shadow:0 0 6px rgba(255,200,90,.9),0 0 14px rgba(255,170,60,.6);font-family:Verdana,Arial,sans-serif}
.chfig{position:relative;display:inline-block;width:93px;height:119px}.chfig img{position:absolute;left:0;top:0;width:93px;height:97.5px}.chfig img.l1{left:18px;top:90px;width:21px;height:14px}.chfig img.l2{left:59px;top:90px;width:18px;height:14px}
.chprev{width:120px;height:130px;display:flex;align-items:center;justify-content:center;background:radial-gradient(circle at 50% 40%,rgba(var(--acrgb),.18),transparent 70%);border-radius:14px}.chprev .chfig{transform:scale(1.1)}
.chmini{width:58px;height:66px;flex:none;position:relative;overflow:hidden;display:block}.chmini .chfig{transform:scale(.55);transform-origin:0 0}
.brand #updbtn{font:700 12px/1 'Segoe UI',sans-serif;font-style:normal;letter-spacing:.02em;padding:6px 12px;border-radius:999px;margin-left:4px;white-space:nowrap;animation:updp 1.8s ease-in-out infinite}
@keyframes updp{0%,100%{box-shadow:0 0 0 0 rgba(var(--acrgb),.55)}50%{box-shadow:0 0 0 7px rgba(var(--acrgb),0)}}
</style></head><body>
<div id="splash" aria-hidden="true"><div class="sp-logo"><img src="/logo.png" alt=""></div>
 <div class="sp-tag">Alle Spiele. <b>Ein Ort.</b></div>
 <div class="sp-bar"><i></i></div></div>
<header>
 <div class="brand"><img src="/logo.png" alt="" onerror="this.remove()"><span>D-<b>HUB</b></span><small id="ver" title="Klicken: nach Updates suchen" style="cursor:pointer"></small><button class="btn pri" id="updbtn" style="display:none"></button></div>
 <nav></nav>
 <div id="tools"><input id="q" type="search" placeholder="Spiele suchen"><button class="btn" id="rf" title="Neu einlesen: neu installierte Spiele erscheinen hier">↻</button><button class="btn pri" id="plus" title="Eigenes Spiel hinzufügen">＋</button><button class="btn" id="vw"></button></div>
<div id="hact">
 <button class="hbtn coinchip" id="coinbtn" title="Coins und Shop" onclick="tab='Shop';render()"><span>🪙</span><b id="coinn">0</b><small id="cointitle"></small></button>
 <button class="hbtn" id="fsbtn" title="Vollbild (F11)" aria-label="Vollbild" onclick="fsToggle()"><svg viewBox="0 0 24 24"><path d="M4 9V4h5M20 9V4h-5M4 15v5h5M20 15v5h-5"/></svg></button>
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
function __e(m){__errs.push(m);try{let b=document.getElementById('__eb');if(!b){b=document.createElement('div');b.id='__eb';b.style.cssText='position:fixed;left:0;right:0;bottom:0;z-index:99999;background:#3a0d0d;color:#ffd0d0;font:13px/1.4 Consolas,monospace;padding:10px 16px;white-space:pre-wrap;max-height:40vh;overflow:auto';document.body.appendChild(b)}b.textContent='D-Hub Fehler (bitte Foto davon schicken):\n'+__errs.slice(-4).join('\n')}catch(x){}try{fetch('/api/jslog',{method:'POST',body:JSON.stringify({m:String(m).slice(0,400)})})}catch(x){}}
addEventListener('error',e=>__e((e.message||'Fehler')+' @'+(e.lineno||'?')));
addEventListener('unhandledrejection',e=>__e('Promise: '+(e.reason&&e.reason.message||e.reason)));
addEventListener('unhandledrejection',e=>__e('Promise: '+((e.reason&&e.reason.message)||e.reason)));
async function dbg(){const w=document.getElementById('wd');w.style.display='flex';let t='';try{t=await (await fetch('/api/debug')).text()}catch(e){t='Server nicht erreichbar: '+e}document.getElementById('wdp').textContent=t+'\n\nJS-Fehler:\n'+(__errs.join('\n')||'-')}
setTimeout(()=>{if(!window.__ok)document.getElementById('wd').style.display='flex'},7000);
async function fsToggle(){try{const r=await (await fetch('/api/fullscreen',{method:'POST',body:'{}'})).json();if(!r.native){if(document.fullscreenElement)await document.exitFullscreen();else await document.documentElement.requestFullscreen()}}catch(e){}}
addEventListener('keydown',e=>{if(e.key=='F11'){e.preventDefault();fsToggle();return}if(e.key=='F5'){e.preventDefault();location.reload()}if(e.ctrlKey&&e.shiftKey&&e.key.toLowerCase()=='d'){e.preventDefault();dbg()}});
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
  row('Preise automatisch laden',chk(T.autoprices!==false,"api('settings',{autoprices:this.checked})"))+row('KumpelFunk beim Start verbinden',chk(T.kfauto!==false,"api('settings',{kfauto:this.checked})"))+row('Updates automatisch suchen',chk(T.autoupdate!==false,"api('settings',{autoupdate:this.checked})"))+row('Updates automatisch installieren (beim Start)',chk(T.autoinstall!==false,"api('settings',{autoinstall:this.checked})"))+row('Overlay ein-/ausblenden (Taste)',sel('hk_toggle',HKS.map(x=>[x,x]),S.hk.toggle))+row('Overlay beenden/starten (Taste)',sel('hk_close',HKS.map(x=>[x,x]),S.hk.close))+row('Im Vollbild starten',chk(T.fullscreen===true,"api('settings',{fullscreen:this.checked})"))+row('Vollbild jetzt umschalten (F11)','<button class="btn" onclick="fsToggle()">Umschalten</button>')+row('Startanimation zeigen',chk(T.splash!==false,"api('settings',{splash:this.checked});try{localStorage.setItem('dhub_nosplash',this.checked?'':'1')}catch(e){}"))+'</div>';
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
 $('#ver').textContent='Beta '+S.version;$('#ver').onclick=()=>{api('action',{name:'checkupdate'});toast('Prüfe auf Updates …');setTimeout(()=>api('state').then(()=>{updChrome();toast(S.upd&&S.upd.state=='available'?'Update '+S.upd.latest+' ist verfügbar.':'D-Hub ist aktuell.')}),2500)};
 $('#tools').style.display=isLib()?'flex':'none';
 $('#vw').textContent=S.settings.view=='cover'?'Als Liste':'Als Cover';
 const kf=tab=='KumpelFunk';
 $('#kfwrap').classList.toggle('hide',!kf);$('main').style.display=kf?'none':'';
 if(kf){if(!$('#kf').getAttribute('src'))$('#kf').src=KF_URL}
 else if(tab=='Arcade'){arcRender()}else{$('main').innerHTML=isLib()?libView():tab=='Shop'?shopView():tab=='Gamble'?gambleView():tab=='Patchnotes'?patchView():tab=='Downloads'?dlView():tab=='Wert'?valView():setView();if(tab=='Gamble')gRefresh()}
 if(tab!='Arcade'){arStop();ivStop();atStop();auStop();arcMode=null}
 if(!arcMode)document.body.classList.remove('ingame');
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
 const sk=c.catalog.find(i=>i.id==c.active.skin),skid=sk&&sk.id!='sk_none'&&!b.classList.contains('light')?sk.id.slice(3):'';
 [...b.classList].filter(x=>x.startsWith('sk-')).forEach(x=>b.classList.remove(x));
 if(skid){b.classList.add('sk-'+skid);b.style.setProperty('--ac',sk.data[0]);b.style.setProperty('--acrgb',sk.data[1])}
 skinSet(skid);
 [...b.classList].filter(x=>x.startsWith('hv-')).forEach(x=>b.classList.remove(x));b.classList.add('hv-'+c.active.hover.slice(2));
 $('#coinn').textContent=c.balance;const t=c.catalog.find(i=>i.id==c.active.title);$('#cointitle').textContent=t&&t.id!='ti_none'?'· '+t.name:'';
 const last=c.feed.length?c.feed[c.feed.length-1].id:0;
 if(window._fid===undefined)window._fid=last;
 else if(last>window._fid){c.feed.filter(f=>f.id>window._fid).forEach(f=>toast(f.msg));window._fid=last}
}
async function coinOp(op,id,extra){
 const r=await (await fetch('/api/coin',{method:'POST',body:JSON.stringify(Object.assign({op,id},extra||{}))})).json();
 S.coins=r.coins;if(r.msg&&!(extra&&extra.game))toast(r.msg);applyCoins();if(tab=='Shop')render();return r}
function itemArt(cat,i){if(cat=='theme')return `<span class="sw" style="background:${i.data[0]};color:${i.data[0]}"></span>`;if(cat=='char')return `<img class="chimg" src="${CH_SRC[(CHARS[i.id]||CHARS.ch_dhub).k]}" alt="">`;if(CHEQ.includes(cat))return `<span class="chmini">${chFull(chEq({[cat]:i.id}))}</span>`;if(cat=='skin')return `<span class="skp skp-${i.id}"></span>`;return ''}
function shopView(){
 const c=S.coins,sec=(cat,title)=>`<h3 style="margin:24px 0 10px">${title}</h3><div class="shop">`+c.catalog.filter(i=>i.cat==cat).map(i=>{
  const act=c.active[cat]==i.id,own=c.owned.includes(i.id);
  const btn=act?'<i class="b ok">Aktiv</i>':own?`<button class="btn" onclick="coinOp('equip','${i.id}')">Ausrüsten</button>`:`<button class="btn pri" ${c.balance<i.price?'disabled':''} onclick="coinOp('buy','${i.id}')">🪙 ${i.price}</button>`;
  return `<div class="item">${itemArt(cat,i)}<div style="flex:1"><b>${esc(i.name)}</b><div class="dim">${esc(i.desc)}</div></div>${btn}</div>`}).join('')+'</div>';
 const done=c.milestones.filter(m=>m.done).length;
 return `<h2>Shop</h2><div class="sub">Coins gibt es nur in D-Hub zu verdienen. Sie haben keinen Geldwert und lassen sich nicht kaufen.</div>
 <div class="panel"><div class="row"><div style="flex:1"><span style="font-size:26px;font-weight:800">🪙 ${c.balance}</span><div class="dim">Serie: ${c.streak} Tage · Insgesamt verdient: ${c.total}</div></div>
  ${c.daily_ready?`<button class="btn pri" onclick="coinOp('daily')">Täglicher Bonus: +${c.daily_amount}</button>`:'<i class="b mute">Heute abgeholt, morgen wieder</i>'}</div>
  <div class="row"><div style="flex:1"><b>Spielzeit heute</b><div class="dim">Alle 10 Minuten Spielen: +2 Coins, höchstens ${c.play_cap} pro Tag.</div></div><div style="width:160px"><div class="bar"><i style="width:${100*c.play_today/c.play_cap}%"></i></div><div class="dim" style="text-align:right">${c.play_today} / ${c.play_cap}</div></div></div></div>
 ${sec('skin','Special-Skins')}${sec('theme','Farbthemen')}${sec('hover','Hover-Effekte für Spiele')}${sec('title','Titel')}${sec('char','Spielfiguren (Arcade)')}${chShop(c)}${sec('hat','Hüte')}${sec('ear','Kopfhörer')}${sec('face','Gesicht')}${sec('shirt','Oberteile')}${sec('feet','Schuhe')}
 <h3 style="margin:24px 0 10px">Meilensteine (${done}/${c.milestones.length})</h3><div class="shop">${c.milestones.map(m=>`<div class="item" style="opacity:${m.done?.55:1}"><span style="font-size:18px">${m.done?'✅':'⬜'}</span><div style="flex:1">${esc(m.label)}</div><b>🪙 ${m.reward}</b></div>`).join('')}</div>`}
/* ===== SPECIAL-SKINS (animierte Hintergründe und Effekte) ===== */
let skRaf=0,skName='',skC=null,skX=null,skSt=null,skW=0,skH=0,skLast=0,skAcc=0;
const SK_GLY='アイウエオカキクケコサシスセソタチツテトナニヌネノ0123456789ABCDEFXYZ';
function skAdd(cv,w,h,fn){const c=document.createElement('canvas');c.width=w;c.height=h;fn(c.getContext('2d'),w,h);return c}
const SKINS={
 matrix:{init(){const n=Math.ceil(skW/18);return {first:1,d:Array.from({length:n},()=>({y:Math.random()*skH/16,s:.4+Math.random()*.9}))}},
  draw(c,w,h,t,dt,s){if(s.first){c.fillStyle='#000';c.fillRect(0,0,w,h);s.first=0}c.fillStyle='rgba(0,6,0,.10)';c.fillRect(0,0,w,h);c.font='15px monospace';
   s.d.forEach((q,i)=>{const x=i*18,y=q.y*16;c.fillStyle='#d8ffe0';c.fillText(SK_GLY[Math.floor(Math.random()*SK_GLY.length)],x,y);c.fillStyle='rgba(25,255,106,.85)';c.fillText(SK_GLY[Math.floor(Math.random()*SK_GLY.length)],x,y-16);q.y+=q.s*dt*22;if(q.y*16>h+40&&Math.random()>.975){q.y=0;q.s=.4+Math.random()*.9}})}},
 crt:{init(){return {}},
  draw(c,w,h,t){const g=c.createLinearGradient(0,0,0,h);g.addColorStop(0,'#03140a');g.addColorStop(1,'#010a05');c.fillStyle=g;c.fillRect(0,0,w,h);
   const y=(t*140)%(h+200)-100,b=c.createLinearGradient(0,y-90,0,y+90);b.addColorStop(0,'rgba(120,255,120,0)');b.addColorStop(.5,'rgba(120,255,120,.10)');b.addColorStop(1,'rgba(120,255,120,0)');c.fillStyle=b;c.fillRect(0,y-90,w,180);
   c.fillStyle='rgba(120,255,120,.05)';for(let i=0;i<6;i++)c.fillRect(0,Math.random()*h,w,1+Math.random()*2);
   const v=c.createRadialGradient(w/2,h/2,h*.3,w/2,h/2,w*.7);v.addColorStop(0,'rgba(0,0,0,0)');v.addColorStop(1,'rgba(0,0,0,.6)');c.fillStyle=v;c.fillRect(0,0,w,h)}},
 synth:{init(){return {st:Array.from({length:70},()=>({x:Math.random(),y:Math.random()*.55,p:Math.random()*6}))}},
  draw(c,w,h,t,dt,s){const hz=h*.58,g=c.createLinearGradient(0,0,0,hz);g.addColorStop(0,'#0d0020');g.addColorStop(.55,'#3b0a5e');g.addColorStop(1,'#ff4f9a');c.fillStyle=g;c.fillRect(0,0,w,hz);
   c.fillStyle='#fff';s.st.forEach(q=>{c.globalAlpha=.3+.6*Math.abs(Math.sin(t+q.p));c.fillRect(q.x*w,q.y*h,1.5,1.5)});c.globalAlpha=1;
   const R=Math.min(w,h)*.24,cx=w/2,cy=hz-R*.1;c.save();c.beginPath();c.rect(0,0,w,hz);c.clip();const sg=c.createLinearGradient(0,cy-R,0,cy+R);sg.addColorStop(0,'#ffe27a');sg.addColorStop(.5,'#ff6a9a');sg.addColorStop(1,'#d62bd6');c.fillStyle=sg;c.shadowColor='#ff4fd8';c.shadowBlur=40;c.beginPath();c.arc(cx,cy,R,0,6.3);c.fill();c.shadowBlur=0;
   c.fillStyle='#3b0a5e';for(let i=0;i<8;i++){const yy=cy+R*.05+i*R*.13+((t*8)%(R*.13)),hh=2+i*1.6;c.fillRect(cx-R,yy,R*2,hh)}c.restore();
   const fl=c.createLinearGradient(0,hz,0,h);fl.addColorStop(0,'#1a0636');fl.addColorStop(1,'#07010f');c.fillStyle=fl;c.fillRect(0,hz,w,h-hz);
   c.strokeStyle='rgba(255,79,216,.75)';c.shadowColor='#ff4fd8';c.shadowBlur=8;c.lineWidth=1.4;const N=16;
   for(let i=0;i<N;i++){const p=((i+((t*.35)%1))/N),y=hz+(h-hz)*Math.pow(p,2.3);c.beginPath();c.moveTo(0,y);c.lineTo(w,y);c.stroke()}
   c.strokeStyle='rgba(60,220,255,.6)';c.shadowColor='#3cdcff';for(let i=-14;i<=14;i++){c.beginPath();c.moveTo(cx+i*w*.012,hz);c.lineTo(cx+i*w*.14,h);c.stroke()}c.shadowBlur=0}},
 inferno:{init(){return {p:[]}},
  draw(c,w,h,t,dt,s){const g=c.createLinearGradient(0,0,0,h);g.addColorStop(0,'#0c0100');g.addColorStop(.6,'#2a0500');g.addColorStop(1,'#7a1a00');c.fillStyle=g;c.fillRect(0,0,w,h);
   const gl=.75+.25*Math.sin(t*3)+Math.random()*.08,hg=c.createRadialGradient(w/2,h*1.05,10,w/2,h*1.05,h*.8);hg.addColorStop(0,'rgba(255,120,20,'+(.55*gl)+')');hg.addColorStop(1,'rgba(255,60,0,0)');c.fillStyle=hg;c.fillRect(0,0,w,h);
   for(let i=0;i<3;i++)s.p.push({x:Math.random()*w,y:h+10,vx:(Math.random()-.5)*30,vy:-(50+Math.random()*130),r:1+Math.random()*3.2,l:1,ph:Math.random()*6});
   c.globalCompositeOperation='lighter';for(const q of s.p){q.x+=q.vx*dt+Math.sin(t*2+q.ph)*.6;q.y+=q.vy*dt;q.l-=dt*.22;const a=Math.max(0,q.l);const gr=c.createRadialGradient(q.x,q.y,0,q.x,q.y,q.r*5);gr.addColorStop(0,'rgba(255,200,90,'+a+')');gr.addColorStop(1,'rgba(255,60,0,0)');c.fillStyle=gr;c.beginPath();c.arc(q.x,q.y,q.r*5,0,6.3);c.fill()}c.globalCompositeOperation='source-over';s.p=s.p.filter(q=>q.l>0&&q.y>-20)}},
 galaxy:{init(){const mk=(n,sp,r)=>Array.from({length:n},()=>({x:Math.random()*skW,y:Math.random()*skH,sp,r:r*(.6+Math.random()*.8),p:Math.random()*6}));
   return {l:[mk(90,6,1),mk(60,16,1.5),mk(30,36,2.1)],sh:null,shT:3,sp:Array.from({length:700},(_,i)=>{const arm=i%2,d=Math.random(),a=d*9+arm*Math.PI;return {a,d,j:(Math.random()-.5)*.5}})}},
  draw(c,w,h,t,dt,s){const g=c.createRadialGradient(w*.5,h*.4,10,w*.5,h*.4,Math.max(w,h)*.8);g.addColorStop(0,'#14134a');g.addColorStop(.5,'#080a2a');g.addColorStop(1,'#02030f');c.fillStyle=g;c.fillRect(0,0,w,h);
   [[.2,.3,'rgba(150,60,220,.22)',340],[.8,.7,'rgba(40,150,230,.18)',380],[.55,.85,'rgba(220,70,150,.14)',300]].forEach((n,i)=>{const x=w*n[0]+Math.sin(t*.15+i)*60,y=h*n[1]+Math.cos(t*.12+i)*40,r=c.createRadialGradient(x,y,0,x,y,n[3]);r.addColorStop(0,n[2]);r.addColorStop(1,'rgba(0,0,0,0)');c.fillStyle=r;c.fillRect(0,0,w,h)});
   const gx=w*.8,gy=h*.28,rot=t*.05;c.fillStyle='rgba(200,215,255,.55)';s.sp.forEach(q=>{const a=q.a+rot,r=q.d*Math.min(w,h)*.2,x=gx+Math.cos(a)*r*(1+q.j),y=gy+Math.sin(a)*r*.45*(1+q.j);c.fillRect(x,y,1.4,1.4)});
   const core=c.createRadialGradient(gx,gy,0,gx,gy,40);core.addColorStop(0,'rgba(255,240,200,.8)');core.addColorStop(1,'rgba(255,240,200,0)');c.fillStyle=core;c.fillRect(gx-40,gy-40,80,80);
   s.l.forEach(L=>L.forEach(q=>{q.x-=q.sp*dt;if(q.x<0)q.x=w;c.globalAlpha=.4+.6*Math.abs(Math.sin(t*1.3+q.p));c.fillStyle='#fff';c.fillRect(q.x,q.y,q.r,q.r)}));c.globalAlpha=1;
   s.shT-=dt;if(s.shT<=0&&!s.sh){s.sh={x:Math.random()*w*.8+w*.2,y:Math.random()*h*.4,l:1};s.shT=5+Math.random()*6}
   if(s.sh){const q=s.sh;q.x-=700*dt;q.y+=300*dt;q.l-=dt*1.6;c.strokeStyle='rgba(255,255,255,'+Math.max(0,q.l)+')';c.lineWidth=2;c.beginPath();c.moveTo(q.x,q.y);c.lineTo(q.x+70,q.y-30);c.stroke();if(q.l<=0)s.sh=null}}},
 gold:{init(){const mb=skAdd(null,skW,skH,(c,w,h)=>{c.fillStyle='#0b0905';c.fillRect(0,0,w,h);c.strokeStyle='rgba(255,215,120,.07)';c.lineWidth=1.2;for(let i=0;i<26;i++){c.beginPath();let x=Math.random()*w,y=Math.random()*h;c.moveTo(x,y);for(let k=0;k<6;k++){x+=(Math.random()-.4)*160;y+=(Math.random()-.3)*120;c.lineTo(x,y)}c.stroke()}
     const v=c.createRadialGradient(w/2,h/2,h*.2,w/2,h/2,w*.75);v.addColorStop(0,'rgba(0,0,0,0)');v.addColorStop(1,'rgba(0,0,0,.7)');c.fillStyle=v;c.fillRect(0,0,w,h)});
   return {mb,sp:Array.from({length:60},()=>({x:Math.random()*skW,y:Math.random()*skH,p:Math.random()*6,r:1.5+Math.random()*3.5,vy:4+Math.random()*14}))}},
  draw(c,w,h,t,dt,s){c.drawImage(s.mb,0,0);const x=((t*180)%(w+h*2))-h,b=c.createLinearGradient(x,0,x+h*.6,h);b.addColorStop(0,'rgba(255,225,140,0)');b.addColorStop(.5,'rgba(255,225,140,.09)');b.addColorStop(1,'rgba(255,225,140,0)');c.fillStyle=b;c.fillRect(0,0,w,h);
   for(const q of s.sp){q.y+=q.vy*dt;if(q.y>h)q.y=-10;const a=Math.max(0,Math.sin(t*2+q.p));c.fillStyle='rgba(255,226,140,'+a*.9+')';c.shadowColor='#ffd23f';c.shadowBlur=8;const r=q.r*a;c.beginPath();c.moveTo(q.x,q.y-r*2);c.lineTo(q.x+r*.5,q.y-r*.5);c.lineTo(q.x+r*2,q.y);c.lineTo(q.x+r*.5,q.y+r*.5);c.lineTo(q.x,q.y+r*2);c.lineTo(q.x-r*.5,q.y+r*.5);c.lineTo(q.x-r*2,q.y);c.lineTo(q.x-r*.5,q.y-r*.5);c.fill()}c.shadowBlur=0}}
};
function skinSet(n){if(n===skName)return;skStop();skName=n;if(!n||!SKINS[n])return;
 const cv=document.createElement('canvas');cv.id='skcv';document.body.prepend(cv);skC=cv;skX=cv.getContext('2d');skResize();skSt=SKINS[n].init();document.body.classList.add('skon');skLast=performance.now();skRaf=requestAnimationFrame(skLoop)}
function skStop(){if(skRaf)cancelAnimationFrame(skRaf);skRaf=0;const cv=$('#skcv');if(cv)cv.remove();skC=null;document.body.classList.remove('skon');skName=''}
function skResize(){if(!skC)return;skW=skC.width=innerWidth;skH=skC.height=innerHeight}
addEventListener('resize',()=>{if(!skC)return;skResize();skSt=SKINS[skName].init()});
function skLoop(now){skRaf=0;if(!skC)return;const dt=Math.min(.06,(now-skLast)/1000);skAcc+=dt;skLast=now;
 if(skAcc>=.033&&!document.hidden&&!(tab=='Arcade'&&arcMode)){const d=skAcc;skAcc=0;try{SKINS[skName].draw(skX,skW,skH,now/1000,d,skSt)}catch(e){}}
 skRaf=requestAnimationFrame(skLoop)}

/* ===== AUDIO (alles per WebAudio erzeugt, keine Dateien) ===== */
const AU={ctx:null,master:null,mus:null,sfxg:null,noise:null,mute:false,vol:.6,track:null,timer:null,step:0,next:0,last:{}};
try{AU.mute=localStorage.getItem('dhub_mute')=='1'}catch(e){}
function auCtx(){if(!AU.ctx){try{const C=window.AudioContext||window.webkitAudioContext;const c=new C();AU.ctx=c;AU.master=c.createGain();AU.master.gain.value=AU.mute?0:AU.vol;AU.master.connect(c.destination);
  AU.mus=c.createGain();AU.mus.gain.value=.55;AU.mus.connect(AU.master);AU.sfxg=c.createGain();AU.sfxg.gain.value=.9;AU.sfxg.connect(AU.master);
  const b=c.createBuffer(1,c.sampleRate,c.sampleRate),d=b.getChannelData(0);for(let i=0;i<d.length;i++)d[i]=Math.random()*2-1;AU.noise=b}catch(e){AU.ctx=null;return null}}
 if(AU.ctx.state=='suspended')AU.ctx.resume();return AU.ctx}
function auMute(m){AU.mute=m;try{localStorage.setItem('dhub_mute',m?'1':'0')}catch(e){}if(AU.master)AU.master.gain.value=m?0:AU.vol;document.querySelectorAll('.mutebtn').forEach(b=>b.textContent=m?'🔇':'🔊')}
const muteBtn=()=>`<button class="btn mutebtn" title="Musik und Ton an/aus" onclick="auMute(!AU.mute)">${AU.mute?'🔇':'🔊'}</button>`;
function auPause(p){if(!AU.ctx)return;if(p)AU.ctx.suspend();else AU.ctx.resume()}
function auTone(o){const c=AU.ctx,t=o.t,g=c.createGain(),os=c.createOscillator();os.type=o.type||'square';os.frequency.setValueAtTime(o.f,t);
 if(o.slide)os.frequency.exponentialRampToValueAtTime(Math.max(20,o.slide),t+o.d);let n=os;
 if(o.lp){const f=c.createBiquadFilter();f.type='lowpass';f.frequency.setValueAtTime(o.lp,t);if(o.lpEnd)f.frequency.exponentialRampToValueAtTime(o.lpEnd,t+o.d);os.connect(f);n=f}
 g.gain.setValueAtTime(.0001,t);g.gain.linearRampToValueAtTime(o.vol||.2,t+(o.a||.006));g.gain.exponentialRampToValueAtTime(.0001,t+o.d);n.connect(g);g.connect(o.dest||AU.mus);os.start(t);os.stop(t+o.d+.05)}
function auNoise(t,d,vol,hp,dest,lp){const c=AU.ctx,s=c.createBufferSource();s.buffer=AU.noise;const f=c.createBiquadFilter();f.type=lp?'lowpass':'highpass';f.frequency.value=hp;const g=c.createGain();g.gain.setValueAtTime(vol,t);g.gain.exponentialRampToValueAtTime(.0001,t+d);s.connect(f);f.connect(g);g.connect(dest||AU.mus);s.start(t);s.stop(t+d+.02)}
function auSweep(t,d,vol,type,f0,f1,q,dest,a){const c=AU.ctx,s=c.createBufferSource();s.buffer=AU.noise;s.loop=true;const f=c.createBiquadFilter();f.type=type;f.Q.value=q||1;f.frequency.setValueAtTime(f0,t);f.frequency.exponentialRampToValueAtTime(Math.max(30,f1),t+d);const g=c.createGain();g.gain.setValueAtTime(.0001,t);g.gain.linearRampToValueAtTime(vol,t+(a||.01));g.gain.exponentialRampToValueAtTime(.0001,t+d);s.connect(f);f.connect(g);g.connect(dest||AU.sfxg);s.start(t);s.stop(t+d+.05)}
function auKick(t,v){const c=AU.ctx,o=c.createOscillator(),g=c.createGain();o.type='sine';o.frequency.setValueAtTime(150,t);o.frequency.exponentialRampToValueAtTime(42,t+.17);g.gain.setValueAtTime(v,t);g.gain.exponentialRampToValueAtTime(.0001,t+.2);o.connect(g);g.connect(AU.mus);o.start(t);o.stop(t+.22)}
function auSnare(t,v){auNoise(t,.14,v,1200);auTone({t,f:210,slide:120,d:.1,type:'triangle',vol:v*.9})}
const NF=n=>440*Math.pow(2,(n-69)/12);
const AUT={
 inv:{bpm:118,step(i,t,sd){const bar=Math.floor(i/16)%4,s=i%16,root=[45,41,48,43][bar],q=bar==0?[0,3,7]:[0,4,7];
  if(s%4==0)auKick(t,.55);if(s==4||s==12)auSnare(t,.22);if(s%2==0)auNoise(t,.04,s%4==2?.09:.05,7000);
  if(s%2==0)auTone({t,f:NF(root+(s%8==6?12:0)),d:sd*1.8,type:'sawtooth',vol:.2,lp:520});
  const ar=[0,1,2,1,2,1,0,2][s%8];auTone({t,f:NF(root+24+q[ar]+(s%8>=4?12:0)),d:sd*.9,type:'square',vol:.045,lp:2200});
  if(s==0)q.forEach(x=>auTone({t,f:NF(root+12+x),d:sd*15,a:.25,type:'sawtooth',vol:.04,lp:900}));
  if(i>=64){const mel=[[0,-1,-1,3,-1,7,-1,-1,5,-1,-1,3,-1,2,-1,-1],[0,-1,3,-1,5,-1,7,-1,12,-1,-1,10,-1,7,-1,-1],[7,-1,-1,7,-1,5,-1,3,5,-1,-1,3,-1,2,-1,-1],[0,-1,2,-1,3,-1,7,-1,5,-1,3,-1,2,-1,0,-1]][bar][s];if(mel>=0)auTone({t,f:NF(69+mel),d:sd*2.2,type:'sawtooth',vol:.06,lp:2600,lpEnd:900})}}},
 surv:{bpm:152,step(i,t,sd){const bar=Math.floor(i/16)%4,s=i%16;
  if([0,6,8,11].includes(s))auKick(t,.6);if(s==4||s==12)auSnare(t,.26);auNoise(t,.03,s%2?.05:.08,8000);
  const A=[0,0,12,0,0,10,0,3,0,0,12,0,0,10,7,5],B=[-2,-2,10,-2,-2,8,-2,1,-4,-4,8,-4,0,0,3,5],r=(bar%2?B:A)[s];
  auTone({t,f:NF(40+r),d:sd*.9,type:'sawtooth',vol:.2,lp:900});auTone({t,f:NF(52+r),d:sd*.6,type:'square',vol:.04,lp:1500});
  if(i>=32){const mel=[[12,-1,15,-1,17,-1,15,12,-1,10,-1,12,-1,-1,7,-1],[12,-1,15,-1,19,-1,17,15,-1,12,-1,15,-1,-1,10,-1]][bar%2][s];if(mel>=0)auTone({t,f:NF(64+mel),d:sd*1.6,type:'square',vol:.055,lp:3000})}}},
 atl:{bpm:84,step(i,t,sd){const bar=Math.floor(i/16)%4,s=i%16,root=[48,45,53,55][bar],ch=[[0,4,7,11],[0,3,7,10],[0,4,7,11],[0,4,7,10]][bar];
  if(s==0||s==10)auKick(t,.3);if(s==4||s==12)auNoise(t,.1,.12,2500);if(s%2==0)auNoise(t,.03,.03,8000);
  if(s==0)ch.forEach(x=>auTone({t,f:NF(root+12+x),d:sd*15,a:.3,type:'triangle',vol:.06,lp:1200}));
  if(s%2==0)auTone({t,f:NF(root+24+ch[[0,2,1,3,2,1,3,2][s/2]]),d:sd*2.5,type:'sine',vol:.09})}}
};
function auPlay(name){if(AU.track==name)return;auStop();if(!auCtx())return;AU.track=name;AU.step=0;AU.next=AU.ctx.currentTime+.12;
 AU.timer=setInterval(()=>{const T=AUT[name];if(!T||!AU.ctx)return;while(AU.next<AU.ctx.currentTime+.3){const sd=60/T.bpm/4;T.step(AU.step,AU.next,sd);AU.next+=sd;AU.step++}},45)}
function auStop(){if(AU.timer){clearInterval(AU.timer);AU.timer=null}AU.track=null}
const SFXTH={shoot:40,pistol:45,smg:50,shotgun:80,rocket:120,rboom:60,hit:35,kill:45,coin:50,ding:0,dclack:40};
function sfx(n,x){if(AU.mute||!AU.ctx||AU.ctx.state!='running')return;const now=performance.now();if(AU.last[n]&&now-AU.last[n]<(SFXTH[n]||0))return;AU.last[n]=now;
 const c=AU.ctx,t=c.currentTime,d=AU.sfxg;
 switch(n){
 case 'shoot':auTone({t,f:900,slide:260,d:.09,type:'square',vol:.07,dest:d});break;
 case 'pistol':{const r=1+Math.random()*.12;auNoise(t,.045,.34,1600,d);auSweep(t,.13,.22,'lowpass',2600,320,.8,d,.002);auTone({t,f:180*r,slide:48,d:.11,type:'sine',vol:.3,dest:d});auSweep(t+.06,.16,.05,'lowpass',1800,400,.6,d,.03);break}
 case 'smg':{const r=1+Math.random()*.25;auNoise(t,.03,.24,2400,d);auSweep(t,.07,.15,'lowpass',2800*r,500,.8,d,.002);auTone({t,f:210*r,slide:70,d:.06,type:'sine',vol:.2,dest:d});break}
 case 'shotgun':{auNoise(t,.07,.4,1300,d);auSweep(t,.34,.5,'lowpass',2200,180,.7,d,.002);auTone({t,f:120,slide:30,d:.3,type:'sine',vol:.45,dest:d});auSweep(t+.1,.3,.08,'lowpass',1500,300,.5,d,.05);
  auNoise(t+.3,.03,.16,2600,d);auTone({t:t+.3,f:240,slide:150,d:.04,type:'square',vol:.05,dest:d});auNoise(t+.39,.04,.2,1700,d);auTone({t:t+.39,f:340,slide:200,d:.05,type:'square',vol:.06,dest:d});break}
 case 'sword':auNoise(t,.1,.12,2500,d);auTone({t,f:500,slide:1100,d:.09,type:'triangle',vol:.06,dest:d});break;
 case 'wand':auTone({t,f:600,slide:1400,d:.18,type:'sine',vol:.09,dest:d});auTone({t,f:1200,slide:1800,d:.12,type:'triangle',vol:.04,dest:d});break;
 case 'rocket':{auTone({t,f:95,slide:38,d:.28,type:'sine',vol:.38,dest:d});auNoise(t,.1,.25,700,d,true);
  auSweep(t,.9,.5,'bandpass',500,2800,1.3,d,.06);auSweep(t+.25,.7,.3,'bandpass',2800,900,1.1,d,.05);auSweep(t,.8,.16,'highpass',4000,2500,.6,d,.05);auTone({t,f:70,slide:55,d:.8,type:'sawtooth',vol:.07,dest:d,lp:260});break}
 case 'rboom':{auSweep(t,1.1,.8,'lowpass',2200,90,.7,d,.003);auTone({t,f:85,slide:24,d:.9,type:'sine',vol:.7,dest:d});auNoise(t,.12,.4,1000,d);
  for(let k=0;k<7;k++){const q=t+.06+Math.random()*.55;auNoise(q,.05+Math.random()*.06,.1+Math.random()*.1,1200+Math.random()*2500,d)}auSweep(t+.1,.7,.14,'highpass',3500,1500,.6,d,.05);break}
 case 'dclack':{const r=1+Math.random()*.4;auNoise(t,.04,.3,1500,d);auTone({t,f:320*r,slide:140,d:.07,type:'triangle',vol:.22,dest:d});auTone({t,f:1900*r,d:.05,type:'sine',vol:.05,dest:d});break}
 case 'dmagic':[1047,1319,1568,2093,2637].forEach((f,i)=>{auTone({t:t+i*.07,f,d:.6,type:'sine',vol:.09,dest:d});auTone({t:t+i*.07,f:f*2.003,d:.3,type:'sine',vol:.025,dest:d})});break
 case 'coinring':auTone({t,f:2400,d:.5,type:'sine',vol:.09,dest:d});auTone({t,f:3610,d:.3,type:'sine',vol:.05,dest:d});auNoise(t,.03,.1,5000,d);break
 case 'boom':auNoise(t,.35,.28,400,d,true);auTone({t,f:90,slide:35,d:.3,type:'sine',vol:.25,dest:d});break;
 case 'hit':auNoise(t,.05,.09,2000,d);break;
 case 'kill':auTone({t,f:340,slide:90,d:.13,type:'sawtooth',vol:.1,dest:d,lp:1400});break;
 case 'hurt':auTone({t,f:170,slide:55,d:.28,type:'sawtooth',vol:.22,dest:d});auNoise(t,.15,.15,600,d,true);break;
 case 'coin':auTone({t,f:1175,d:.07,type:'square',vol:.05,dest:d});auTone({t:t+.06,f:1568,d:.12,type:'square',vol:.05,dest:d});break;
 case 'buy':auTone({t,f:880,d:.08,type:'square',vol:.07,dest:d});auTone({t:t+.07,f:1320,d:.08,type:'square',vol:.07,dest:d});auTone({t:t+.14,f:1760,d:.16,type:'square',vol:.07,dest:d});break;
 case 'wave':[523,659,784,1047].forEach((f,i)=>auTone({t:t+i*.09,f,d:.2,type:'square',vol:.08,dest:d}));break;
 case 'pow':[600,800,1000,1300,1700].forEach((f,i)=>auTone({t:t+i*.05,f,d:.1,type:'triangle',vol:.09,dest:d}));break;
 case 'ufo':auTone({t,f:700,slide:350,d:.3,type:'sine',vol:.08,dest:d});auTone({t:t+.15,f:420,slide:900,d:.3,type:'sine',vol:.06,dest:d});break;
 case 'over':[400,330,260,180].forEach((f,i)=>auTone({t:t+i*.16,f,d:.3,type:'sawtooth',vol:.12,dest:d,lp:1500}));break;
 case 'ding':{const f=1568*(1+(x||0)*.07);auTone({t,f,d:.35,type:'sine',vol:.13,dest:d});auTone({t,f:f*2.01,d:.18,type:'sine',vol:.05,dest:d});auTone({t,f:f*3.02,d:.08,type:'sine',vol:.03,dest:d});break}
 case 'clack':auNoise(t,.05,.2,1200,d);auTone({t,f:180,slide:90,d:.1,type:'triangle',vol:.18,dest:d});auTone({t,f:1318,d:.5,type:'sine',vol:.1,dest:d});break;
 case 'win':[659,784,988,1318,988,1318,1568].forEach((f,i)=>auTone({t:t+i*.1,f,d:.22,type:'square',vol:.07,dest:d}));break;
 case 'jackpot':for(let k=0;k<12;k++)auTone({t:t+k*.09,f:[784,988,1175,1568][k%4]*(1+Math.floor(k/4)*.25),d:.3,type:'square',vol:.07,dest:d});break;
 case 'lose':auTone({t,f:300,slide:150,d:.35,type:'triangle',vol:.1,dest:d});break;
 case 'tick':auTone({t,f:1200,d:.03,type:'square',vol:.04,dest:d});break}}

/* ===== ARCADE (Brotato-Stil) ===== */
const ARS={body:'data:image/png;base64,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',ll:'data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAACoAAAAcCAYAAAAX4C3rAAAGzklEQVR42rWYS4wcVxWGv3Nu9bwfOPY42E5ixzbOghAhxIYQaUxYskGW7A1SpDgLXlKUBTuQMqOAhFhaiB0rFpCMyCZSJKxgzwgkY0MCyK84kT3u8Tgz+BHPK56errrnsLhV3W2b8SM2t9Wqul1dt/7zn8d/bgnlcEdE8O++KYe2fSNcO/mOnpqdbP7jwgQz3DZE4c1IOA0yOQmbr+ITp3HGccD5PwypTl53dFzo+tH52pmndvY+vbSS01hZW1i7wceNBf3Xfy7YRx7s+Jn3+Gj6tywAa3csJmCGAoyBMgmTk7B3DGMMxh/CkAT0dZRxbOTL7Nr3dtfHG/e4URSiGZohKILjxEaNfIWlhfl82YzjK5flXHPYT9aP2PV54eTMz7gBNO76QElPtYhWxox/i+JeQLOEE8aB53/SHQc2iEMzSMQtijfBRc0kgPQU2tPD0LZNDAVknzwnFCjPfK2Lzxa0Yd+TWae4vDSdr27YrUfmTiArV7LZ2uPx9Lk/5HJmggvuLOEgggH2QIyWbrfvH+v60savhA+1P1ctzF1c8JICSR5zEzfcJWDlFUSDKqIKCIYDQg0IGLB2s8najQiBuWbui415VyvCpcGn9Oz8+3nz2Kv+03qdRrmcr8vo5BgK2IfvFS+88HVRkALRDByXFFZeWaUuAoK7IikkLEYKcHMc3B0QNVeVlHt9rn19ToAtAbaEJxXH9vSj315Z5u16neZbTjggxPUYVQD2psljz9AVMsWsAiiIaBlYbVOl9J0DiXFAXFRcFUImEmpIFowsGAFDPCJFxJqFWKPQWFhWXF0slo4d89cAO32PJNMOnIzswjWVqpIXJx0kpZQIjuDScno5B2mFSJqnNZIdQnVJVIKoq3ivZln9ff5y+iCX3nLCuNw9Xm9hdHAzKNYBsqwmJWkVKC8Lb5vQBFhEkPLPbaLTdUFxhMIdV5frn+b+wRH7pTsyMXHvZNLOSa2vWrSDmQqUlM4vmarCIdF2a3qKC3gH86UnEAWX2I2F2X8XfzrzC/4KyMSB9WPzlmRqoQ4QPEAEdyvZ7ASZEKlICbYNuOIy/e7lrZ6YdVLYuNEjIkvXzD446mPuyIGJtujcP6ONwJDUGO5W+mpOrRYJmYGAF4rFgEfFooAr4gEhoISWa6UKyA6ahcSwmMZudb143N89+wbHJ0Dvh812edo7CkwxsW+Tb9ndw8COVYb2rDK4M+exHTkDIxC6FCPSxIlAxFLFjoqYtMqXV5EiCbSUSWe492iQq1eEk4djxeYDa30GFFtHnvhxV+j9dbORFyhZrU8Y2CgMP+kMP10wtLtBz9acvm1NekciXSMFPcNOIBBQUrBYu+aiGMnAoshjXybh1FH/3e9f9JfuVTfXi1EHsFCc0t6m9/ajIuqCSGNRWb2mXP57DY894AZdke5ep3uTMfC4M7gVhp4wujcYtZEmfV+M9A5DbRB6Bx2puXf3q8zP2sL18/6aOyJjD9ac3AJ0YGDgfJHnHgiKlqkTIpJFanj6lL4tTMgvB5ZnAnNRMKvKkqEZhC4n9Bmht4As91q/MfDcysG/HVr9VGZTE/R52jwFbPPw8M7+DV84F0KWeamEVS0tE5mqMkkKwFY3VF5JOuqJeDfBDHN3ikZxdu76rq+yf8qZKKPkAUaV9QbolcXFGRE5pyrgbt6Sl3JVEbRV1FORtQhWgBWC5QmcWykPaoSaWVevaDZob4hMFUys33jcX3nav1+AwszPulcq6u0mslPvSwarMGgX/aRmKeMVEY0h1LIityOXLn7yR3cC3Lv3vCvQ0StXBMDcJ3EXN2srDaWr29uW1LikOlRKaDqXtgC4qGAxLjWbzZdLgJ97m9ICOjU1ZQBra2vvFhbXRDVIKUuJuFLbS7opQ8BLee00JBlsUUVDXhS/mpubmxkdHc0epFFed89Udvo6DrZjx/Y/17LsxRgtikjoBNlSmrJH5Xa2k9TGELKQx+JE/WL9+Y48eHhGSdsRKV3782rJCk7l5Qqid7g5sdy6wVRVotlSY7XxSgfAh9qdhtvmDuji4uL00NDQN7Ms2+3RCi8NuiNdO2My9aDW0RfunZ2d/Wf5DOMhh64XEuarPyiK2BCRDPeCdvPb/nbsLN0pRERFJdxcXXt1enr6RCkokUcw/hdQA3Rm5sqFZt78TjRb1RAyhJjqf3KzmVVHAzzLsgy43misHZyf/+Q3o2X/8KheQIR1fncgLC8vX+jr7z+ShfBs0PBkp1K1elNVKQ1457ObN1+an58/PDpKNlV/dCDvyPp1DIlAz/bt238owss4z6Ym2DH3VVUOu8uher1+tLMTe9SvdP4LX7u6MpJ88BAAAAAASUVORK5CYII=',rl:'data:image/png;base64,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'};
const ARS_OZ={body:'data:image/png;base64,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',ll:'data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAADUAAAAcCAYAAADBRfeOAAAKG0lEQVR42rWYe4xdVb3HP7/fWvucM2dmOm2nItRy5VGqosTYXitXRdGooDG+wvgKaoyKesV7jY+oiFSjkIsXJQqaGF9/GEGpIJIoGoyk+IoBGi0Pi7ZFOkM705nOzJnHmTln77V+94+993mUFl/cPVkz+7HWXuv7/X3X9/fbIwBPPoOTVlI98MYf6uDp28UaR4Jc/+xIcxIQwMgPUYbPfjPDG7YQ0ybEkD8UAAWR/LRzTxDreYcYZQ+Kfn13RMpuIAIqQEQrg8zvu4PFv/4aRMEi3gtZBmdcoLzzJ46HfpraTz+q4us2owBTB0hra7E1m4QER+NhR3M6f28JSABRD9kqsb2EiKC+ivoqIgmCdgCZGRYNouUvKIEhHX5KEL0Qe7jIL83AJWSrDZYP/aG8CUAs/v77ezx1l4T9d8HykXh7msatHgE8pw1tlOrgeo1Kpkf/FCGAOLDQnVNDm8WHbmEpqeMH1pMMnUx1zUZ8/SRcZQhxVUQ8IgKWg8NiEalYLMjyHytI64HVvS7oiQHRARbHdxVEKmYR0VwkG842znqVMb+Q2cQ9iKvbzrkHGPcYuBpjo5slqddcZrT0yAPdScpoGVDis7RJmjZJFyZoHsojqMkgrjqCr6/D1dbiqmvRyjC+UsclA4ivoS7J5VaA7oTHYlfhdCOhvkq6NMXyxN35E4s5uZKv5dwPOYYHMvvDrejcn6VZP9V+3TyEehFIqnLu8EbDEcWAmX08ziEdRvO1GcSM0GoQWg3aCwf7e7tKLtPKIFoZxlUGc5BJHV8ZRHwNcVVUq6jzucSlkG9SZ2nibiy0cnYt31IxwinbHdve4QgxDff/SFx7ns8vHeIvgPcIhDaNgVGAQEyVhcnYla8dC8oKksu9UjBPv5Q6eyu0CaFNaC0CkyemShyoR9T1mJNgYbUr52IKi/AfH1TWVMQO7BYdv9tsaKN9d3YfAkSPQbokt9dq9joHlrYT0uVWEW4rBQLAhtFRohmrq6uk7ZQ0S7uT9fQUjgXLcUB3+5sZZgFC6NvDpdTKExEjBjjpHOEZr4eMNO65Gbc8a98Mi0wADgi5UUQ56iq52ldXlJVmb0SKd6pSr9dx3jMyspbEO9ppSqvVot1ud1qWZYQQ6Bt8DIhyv5oJUpi6SRd2OTTnpHgeQb0QgRd/yrNmUOLRh8U9+OO4J1vgg8XQCODzzrYxGRQUs9ZCpNV47HKcc6gqGFSqCarCgHfUBwY6EYkWybJAmubg0jQjTVOyLCPLUkIWCDESY8RiLJywR8q9KbFn7miGOMgy+LcXOs5+vQda8f5bhdm9/FKEVXsRnl1kAF4E3LC9XL0aIK3FjGypZLLnryqqSlKpoJpLU4plRQMpXC3xniTxiChS5CEDLMYOoLwFQghYNCIGZsRoeOdZba0yPX2kANRP7vM/7KlWUhYbyB9/iFbX2O9W54Bd3T6+oGd9qyliGCHGPuV0zmMsgEkhcSl4ztOu9fY1I5KB0JOU8zHOOxDfk3i7LhpjRFWZPziXE9ktZLAAm84Vznx1RLFw3824qfvirekCO0VQyKOU1zYC6YJ9tTlrC4bhK4ImPVVEKa0Yi4mtZ/XSyZUiIE4QzceoaN60MAzJ+5bgLRrRDLMiciHivKfRaLCwsFg6eJ8WX3SF4JNoi40o937LmtmiXSryWH/WHwQckRvrw/L9jMSt2eTCyCmCmeYLLICFGIkh9GldpGBFCmQFWMkfdKs6ye/lTbvnZeVQ6DxLM6ampsv4IQjO51HacpFy2oWCI8Q9N4pO7eFOER5lDFcaRAfUzmLavT8Pm1Yyx9CI8qQzSsfr2cpmrKys4JzrY8/6f/UF0KSDqKhOSkDdasWKwKsqhw8fpt1uIQX9qrl8a+uFl17pSSTa0oTw+68xR2KfMEPYeZxIHTk/n//QPTa7eDgzj9lJ5xjk27eYNB83Nz+f23VPYpZjrJqOp5VcP6aM7dZ2RTatVivMzh5lfn4+B1TUjaZGlhkvvEx5yhao41u/+Yq4o/vizdk89zOGHhslAN21K2dsZq9978AdUSIim16mVnhDj9SEdrvN9MwMSZL0LVBF+iBI5zOkp+y2jg/2kVCtJMzNzXH48GQ+vpSwg5jBxnPhef9pKIEDd1H74w02P7KFz5ihx4tS8RFEdkVECfzsr78JH2ospm7LSyxu3CaFlXcdTUWYPXqU2fl5qtUaWpQ0ZZHabd3UU7KehzUHHWN+niSe6ZkZDj5ysNhX1jEdDHwdXnW92sCAWWs+rtxxVfaN1hF72eweHi2YiScCxWeFuONO/O5vxy/P7Xe3ravgzvs4GQZlKdYbsUfHJ5icnERVcc7nrmiRaLFT85UiNHr2FSAqJElCyDIeOXiQiYmJDpDSVNXn9d2F1yqnbcMcIr/9Grcd+LldkgXuxdDjVaV9oAAenMZ2GPrgTXx6esY1njemuu1tYiFVkkpBS4d1mJycZN/+/TQajU7F4b3HOZ9XH05xqnjnSJKEJElQdbTbbaampti3bz9zs3PIMWWEOCGksP2jjudf4mJClAdus1/94lP27h2Gt/hYt+PxKswxw+0Uwvb36+1j11UuTJtkXz6/7ad2Q1KJpO3+geVaqtUK9fogA7UalUoFddqTvPPc1m63WV5eZnl5uWM2vTJFwTvIUnjO+5Q3fNXFYZX0nluseuPF2ZVx1S63rSTcS8o/eLgdeWhf8oKP+XCtjYRPjtfTDc+QCGLqsbJ4AExETPp3/9/VRKTvWl33/Ln/pfEqq4VrrWIX3+ps8FQdHzqFpxc8Kv/k4fLh/mPnfaJq11jNPnlYbON2Yu8ipBdcCbBsnPhZB4xg4jDR/NoPYhf8L3aViV1t3t66s7q3/iS5pVbj1BN8t/zDh2rOydY33cSll01L8/NGOP9yMjfQXbBzeVM9UUQKUCWIAojzmBMp7A7bfCF26R6J15lml09q++0/1suA6vH2/r8MrLTzM1+hb3rzTWpfMLX/fpiw9RJidbRjqV0QLm/qMOclb0m/vEor1oS4+dUS3/XLJLvekvRq89nYdyr21PPc1UUGEXagTySgjhTHDJeHPtn2ymv4wPvucvYlq9jnluvZxd9P7JyLJAw+mRRIe0BGkAhaNAlAGDyZeNYrJbz2OrGPHMCuMbUr2zV7yw3eTn+FM9D3AlzydZJ/RW5/10Cz/L9TGCjJu5/+Gr74zIvcmme9Ji7VR9KhxTljbhxmHoGFOQlZ07mQAi5QGzLq62DDZlh/OgzVhDSVcGg3dt+PZGnfL2z3zEPx0WyF74pxxxUB/aw8vmU/IaB6nNGKCU8GnsYge8e+wlmjZ7J1YJTnNJd4nVuna5MBtdp6ZsNqtNaCIcaRhXHc+G95yvxB7lxZtCsevKHSBF2F1YM9iVf/Vg76/znGcKI95VM/7gsGRtz/rBnVD6yt8dRhGAXWA+ugcjZUN/d+toiDsZtwxeeDe6KW+H9aaVObbPtn1AAAAABJRU5ErkJggg==',rl:'data:image/png;base64,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'};
const OZ_FRIENDLY='data:image/png;base64,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';
const mkSet=o=>{const r={};for(const k in o){const i=new Image();i.src=o[k];r[k]=i}return r};
const ARIS={dhub:mkSet(ARS),ozzy:mkSet(ARS_OZ)},CH_SRC={dhub:ARS.body,ozzy:ARS_OZ.body,get blank(){return chRender(chEq()).body}},CHARS={ch_dhub:{k:'dhub',lx:[36.9,117.9],n:'D-Hub'},ch_blank:{k:'blank',lx:[36.9,117.9],n:'Männchen'},ch_ozzy:{k:'ozzy',lx:[37.4,100.1],n:'Ozzy'}};
let ARI=ARIS.dhub,ARLX=[36.9,117.9],CHK='dhub';
function arSetChar(){const id=(S&&S.coins&&S.coins.active&&S.coins.active.char)||'ch_dhub',c=CHARS[id]||CHARS.ch_dhub;CHK=c.k;if(c.k=='blank'){const r=chRender(chEq());if(!r.set)r.set=mkSet({body:r.body,ll:r.ll,rl:r.rl});ARIS.blank=r.set}ARI=ARIS[c.k];ARLX=c.lx}

/* ===== MÄNNCHEN-BAUKASTEN (Blanko-Figur + Kleidung) ===== */
const CHEQ=['hat','ear','face','shirt','feet'],chCache={};
function chEq(over){const a=(S&&S.coins&&S.coins.active)||{},e={};CHEQ.forEach(k=>e[k]=a[k]||k+'_none');return Object.assign(e,over||{})}
const CHO='#1c2026';
function chGrad(c,x0,y0,x1,y1,st){const g=c.createLinearGradient(x0,y0,x1,y1);st.forEach((q,i)=>g.addColorStop(i/(st.length-1),q));return g}
function chFootPath(c,w,h){c.beginPath();c.moveTo(2,3);c.lineTo(w-2,1);c.bezierCurveTo(w-1,h*.7,w*.65,h-1,w*.5,h-1);c.bezierCurveTo(w*.25,h-1,3,h*.7,2,3);c.closePath()}
function chFoot(w,h,type){const cv=document.createElement('canvas');cv.width=w;cv.height=h;const c=cv.getContext('2d');chFootPath(c,w,h);c.save();c.clip();
 if(type=='feet_sneaker'){c.fillStyle='#16191d';c.fillRect(0,0,w,h);c.fillStyle='#5cff0c';c.fillRect(0,0,w,h*.34);c.fillStyle='rgba(255,255,255,.18)';c.fillRect(0,h*.34,w,2)}
 else if(type=='feet_gold'){c.fillStyle=chGrad(c,0,0,w,h,['#fff0a8','#e0a812','#8a6210','#ffd23f']);c.fillRect(0,0,w,h);c.fillStyle='#7a560c';c.fillRect(0,0,w,5);c.fillStyle='rgba(255,255,255,.55)';c.fillRect(4,6,5,h*.5)}
 else{c.fillStyle='#f4f6f8';c.fillRect(0,0,w,h);c.fillStyle='rgba(0,0,0,.08)';c.fillRect(0,h*.65,w,h)}
 c.restore();chFootPath(c,w,h);c.strokeStyle=CHO;c.lineWidth=2.4;c.stroke();return cv}
function chBodyDraw(c,eq){
 c.lineJoin='round';c.lineCap='round';
 // Oberkörper
 const tor=()=>{c.beginPath();c.roundRect?c.roundRect(52,126,82,76,[20,20,8,8]):c.rect(52,126,82,76)};
 tor();c.fillStyle=chGrad(c,0,126,0,195,['#f7f9fb','#dfe4e9']);c.fill();c.strokeStyle=CHO;c.lineWidth=3;c.stroke();
 if(eq.shirt=='shirt_hoodie'){tor();c.save();c.clip();c.fillStyle='#16191d';c.fillRect(40,120,110,90);c.fillStyle='#252a31';c.beginPath();c.ellipse(93,128,36,16,0,0,6.3);c.fill();
  c.strokeStyle='#5cff0c';c.lineWidth=2.4;c.beginPath();c.moveTo(81,138);c.lineTo(80,156);c.moveTo(105,138);c.lineTo(106,156);c.stroke();
  c.font='italic 800 17px Arial,sans-serif';c.textAlign='center';c.fillStyle='#fff';c.fillText('D-',77,184);c.fillStyle='#5cff0c';c.fillText('HUB',108,184);c.restore();tor();c.strokeStyle=CHO;c.lineWidth=3;c.stroke()}
 if(eq.shirt=='shirt_gold'){tor();c.save();c.clip();c.fillStyle=chGrad(c,52,126,134,195,['#fff0a8','#e0a812','#fff0a8','#9a6d10','#ffd23f']);c.fillRect(40,120,110,90);c.strokeStyle='#7a560c';c.lineWidth=2;c.beginPath();c.moveTo(93,128);c.lineTo(93,195);c.moveTo(56,160);c.quadraticCurveTo(93,172,130,160);c.stroke();
  c.fillStyle='#c0262d';c.beginPath();c.arc(93,150,6,0,6.3);c.fill();c.strokeStyle='#7a560c';c.stroke();c.fillStyle='rgba(255,255,255,.5)';c.fillRect(60,134,6,40);c.restore();tor();c.strokeStyle=CHO;c.lineWidth=3;c.stroke()}
 // Kopf
 c.beginPath();c.ellipse(93,84,64,56,0,0,6.3);c.fillStyle=chGrad(c,0,28,0,140,['#ffffff','#e9edf1']);c.fill();c.strokeStyle=CHO;c.lineWidth=3;c.stroke();
 // Gesicht
 c.fillStyle='#14171b';[[68,94],[118,94]].forEach(([x,y])=>{c.beginPath();c.ellipse(x,y,8.5,11.5,0,0,6.3);c.fill()});c.fillStyle='#fff';[[66,89],[116,89]].forEach(([x,y])=>{c.beginPath();c.arc(x,y,3,0,6.3);c.fill()});
 c.strokeStyle=CHO;c.lineWidth=2.6;c.beginPath();c.moveTo(82,118);c.quadraticCurveTo(93,126,104,118);c.stroke();
 c.fillStyle='rgba(255,120,120,.28)';[[46,112],[140,112]].forEach(([x,y])=>{c.beginPath();c.ellipse(x,y,9,5,0,0,6.3);c.fill()});
 if(eq.face=='face_shades'){c.fillStyle='#0b0c0e';[[67,94],[119,94]].forEach(([x,y])=>{c.beginPath();c.ellipse(x,y,17,13,0,0,6.3);c.fill()});c.strokeStyle='#0b0c0e';c.lineWidth=4;c.beginPath();c.moveTo(83,92);c.lineTo(103,92);c.moveTo(50,90);c.lineTo(34,84);c.moveTo(136,90);c.lineTo(152,84);c.stroke();c.strokeStyle='rgba(255,255,255,.65)';c.lineWidth=2.4;c.beginPath();c.moveTo(58,88);c.lineTo(66,84);c.moveTo(110,88);c.lineTo(118,84);c.stroke()}
 if(eq.ear=='ear_phones'){c.strokeStyle=CHO;c.lineWidth=9;c.beginPath();c.ellipse(93,86,66,60,0,Math.PI*1.04,Math.PI*1.96);c.stroke();c.strokeStyle='#5cff0c';c.lineWidth=2.5;c.beginPath();c.ellipse(93,86,66,60,0,Math.PI*1.07,Math.PI*1.93);c.stroke();
  [[27,92],[159,92]].forEach(([x,y])=>{c.fillStyle='#1c2026';c.beginPath();c.ellipse(x,y,13,23,0,0,6.3);c.fill();c.strokeStyle='#5cff0c';c.lineWidth=3;c.beginPath();c.ellipse(x,y,8,16,0,0,6.3);c.stroke();c.fillStyle='rgba(92,255,12,.25)';c.fill()})}
 if(eq.hat=='hat_cap'){c.fillStyle='#14171b';c.beginPath();c.ellipse(93,62,58,40,0,Math.PI,0);c.closePath();c.fill();c.strokeStyle=CHO;c.lineWidth=3;c.stroke();
  c.fillStyle='#0c0e11';c.beginPath();c.ellipse(93,64,66,11,0,0,6.3);c.fill();c.stroke();c.fillStyle='#5cff0c';c.beginPath();c.moveTo(70,46);c.lineTo(116,46);c.lineTo(110,58);c.lineTo(64,58);c.fill();c.fillStyle='#14171b';c.font='italic 800 15px Arial,sans-serif';c.textAlign='center';c.fillText('D',91,57)}
 if(eq.hat=='hat_wizard'){c.fillStyle=chGrad(c,40,0,140,60,['#7d4fd6','#4b2596','#34187a']);c.beginPath();c.moveTo(50,56);c.lineTo(112,3);c.quadraticCurveTo(122,10,120,20);c.lineTo(136,56);c.closePath();c.fill();c.strokeStyle=CHO;c.lineWidth=3;c.stroke();
  c.fillStyle='#4b2596';c.beginPath();c.ellipse(93,56,74,13,0,0,6.3);c.fill();c.stroke();c.fillStyle='#ffd23f';c.beginPath();c.moveTo(60,50);c.lineTo(126,50);c.lineTo(128,57);c.lineTo(58,57);c.fill();
  c.fillStyle='#ffe27a';[[88,34,4],[108,24,3],[100,42,3]].forEach(([x,y,r])=>{c.beginPath();for(let i=0;i<10;i++){const a=i*Math.PI/5-Math.PI/2,rr=i%2?r*.45:r;c.lineTo(x+Math.cos(a)*rr,y+Math.sin(a)*rr)}c.fill()})}
 if(eq.hat=='hat_crown'){c.fillStyle=chGrad(c,48,6,138,44,['#fff0a8','#e0a812','#9a6d10','#ffd23f']);c.beginPath();c.moveTo(50,46);c.lineTo(46,14);c.lineTo(70,30);c.lineTo(93,6);c.lineTo(116,30);c.lineTo(140,14);c.lineTo(136,46);c.closePath();c.fill();c.strokeStyle='#7a560c';c.lineWidth=3;c.stroke();
  c.fillStyle='#d83a3a';c.beginPath();c.arc(93,32,5,0,6.3);c.fill();c.fillStyle='#3a8aff';[[66,38],[120,38]].forEach(([x,y])=>{c.beginPath();c.arc(x,y,4,0,6.3);c.fill()});c.fillStyle='#fff';[[46,14],[93,6],[140,14]].forEach(([x,y])=>{c.beginPath();c.arc(x,y,3.2,0,6.3);c.fill()})}
}
function chRender(eq){const key=CHEQ.map(k=>eq[k]).join('|');if(chCache[key])return chCache[key];
 const cv=document.createElement('canvas');cv.width=186;cv.height=195;chBodyDraw(cv.getContext('2d'),eq);
 return chCache[key]={body:cv.toDataURL(),ll:chFoot(42,28,eq.feet).toDataURL(),rl:chFoot(36,28,eq.feet).toDataURL()}}
function chFull(eq){const r=chRender(eq);return `<span class="chfig"><img src="${r.body}" alt=""><img class="l1" src="${r.ll}" alt=""><img class="l2" src="${r.rl}" alt=""></span>`}
function chShop(c){return `<h3 style="margin:24px 0 10px">Männchen ausrüsten</h3><div class="panel"><div class="row"><div class="chprev">${chFull(chEq())}</div><div style="flex:1"><b>Dein Männchen</b><div class="dim">Kaufe Kleidung und Ausrüstung und trage sie in Survivor und Invaders. Ausgerüstet wird, was du hier aktiv hast.</div></div>${c.active.char=='ch_blank'?'<i class="b ok">Spielfigur aktiv</i>':'<button class="btn pri" onclick="chPick(\'ch_blank\')">Als Spielfigur wählen</button>'}</div></div>`}

function chBody(){arSetChar();return CH_SRC[CHK]}
async function chPick(id){const c=S.coins;if(c.owned.includes(id)){await coinOp('equip',id);arSetChar();const m=$('main');if($('#arhub'))m.innerHTML=arcHub()}else{toast('Noch nicht freigeschaltet. Du kannst sie im Shop kaufen.');tab='Shop';render()}}
const AWW=1400,AWH=875,AZ=960/1400,AX0=30,AY0=72,AX1=AWW-30,AY1=AWH-26;
const AW=[
 {id:'pistol',n:'Pistole',cd:.55,dmg:10,pel:1,rng:420,price:12,desc:'Zuverlässig, mittlere Reichweite'},
 {id:'smg',n:'Maschinenpistole',cd:.15,dmg:3.5,pel:1,rng:340,price:18,desc:'Sehr schnell, wenig Schaden pro Schuss'},
 {id:'shotgun',n:'Schrotflinte',cd:1.1,dmg:6,pel:5,rng:280,price:22,desc:'Fünf Kugeln, stark auf kurze Distanz'},
 {id:'sword',n:'Schwert',cd:.65,dmg:16,pel:1,rng:135,price:15,desc:'Stößt zu und trifft mehrere Gegner'},
 {id:'wand',n:'Zauberstab',cd:.9,dmg:9,pel:1,rng:460,price:20,desc:'Zielsuchende Magie-Kugeln'},
 {id:'rocket',n:'Raketenwerfer',cd:1.7,dmg:24,pel:1,rng:500,price:30,desc:'Explosion trifft alle in der Nähe'}];
const AIT=[
 {id:'hp',s:'hp',v:8,n:'Energydrink',d:'+8 Max-HP',price:10,ic:'<path d="M12 21s-7-4.6-9-9a5 5 0 0 1 9-3 5 5 0 0 1 9 3c-2 4.4-9 9-9 9z"/>',col:'#ff5a6e'},
 {id:'dmg',s:'dmg',v:10,n:'Sammler-Edition',d:'+10 % Schaden',price:14,ic:'<path d="M13 2 4 14h6l-1 8 9-12h-6z"/>',col:'#ffb03a'},
 {id:'spd',s:'spd',v:7,n:'Turnschuhe',d:'+7 % Tempo',price:10,ic:'<path d="M3 17c0-3 2-4 4-4l3-5 3 2 3 1c3 1 5 2 5 5v2H3z"/><path d="M3 19h18"/>',col:'#4dd2ff'},
 {id:'as',s:'as',v:10,n:'Controller',d:'+10 % Angriffstempo',price:14,ic:'<rect x="3" y="8" width="18" height="9" rx="4"/><path d="M8 11v3M6.5 12.5h3"/><circle cx="16" cy="12" r=".8"/><circle cx="18" cy="14" r=".8"/>',col:'#b58aff'},
 {id:'arm',s:'arm',v:2,n:'Poster-Schild',d:'+2 Rüstung',price:10,ic:'<path d="M12 3 5 6v6c0 4.5 3 7.5 7 9 4-1.5 7-4.5 7-9V6z"/>',col:'#7aa8ff'},
 {id:'reg',s:'reg',v:1,n:'Pizza',d:'+0,5 Leben pro Sekunde',price:12,ic:'<path d="M3 6c6-3 12-3 18 0l-9 15z"/><circle cx="10" cy="9" r="1"/><circle cx="14" cy="11" r="1"/><circle cx="12" cy="15" r="1"/>',col:'#ff9a4a'},
 {id:'pick',s:'pick',v:30,n:'Magnet-Maus',d:'+30 Sammelreichweite',price:8,ic:'<path d="M5 3v8a7 7 0 0 0 14 0V3h-4v8a3 3 0 0 1-6 0V3z"/><path d="M5 7h4M15 7h4"/>',col:'#e8ecf2'}];
const CVK=1.5,ATM=[1,1.9,3.2,5],ATC=['#c9d1d9','#5ab0ff','#b06aff','#ff5a4a'],ATN=['I','II','III','IV'],AWAVES=20,ASC=.55;
const ATYPES={basic:{w:34,h:46,hp:1,sp:70,dmg:1,drop:1,col:'#cfd6dd',tint:'rgba(0,0,0,0)'},
 runner:{w:26,h:34,hp:.5,sp:140,dmg:1,drop:1,col:'#7CFC9a',tint:'rgba(0,255,120,.20)'},
 tank:{w:54,h:72,hp:3.5,sp:44,dmg:2,drop:3,col:'#b06aff',tint:'rgba(90,0,150,.30)'},
 elite:{w:46,h:62,hp:6,sp:66,dmg:2,drop:6,col:'#ff5a4a',tint:'rgba(255,0,0,.25)'},
 boss:{w:112,h:150,hp:14,sp:50,dmg:3,drop:30,col:'#ffd23f',tint:'rgba(255,190,0,.18)'}};
let AR=null,arRaf=0,arK={},arLast=0,arImgs=[],arCtx=null,arBGc=null;
const arClamp=(v,a,b)=>Math.max(a,Math.min(b,v));
/* --- Waffen zeichnen (zeigt nach rechts, Mittelpunkt = Griff) --- */
function arGrad(c,y0,y1,a,b){const g=c.createLinearGradient(0,y0,0,y1);g.addColorStop(0,a);g.addColorStop(1,b);return g}
function arWeaponArt(c,id,tier,th){const col=ATC[tier];c.lineJoin='round';c.lineCap='round';
 const OUT='#10131a',steel=arGrad(c,-6,6,'#d5dbe2','#6d7783'),dark=arGrad(c,-6,6,'#59626e','#2b313a'),wood=arGrad(c,-6,6,'#a8703a','#5c3818');
 const poly=(pts,fill,stroke)=>{c.beginPath();pts.forEach((p,i)=>i?c.lineTo(p[0],p[1]):c.moveTo(p[0],p[1]));c.closePath();if(fill){c.fillStyle=fill;c.fill()}if(stroke!==false){c.strokeStyle=OUT;c.lineWidth=1.6;c.stroke()}};
 const rr=(x,y,w,h,r,fill)=>{c.beginPath();c.roundRect?c.roundRect(x,y,w,h,r):c.rect(x,y,w,h);c.fillStyle=fill;c.fill();c.strokeStyle=OUT;c.lineWidth=1.6;c.stroke()};
 if(id=='pistol'){poly([[-9,0],[-3,0],[-1,13],[-9,13]],wood);rr(-7,-7,30,9,2,steel);rr(20,-5,7,5,1,dark);c.strokeStyle=OUT;c.lineWidth=1.6;c.beginPath();c.arc(3,3,5,.2,2.9);c.stroke();rr(-4,-5,18,2,1,col);c.fillStyle='rgba(255,255,255,.55)';c.fillRect(-4,-6,22,1.2)}
 else if(id=='smg'){poly([[-17,-1],[-8,-3],[-8,7],[-17,9]],dark);rr(-9,-8,40,10,2,steel);poly([[4,2],[11,2],[10,19],[2,19]],dark);poly([[-6,2],[-1,2],[-2,13],[-8,13]],wood);rr(30,-6,10,5,1,dark);rr(8,-12,9,4,1.5,'#222a33');c.fillStyle='#ff3b3b';c.fillRect(12,-11,2,2);rr(-5,-6,22,2,1,col);c.fillStyle='rgba(255,255,255,.5)';c.fillRect(-8,-7,36,1.2)}
 else if(id=='shotgun'){poly([[-24,-2],[-6,-5],[-6,8],[-24,13]],wood);rr(-8,-8,16,13,2,dark);rr(6,-8,36,5,2,steel);rr(6,-3,36,5,2,steel);rr(14,3,14,5,2.5,wood);rr(38,-9,4,15,1,'#b88a2a');rr(-4,-6,8,2,1,col);c.fillStyle='rgba(255,255,255,.5)';c.fillRect(8,-7.5,32,1)}
 else if(id=='sword'){const off=th||0;c.translate(off,0);poly([[-14,-2.5],[-4,-2.5],[-4,2.5],[-14,2.5]],arGrad(c,-3,3,'#7a4a22','#3d2410'));c.fillStyle=col;c.shadowColor=col;c.shadowBlur=8;c.beginPath();c.arc(-16,0,3.6,0,6.3);c.fill();c.shadowBlur=0;c.strokeStyle=OUT;c.lineWidth=1.4;c.stroke();
  poly([[-4,-11],[0,-8],[0,8],[-4,11],[-6,0]],arGrad(c,-11,11,'#ffe27a','#b8861c'));poly([[0,-4.5],[48,-4.5],[60,0],[48,4.5],[0,4.5]],arGrad(c,-5,5,'#f4f8ff','#8da0b6'));c.strokeStyle='rgba(60,80,110,.6)';c.lineWidth=1.2;c.beginPath();c.moveTo(4,0);c.lineTo(52,0);c.stroke();c.strokeStyle=col;c.globalAlpha=.7;c.beginPath();c.moveTo(8,-2.4);c.lineTo(46,-2.4);c.stroke();c.globalAlpha=1}
 else if(id=='wand'){poly([[-20,-2.2],[30,-1.6],[30,1.6],[-20,2.2]],wood);c.strokeStyle='rgba(255,220,160,.5)';c.lineWidth=1;for(let x=-16;x<26;x+=8){c.beginPath();c.moveTo(x,-2);c.lineTo(x+4,2);c.stroke()}
  c.fillStyle='#d9a52a';c.beginPath();c.moveTo(28,-4);c.lineTo(34,-7);c.lineTo(34,7);c.lineTo(28,4);c.fill();c.strokeStyle=OUT;c.lineWidth=1.3;c.stroke();
  c.save();c.shadowColor='#c58bff';c.shadowBlur=16;const og=c.createRadialGradient(40,-1,1,40,0,10);og.addColorStop(0,'#fff');og.addColorStop(.4,'#d8a8ff');og.addColorStop(1,'#7a3bd8');c.fillStyle=og;c.beginPath();c.arc(40,0,9,0,6.3);c.fill();c.restore();c.strokeStyle=col;c.lineWidth=1.6;c.beginPath();c.arc(40,0,9,0,6.3);c.stroke();c.fillStyle='#fff';c.fillRect(46,-9,2,2);c.fillRect(34,6,2,2)}
 else if(id=='rocket'){poly([[-12,-7],[-4,-7],[-4,7],[-12,7]],dark);poly([[-4,-9],[36,-9],[36,9],[-4,9]],arGrad(c,-9,9,'#7d8f55','#3e4a26'));c.fillStyle='rgba(255,255,255,.18)';c.fillRect(-2,-8,36,2.4);c.fillStyle='#f2f2f2';c.fillRect(12,-9,4,18);c.fillStyle=col;c.fillRect(20,-9,3,18);
  poly([[36,-8],[44,-6],[50,0],[44,6],[36,8]],arGrad(c,-8,8,'#ff6b4a','#a8281a'));poly([[2,9],[10,9],[8,19],[0,19]],wood);rr(10,-14,8,5,1.5,dark);c.fillStyle='#4dd2ff';c.fillRect(12,-13,4,2)}}
const arIcons={};
function arIcon(id,tier){const k=id+tier;if(arIcons[k])return arIcons[k];const cv=document.createElement('canvas');cv.width=220;cv.height=110;const c=cv.getContext('2d');
 const gl=c.createRadialGradient(110,55,5,110,55,90);gl.addColorStop(0,ATC[tier]+'55');gl.addColorStop(1,'rgba(0,0,0,0)');c.fillStyle=gl;c.fillRect(0,0,220,110);
 c.translate(110,55);c.rotate(-.35);c.scale(1.75,1.75);c.translate(-14,0);if(tier>0){c.shadowColor=ATC[tier];c.shadowBlur=10}arWeaponArt(c,id,tier,0);return arIcons[k]=cv.toDataURL()}
function arWeapon(c,w,P){c.save();c.translate(P.x,P.y);c.rotate(w.ang);if(Math.cos(w.ang)<0)c.scale(1,-1);if(w.tier>0){c.shadowColor=ATC[w.tier];c.shadowBlur=9}
 arWeaponArt(c,w.id,w.tier,w.id=='sword'&&w.th>0?Math.sin((1-w.th/.2)*Math.PI)*34:0);c.restore()}
/* --- Ansicht --- */
function arView(){const c=S.coins,b=c.arc_best||{};
 return `<h2>Survivor</h2><div class="sub">Überlebe ${AWAVES} Wellen. Belohnung nur in Spiel-Coins (kein Echtgeld, kein Geldwert). Heute verdient: <b>${c.arc_today||0} / ${c.arc_cap||150}</b> 🪙 · Beste Runde: Welle ${b.wave||0}, ${b.kills||0} Kills</div>
 <div class="arwrap"><canvas id="arc" width="1440" height="900"></canvas><div id="arov" class="arov"></div></div>
 <div class="dim" style="margin-top:8px">WASD oder Pfeiltasten bewegen · P = Pause · Die Waffen kreisen um dich und zielen automatisch. Kaufe so viele Waffen, wie du willst (bis zu 6). Zwei gleiche der gleichen Stufe verschmelzen.</div>`}
function arStop(){if(arRaf){cancelAnimationFrame(arRaf);arRaf=0}}
function arLoadImgs(){arImgs=[];const gs=(S.games||[]).slice().sort(()=>Math.random()-.5).slice(0,14);
 gs.forEach(g=>{const u=coverOf(g);if(!u)return;const i=new Image();i.src=u;arImgs.push({img:i,name:g.title||'?'})})}
function arNew(){return {w:1,t:0,dur:20,phase:'start',sp:0,kills:0,mat:0,rr:0,paused:false,orb:-Math.PI/2,
 p:{x:AWW/2,y:AWH*.62,hp:30,mhp:30,walk:0,mv:false,inv:0},s:{hp:0,dmg:0,spd:0,as:0,arm:0,reg:0,pick:0},
 weapons:[],en:[],pr:[],gems:[],pt:[],sm:[],cr:[],bl:[],shake:0,offers:[],time:0}}
function arInit(){arStop();arK={};arSetChar();arCtx=$('#arc').getContext('2d');arLoadImgs();AR=arNew();arShowStart();arLast=performance.now();arRaf=requestAnimationFrame(arLoop)}
function arShowStart(){AR=arNew();auStop();const o=$('#arov');
 o.innerHTML=`<div class="arbox"><h3 class="shh">Wähle deine Startwaffe</h3><div class="shcards">${AW.map(w=>`<div class="shc" style="--tc:${ATC[0]}" onclick="arStart('${w.id}')"><div class="shart"><img src="${arIcon(w.id,0)}" alt=""></div><b>${w.n}</b><div class="dim">${w.desc}</div></div>`).join('')}</div></div>`;o.style.display='flex'}
function arStart(id){const o=$('#arov');o.style.display='none';AR=arNew();AR.weapons=[{id,tier:0,cd:.3,ang:-Math.PI/2,th:0}];AR.phase='play';AR.boss=false;auCtx();auPlay('surv');sfx('wave');
 fetch('/api/coin',{method:'POST',body:JSON.stringify({op:'arcstart'})}).catch(()=>{})}
function arDef(id){return AW.find(w=>w.id==id)}
function arPos(i,n){const p=AR.p,a=AR.orb+i*2*Math.PI/n,R=76;return {x:p.x+Math.cos(a)*R,y:p.y-52+Math.sin(a)*R,a}}
function arHpBase(w){return 8+w*3.2+w*w*.12}
function arSpawn(type){const g=AR,T=ATYPES[type],side=Math.floor(Math.random()*4);let x,y;
 if(side==0){x=AX0+20+Math.random()*(AX1-AX0-40);y=AY0+10}else if(side==1){x=AX0+20+Math.random()*(AX1-AX0-40);y=AY1-6}else if(side==2){x=AX0+14;y=AY0+30+Math.random()*(AY1-AY0-60)}else{x=AX1-14;y=AY0+30+Math.random()*(AY1-AY0-60)}
 const hp=arHpBase(g.w)*T.hp;g.en.push({x,y,t:type,hp,mhp:hp,sp:T.sp*(1+g.w*.012),dmg:T.dmg,w:T.w,h:T.h,col:T.col,tint:T.tint,drop:T.drop,ph:Math.random()*6.28,fl:0,kx:0,ky:0,
  im:arImgs.length?arImgs[Math.floor(Math.random()*arImgs.length)]:null})}
function arShoot(w,d,ang,P){const g=AR,dm=d.dmg*ATM[w.tier]*(1+g.s.dmg/100),bx=P.x+Math.cos(ang)*34,by=P.y+Math.sin(ang)*34;
 const mk=(a,sp,life,x)=>g.pr.push(Object.assign({x:bx,y:by,vx:Math.cos(a)*sp,vy:Math.sin(a)*sp,dmg:dm,life,r:5,col:ATC[w.tier]},x||{}));
 if(d.id=='pistol'){mk(ang,760,d.rng/760+.1);sfx('pistol');arFlash(bx,by,9)}
 else if(d.id=='smg'){mk(ang+(Math.random()-.5)*.25,820,d.rng/820+.1,{r:4});sfx('smg');arFlash(bx,by,7)}
 else if(d.id=='shotgun'){for(let i=0;i<5;i++)mk(ang+(i-2)*.13+(Math.random()-.5)*.05,680,d.rng/680+.05,{r:4});sfx('shotgun');arFlash(bx,by,15)}
 else if(d.id=='wand'){mk(ang,360,2.4,{homing:1,r:7,col:'#c58bff'});sfx('wand')}
 else if(d.id=='rocket'){mk(ang,400,d.rng/400+.3,{aoe:95,r:8,col:'#ff9a3a',rocket:1,ang,sm:0});sfx('rocket');arFlash(bx,by,18);for(let i=0;i<7;i++)g.sm.push({x:P.x+Math.cos(ang+Math.PI)*8,y:P.y+Math.sin(ang+Math.PI)*8,vx:Math.cos(ang+Math.PI+(Math.random()-.5)*1.2)*(60+Math.random()*90),vy:Math.sin(ang+Math.PI+(Math.random()-.5)*1.2)*(60+Math.random()*90),r:6+Math.random()*5,life:.7,max:.7})}}
function arFlash(x,y,r){AR.sm.push({x,y,vx:0,vy:0,r,life:.07,max:.07,fl:1})}
function arBoom(b){const g=AR;if(b.ex)return;b.ex=1;sfx('rboom');g.shake=.35;
 for(const o of g.en)if(!o.dead&&Math.hypot(o.x-b.x,o.y-b.y)<b.aoe+o.w/3)arHit(o,b.dmg,(o.x-b.x)*3,(o.y-b.y)*3);
 g.cr.push({x:b.x,y:b.y,r:b.aoe*.5,rot:Math.random()*6.28,t:0});if(g.cr.length>14)g.cr.shift();g.bl.push({x:b.x,y:b.y,t:0,R:b.aoe});
 for(let i=0;i<22;i++){const a=Math.random()*6.28,v=80+Math.random()*320;g.pt.push({x:b.x,y:b.y,vx:Math.cos(a)*v,vy:Math.sin(a)*v,life:.35+Math.random()*.4,col:['#ffb04a','#ff7a1a','#ffe27a','#4a3a2a'][i%4]})}
 for(let i=0;i<10;i++){const a=Math.random()*6.28,v=30+Math.random()*110;g.sm.push({x:b.x,y:b.y,vx:Math.cos(a)*v,vy:Math.sin(a)*v-20,r:14+Math.random()*14,life:.9+Math.random()*.7,max:1.6,dark:1})}}
function arHit(e,dm,kx,ky){e.hp-=dm;e.fl=.09;e.kx+=kx||0;e.ky+=ky||0;sfx('hit');
 for(let i=0;i<2;i++)AR.pt.push({x:e.x,y:e.y,vx:(Math.random()-.5)*160,vy:(Math.random()-.5)*160,life:.3,col:'#fff'});
 if(e.hp<=0&&!e.dead){e.dead=1;AR.kills++;sfx('kill');AR.gems.push({x:e.x,y:e.y,v:e.drop});for(let i=0;i<6;i++)AR.pt.push({x:e.x,y:e.y,vx:(Math.random()-.5)*260,vy:(Math.random()-.5)*260,life:.45,col:e.col})}}
function arUpdate(dt){const g=AR,p=g.p,s=g.s;g.time+=dt;g.orb+=dt*1.15;
 let dx=(arK.KeyD||arK.ArrowRight?1:0)-(arK.KeyA||arK.ArrowLeft?1:0),dy=(arK.KeyS||arK.ArrowDown?1:0)-(arK.KeyW||arK.ArrowUp?1:0);const l=Math.hypot(dx,dy);p.mv=l>0;
 if(l){dx/=l;dy/=l;const sp=225*(1+s.spd/100);p.x+=dx*sp*dt;p.y+=dy*sp*dt;p.walk+=dt*sp/19}
 p.x=arClamp(p.x,AX0+40,AX1-40);p.y=arClamp(p.y,AY0+118,AY1-6);p.inv=Math.max(0,p.inv-dt);
 p.mhp=30+s.hp;p.hp=Math.min(p.mhp,p.hp+s.reg*.5*dt);
 g.t+=dt;
 if(!g.boss&&g.w%5==0){g.boss=true;arSpawn('boss')}
 g.sp-=dt;if(g.sp<=0){g.sp=Math.max(.2,1-g.w*.04);const n=1+Math.floor(g.w/5);
  for(let i=0;i<n&&g.en.length<140;i++){const r=Math.random();arSpawn(g.w>=6&&r<.06?'elite':g.w>=4&&r<.2?'tank':g.w>=2&&r<.45?'runner':'basic')}}
 const cx=p.x,cy=p.y-45;
 for(const e of g.en){const ex=cx-e.x,ey=cy-e.y,d=Math.hypot(ex,ey)||1;e.x+=(ex/d*e.sp+e.kx)*dt;e.y+=(ey/d*e.sp+e.ky)*dt;e.kx*=.9;e.ky*=.9;e.fl=Math.max(0,e.fl-dt);
  if(d<e.w*.45+24&&p.inv<=0){p.hp-=Math.max(1,Math.round(e.dmg*(1+g.w*.1)/(1+s.arm*.1)));p.inv=.5;sfx('hurt')}}
 if(p.hp<=0){arEnd(false);return}
 const n=g.weapons.length;
 g.weapons.forEach((w,i)=>{const d=arDef(w.id),P=arPos(i,n);let tg=null,bd=1e9;
  for(const e of g.en){const q=Math.hypot(e.x-P.x,e.y-P.y);if(q<bd){bd=q;tg=e}}
  let want=P.a;const inr=tg&&bd<=d.rng+(tg.w/2);if(inr)want=Math.atan2(tg.y-P.y,tg.x-P.x);
  let df=want-w.ang;df=Math.atan2(Math.sin(df),Math.cos(df));w.ang+=df*Math.min(1,dt*14);
  w.cd-=dt*(1+s.as/100);w.th=Math.max(0,w.th-dt);
  if(inr&&w.cd<=0){w.cd=d.cd;
   if(d.id=='sword'){w.th=.2;sfx('sword');const dm=d.dmg*ATM[w.tier]*(1+s.dmg/100),ax=Math.cos(w.ang),ay=Math.sin(w.ang);
    for(const e of g.en){const vx=e.x-P.x,vy=e.y-P.y,q=Math.hypot(vx,vy);if(q<d.rng+e.w/2&&(vx*ax+vy*ay)/(q||1)>.35)arHit(e,dm,ax*220,ay*220)}}
   else arShoot(w,d,w.ang,P)}});
 for(const b of g.pr){if(b.homing){let t=null,bd=1e9;for(const e of g.en){const q=Math.hypot(e.x-b.x,e.y-b.y);if(q<bd){bd=q;t=e}}
   if(t){const sp=Math.hypot(b.vx,b.vy),a=Math.atan2(b.vy,b.vx);let df=Math.atan2(t.y-b.y,t.x-b.x)-a;df=Math.atan2(Math.sin(df),Math.cos(df));const na=a+df*Math.min(1,dt*5);b.vx=Math.cos(na)*sp;b.vy=Math.sin(na)*sp}}
  b.x+=b.vx*dt;b.y+=b.vy*dt;b.life-=dt;
  if(b.rocket){b.sm-=dt;if(b.sm<=0){b.sm=.018;g.sm.push({x:b.x-Math.cos(b.ang)*14,y:b.y-Math.sin(b.ang)*14,vx:(Math.random()-.5)*24,vy:(Math.random()-.5)*24,r:5+Math.random()*3,life:.55,max:.55})}}
  for(const e of g.en){if(e.dead)continue;if(Math.abs(e.x-b.x)<e.w/2+b.r&&Math.abs(e.y-b.y)<e.h/2+b.r){
    if(b.aoe){arBoom(b)}
    else arHit(e,b.dmg,b.vx*.2,b.vy*.2);b.life=0;break}}}
 for(const b of g.pr)if(b.aoe&&!b.ex&&(b.life<=0||b.x<=AX0||b.x>=AX1||b.y<=AY0||b.y>=AY1))arBoom(b);
 g.pr=g.pr.filter(b=>b.life>0&&b.x>AX0&&b.x<AX1&&b.y>AY0&&b.y<AY1);g.en=g.en.filter(e=>!e.dead);
 for(const q of g.sm){q.x+=q.vx*dt;q.y+=q.vy*dt;q.vx*=.96;q.vy*=.96;q.life-=dt}g.sm=g.sm.filter(q=>q.life>0);for(const q of g.bl)q.t+=dt;g.bl=g.bl.filter(q=>q.t<.6);for(const q of g.cr)q.t+=dt;g.cr=g.cr.filter(q=>q.t<9);g.shake=Math.max(0,g.shake-dt);
 const pr=80+s.pick;
 for(const m of g.gems){const d=Math.hypot(cx-m.x,cy-m.y);if(d<pr){m.x+=(cx-m.x)/d*340*dt;m.y+=(cy-m.y)/d*340*dt}if(d<20){g.mat+=m.v;m.got=1;sfx('coin')}}
 g.gems=g.gems.filter(m=>!m.got);
 for(const q of g.pt){q.x+=q.vx*dt;q.y+=q.vy*dt;q.life-=dt}g.pt=g.pt.filter(q=>q.life>0);
 if(g.t>=g.dur)arWaveEnd()}
function arWaveEnd(){const g=AR;g.gems.forEach(m=>g.mat+=m.v);g.gems=[];g.en=[];g.pr=[];g.pt=[];g.sm=[];g.bl=[];
 if(g.w>=AWAVES){arEnd(true);return}
 sfx('wave');g.phase='shop';g.rr=0;arOffers();arShowShop()}
function arGameName(){const gs=S.games||[];if(!gs.length)return 'D-Hub';let t=(gs[Math.floor(Math.random()*gs.length)].title||'D-Hub');return t.length>20?t.slice(0,19)+'…':t}
function arOffers(){const g=AR,o=[],kinds=['w','w','i',Math.random()<.5?'w':'i'].sort(()=>Math.random()-.5);
 for(const k of kinds){if(k=='w'){const d=AW[Math.floor(Math.random()*AW.length)];let t=0;const r=Math.random();if(g.w>=13)t=r<.4?2:1;else if(g.w>=7)t=r<.4?1:0;
   o.push({k:'w',id:d.id,tier:t,price:Math.round(d.price*[1,2.1,3.8,6][t]*(1+g.w*.04))})}
  else{const it=AIT[Math.floor(Math.random()*AIT.length)];o.push({k:'i',id:it.id,gn:arGameName(),price:Math.round(it.price*(1+g.w*.06))})}}
 g.offers=o}
function arMerge(){const g=AR;let ch=true;while(ch){ch=false;
 for(let i=0;i<g.weapons.length&&!ch;i++)for(let j=i+1;j<g.weapons.length;j++){const a=g.weapons[i],b=g.weapons[j];
  if(a.id==b.id&&a.tier==b.tier&&a.tier<3){a.tier++;g.weapons.splice(j,1);ch=true;break}}}}
function arCanMerge(id,t){return AR.weapons.some(w=>w.id==id&&w.tier==t&&t<3)}
function arBuy(i){const g=AR,o=g.offers[i];if(!o||o.sold||g.mat<o.price)return;
 if(o.k=='w'){if(g.weapons.length>=6&&!arCanMerge(o.id,o.tier)){toast('Alle 6 Waffenplätze belegt');return}
  g.weapons.push({id:o.id,tier:o.tier,cd:.4,ang:AR.orb,th:0});arMerge()}
 else{const it=AIT.find(x=>x.id==o.id);g.s[it.s]+=it.v;if(it.s=='hp')g.p.hp+=it.v}
 g.mat-=o.price;o.sold=true;sfx('buy');arShowShop()}
function arReroll(){const g=AR,c=Math.round(2+g.rr*2+g.w*.5);if(g.mat<c)return;g.mat-=c;g.rr++;arOffers();arShowShop()}
function arNext(){const g=AR;g.w++;g.t=0;g.dur=20+Math.min(10,Math.floor(g.w/2));g.boss=false;g.sp=0;g.phase='play';g.p.inv=1.2;g.p.hp=Math.min(30+g.s.hp,g.p.hp+(30+g.s.hp)*.3);$('#arov').style.display='none'}
const arBar=(l,v)=>`<div class="shb"><span>${l}</span><i><u style="width:${Math.round(Math.min(1,v)*100)}%"></u></i></div>`;
function arShowShop(){const g=AR,o=$('#arov'),c=Math.round(2+g.rr*2+g.w*.5),s=g.s;
 const offer=(x,i)=>{if(x.sold)return `<div class="shc sold"><b>Verkauft</b></div>`;const aff=g.mat>=x.price;
  if(x.k=='w'){const d=arDef(x.id),tm=ATM[x.tier];return `<div class="shc ${aff?'':'dis'}" style="--tc:${ATC[x.tier]}" onclick="arBuy(${i})"><i class="sht">${ATN[x.tier]}</i><div class="shart"><img src="${arIcon(x.id,x.tier)}" alt=""></div><b>${d.n}</b>
    ${arBar('Schaden',d.dmg*d.pel*tm/cd2(d)/70)}${arBar('Tempo',1/d.cd/7)}${arBar('Reichweite',d.rng/520)}<div class="shd">${d.desc}</div><div class="shp">✦ ${x.price}</div></div>`}
  const it=AIT.find(y=>y.id==x.id);return `<div class="shc ${aff?'':'dis'}" style="--tc:${it.col}" onclick="arBuy(${i})"><div class="shart"><span class="shi" style="background:${it.col}22;border-color:${it.col}"><svg viewBox="0 0 24 24" style="stroke:${it.col}">${it.ic}</svg></span></div><b>${esc(x.gn)}-${it.n}</b><div class="shd" style="color:${it.col}">${it.d}</div><div class="shd">Gegenstand</div><div class="shp">✦ ${x.price}</div></div>`};
 o.innerHTML=`<div class="arbox"><h3 class="shh">Welle ${g.w} geschafft</h3><div class="shtop"><span class="shchip" style="color:#ffd23f">✦ ${g.mat}</span><span class="shchip">☠ ${g.kills}</span><span class="shchip">♥ ${Math.round(g.p.hp)}/${30+s.hp}</span></div>
 <div class="shcards">${g.offers.map(offer).join('')}</div>
 <div class="shown"><div class="shl">Deine Waffen (${g.weapons.length}/6)</div><div class="shrow">${g.weapons.map(w=>`<div class="shmini" style="--tc:${ATC[w.tier]}"><img src="${arIcon(w.id,w.tier)}" alt=""><i>${ATN[w.tier]}</i></div>`).join('')||'<span class="dim">keine</span>'}</div>
 <div class="shrow" style="margin-top:6px"><span class="shchip">Schaden +${s.dmg}%</span><span class="shchip">Tempo +${s.spd}%</span><span class="shchip">Angriffstempo +${s.as}%</span><span class="shchip">Rüstung ${s.arm}</span><span class="shchip">Regen ${s.reg*.5}/s</span></div></div>
 <div style="margin-top:12px"><button class="btn" ${g.mat<c?'disabled':''} onclick="arReroll()">Neu würfeln ✦ ${c}</button> <button class="btn pri" onclick="arNext()">Welle ${g.w+1} starten</button></div></div>`;o.style.display='flex'}
const cd2=d=>d.cd;
async function arEnd(win){const g=AR;g.phase='over';auStop();sfx(win?'win':'over');const done=win?AWAVES:g.w-1,o=$('#arov');
 o.innerHTML=`<div class="arbox"><h3 class="shh">${win?'🏆 Geschafft! Alle '+AWAVES+' Wellen überlebt':'💀 Ausgeschieden in Welle '+g.w}</h3><div class="dim">Kills: ${g.kills} · Berechne Belohnung …</div></div>`;o.style.display='flex';
 let msg='';try{const r=await coinOp('arcade',null,{completed:done,kills:g.kills,win:win?1:0});msg=r.msg||''}catch(e){msg='Belohnung konnte nicht abgeholt werden.'}
 if(tab!='Arcade')return;
 o.innerHTML=`<div class="arbox"><h3 class="shh">${win?'🏆 Geschafft! Alle '+AWAVES+' Wellen überlebt':'💀 Ausgeschieden in Welle '+g.w}</h3><div class="dim">Geschaffte Wellen: ${done} · Kills: ${g.kills}</div><p><b>${esc(msg)}</b></p><button class="btn pri" onclick="arShowStart()">Nochmal spielen</button></div>`}
/* --- Zeichnen --- */
function arRR(c,x,y,w,h,r){c.beginPath();if(c.roundRect)c.roundRect(x,y,w,h,r);else c.rect(x,y,w,h)}
function arBG(){if(arBGc)return arBGc;const cv=document.createElement('canvas');cv.width=AWW;cv.height=AWH;const c=cv.getContext('2d'),r=rng(7),W=AWW,H=AWH,k=W*H/(960*600);
 c.fillStyle='#0d110c';c.fillRect(0,0,W,H);
 const g=c.createLinearGradient(0,AY0,0,AY1);g.addColorStop(0,'#4f9436');g.addColorStop(1,'#3b7a2a');c.fillStyle=g;c.fillRect(AX0,AY0,AX1-AX0,AY1-AY0);
 for(let x=AX0,i=0;x<AX1;x+=96,i++)if(i%2){c.fillStyle='rgba(255,255,255,.035)';c.fillRect(x,AY0,96,AY1-AY0)}
 for(let i=0;i<60;i++){const x=AX0+r()*(AX1-AX0),y=AY0+r()*(AY1-AY0),rad=60+r()*130,l=r()<.5,gr=c.createRadialGradient(x,y,0,x,y,rad);gr.addColorStop(0,l?'rgba(170,230,110,.16)':'rgba(10,60,10,.2)');gr.addColorStop(1,'rgba(0,0,0,0)');c.fillStyle=gr;c.fillRect(AX0,AY0,AX1-AX0,AY1-AY0)}
 c.save();c.beginPath();c.rect(AX0,AY0,AX1-AX0,AY1-AY0);c.clip();c.lineWidth=1.6;
 for(let i=0;i<3200*k;i++){const x=AX0+r()*(AX1-AX0),y=AY0+r()*(AY1-AY0),h=4+r()*8;c.strokeStyle=r()<.5?'rgba(15,65,15,.5)':'rgba(170,235,110,.38)';c.beginPath();c.moveTo(x,y);c.lineTo(x+(r()-.5)*5,y-h);c.stroke()}
 const fl=['#fff3a0','#ffffff','#ff9ec0','#c9a0ff'];for(let i=0;i<80*k;i++){const x=AX0+10+r()*(AX1-AX0-20),y=AY0+10+r()*(AY1-AY0-20);c.fillStyle=fl[i%4];c.beginPath();c.arc(x,y,2.4,0,6.3);c.fill();c.fillStyle='rgba(255,200,0,.8)';c.fillRect(x-.6,y-.6,1.2,1.2)}
 for(let i=0;i<10*k;i++){const x=AX0+30+r()*(AX1-AX0-60),y=AY0+30+r()*(AY1-AY0-60),w=14+r()*22;c.fillStyle='rgba(0,0,0,.25)';c.beginPath();c.ellipse(x+4,y+7,w,w*.5,0,0,6.3);c.fill();c.fillStyle='#8b9096';c.beginPath();c.ellipse(x,y,w,w*.62,0,0,6.3);c.fill();c.fillStyle='#b0b6bc';c.beginPath();c.ellipse(x-w*.2,y-w*.2,w*.55,w*.3,0,0,6.3);c.fill()}
 for(let i=0;i<14*k;i++){const x=AX0+30+r()*(AX1-AX0-60),y=AY0+30+r()*(AY1-AY0-60);c.fillStyle='#2f6a22';for(let q=-1;q<=1;q++){c.beginPath();c.moveTo(x+q*5,y);c.lineTo(x+q*9,y-15);c.lineTo(x+q*5+4,y);c.fill()}}
 c.restore();
 const v=c.createRadialGradient(W/2,H/2,H*.35,W/2,H/2,W*.62);v.addColorStop(0,'rgba(0,0,0,0)');v.addColorStop(1,'rgba(0,0,0,.45)');c.fillStyle=v;c.fillRect(AX0,AY0,AX1-AX0,AY1-AY0);
 c.lineWidth=16;c.strokeStyle='#2a2017';c.strokeRect(AX0-8,AY0-8,AX1-AX0+16,AY1-AY0+16);c.lineWidth=3;c.strokeStyle='#6b5338';c.strokeRect(AX0,AY0,AX1-AX0,AY1-AY0);
 return arBGc=cv}
function arPlayer(c,g,t){const p=g.p,ph=p.walk,mv=p.mv,bs=mv?Math.abs(Math.sin(ph))*3:Math.sin(t*2.2)*1.2,bw=ARI.body.naturalWidth*ASC,bh=ARI.body.naturalHeight*ASC;const lh=ARI.ll.naturalHeight*ASC;
 if(!bw)return;
 c.save();c.fillStyle='rgba(0,0,0,.35)';c.beginPath();c.ellipse(p.x,p.y+2,38,9,0,0,6.3);c.fill();c.restore();
 c.save();if(p.inv>0&&Math.floor(t*20)%2)c.globalAlpha=.45;
 const top=p.y-lh+2-bh-bs;
 const leg=(img,ox,k)=>{const lw=img.naturalWidth*ASC,lh=img.naturalHeight*ASC,s=mv?Math.sin(ph+k*Math.PI):0,lift=mv?Math.max(0,s)*7:0,sw=mv?s*.5:0;c.save();c.translate(p.x+ox+lw/2,p.y-lh+2-lift);c.rotate(sw);c.drawImage(img,-lw/2,0,lw,lh);c.restore()};
 leg(ARI.ll,ARLX[0]*ASC-bw/2,0);leg(ARI.rl,ARLX[1]*ASC-bw/2,1);
 c.save();c.translate(p.x,top+bh);c.rotate(mv?Math.sin(ph)*.045:0);c.drawImage(ARI.body,-bw/2,-bh,bw,bh);c.restore();
 c.restore()}
function arEnemy(c,e,t){const w=e.w,h=e.h;c.save();c.translate(e.x,e.y);c.rotate(Math.sin(t*9+e.ph)*.12);
 c.save();arRR(c,-w/2,-h/2,w,h,6);c.clip();const im=e.im;
 if(im&&im.img.complete&&im.img.naturalWidth)c.drawImage(im.img,-w/2,-h/2,w,h);else{c.fillStyle='hsl('+((e.ph*57)|0)+',45%,32%)';c.fillRect(-w/2,-h/2,w,h);c.fillStyle='#fff';c.font='bold '+(w*.5|0)+'px sans-serif';c.textAlign='center';c.textBaseline='middle';c.fillText(im?(im.name[0]||'?').toUpperCase():'?',0,0)}
 c.fillStyle=e.tint;c.fillRect(-w/2,-h/2,w,h);if(e.fl>0){c.fillStyle='rgba(255,255,255,.75)';c.fillRect(-w/2,-h/2,w,h)}c.restore();
 c.lineWidth=e.t=='boss'?4:2.4;c.strokeStyle=e.col;if(e.t=='elite'||e.t=='boss'){c.shadowColor=e.col;c.shadowBlur=12}arRR(c,-w/2,-h/2,w,h,6);c.stroke();c.shadowBlur=0;
 const ey=-h*.12,ex=w*.2,er=Math.max(3,w*.09);c.fillStyle='#fff';c.beginPath();c.arc(-ex,ey,er,0,6.3);c.arc(ex,ey,er,0,6.3);c.fill();
 c.fillStyle='#111';c.beginPath();c.arc(-ex,ey+1,er*.5,0,6.3);c.arc(ex,ey+1,er*.5,0,6.3);c.fill();
 c.strokeStyle='#111';c.lineWidth=Math.max(2,w*.05);c.beginPath();c.moveTo(-ex-er,ey-er-1);c.lineTo(-ex+er,ey-er+3);c.moveTo(ex+er,ey-er-1);c.lineTo(ex-er,ey-er+3);c.stroke();
 if(e.hp<e.mhp){c.fillStyle='rgba(0,0,0,.6)';c.fillRect(-w/2,-h/2-8,w,4);c.fillStyle='#e0463a';c.fillRect(-w/2,-h/2-8,w*Math.max(0,e.hp/e.mhp),4)}
 c.restore()}
function arDraw(t){const c=arCtx,g=AR;if(!c||!g)return;
 c.fillStyle='#0d110c';c.fillRect(0,0,960,600);
 c.setTransform(CVK,0,0,CVK,0,0);c.fillStyle='#0d110c';c.fillRect(0,0,960,600);
 c.save();c.scale(AZ,AZ);if(g.shake>0)c.translate((Math.random()-.5)*g.shake*28,(Math.random()-.5)*g.shake*28);c.drawImage(arBG(),0,0);
 for(const k of g.cr){const al=Math.min(1,(9-k.t)/2.5),R=k.r*Math.min(1,.5+k.t*4);c.save();c.globalAlpha=Math.max(0,al);c.translate(k.x,k.y);c.rotate(k.rot);c.scale(1,.62);
  let gr=c.createRadialGradient(0,0,R*.2,0,0,R*1.55);gr.addColorStop(0,'rgba(15,10,5,.75)');gr.addColorStop(.55,'rgba(25,16,8,.55)');gr.addColorStop(1,'rgba(0,0,0,0)');c.fillStyle=gr;c.beginPath();c.arc(0,0,R*1.55,0,6.3);c.fill();
  c.fillStyle='#6b4a2a';c.beginPath();c.arc(0,0,R*1.02,0,6.3);c.fill();gr=c.createRadialGradient(0,-R*.15,R*.1,0,0,R);gr.addColorStop(0,'#120a05');gr.addColorStop(1,'#3a2512');c.fillStyle=gr;c.beginPath();c.arc(0,0,R*.86,0,6.3);c.fill();
  c.strokeStyle='rgba(255,210,150,.25)';c.lineWidth=2;c.beginPath();c.arc(0,0,R*1.0,Math.PI*1.1,Math.PI*1.9);c.stroke();
  if(k.t<2){c.fillStyle='rgba(255,120,30,'+(.5*(1-k.t/2))+')';c.beginPath();c.arc(0,0,R*.55,0,6.3);c.fill()}c.restore()}
 for(const m of g.gems){c.save();c.translate(m.x,m.y);c.rotate(.785);c.fillStyle='#ffd23f';c.strokeStyle='#5a3d00';c.lineWidth=2;c.fillRect(-6,-6,12,12);c.strokeRect(-6,-6,12,12);c.restore()}
 for(const e of g.en)arEnemy(c,e,t);
 for(const q of g.sm)if(!q.fl){const k=q.life/q.max;c.globalAlpha=Math.max(0,k*(q.dark?.55:.4));c.fillStyle=q.dark?'#3a3633':'#cfd3d8';c.beginPath();c.arc(q.x,q.y,q.r*(1.6-k*.6),0,6.3);c.fill()}c.globalAlpha=1;
 arPlayer(c,g,t);
 const n=g.weapons.length;g.weapons.forEach((w,i)=>arWeapon(c,w,arPos(i,n)));
 for(const b of g.pr){if(b.rocket){c.save();c.translate(b.x,b.y);c.rotate(b.ang);c.scale(1.45,1.45);const fl=10+Math.random()*7;let gr=c.createLinearGradient(-12,0,-12-fl-8,0);gr.addColorStop(0,'rgba(255,240,170,.95)');gr.addColorStop(.5,'rgba(255,140,30,.8)');gr.addColorStop(1,'rgba(255,60,0,0)');c.fillStyle=gr;c.beginPath();c.moveTo(-10,-4);c.lineTo(-12-fl-8,0);c.lineTo(-10,4);c.fill();
   c.fillStyle='#8f969c';c.fillRect(-11,-5,22,10);c.fillStyle='#d8dde2';c.fillRect(-11,-5,22,3.2);c.fillStyle='#d83a2a';c.beginPath();c.moveTo(11,-5);c.lineTo(20,0);c.lineTo(11,5);c.fill();c.fillStyle='#c9a227';c.fillRect(-1,-5,3,10);
   c.fillStyle='#3d4348';c.beginPath();c.moveTo(-11,-5);c.lineTo(-16,-10);c.lineTo(-6,-5);c.fill();c.beginPath();c.moveTo(-11,5);c.lineTo(-16,10);c.lineTo(-6,5);c.fill();c.restore()}
  else{c.fillStyle=b.col;c.shadowColor=b.col;c.shadowBlur=10;c.beginPath();c.arc(b.x,b.y,b.r,0,6.3);c.fill();c.shadowBlur=0}}
 for(const q of g.sm)if(q.fl){const k=q.life/q.max;const gr=c.createRadialGradient(q.x,q.y,0,q.x,q.y,q.r*1.6);gr.addColorStop(0,'rgba(255,250,200,'+k+')');gr.addColorStop(.5,'rgba(255,170,50,'+k*.7+')');gr.addColorStop(1,'rgba(255,100,0,0)');c.fillStyle=gr;c.beginPath();c.arc(q.x,q.y,q.r*1.6,0,6.3);c.fill()}
 for(const q of g.bl){const k=q.t/.6,R=q.R*(.25+k*.95);c.save();c.globalAlpha=Math.max(0,1-k);const gr=c.createRadialGradient(q.x,q.y,0,q.x,q.y,R);gr.addColorStop(0,'rgba(255,255,230,'+(k<.15?1:.8)+')');gr.addColorStop(.35,'rgba(255,190,60,.9)');gr.addColorStop(.75,'rgba(230,70,10,.55)');gr.addColorStop(1,'rgba(120,20,0,0)');c.fillStyle=gr;c.beginPath();c.arc(q.x,q.y,R,0,6.3);c.fill();
  c.strokeStyle='rgba(255,230,180,'+(1-k)*.8+')';c.lineWidth=5*(1-k)+1;c.beginPath();c.arc(q.x,q.y,q.R*(.3+k*1.25),0,6.3);c.stroke();c.restore()}
 for(const q of g.pt){c.globalAlpha=Math.max(0,q.life*2.2);c.fillStyle=q.col;c.fillRect(q.x-2.5,q.y-2.5,5,5)}c.globalAlpha=1;
 c.restore();
 if(g.phase=='play'||g.phase=='shop'){c.fillStyle='rgba(0,0,0,.55)';c.fillRect(0,0,960,38);
  const mh=30+g.s.hp,f=arClamp(g.p.hp/mh,0,1);c.fillStyle='#331';c.fillRect(12,10,220,18);c.fillStyle=f>.3?'#4cc864':'#e0463a';c.fillRect(12,10,220*f,18);
  c.fillStyle='#fff';c.font='bold 13px sans-serif';c.textBaseline='middle';c.textAlign='left';c.fillText('HP '+Math.ceil(g.p.hp)+' / '+mh,20,19);
  c.textAlign='center';c.font='bold 16px sans-serif';c.fillText('Welle '+g.w+' / '+AWAVES+(g.phase=='play'?'  ·  '+Math.max(0,Math.ceil(g.dur-g.t))+' s':''),480,19);
  c.textAlign='right';c.fillStyle='#ffd23f';c.fillText('✦ '+g.mat,830,19);c.fillStyle='#fff';c.fillText('☠ '+g.kills,940,19)}
 if(g.paused){c.fillStyle='rgba(0,0,0,.6)';c.fillRect(0,0,960,600);c.fillStyle='#fff';c.font='bold 38px sans-serif';c.textAlign='center';c.fillText('Pause (P)',480,300)}}
function arLoop(now){arRaf=0;if(tab!='Arcade'||!$('#arc'))return;const dt=Math.min(.05,(now-arLast)/1000||0);arLast=now;
 if(AR&&AR.phase=='play'&&!AR.paused)arUpdate(dt);
 arDraw(now/1000);arRaf=requestAnimationFrame(arLoop)}
function arTogglePause(){if(AR&&AR.phase=='play'){AR.paused=!AR.paused;auPause(AR.paused)}}
addEventListener('keydown',e=>{if(tab!='Arcade'||arcMode!='surv')return;if(/^(Arrow|Key[WASD])/.test(e.code)){arK[e.code]=1;if(e.code.startsWith('Arrow'))e.preventDefault()}
 if(e.code=='KeyP'||e.code=='Escape')arTogglePause()});
addEventListener('keyup',e=>{delete arK[e.code]});addEventListener('blur',()=>{arK={};if(AR&&AR.phase=='play'&&!AR.paused)arTogglePause()});

/* ===== INVADERS ===== */
let IV=null,ivRaf=0,ivLast=0,ivCtx=null;
const IVCAP={rapid:{c:'#ff9a3a',l:'R'},triple:{c:'#4dd2ff',l:'3'},shield:{c:'#7CFC9a',l:'S'},life:{c:'#ff5a8a',l:'+'}};
function ivView(){const b=S.coins.inv_best||{};
 return `<h2>Invaders</h2><div class="sub">Deine eigenen Spiele-Cover greifen an. Belohnung nur in Spiel-Coins (kein Echtgeld). Heute Arcade-Coins: <b>${S.coins.arc_today||0} / ${S.coins.arc_cap||150}</b> · Beste Runde: Welle ${b.wave||0}, ${b.kills||0} Kills</div>
 <div class="arwrap"><canvas id="ivc" width="1440" height="900"></canvas><div id="ivov" class="arov"></div></div>
 <div class="dim" style="margin-top:8px">A/D oder ←/→ bewegen · Leertaste schießen (halten) · P = Pause. Dicke rote Gegner lassen manchmal Power-Ups fallen (R = Schnellfeuer, 3 = Dreifachschuss, S = Schild, + = Leben). Ab Welle 2 fliegt ein Ufo vorbei: Schaffst du es, hast du die nächsten 2 Wellen Vollautomatik.</div>`}
function ivStop(){if(ivRaf){cancelAnimationFrame(ivRaf);ivRaf=0}}
function ivNew(){return {phase:'start',w:1,px:480,lives:3,inv:0,cd:0,kills:0,bul:[],eb:[],pt:[],en:[],caps:[],fx:70,fy:70,dir:1,ft:1,walk:0,total:1,paused:false,banner:0,msg:'',msgT:0,
 buff:{rapid:0,triple:0,auto:0},shield:false,ufo:null,ufoDone:true,ufoAt:1e9,wt:0}}
function ivGrid(g){const rows=Math.min(5,3+Math.floor(g.w/3)),cols=8;g.en=[];
 for(let r=0;r<rows;r++)for(let c=0;c<cols;c++){const im=arImgs.length?arImgs[(r*cols+c+g.w)%arImgs.length]:null;g.en.push({r,c,hp:r==0&&g.w>1?2:1,mhp:r==0&&g.w>1?2:1,im,ph:Math.random()*6.28,fl:0})}
 g.total=g.en.length;g.fx=70;g.fy=-270;g.entering=true;g.dir=1;g.bul=[];g.eb=[];g.caps=[];g.ufo=null;g.wt=0;g.ufoDone=g.w<2;g.ufoAt=4+Math.random()*6}
function ivInit(){ivStop();arK={};arSetChar();ivCtx=$('#ivc').getContext('2d');arLoadImgs();IV=ivNew();ivShowStart();ivLast=performance.now();ivRaf=requestAnimationFrame(ivLoop)}
function ivShowStart(){IV=ivNew();auStop();const o=$('#ivov');o.innerHTML=`<div class="arbox"><h3 class="shh">Invaders</h3><div class="dim" style="margin-bottom:12px">Halte deine Spiele-Sammlung auf Abstand!</div><button class="btn pri" onclick="ivBegin()">Start</button></div>`;o.style.display='flex'}
function ivBegin(){$('#ivov').style.display='none';IV=ivNew();IV.phase='play';ivGrid(IV);auCtx();auPlay('inv');sfx('wave');fetch('/api/coin',{method:'POST',body:JSON.stringify({op:'arcstart'})}).catch(()=>{})}
const ivPos=(g,e)=>({x:g.fx+e.c*72,y:g.fy+e.r*72});
function ivBoom(g,x,y,col,n){for(let i=0;i<n;i++)g.pt.push({x,y,vx:(Math.random()-.5)*300,vy:(Math.random()-.5)*300,life:.5,col})}
function ivDrop(g,x,y){const r=Math.random()*100;let t=r<30?'rapid':r<60?'triple':r<85?'shield':'life';if(t=='life'&&g.lives>=5)t='rapid';g.caps.push({x,y,t})}
function ivGet(g,t){sfx('pow');g.msg={rapid:'SCHNELLFEUER!',triple:'DREIFACHSCHUSS!',shield:'SCHILD!',life:'+1 LEBEN'}[t];g.msgT=1.6;
 if(t=='rapid')g.buff.rapid=10;else if(t=='triple')g.buff.triple=12;else if(t=='shield')g.shield=true;else g.lives=Math.min(5,g.lives+1)}
function ivHurt(g){if(g.inv>0)return false;if(g.shield){g.shield=false;g.inv=.8;sfx('boom');ivBoom(g,g.px,515,'#7CFC9a',14);return false}
 g.lives--;g.inv=1.5;sfx('hurt');return g.lives<=0}
function ivUpdate(dt){const g=IV;
 const dx=(arK.KeyD||arK.ArrowRight?1:0)-(arK.KeyA||arK.ArrowLeft?1:0);g.px=arClamp(g.px+dx*370*dt,40,920);if(dx)g.walk+=dt*14;
 const bf=g.buff;bf.rapid=Math.max(0,bf.rapid-dt);bf.triple=Math.max(0,bf.triple-dt);g.msgT=Math.max(0,g.msgT-dt);
 g.cd-=dt;const auto=bf.auto>0;
 if((arK.Space||auto)&&g.cd<=0&&g.phase=='play'){g.cd=auto?.1:bf.rapid>0?.16:.38;sfx('shoot');
  if(bf.triple>0){for(const a of[-.2,0,.2])g.bul.push({x:g.px+a*30,y:488,vx:Math.sin(a)*640,vy:-Math.cos(a)*640})}else g.bul.push({x:g.px,y:488,vx:0,vy:-640})}
 g.inv=Math.max(0,g.inv-dt);g.banner=Math.max(0,g.banner-dt);
 const al=g.en.filter(e=>e.hp>0);
 if(!al.length&&!g.ufo){g.w++;g.lives=Math.min(5,g.lives+1);if(bf.auto>0)bf.auto--;sfx('wave');ivGrid(g);g.banner=1.8;return}
 if(g.entering){g.fy=Math.min(70,g.fy+230*dt);if(g.fy>=70)g.entering=false}
 else{g.wt+=dt;
  if(!g.ufo&&!g.ufoDone&&g.wt>=g.ufoAt&&al.length){g.ufoDone=true;g.ufo={x:Math.random()<.5?60:900,y:-40,vx:(Math.random()<.5?-1:1)*(300+g.w*8),hp:5+Math.floor(g.w*1.4),mhp:5+Math.floor(g.w*1.4),sh:1,life:0,leave:false};g.ufo.mhp=g.ufo.hp;sfx('ufo')}}
 let mn=1e9,mx=-1e9,my=0;for(const e of al){const p=ivPos(g,e);mn=Math.min(mn,p.x);mx=Math.max(mx,p.x);my=Math.max(my,p.y)}
 const sp=(30+g.w*7+(1-al.length/g.total)*150)*g.dir;
 if(!g.entering){if((mx+24>940&&g.dir>0)||(mn-24<20&&g.dir<0)){g.dir*=-1;g.fy+=22}else g.fx+=sp*dt}
 if(al.length&&my+30>=470){ivEnd();return}
 g.ft-=dt;if(g.ft<=0&&!g.entering){g.ft=Math.max(.35,1.3-g.w*.08);const cands=al.filter(a=>!al.some(b=>b.c==a.c&&b.r>a.r));const e=cands[Math.floor(Math.random()*cands.length)];if(e){const p=ivPos(g,e);g.eb.push({x:p.x,y:p.y+30,vx:0,vy:250+g.w*8})}}
 const u=g.ufo;
 if(u){u.life+=dt;if(u.life>24)u.leave=true;
  if(u.leave){u.y-=200*dt;if(u.y<-60)g.ufo=null}
  else{u.y=Math.min(62,u.y+160*dt);u.x+=u.vx*dt;if(u.x<50){u.x=50;u.vx=Math.abs(u.vx)}if(u.x>910){u.x=910;u.vx=-Math.abs(u.vx)}
   u.sh-=dt;if(u.sh<=0&&u.y>=60){u.sh=Math.max(.7,1.5-g.w*.05);const ax=g.px-u.x,ay=515-u.y,l=Math.hypot(ax,ay);g.eb.push({x:u.x,y:u.y+16,vx:ax/l*320,vy:ay/l*320,hot:1});sfx('tick')}}}
 for(const b of g.bul){b.x+=b.vx*dt;b.y+=b.vy*dt;let used=false;
  for(const e of al){if(e.hp<=0)continue;const p=ivPos(g,e);if(Math.abs(b.x-p.x)<24&&Math.abs(b.y-p.y)<31){b.y=-99;used=true;e.hp--;e.fl=.1;sfx('hit');
   ivBoom(g,p.x,p.y,'#7CFC9a',5);if(e.hp<=0){g.kills++;sfx('kill');if(e.mhp>1&&Math.random()<.28||Math.random()<.025)ivDrop(g,p.x,p.y)}break}}
  if(!used&&u&&!u.leave&&Math.abs(b.x-u.x)<38&&Math.abs(b.y-u.y)<18){b.y=-99;u.hp--;sfx('hit');ivBoom(g,u.x,u.y,'#ffe27a',5);
   if(u.hp<=0){g.kills+=5;ivBoom(g,u.x,u.y,'#ffe27a',36);sfx('boom');g.caps.push({x:u.x,y:u.y,t:'shield'});bf.auto=3;g.msg='VOLLAUTOMATIK FÜR 2 WELLEN!';g.msgT=2.6;g.banner=0;sfx('pow');g.ufo=null}}}
 for(const b of g.eb){b.x+=b.vx*dt;b.y+=b.vy*dt;if(Math.abs(b.x-g.px)<26&&Math.abs(b.y-515)<30&&g.inv<=0){b.y=999;if(ivHurt(g)){ivEnd();return}}}
 for(const c of g.caps){c.y+=140*dt;if(Math.abs(c.x-g.px)<38&&Math.abs(c.y-515)<36){c.y=999;ivGet(g,c.t)}}
 g.bul=g.bul.filter(b=>b.y>0&&b.x>0&&b.x<960);g.eb=g.eb.filter(b=>b.y<610&&b.x>-20&&b.x<980);g.caps=g.caps.filter(c=>c.y<600);
 for(const e of g.en)e.fl=Math.max(0,e.fl-dt);
 for(const q of g.pt){q.x+=q.vx*dt;q.y+=q.vy*dt;q.life-=dt}g.pt=g.pt.filter(q=>q.life>0)}
async function ivEnd(){const g=IV;g.phase='over';auStop();sfx('over');const done=g.w-1,o=$('#ivov');
 o.innerHTML=`<div class="arbox"><h3 class="shh">💀 Invasion in Welle ${g.w}</h3><div class="dim">Kills: ${g.kills} · Berechne Belohnung …</div></div>`;o.style.display='flex';
 let msg='';try{const r=await coinOp('arcade',null,{mode:'inv',completed:done,kills:g.kills});msg=r.msg||''}catch(e){msg='Belohnung konnte nicht abgeholt werden.'}
 if(tab!='Arcade')return;
 o.innerHTML=`<div class="arbox"><h3 class="shh">💀 Invasion in Welle ${g.w}</h3><div class="dim">Geschaffte Wellen: ${done} · Kills: ${g.kills}</div><p><b>${esc(msg)}</b></p><button class="btn pri" onclick="ivShowStart()">Nochmal spielen</button></div>`}
function ivUfoArt(c,u,t){c.save();c.translate(u.x,u.y);c.shadowColor='#ff4fd8';c.shadowBlur=22;
 c.fillStyle=arGrad(c,-4,16,'#e8ecf5','#6a7388');c.beginPath();c.ellipse(0,6,40,13,0,0,6.3);c.fill();c.shadowBlur=0;c.strokeStyle='#1a1d2b';c.lineWidth=2;c.stroke();
 const dg=c.createRadialGradient(-6,-8,2,0,-4,22);dg.addColorStop(0,'rgba(220,255,255,.95)');dg.addColorStop(1,'rgba(60,170,230,.75)');c.fillStyle=dg;c.beginPath();c.ellipse(0,0,19,17,0,Math.PI,0);c.fill();c.stroke();
 c.fillStyle='#7CFC9a';c.beginPath();c.arc(0,-3,4,0,6.3);c.fill();c.fillStyle='#111';c.fillRect(-2.5,-4,1.6,2.4);c.fillRect(1,-4,1.6,2.4);
 for(let i=-3;i<=3;i++){c.fillStyle=['#ff4fd8','#ffe27a','#4dd2ff'][(i+3+Math.floor(t*6))%3];c.beginPath();c.arc(i*10,9+Math.abs(i)*-.6,2.6,0,6.3);c.fill()}
 c.fillStyle='rgba(0,0,0,.6)';c.fillRect(-30,-30,60,5);c.fillStyle='#ff4fd8';c.fillRect(-30,-30,60*Math.max(0,u.hp/u.mhp),5);c.restore()}
function ivDraw(t){const c=ivCtx,g=IV;if(!c||!g)return;c.setTransform(CVK,0,0,CVK,0,0);
 c.drawImage(ivBG(),0,0);c.fillStyle='#fff';ivStars.forEach(q=>{c.globalAlpha=.35+.55*Math.abs(Math.sin(t*1.4+q.p));c.fillRect(q.x,q.y,q.s,q.s)});c.globalAlpha=1;ivBlink.forEach((b,i)=>{if(Math.floor(t*1.2+i)%2){c.fillStyle='#ff3b3b';c.shadowColor='#ff3b3b';c.shadowBlur=8;c.fillRect(b.x-1.5,b.y-2,3,3);c.shadowBlur=0}});
 const sh=(t%11)/11;if(sh<.07){c.strokeStyle='rgba(255,255,255,'+(1-sh/.07)+')';c.lineWidth=2;c.beginPath();c.moveTo(150+sh*4000,40+sh*1500);c.lineTo(110+sh*4000,28+sh*1500);c.stroke()}
 for(const e of g.en){if(e.hp<=0)continue;const p=ivPos(g,e),w=44,h=58;c.save();c.translate(p.x,p.y+Math.sin(t*3+e.ph)*2);
  c.save();arRR(c,-w/2,-h/2,w,h,6);c.clip();const im=e.im;if(im&&im.img.complete&&im.img.naturalWidth)c.drawImage(im.img,-w/2,-h/2,w,h);else{c.fillStyle='hsl('+((e.ph*57)|0)+',45%,32%)';c.fillRect(-w/2,-h/2,w,h);c.fillStyle='#fff';c.font='bold 22px sans-serif';c.textAlign='center';c.textBaseline='middle';c.fillText(im?(im.name[0]||'?').toUpperCase():'?',0,0)}
  c.fillStyle=e.mhp>1?'rgba(255,0,0,.22)':'rgba(0,255,120,.12)';c.fillRect(-w/2,-h/2,w,h);if(e.fl>0){c.fillStyle='rgba(255,255,255,.7)';c.fillRect(-w/2,-h/2,w,h)}c.restore();
  c.lineWidth=2;c.strokeStyle=e.mhp>1?'#ff5a4a':'#cfd6dd';if(e.mhp>1){c.shadowColor='#ff5a4a';c.shadowBlur=8}arRR(c,-w/2,-h/2,w,h,6);c.stroke();c.shadowBlur=0;
  c.fillStyle='#fff';c.beginPath();c.arc(-9,-6,4,0,6.3);c.arc(9,-6,4,0,6.3);c.fill();c.fillStyle='#111';c.beginPath();c.arc(-9,-5,2,0,6.3);c.arc(9,-5,2,0,6.3);c.fill();
  c.strokeStyle='#111';c.lineWidth=2;c.beginPath();c.moveTo(-14,-12);c.lineTo(-5,-9);c.moveTo(14,-12);c.lineTo(5,-9);c.stroke();c.restore()}
 if(g.ufo)ivUfoArt(c,g.ufo,t);
 for(const k of g.caps){const m=IVCAP[k.t];c.save();c.translate(k.x,k.y);c.shadowColor=m.c;c.shadowBlur=14;c.fillStyle=m.c;arRR(c,-14,-11,28,22,8);c.fill();c.shadowBlur=0;c.strokeStyle='#fff';c.lineWidth=2;c.stroke();c.fillStyle='#10131a';c.font='bold 15px sans-serif';c.textAlign='center';c.textBaseline='middle';c.fillText(m.l,0,1);c.restore()}
 const S2=.3,bw=ARI.body.naturalWidth*S2,bh=ARI.body.naturalHeight*S2;
 if(bw){c.save();if(g.inv>0&&Math.floor(t*20)%2)c.globalAlpha=.4;const fy=548,mv=arK.KeyA||arK.KeyD||arK.ArrowLeft||arK.ArrowRight;
  const ll=ARI.ll,rl=ARI.rl,lh=ll.naturalHeight*S2,bob=mv?Math.abs(Math.sin(g.walk))*2:0;
  const leg=(im,ox,k)=>{const s=mv?Math.sin(g.walk+k*Math.PI):0;c.save();c.translate(g.px+ox+im.naturalWidth*S2/2,fy-lh+1-(mv?Math.max(0,s)*4:0));c.rotate(s*.5);c.drawImage(im,-im.naturalWidth*S2/2,0,im.naturalWidth*S2,im.naturalHeight*S2);c.restore()};
  leg(ll,ARLX[0]*S2-bw/2,0);leg(rl,ARLX[1]*S2-bw/2,1);
  c.drawImage(ARI.body,g.px-bw/2,fy-lh+1-bh-bob,bw,bh);
  c.fillStyle='#8a939c';c.fillRect(g.px-4,fy-lh-bh-bob-14,8,16);c.fillStyle=g.buff.auto>0?'#ff9a3a':'#7CFC9a';c.fillRect(g.px-4,fy-lh-bh-bob-14,8,4);
  if(g.shield){c.globalAlpha=.55+.25*Math.sin(t*8);c.strokeStyle='#7CFC9a';c.shadowColor='#7CFC9a';c.shadowBlur=14;c.lineWidth=3;c.beginPath();c.arc(g.px,fy-lh-bh/2,44,0,6.3);c.stroke()}c.restore()}
 c.fillStyle='#7CFC9a';for(const b of g.bul){c.save();c.translate(b.x,b.y);c.rotate(Math.atan2(b.vx,-b.vy));c.shadowColor='#7CFC9a';c.shadowBlur=8;c.fillRect(-2,-9,4,16);c.restore()}
 for(const b of g.eb){c.fillStyle=b.hot?'#ff4fd8':'#ff5a4a';c.shadowColor=c.fillStyle;c.shadowBlur=10;c.beginPath();c.arc(b.x,b.y,b.hot?6:5,0,6.3);c.fill();c.shadowBlur=0}
 for(const q of g.pt){c.globalAlpha=Math.max(0,q.life*2.2);c.fillStyle=q.col;c.fillRect(q.x-2,q.y-2,4,4)}c.globalAlpha=1;
 c.fillStyle='rgba(0,0,0,.55)';c.fillRect(0,0,960,38);c.fillStyle='#fff';c.font='bold 16px sans-serif';c.textBaseline='middle';
 c.textAlign='left';c.fillText('❤'.repeat(Math.max(0,g.lives)),16,19);c.textAlign='center';c.fillText('Welle '+g.w,480,19);c.textAlign='right';c.fillText('☠ '+g.kills,944,19);
 const tags=[];if(g.buff.auto>0)tags.push(['AUTO ×'+g.buff.auto,'#ff9a3a']);if(g.buff.rapid>0)tags.push(['SCHNELL '+Math.ceil(g.buff.rapid)+'s','#ff9a3a']);if(g.buff.triple>0)tags.push(['3-WEG '+Math.ceil(g.buff.triple)+'s','#4dd2ff']);if(g.shield)tags.push(['SCHILD','#7CFC9a']);
 c.font='bold 13px sans-serif';let tx=140;c.textAlign='left';tags.forEach(a=>{c.fillStyle=a[1];c.fillText(a[0],tx,19);tx+=c.measureText(a[0]).width+16});
 c.textAlign='center';
 if(g.banner>0){c.fillStyle='#fff';c.font='bold 34px sans-serif';c.fillText('Welle '+g.w+'!',480,300)}
 if(g.msgT>0&&g.msg){c.save();c.globalAlpha=Math.min(1,g.msgT*2);c.fillStyle='#ffe27a';c.shadowColor='#ff9a3a';c.shadowBlur=16;c.font='bold 30px sans-serif';c.fillText(g.msg,480,200);c.restore()}
 if(g.paused){c.fillStyle='rgba(0,0,0,.6)';c.fillRect(0,0,960,600);c.fillStyle='#fff';c.font='bold 38px sans-serif';c.textAlign='center';c.fillText('Pause (P)',480,300)}}
function ivLoop(now){ivRaf=0;if(tab!='Arcade'||!$('#ivc'))return;const dt=Math.min(.05,(now-ivLast)/1000||0);ivLast=now;
 if(IV&&IV.phase=='play'&&!IV.paused)ivUpdate(dt);ivDraw(now/1000);ivRaf=requestAnimationFrame(ivLoop)}
function ivTogglePause(){if(IV&&IV.phase=='play'){IV.paused=!IV.paused;auPause(IV.paused)}}
addEventListener('keydown',e=>{if(tab!='Arcade'||arcMode!='inv')return;if(/^(Arrow|Key[AD]|Space)/.test(e.code)){arK[e.code]=1;if(e.code=='Space'||e.code.startsWith('Arrow'))e.preventDefault()}
 if(e.code=='KeyP'||e.code=='Escape')ivTogglePause()});
addEventListener('blur',()=>{if(IV&&IV.phase=='play'&&!IV.paused)ivTogglePause()});

/* ===== OZZYS ATLAS ===== */
let atRef=null,atTimer=0,atSel=null,atLast=null;
const ATN_T={wiese:'Wiese',acker:'Acker',wald:'Wald',see:'See',berg:'Berg',stadt:'Stadt'};
const atF=n=>n>=1000?n.toLocaleString('de-DE',{maximumFractionDigits:2}):n>=1?n.toFixed(6):n.toFixed(9);
function atArt(t,own,exp,k){const r=rng(k*7+3);let s='';
 const tree=(x,y,sc)=>`<polygon points="${x},${y-18*sc} ${x-8*sc},${y} ${x+8*sc},${y}" fill="#1f6b2c"/><polygon points="${x},${y-27*sc} ${x-6*sc},${y-12*sc} ${x+6*sc},${y-12*sc}" fill="#2b8a3a"/><rect x="${x-1.5*sc}" y="${y}" width="${3*sc}" height="${4*sc}" fill="#5a3a1a"/>`;
 if(t=='wiese'){s+=`<rect width="150" height="100" fill="url(#gW)"/>`;for(let i=0;i<22;i++)s+=`<path d="M${r()*150} ${20+r()*80} l1.5 -6 M${r()*150} ${20+r()*80} l-1.5 -5" stroke="#2d6f2a" stroke-width="1.2"/>`;for(let i=0;i<9;i++)s+=`<circle cx="${r()*150}" cy="${25+r()*70}" r="1.8" fill="${['#fff3a0','#fff','#ff9ec0'][i%3]}"/>`}
 else if(t=='acker'){s+=`<rect width="150" height="100" fill="#8a6234"/>`;for(let i=0;i<8;i++)s+=`<rect x="0" y="${8+i*12}" width="150" height="6" fill="${i%2?'#6aa83a':'#7fbf45'}" opacity=".85"/>`}
 else if(t=='wald'){s+=`<rect width="150" height="100" fill="#2a6a2c"/>`;for(let i=0;i<14;i++)s+=tree(10+r()*130,34+r()*62,.8+r()*.5)}
 else if(t=='see'){s+=`<rect width="150" height="100" fill="#d9c98a"/><ellipse cx="75" cy="52" rx="62" ry="38" fill="url(#gS)"/>`;for(let i=0;i<5;i++)s+=`<path d="M${30+i*20} ${40+(i%3)*12} q6 -5 12 0 t12 0" stroke="rgba(255,255,255,.5)" fill="none" stroke-width="1.4"/>`}
 else if(t=='berg'){s+=`<rect width="150" height="100" fill="#6d7a62"/><polygon points="10,100 60,22 110,100" fill="#8a8f98"/><polygon points="60,22 49,43 60,38 70,45" fill="#fff"/><polygon points="70,100 112,40 150,100" fill="#737982"/><polygon points="112,40 104,55 112,51 120,57" fill="#fff"/>`}
 else{s+=`<rect width="150" height="100" fill="#3a3f4a"/>`;for(let i=0;i<7;i++){const x=6+i*20,h=30+r()*50;s+=`<rect x="${x}" y="${100-h}" width="16" height="${h}" fill="${['#566074','#4b5468','#626d82'][i%3]}"/>`;for(let w=0;w<h/12;w++)s+=`<rect x="${x+3}" y="${100-h+4+w*11}" width="4" height="5" fill="${r()<.6?'#ffd27a':'#2d3240'}"/><rect x="${x+9}" y="${100-h+4+w*11}" width="4" height="5" fill="${r()<.6?'#ffd27a':'#2d3240'}"/>`}}
 if(own)for(let i=0;i<exp;i++){const x=14+i*30;s+=`<g transform="translate(${x},72)"><rect x="0" y="8" width="20" height="14" fill="#e8d9b0" stroke="#3a2a14" stroke-width="1.2"/><polygon points="-3,9 10,-2 23,9" fill="#b8402e" stroke="#3a2a14" stroke-width="1.2"/><rect x="8" y="14" width="5" height="8" fill="#5a3a1a"/></g>`}
 return s}
const AT_DEFS='<defs><linearGradient id="gW" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#5aa03a"/><stop offset="1" stop-color="#3e7a2a"/></linearGradient><radialGradient id="gS"><stop offset="0" stop-color="#5fc1f0"/><stop offset="1" stop-color="#2a7fc0"/></radialGradient></defs>';
function atBal(){if(!atRef)return 0;return atRef.bal+atRef.inc*(performance.now()-atRef.at)/1000}
function atSync(){const a=S.atlas;if(!a)return;if(a!==atLast){atLast=a;atRef={bal:a.bal,inc:a.inc,at:performance.now()}}}
function atlasView(){return `<div id="atl"><div class="atlh"><div class="atozzy"><img src="${OZ_FRIENDLY}" alt=""><div class="atbub" id="atmsg"></div></div><div><div class="dim">Atlas-Coins</div><div class="atbig" id="atbal">0</div></div><div><div class="dim">Einkommen pro Sekunde</div><div class="atmid" id="atinc">0</div></div><div><div class="dim">pro Stunde / pro Tag</div><div class="atmid" id="atday">0</div></div><div id="atx"></div></div><div class="atmain"><div class="atmap" id="atmap"></div><div class="atside" id="atside"></div></div></div>`}
function atRender(){const a=S.atlas;if(!a||!$('#atmap'))return;atSync();
 $('#atmap').innerHTML=a.tiles.map(t=>`<div class="atile ${t.own?'own':''} ${atSel==t.i?'sel':''}" onclick="atPick(${t.i})"><svg viewBox="0 0 150 100" preserveAspectRatio="xMidYMid slice">${AT_DEFS}${atArt(t.t,t.own,t.exp||1,t.i)}</svg>
  <div class="atinfo">${t.own?`<b>${ATN_T[t.t]}</b> · Lv ${t.lvl}<span>${atF(t.inc)}/s</span>`:`<b>${ATN_T[t.t]}</b><span class="atp" data-cost="${t.price}">✦ ${atF(t.price)}</span>`}</div>${t.own?'':'<i class="atlock"></i>'}</div>`).join('');
 const t=atSel==null?null:a.tiles[atSel];
 $('#atside').innerHTML=!t?`<h3>Ozzys Atlas</h3><div class="dim">Kaufe Grundstücke auf der Karte, verbessere und erweitere sie. Sie bringen dir rund um die Uhr passiv Atlas-Coins, auch wenn D-Hub zu ist (bis zu 12 Stunden). Klicke ein Grundstück an.</div><div class="dim" style="margin-top:10px">Du hast ${a.owned} von ${a.tiles.length} Grundstücken. Das nächste kostet ✦ ${atF(a.tiles.find(x=>!x.own)?.price||0)}.</div>`
 :t.own?`<h3>${ATN_T[t.t]} #${t.i+1}</h3><div class="dim">${t.acres.toLocaleString('de-DE')} Acres · Stufe ${t.lvl}/10 · Ausbau ${t.exp}/4</div><div class="atmid" style="margin:8px 0">${atF(t.inc)} /s</div>
  <button class="btn" ${t.up==null?'disabled':''} data-cost="${t.up??''}" onclick="atOp('atl_up',${t.i})">Verbessern ${t.up==null?'(max.)':'✦ '+atF(t.up)}</button><div class="dim" style="margin:4px 0 10px">+25 % Grundeinkommen pro Stufe</div>
  <button class="btn" ${t.ex==null?'disabled':''} data-cost="${t.ex??''}" onclick="atOp('atl_exp',${t.i})">Erweitern ${t.ex==null?'(voll)':'✦ '+atF(t.ex)}</button><div class="dim" style="margin-top:4px">Mehr Fläche und neue Gebäude: Einkommen ×${[2,1.75,1.71][t.exp-1]?[2,1.75,1.71][t.exp-1].toFixed(2):'–'}</div>`
 :`<h3>${ATN_T[t.t]} #${t.i+1} (zu verkaufen)</h3><div class="dim">${t.acres.toLocaleString('de-DE')} Acres</div><button class="btn pri" style="margin-top:10px" data-cost="${t.price}" onclick="atOp('atl_buy',${t.i})">Kaufen ✦ ${atF(t.price)}</button><div class="dim" style="margin-top:8px">Jedes weitere Grundstück wird teurer, bringt aber auch mehr Fläche.</div>`;
 $('#atx').innerHTML=`<div class="dim">In D-Hub-Coins tauschen (1 : 1, heute noch ${a.x_left} von ${a.xcap})</div><div class="row" style="padding:4px 0;gap:6px"><input type="number" id="atxn" min="1" max="${a.xcap}" value="${Math.max(1,Math.min(a.x_left,10))}" style="width:80px"><button class="btn pri" onclick="atOp('atl_x',0,+$('#atxn').value)">Tauschen</button></div>`;
 atTick()}
function atTick(){if(!$('#atbal')){clearInterval(atTimer);atTimer=0;return}atSync();const b=atBal(),i=atRef?atRef.inc:0;
 {const a=S.atlas,nx=a&&a.tiles.find(x=>!x.own),m=!a?'':a.owned==0?'Willkommen in Ozzys Atlas! Such dir ein Grundstück aus. Dein Startkapital reicht für genau eins.':(nx&&b>=nx.price)?'Du kannst dir schon das nächste Grundstück leisten!':nx?'Deine Grundstücke arbeiten für dich. Schau später wieder rein.':'Du besitzt die ganze Karte. Respekt!',e=$('#atmsg');if(e&&e.textContent!==m)e.textContent=m}
 $('#atbal').textContent=atF(b);$('#atinc').textContent=i.toFixed(10);$('#atday').textContent=(i*3600).toFixed(7)+' / '+(i*86400).toFixed(6);
 document.querySelectorAll('#atl [data-cost]').forEach(e=>{const c=parseFloat(e.dataset.cost);if(isNaN(c))return;const poor=b<c;if(e.tagName=='BUTTON')e.disabled=poor;else e.style.opacity=poor?.55:1})}
function atPick(i){atSel=i;atRender()}
async function atOp(op,id,amount){try{const r=await (await fetch('/api/coin',{method:'POST',body:JSON.stringify({op,id,amount})})).json();S.coins=r.coins;S.atlas=r.atlas;applyCoins();toast(r.msg);if(r.ok)sfx(op=='atl_x'?'win':'buy');atRender()}catch(e){toast('Fehler')}}
function atInit(){atSel=null;atSync();atRender();clearInterval(atTimer);atTimer=setInterval(atTick,100);auCtx();auPlay('atl');
 try{const a=S.atlas,seen=parseFloat(localStorage.getItem('dhub_atl_seen')||'');if(!isNaN(seen)&&a.earned-seen>1e-12)toast('Seit deinem letzten Besuch verdient: '+atF(a.earned-seen)+' Atlas-Coins')}catch(e){}}
function atStop(){clearInterval(atTimer);atTimer=0;try{if(S.atlas)localStorage.setItem('dhub_atl_seen',String(S.atlas.earned+(atBal()-(atRef?atRef.bal:S.atlas.bal))))}catch(e){}}

/* ===== PATCH NOTES (bei jeder Änderung oben ergänzen!) ===== */
const PATCHES=[
 {v:'3.5',d:'15.11.2026',t:'Update-Knopf an der Versionsnummer',i:[['Neu','Sobald ein neues Release auf GitHub bereitliegt, erscheint direkt neben der Versionsnummer oben links ein pulsierender Knopf „Update x.y“. Ein Klick lädt das Update und startet D-Hub neu.'],['Neu','Ein Klick auf die Versionsnummer selbst sucht sofort nach Updates und meldet, ob eine neue Version da ist.']]},
 {v:'3.4',d:'15.11.2026',t:'Start-Reparatur',i:[['Fix','Startwächter: Wenn das D-Hub-Fenster beim Start leer bleibt, startet D-Hub beim nächsten Mal automatisch im Edge-Fenster und löscht dabei das beschädigte WebView2-Profil. Danach wird das eigene Fenster erneut versucht. Klappt es auch dann nicht, bleibt D-Hub dauerhaft im Edge-Fenster.'],['Fix','Fehler beim Laden werden jetzt als roter Kasten im Fenster angezeigt und in dhub.log protokolliert, statt als schwarzer Bildschirm.'],['Fix','Der Installer beendet jetzt auch die zugehörigen WebView2-Prozesse des alten D-Hub, die sonst das Fenster-Profil gesperrt halten konnten.']]},
 {v:'3.3',d:'15.11.2026',t:'Start-Absicherung',i:[['Fix','Bleibt das D-Hub-Fenster nach dem Start schwarz oder leer, öffnet D-Hub jetzt nach etwa 20 Sekunden automatisch ein Edge-Fenster mit derselben Oberfläche als Ersatz, statt nur eine Fehlerseite zu zeigen. Der Vorgang wird in dhub.log protokolliert.']]},
 {v:'3.2',d:'15.11.2026',t:'Männchen-Baukasten',i:[['Neu','Neue Blanko-Figur „Männchen“: das D-Hub-Männchen ohne Kappe, Kopfhörer, Oberteil und Schuhe. Sie ist gratis und lässt sich im Arcade-Reiter oder im Shop als Spielfigur wählen.'],['Neu','Im Shop gibt es jetzt Ausrüstung für das Männchen in fünf Plätzen: Hüte (D-Hub-Kappe, Zauberhut, Goldene Krone), Kopfhörer, Sonnenbrille, Oberteile (D-Hub-Hoodie, Goldrüstung) und Schuhe (Neon-Sneaker, Goldstiefel). Jedes Teil trägst du einzeln und kannst es jederzeit wieder ablegen.'],['Neu','Das Männchen trägt die Ausrüstung in Survivor und Invaders, inklusive Laufanimation der Schuhe. Im Shop siehst du eine Live-Vorschau deiner Figur und bei jedem Teil ein Vorschaubild.'],['Info','Das bisherige Maskottchen mit allem dran bleibt als Spielfigur „D-Hub“ unverändert erhalten. Das ist ein erstes Beispiel, weitere Teile lassen sich nach dem gleichen Muster ergänzen.']]},
 {v:'3.1',d:'13.11.2026',t:'Raketen, echte Sounds, Großbild, 3D-Münze und W20',i:[['Neu','Survivor: Der Raketenwerfer feuert jetzt echte Raketen mit Flamme und Rauchspur. Sie explodieren beim Aufprall oder am Ende der Reichweite mit Feuerball, Druckwelle, Funken und Rauchwolken. Dort bleibt ein kleiner Krater am Boden, der nach einigen Sekunden verblasst. Der Bildschirm wackelt kurz, und beim Abschuss gibt es Mündungsfeuer.'],['Neu','Survivor: Realistischere Waffensounds. Die Pistole knallt mit scharfem Schuss und dumpfem Nachhall, die MP rattert kürzer und variiert, die Schrotflinte donnert und repetiert, der Raketenwerfer zischt mit Startschlag und Düsenrauschen, die Explosion grollt mit Splittergeräuschen. Das Laser-Piu gibt es nur noch in Invaders.'],['Neu','Arcade-Spiele sind jetzt deutlich größer: Beim Spielen wird die Navigationsleiste ausgeblendet und das Spielfeld füllt das Fenster. Die Spielfläche wird zudem schärfer berechnet.'],['Neu','Gamble: Der Münzwurf nutzt eine echte 3D-Münze mit Dicke und Rand. Sie fliegt hoch, dreht sich mehrfach, landet mit Hüpfern und Schatten, dazu Klingeln beim Start und Aufprall.'],['Neu','Gamble: Das Würfeln läuft mit einem 3D-W20 mit 20 Flächen und Zahlen in Gold auf dunklem Violett, mit magischer Aura, kreisenden Runenringen und Funken. Er wird geworfen, hüpft und rollt aus. Das Ergebnis bleibt serverseitig dasselbe, der W20 zeigt den Bereich (jede Zahl = 5 Prozentpunkte).'],['Neu','Gamble ist jetzt mittig im Fenster ausgerichtet.'],['Fix','Special-Skins: Bei aktivem Skin konnte der Inhalt im Gamble-Reiter und anderen Reitern unsichtbar bleiben, weil eine versteckte Fläche Platz belegte. Behoben.']]},
 {v:'3.0',d:'13.11.2026',t:'Ozzy, Special-Skins, freundlicher Atlas',i:[['Neu','Neue Spielfigur „Ozzy“ nach deinem Bild, mit Laufanimation. Sie lässt sich im Shop freischalten (600 Coins) und ist dann in Survivor und Invaders spielbar. Die Auswahl steht oben im Arcade-Reiter.'],['Neu','Ozzys Atlas hat jetzt ein freundliches Gesicht auf dem Cover und im Spiel: gleiche Figur, aber mit großen freundlichen Augen und einem Lächeln, dazu eine Sprechblase, die dir passend zur Lage etwas sagt (Willkommen, nächstes Grundstück leistbar usw.).'],['Neu','Sechs Special-Skins im Shop, die nicht nur die Farbe ändern: Matrix-Regen (fallender Code, Terminal-Schrift), Retro-Röhre (Scanlines, Flackern, Computerschrift), Neon Dusk (Synthwave-Sonne mit bewegtem Gitterboden und Logo-Glitch), Inferno (aufsteigende Glut, Flammenschrift), Galaxie (Sternenhimmel, Spiralnebel, Sternschnuppen, Milchglas) und Royal Gold (Marmor, wandernder Goldglanz, Funkeln).'],['Info','Skins sind teuer (1.200 bis 3.500 Coins). Im hellen Design und während eines laufenden Arcade-Spiels pausiert der animierte Hintergrund, damit nichts ruckelt.']]},
 {v:'2.9',d:'13.11.2026',t:'Sound, Ozzys Atlas, Vollbild',i:[['Neu','Musik und Effekte: Invaders mit Synthwave, Survivor mit hartem Chiptune-Riff, Ozzys Atlas mit ruhigem Lofi-Beat. Alles wird live im Programm erzeugt, es gibt keine Audiodateien. Ein Lautsprecher-Knopf schaltet den Ton an und aus.'],['Neu','Ozzys Atlas, das dritte Arcade-Spiel: Kaufe Grundstücke auf einer Karte (Wiese, Acker, Wald, See, Berg, Stadt), verbessere sie bis Stufe 10 und erweitere sie in vier Stufen. Sie bringen passiv Atlas-Coins, auch bei geschlossenem D-Hub (bis 12 Stunden). Du startest mit 0,000002 Atlas-Coins, das reicht für genau ein Grundstück, das erste bringt 0,00000001 pro Sekunde.'],['Neu','Atlas-Coins lassen sich 1 : 1 in D-Hub-Coins tauschen, höchstens 50 pro Tag.'],['Neu','Survivor: Die Waffen kreisen jetzt dauerhaft rundum, auch das Schwert, damit du von allen Seiten geschützt bist. Du kannst beliebig viele Waffen kaufen (bis 6), im Shop gibt es immer mindestens zwei Waffen im Angebot.'],['Neu','Survivor: Die Kamera ist weiter herausgezoomt, die ganze Arena ist größer und die Figuren kleiner.'],['Neu','Survivor: Neues Kaufmenü mit Karten, Werte-Balken, Stufen-Leuchten und selbst gezeichneten Waffen statt Emojis (Pistole, MP, Schrotflinte, Schwert, Zauberstab, Raketenwerfer). Auch die Gegenstände haben eigene Symbole.'],['Neu','Invaders: Dicke rote Gegner lassen manchmal Power-Ups fallen (Schnellfeuer, Dreifachschuss, Schild, Extra-Leben).'],['Neu','Invaders: Ab Welle 2 fliegt ein Ufo schnell hin und her und schießt gezielt zurück. Besiegst du es, hast du für den Rest der Welle und die nächsten 2 Wellen Vollautomatik.'],['Neu','Gamble: Die Slotmaschine ist jetzt zentriert und edel gestaltet (Goldrahmen, Lichterkette, Glasreflex, Gewinnlinie). Beim Drehen klingelt es, jede Walze rastet hörbar ein, dazu Gewinn- und Jackpot-Sounds.'],['Neu','D-Hub hat jetzt einen echten Vollbildmodus (Knopf oben, Taste F11, oder in den Einstellungen als Startoption). Alt+Tab funktioniert dabei normal.']]},
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
let arcMode=null,ivBGc=null,ivStars=[],ivBlink=[];
function rng(seed){let s=seed>>>0;return()=>((s=(s*1664525+1013904223)>>>0)/4294967296)}
function arcRender(){const m=$('main');if(!m.querySelector('#arhub,#arc,#ivc,#atl')){arcMode=null;m.innerHTML=arcHub()}}
function arcHub(){const c=S.coins,b=c.arc_best||{},v=c.inv_best||{};
 const ring=['pistol','sword','wand','rocket','smg','shotgun'].map((id,i)=>`<span style="transform:rotate(${i*60}deg) translateX(78px)"><i style="display:block;transform:rotate(${-i*60}deg)"><img class="gcs" src="${arIcon(id,i%3)}" alt=""></i></span>`).join('');
 const tiles=[0,1,2,3,4].map(i=>`<div class="gtile" style="left:${10+i*18}%;top:${14+(i%2)*22}px;animation-delay:${i*.35}s;background:hsl(${i*70+260},45%,38%)"></div>`).join('');
 const mini=['#5aa03a','#8a6234','#2a6a2c','#2a7fc0','#8a8f98','#3a3f4a','#5aa03a','#2a6a2c','#8a6234','#2a7fc0','#3a3f4a','#8a8f98'].map((c,i)=>`<i style="background:${c};animation-delay:${i*.25}s"></i>`).join('');
 const at=S.atlas||{owned:0,inc:0};
 return `<h2>Arcade</h2><div class="sub">Wähle ein Spiel. Belohnung nur in Spiel-Coins (kein Echtgeld, kein Geldwert). Heute verdient: <b>${c.arc_today||0} / ${c.arc_cap||150}</b> 🪙</div>
 <div class="chsel"><span class="dim">Spielfigur:</span>${Object.keys(CHARS).map(id=>{const it=c.catalog.find(x=>x.id==id),own=c.owned.includes(id),on=c.active.char==id;return `<button class="chb ${on?'on':''} ${own?'':'lock'}" onclick="chPick('${id}')"><img src="${CH_SRC[CHARS[id].k]}" alt=""><b>${CHARS[id].n}</b>${own?(on?'<i>aktiv</i>':''):`<i>🔒 ✦ ${it.price}</i>`}</button>`}).join('')}</div>
 <div id="arhub" class="arhub">
  <div class="gcard" tabindex="0" onclick="arcOpen('surv')" onkeydown="if(event.key=='Enter')arcOpen('surv')"><div class="gart gsurv"><div class="gring">${ring}</div><img class="gmas" src="${chBody()}" alt=""></div>
   <h3>Survivor</h3><div class="dim">Überlebe 20 Wellen auf der Wiese. Waffen kreisen um dich, nach jeder Welle kaufst du neue.</div>
   <div class="gfoot"><span class="dim">Beste Runde: Welle ${b.wave||0} · ${b.kills||0} Kills</span><button class="btn pri">Spielen</button></div></div>
  <div class="gcard" tabindex="0" onclick="arcOpen('inv')" onkeydown="if(event.key=='Enter')arcOpen('inv')"><div class="gart ginv">${tiles}<div class="gcity"></div><img class="gmas2" src="${chBody()}" alt=""></div>
   <h3>Invaders</h3><div class="dim">Deine Spiele-Sammlung fällt aus dem All über die nächtliche Stadt her. Power-Ups und Ufo-Bonus inklusive.</div>
   <div class="gfoot"><span class="dim">Beste Runde: Welle ${v.wave||0} · ${v.kills||0} Kills</span><button class="btn pri">Spielen</button></div></div>
  <div class="gcard" tabindex="0" onclick="arcOpen('atlas')" onkeydown="if(event.key=='Enter')arcOpen('atlas')"><div class="gart gatl"><div class="gmini">${mini}</div><img class="gozzy" src="${OZ_FRIENDLY}" alt=""><span class="gbub">Willkommen!</span><span class="gcoin">✦ 0.000000001</span></div>
   <h3>Ozzys Atlas</h3><div class="dim">Kaufe Grundstücke, verbessere und erweitere sie und verdiene passiv Atlas-Coins, auch wenn D-Hub zu ist.</div>
   <div class="gfoot"><span class="dim">${at.owned} Grundstücke · ${(at.inc||0).toFixed(9)} /s</span><button class="btn pri">Spielen</button></div></div>
 </div>`}
const GNAME={surv:'Survivor',inv:'Invaders',atlas:'Ozzys Atlas'};
function arcOpen(m){arStop();ivStop();atStop();arcMode=m;document.body.classList.add('ingame');
 const body=m=='surv'?arView():m=='inv'?ivView():atlasView();
 $('main').innerHTML=`<div class="gshell gopen"><div class="gbar"><button class="btn" onclick="arcBack()">← Zurück zur Arcade</button><b class="gtitle">${GNAME[m]}</b>${muteBtn()}</div>${body}</div>`;
 if(m=='surv')arInit();else if(m=='inv')ivInit();else atInit()}
function arcBack(){arStop();ivStop();atStop();auStop();arcMode=null;document.body.classList.remove('ingame');AR=null;IV=null;$('main').innerHTML=arcHub()}
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

let gm='flip',gbet=10,gbusy=false,dmode='under',dtarget=50,wrot=0,coinRot=0;
function coinEdge(){let h='';for(let z=-7;z<=7;z+=1)h+=`<i class="ce" style="transform:translateZ(${z}px)"></i>`;return h}
const D20={faces:null};
function d20Build(){if(D20.faces)return D20.faces;const T=(1+Math.sqrt(5))/2,V=[[-1,T,0],[1,T,0],[-1,-T,0],[1,-T,0],[0,-1,T],[0,1,T],[0,-1,-T],[0,1,-T],[T,0,-1],[T,0,1],[-T,0,-1],[-T,0,1]],
 F=[[0,11,5],[0,5,1],[0,1,7],[0,7,10],[0,10,11],[1,5,9],[5,11,4],[11,10,2],[10,7,6],[7,1,8],[3,9,4],[3,4,2],[3,2,6],[3,6,8],[3,8,9],[4,9,5],[2,4,11],[6,2,10],[8,6,7],[9,8,1]],
 nm=v=>{const l=Math.hypot(v[0],v[1],v[2]);return [v[0]/l,v[1]/l,v[2]/l]},cr=(a,b)=>[a[1]*b[2]-a[2]*b[1],a[2]*b[0]-a[0]*b[2],a[0]*b[1]-a[1]*b[0]],K=44;
 const fs=F.map(f=>{const a=V[f[0]],b=V[f[1]],c=V[f[2]],ce=[(a[0]+b[0]+c[0])/3,(a[1]+b[1]+c[1])/3,(a[2]+b[2]+c[2])/3],n=nm(ce),up=nm([a[0]-ce[0],a[1]-ce[1],a[2]-ce[2]]),yv=[-up[0],-up[1],-up[2]],xv=cr(yv,n),r=Math.hypot(ce[0],ce[1],ce[2])*K;
  return {n,xv,yv,zv:n,r,light:.02+.2*Math.max(0,(-n[1]*.7+n[0]*.4+n[2]*.3)),num:0}});
 const perm=[7,2,9,4,10,1,6,3,8,5];let p=0;fs.forEach((f,i)=>{if(f.num)return;const j=fs.findIndex((g,k)=>k!=i&&g.n[0]*f.n[0]+g.n[1]*f.n[1]+g.n[2]*f.n[2]<-.99);f.num=perm[p];fs[j].num=21-perm[p];p++});
 fs.forEach(f=>{const{xv:x,yv:y,zv:z,n,r}=f;f.m=[x[0],x[1],x[2],0,y[0],y[1],y[2],0,z[0],z[1],z[2],0,n[0]*r,n[1]*r,n[2]*r,1];f.R=[x[0],y[0],z[0],0,x[1],y[1],z[1],0,x[2],y[2],z[2],0,0,0,0,1]});
 return D20.faces=fs}
let d20Cur=20;
function d20Html(){const fs=d20Build(),cur=fs.find(f=>f.num==d20Cur)||fs[0];
 return `<div class="d20w" id="d20w"><div class="d20ring"></div><div class="d20ring r2"></div><div class="d20aura"></div>${'<u class="d20sp"></u>'.repeat(7)}<div class="d20j" id="d20j"><div class="d20t"><div class="d20s" id="d20s"><div class="d20fin" id="d20f" style="transform:matrix3d(${cur.R.join(',')})">${fs.map(f=>`<div class="d20f3" style="transform:matrix3d(${f.m.join(',')});--l:${f.light.toFixed(3)}"><i>${f.num}${f.num==6||f.num==9?'.':''}</i></div>`).join('')}</div></div></div></div></div>`}
async function d20Roll(n){const f=d20Build().find(x=>x.num==n),sp=$('#d20s'),fin=$('#d20f'),j=$('#d20j'),w=$('#d20w');if(!sp)return;
 fin.style.transform='matrix3d('+f.R.join(',')+')';d20Cur=n;w.classList.remove('rolled','win','lose');w.classList.add('rolling');
 const ax=[.4+Math.random(),.4+Math.random(),.2+Math.random()].map(x=>x.toFixed(2)),turns=1440+Math.floor(Math.random()*3)*360;
 sp.animate([{transform:`rotate3d(${ax},0deg)`},{transform:`rotate3d(${ax},${turns}deg)`}],{duration:2000,easing:'cubic-bezier(.1,.7,.2,1)',fill:'forwards'});
 j.animate([{transform:'translateY(0) scale(1)',easing:'cubic-bezier(.2,.7,.4,1)'},{transform:'translateY(-72px) scale(1.15)',offset:.26,easing:'cubic-bezier(.6,0,.9,.5)'},{transform:'translateY(0) scale(1)',offset:.46,easing:'ease-out'},{transform:'translateY(-34px)',offset:.6,easing:'ease-in'},{transform:'translateY(0)',offset:.72,easing:'ease-out'},{transform:'translateY(-9px)',offset:.82,easing:'ease-in'},{transform:'translateY(0)'}],{duration:2000});
 [500,920,1200,1440,1640].forEach((ms,i)=>setTimeout(()=>sfx('dclack'),ms));
 await sleep(2050);w.classList.remove('rolling');w.classList.add('rolled')}
async function coinFlip(out){const cn=$('#cn'),cj=$('#cj'),sh=$('#csh');if(!cn)return;const end=Math.ceil(coinRot/360)*360+360*6+(out=='kopf'?0:180),from=coinRot;coinRot=end;
 sfx('coinring');cn.animate([{transform:`rotateX(${from}deg)`},{transform:`rotateX(${end}deg)`}],{duration:1700,easing:'cubic-bezier(.25,.55,.35,1)'});cn.style.transform=`rotateX(${end}deg)`;
 cj.animate([{transform:'translateY(0) scale(1)',easing:'cubic-bezier(.15,.7,.35,1)'},{transform:'translateY(-98px) scale(1.25)',offset:.4,easing:'cubic-bezier(.55,0,.85,.45)'},{transform:'translateY(0) scale(1)',offset:.72,easing:'ease-out'},{transform:'translateY(-22px) scale(1.04)',offset:.82,easing:'ease-in'},{transform:'translateY(0)',offset:.9,easing:'ease-out'},{transform:'translateY(-6px)',offset:.95,easing:'ease-in'},{transform:'translateY(0)'}],{duration:1700});
 if(sh)sh.animate([{transform:'scale(1)',opacity:.6},{transform:'scale(.45)',opacity:.25,offset:.4},{transform:'scale(1)',opacity:.6,offset:.72},{transform:'scale(.9)',offset:.82},{transform:'scale(1)',opacity:.6}],{duration:1700});
 setTimeout(()=>sfx('clack'),1230);setTimeout(()=>sfx('coinring'),1400);setTimeout(()=>sfx('tick'),1560);await sleep(1750)}
const WHEEL=[0,0,0,0,0.5,0.5,1,1,1,1.5,2,4],WCOL={0:'#2b312b',0.5:'#7a5a1a',1:'#2f6b3a',1.5:'#1f8a8a',2:'#5a4ab0',4:'#d9a400'};
const SLOTS=['🍒','🍋','🔔','⭐','💎','7️⃣'],SLOTPAY=[5,8,15,30,100,250];
function gambleView(){
 const c=S.coins;
 return `<div class="gcent"><h2>Gamble</h2><div class="sub">Nur Spiel-Coins zum Spaß: kein Echtgeld, keine Auszahlung, Coins lassen sich nicht kaufen.</div>
 <div class="panel"><div class="row"><div style="flex:1">Kontostand <b id="gbal" style="font-size:20px">${c.balance}</b> 🪙 · Heute <b id="gnet"></b> · Runden <span id="grounds">${c.rounds}</span></div></div>
  <div class="row"><span style="width:70px">Einsatz</span><input type="number" id="gb" min="1" max="${c.max_bet}" value="${gbet}" style="width:90px" onchange="setBet(+this.value||1)">
   ${[10,50,100,250].map(x=>`<button class="btn" onclick="setBet(${x})">${x}</button>`).join('')}<button class="btn" onclick="setBet(Math.floor(gbet/2))">½</button><button class="btn" onclick="setBet(gbet*2)">×2</button><span class="dim">max. ${c.max_bet}</span></div>
  <div class="dim" id="gnote"></div></div>
 <div style="display:flex;gap:6px;margin:12px 0;flex-wrap:wrap">${[['flip','Münzwurf'],['wheel','Glücksrad'],['slots','Slots'],['dice','Würfel']].map(([k,l])=>`<button class="btn ${k==gm?'pri':''}" onclick="gm='${k}';render()">${l}</button>`).join('')}${muteBtn()}</div>
 <div class="panel" id="gstage" style="text-align:center">${gStage()}</div><div id="gres" class="gres"></div></div>`}
function gRefresh(){const c=S.coins,n=$('#gnet');if(!n)return;n.textContent=(c.net>=0?'+':'')+c.net;n.style.color=c.net>=0?'var(--ac)':'var(--warn)';$('#gbal').textContent=c.balance;$('#grounds').textContent=c.rounds;$('#coinn').textContent=c.balance;
 $('#gnote').textContent=c.limit_hit?'Für heute ist Schluss: Das Tageslimit von '+c.loss_limit+' Coins Verlust ist erreicht. Morgen geht es weiter.':c.rounds>=30?'Du spielst schon eine Weile. Vielleicht eine Pause?':'Tageslimit: Nach '+c.loss_limit+' Coins Verlust ist für heute Schluss.'}
function setBet(v){gbet=Math.max(1,Math.min(S.coins.max_bet,Math.floor(v)));const i=$('#gb');if(i)i.value=gbet}
function gStage(){
 if(gm=='flip')return `<div class="coinwrap"><div class="cshadow" id="csh"></div><div class="ctilt"><div id="cj" class="cjump"><div id="cn" class="cn" style="transform:rotateX(${coinRot}deg)">${coinEdge()}<div class="cf"><span class="cic">♛</span><b>KOPF</b></div><div class="cf cb"><span class="cic">◆</span><b>ZAHL</b></div></div></div></div></div><p class="dim">Richtig geraten: Einsatz ×1,9</p><button class="btn pri" onclick="play('kopf')">Kopf</button> <button class="btn pri" onclick="play('zahl')">Zahl</button>`;
 if(gm=='wheel')return `<div class="wheelwrap"><div class="ptr"></div><div id="wh" class="wh" style="background:conic-gradient(${WHEEL.map((m,i)=>`${WCOL[m]} ${i*30}deg ${i*30+30}deg`).join(',')});transform:rotate(${wrot}deg)">${WHEEL.map((m,i)=>`<span style="transform:rotate(${i*30+15}deg) translateY(-82px)">×${m}</span>`).join('')}</div></div><p class="dim">Von ×0 bis ×4</p><button class="btn pri" onclick="play()">Drehen</button>`;
 if(gm=='slots')return `<div class="slot" id="slotm"><div class="slotlights">${'<i></i>'.repeat(12)}</div><div class="slottitle">★ D-HUB SLOTS ★</div><div class="rwin"><div class="reels">${['🍒','🍋','🔔'].map((e,i)=>`<div class="reel" id="r${i}"><div class="rsym">${e}</div></div>`).join('')}</div></div><div class="slotpay">${SLOTS.map((s,i)=>`<span>${s}<b>×${SLOTPAY[i]}</b></span>`).join('')}<span>🍒🍒<b>×1,1</b></span></div><div class="slotlights bot">${'<i></i>'.repeat(12)}</div></div><button class="btn pri slotbtn" onclick="play()">Drehen</button>`;
 const ch=dmode=='under'?dtarget:100-dtarget;
 return `${d20Html()}<div class="dnum" id="dn">–</div><div class="dim" id="d20c">Der W20 bestimmt den Bereich (jede Zahl = 5 Prozentpunkte), die genaue Zahl steht darüber.</div><div class="row" style="justify-content:center"><button class="btn ${dmode=='under'?'pri':''}" onclick="dmode='under';render()">Unter</button><button class="btn ${dmode=='over'?'pri':''}" onclick="dmode='over';render()">Über</button></div>
 <input type="range" min="5" max="95" value="${dtarget}" style="width:80%;margin:12px 0" oninput="dtarget=+this.value;dInfo()"><div id="dinfo" class="dim">${dInfoText()}</div><button class="btn pri" onclick="play()">Würfeln</button>`}
function dInfoText(){const ch=dmode=='under'?dtarget:100-dtarget;return `${dmode=='under'?'Wurf ≤ ':'Wurf > '}${dtarget} · Chance ${ch} % · Auszahlung ×${(95/ch).toFixed(2)}`}
function dInfo(){const e=$('#dinfo');if(e)e.textContent=dInfoText()}
const sleep=ms=>new Promise(r=>setTimeout(r,ms));
async function play(pick){
 if(gbusy)return;auCtx();gbusy=true;document.querySelectorAll('#gstage button,#gstage input').forEach(b=>b.disabled=true);$('#gres').textContent='';
 const body={game:gm,bet:gbet};if(gm=='flip')body.pick=pick;if(gm=='dice'){body.mode=dmode;body.target=dtarget}
 let r;try{r=await coinOp('gamble','',body)}catch(e){gbusy=false;return}
 if(!r.ok){toast(r.msg);gbusy=false;document.querySelectorAll('#gstage button,#gstage input').forEach(b=>b.disabled=false);gRefresh();return}
 const x=r.result,d=x.detail;
 if(gm=='flip'){await coinFlip(d.out)}
 else if(gm=='wheel'){wrot=Math.ceil(wrot/360)*360+360*5+(360-(d.index*30+15));const w=$('#wh');w.style.transition='transform 4s cubic-bezier(.12,.7,.15,1)';w.style.transform='rotate('+wrot+'deg)';await sleep(4100)}
 else if(gm=='slots'){const rs=[0,1,2].map(i=>$('#r'+i)),stops=[1000,1550,2100],t0=Date.now(),fin=[false,false,false];let nd=0,dn=0;rs.forEach(r=>r.classList.add('spin'));
  while(Date.now()-t0<2200){const el=Date.now()-t0;if(el>=nd){sfx('ding',dn++%7);nd=el+115}
   for(let i=0;i<3;i++){if(fin[i])continue;if(el>=stops[i]){fin[i]=true;const r=rs[i];r.classList.remove('spin');r.querySelector('.rsym').textContent=d.syms[i];r.classList.add('land');setTimeout(()=>r.classList.remove('land'),420);sfx('clack')}else rs[i].querySelector('.rsym').textContent=SLOTS[Math.floor(Math.random()*6)]}
   await sleep(45)}
  for(let i=0;i<3;i++){rs[i].classList.remove('spin');rs[i].querySelector('.rsym').textContent=d.syms[i]}
  if(x.net>0){const m=$('#slotm');m&&m.classList.add('win');setTimeout(()=>m&&m.classList.remove('win'),2500)}}
 else{const n20=Math.ceil(d.roll/5),dn=$('#dn');dn.textContent='…';dn.style.color='';await d20Roll(n20);dn.textContent='Wurf '+d.roll;dn.style.color=x.payout>0?'var(--ac)':'var(--warn)';const dw=$('#d20w');dw&&dw.classList.add(x.payout>0?'win':'lose');sfx('dmagic');const cc=$('#d20c');if(cc)cc.textContent='W20 zeigt '+n20+' (Bereich '+((n20-1)*5+1)+' bis '+n20*5+')'}
 const res=$('#gres');res.className='gres '+(x.net>0?'win':x.net<0?'lose':'even');
 res.textContent=x.net>0?`Gewonnen: +${x.net} Coins`+(x.payout>=x.bet*9?'  🎉 JACKPOT':''):x.net<0?`Verloren: ${x.net} Coins`:'Einsatz zurück';
 sfx(x.net>0?(x.payout>=x.bet*9?'jackpot':'win'):x.net<0?'lose':'tick');gRefresh();gbusy=false;document.querySelectorAll('#gstage button,#gstage input').forEach(b=>b.disabled=false)}
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
 const ub=$('#updbtn');ub.style.display=av?'':'none';ub.textContent=u.state=='available'?'⬆ Update '+u.latest:'Update läuft …';ub.title=u.state=='available'?'Jetzt auf Version '+u.latest+' aktualisieren':'';ub.disabled=u.state!='available';
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
api('state').then(()=>{if(S.fatal)throw new Error('Server: '+S.fatal);const st=S.settings.starttab;if(st&&['Alle',...S.launchers,'Downloads','Wert','KumpelFunk'].includes(st))tab=st;render();window.__ok=1;initKF();updChrome(true)});
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
            try:
                self.send(state())
            except Exception:
                import traceback
                tb = traceback.format_exc()
                log("state() Fehler: " + tb[-600:])
                self.send(json.dumps({"fatal": tb[-500:]}).encode("utf-8"), "application/json")
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
        elif p == "/api/fullscreen":
            res = dict(native=False, fs=FS[0])
            if WINMODE[0].startswith("eigenes") and WIN[0] is not None:
                try:
                    want = (not FS[0]) if b.get("on") is None else bool(b.get("on"))
                    if want != FS[0]:
                        WIN[0].toggle_fullscreen()
                        FS[0] = want
                    try:
                        WIN[0].on_top = False  # nicht ueber anderen Fenstern festhalten, Alt+Tab soll normal gehen
                    except Exception:
                        pass
                    res = dict(native=True, fs=FS[0])
                except Exception as e:
                    log("Vollbild: %s" % e)
            self.send(res)
            return
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
